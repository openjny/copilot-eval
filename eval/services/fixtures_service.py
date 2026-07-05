"""Fixture content-hash pinning (issue #89).

Fixtures are plain directories with no integrity guarantees, so between eval
runs their files can be silently modified — invalidating cross-run comparisons
and leaving no proof that two runs used identical inputs.

This module adds a *content-addressed* pin: `pin_fixtures` walks every fixture
referenced by the config's tasks and writes a human-readable YAML lockfile
(``fixtures.lock``) recording, per fixture, a sha256 over its file contents,
a per-file sha256/size breakdown, and the total byte size. At `run` time
`verify_fixtures` re-hashes the fixtures and reports drift (a non-blocking
warning by default, a hard failure under ``--strict-fixtures``), and the run's
`results.json` manifest records the fixture hashes for reproducibility auditing.

The lockfile is deliberately simple (plain YAML, no timestamp, deterministic
ordering) per the project's zero-infrastructure principle: it is fully
content-addressed, so re-pinning unchanged fixtures produces an identical file
and git diffs stay meaningful.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from eval.config import Config, Task

# Lockfile lives next to eval-config.yaml so it is versioned alongside the
# fixtures it pins.
FIXTURE_LOCKFILE_NAME = "fixtures.lock"
# Bumped only on breaking changes to the lockfile schema so `verify_fixtures`
# can refuse to compare against a format it does not understand.
LOCKFILE_VERSION = 1

_CHUNK = 65536


class FixtureLockError(ValueError):
    """Raised when the lockfile exists but cannot be parsed/understood."""


def fixtures_dir(config: Config) -> Path:
    return config.config_dir / "fixtures"


def lockfile_path(config: Config) -> Path:
    return config.config_dir / FIXTURE_LOCKFILE_NAME


def referenced_fixtures(tasks: list[Task]) -> list[str]:
    """Unique fixture directory names referenced by `tasks`, in first-seen order.

    Uses :meth:`Task.fixture_names`, so a task with no explicit fixture still
    contributes its task-name fallback (matching what the runner copies).
    """
    seen: set[str] = set()
    names: list[str] = []
    for task in tasks:
        for name in task.fixture_names():
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def explicit_fixtures(tasks: list[Task]) -> set[str]:
    """Fixture names a task *explicitly* declares via ``fixture:``/``fixtures:``.

    Excludes the implicit task-name fallback (``Task.fixture_names`` returns the
    task name when nothing is declared). Used by :func:`verify_fixtures` so
    ``--strict-fixtures`` can fail on an explicitly-configured fixture that is
    missing on disk and unpinned, while still tolerating a task that simply has
    no fixture directory at all.
    """
    names: set[str] = set()
    for task in tasks:
        if task.fixtures:
            names.update(task.fixtures)
        elif task.fixture:
            names.add(task.fixture)
    return names


def _hash_file(path: Path) -> tuple[str, int]:
    """Stream a file through sha256, returning ``(hexdigest, size_bytes)``."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _iter_fixture_files(fixture_path: Path) -> list[Path]:
    """Files under `fixture_path` whose content reaches the run sandbox, sorted
    by relative POSIX path so the fixture digest is deterministic across
    machines and filesystems.

    ``Path.is_file()`` follows symlinks, so a symlink pointing at a file is
    included and hashed by its *target* content — mirroring the runner, which
    copies fixtures with ``shutil.copytree(..., symlinks=False)`` and therefore
    dereferences file symlinks into real files. Broken/dangling symlinks
    (``is_file()`` is False) are skipped. Not covered (and thus outside the
    integrity guarantee): empty directories, directory symlinks — which
    ``rglob`` does not descend — and POSIX file modes; these rarely change a
    fixture's effective input.
    """
    files = [p for p in fixture_path.rglob("*") if p.is_file()]
    return sorted(files, key=lambda p: p.relative_to(fixture_path).as_posix())


def hash_fixture(fixture_path: Path) -> dict[str, Any]:
    """Content hash for a single fixture directory.

    Returns a dict with the per-fixture ``sha256`` (computed over the sorted
    ``path\\0sha256\\0size`` of every file), the per-file breakdown, and the
    total byte size — the shape written per fixture in the lockfile. NUL cannot
    appear in a POSIX path, so the ``\\0``-delimited concatenation is injective
    (no two distinct file sets can produce the same digest).
    """
    hasher = hashlib.sha256()
    files: list[dict[str, Any]] = []
    total_size = 0
    for file_path in _iter_fixture_files(fixture_path):
        rel_path = file_path.relative_to(fixture_path)
        rel = rel_path.as_posix()
        file_sha, size = _hash_file(file_path)
        files.append({"path": rel, "sha256": file_sha, "size": size})
        total_size += size
        # Hash the raw path bytes (os.fsencode) rather than a UTF-8 re-encode,
        # so a non-UTF-8 filename (surrogate-escaped on POSIX) never crashes
        # hashing — this runs unconditionally on the core `run` path.
        hasher.update(os.fsencode(rel_path))
        hasher.update(b"\0")
        hasher.update(file_sha.encode("ascii"))
        hasher.update(b"\0")
        hasher.update(str(size).encode("ascii"))
        hasher.update(b"\n")
    return {"sha256": hasher.hexdigest(), "total_size": total_size, "files": files}


@dataclass
class PinResult:
    """Outcome of :func:`pin_fixtures`."""

    path: Path
    fixtures: dict[str, dict[str, Any]]
    missing: list[str] = field(default_factory=list)


def pin_fixtures(config: Config, tasks: list[Task] | None = None) -> PinResult:
    """Hash every fixture referenced by `tasks` and write the YAML lockfile.

    Fixtures referenced by the config but absent on disk are skipped and
    returned in ``PinResult.missing`` so the caller can warn (mirroring the
    runner, which tolerates a missing fixture directory).
    """
    tasks = config.tasks if tasks is None else tasks
    root = fixtures_dir(config)
    entries: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in referenced_fixtures(tasks):
        path = root / name
        if path.is_dir():
            entries[name] = hash_fixture(path)
        else:
            missing.append(name)

    # Deterministic key order keeps the lockfile diff-friendly across re-pins.
    ordered = {name: entries[name] for name in sorted(entries)}
    document: dict[str, Any] = {"version": LOCKFILE_VERSION, "fixtures": ordered}
    out_path = lockfile_path(config)
    out_path.write_text(_dump_lockfile(document), encoding="utf-8")
    return PinResult(path=out_path, fixtures=ordered, missing=missing)


def _dump_lockfile(document: dict[str, Any]) -> str:
    header = (
        "# fixtures.lock — generated by `copilot-eval pin-fixtures`.\n"
        "# Content-addressed integrity manifest for fixtures/. Do not edit by hand;\n"
        "# re-run `copilot-eval pin-fixtures` after intentionally changing a fixture.\n"
    )
    body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
    return header + body


def load_lockfile(config: Config) -> dict[str, Any] | None:
    """Load and validate ``fixtures.lock``. Returns None if absent.

    Any problem with a *present* lockfile (unreadable, invalid YAML,
    unsupported ``version``, or missing/wrong-typed ``fixtures`` mapping) raises
    :class:`FixtureLockError` so the caller can turn it into a clean warning
    (non-strict) or a Click abort with exit code 1 (``--strict-fixtures``),
    rather than leaking a raw traceback.
    """
    path = lockfile_path(config)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise FixtureLockError(f"{path} could not be read/parsed: {exc}") from exc
    if not isinstance(data, dict):
        raise FixtureLockError(f"{path} is malformed: expected a YAML mapping.")
    version = data.get("version")
    if version != LOCKFILE_VERSION:
        raise FixtureLockError(
            f"{path} has unsupported version {version!r} (expected {LOCKFILE_VERSION}). "
            "Re-run `copilot-eval pin-fixtures` to regenerate it."
        )
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, dict):
        raise FixtureLockError(f"{path} is malformed: missing 'fixtures' mapping.")
    return data


@dataclass
class FixtureVerification:
    """Result of comparing on-disk fixtures against the lockfile."""

    lockfile_present: bool
    matched: list[str] = field(default_factory=list)
    # Human-readable messages describing each detected problem.
    drifted: list[str] = field(default_factory=list)
    unpinned: list[str] = field(default_factory=list)
    # Freshly computed full hashes (sha256/total_size/files) for every
    # referenced fixture that exists on disk — reused by the run manifest so
    # fixtures are hashed only once per run.
    current_hashes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def problems(self) -> list[str]:
        """All drift + unpinned messages (what ``--strict-fixtures`` fails on)."""
        return self.drifted + self.unpinned

    @property
    def has_problems(self) -> bool:
        return bool(self.problems)


def _describe_file_drift(
    locked_files: list[dict[str, Any]], current_files: list[dict[str, Any]]
) -> str:
    """Short summary of which files were added/removed/modified between two
    per-file breakdowns, for a readable drift warning."""
    locked = {f["path"]: f for f in locked_files}
    current = {f["path"]: f for f in current_files}
    added = sorted(current.keys() - locked.keys())
    removed = sorted(locked.keys() - current.keys())
    modified = sorted(
        p
        for p in locked.keys() & current.keys()
        if locked[p].get("sha256") != current[p].get("sha256")
    )
    parts: list[str] = []
    if modified:
        parts.append(f"modified: {', '.join(modified)}")
    if added:
        parts.append(f"added: {', '.join(added)}")
    if removed:
        parts.append(f"removed: {', '.join(removed)}")
    return "; ".join(parts) if parts else "content changed"


def verify_fixtures(config: Config, tasks: list[Task] | None = None) -> FixtureVerification:
    """Compare fixtures referenced by `tasks` against the lockfile.

    Hashes every referenced fixture that exists on disk exactly once (exposed as
    ``current_hashes`` for the run manifest), then classifies each as matched,
    drifted (pinned but hashing differently or missing on disk, or explicitly
    declared yet absent-and-unpinned), or unpinned (present on disk but absent
    from the lockfile). Never raises on drift — the caller decides whether to
    warn or fail (``--strict-fixtures``). May raise :class:`FixtureLockError`
    if a *present* lockfile is malformed.
    """
    tasks = config.tasks if tasks is None else tasks
    lock = load_lockfile(config)
    locked_fixtures: dict[str, Any] = lock.get("fixtures", {}) if lock else {}
    explicit = explicit_fixtures(tasks)
    root = fixtures_dir(config)
    result = FixtureVerification(lockfile_present=lock is not None)

    for name in referenced_fixtures(tasks):
        path = root / name
        current = hash_fixture(path) if path.is_dir() else None
        if current is not None:
            result.current_hashes[name] = current

        if not result.lockfile_present:
            # No lockfile to compare against; still record current_hashes above
            # so the run manifest can capture fixture identity.
            continue

        entry = locked_fixtures.get(name)
        if entry is None:
            if current is not None:
                result.unpinned.append(f"fixture '{name}' is not pinned in {FIXTURE_LOCKFILE_NAME}")
            elif name in explicit:
                # Explicitly declared, but neither on disk nor pinned — a real
                # misconfiguration that strict mode should catch. (An implicit
                # task-name fallback with no directory is tolerated silently.)
                result.drifted.append(
                    f"fixture '{name}' is declared but missing on disk and not "
                    f"pinned in {FIXTURE_LOCKFILE_NAME}"
                )
            continue
        if current is None:
            result.drifted.append(
                f"fixture '{name}' is pinned but missing on disk (expected at {path})"
            )
            continue
        if current["sha256"] == entry.get("sha256"):
            result.matched.append(name)
        else:
            detail = _describe_file_drift(entry.get("files", []), current["files"])
            result.drifted.append(f"fixture '{name}' changed since pinning ({detail})")
    return result


def _compact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Reduce a full fixture hash entry to the compact form recorded in the
    run manifest (identity hash + size, without the per-file breakdown)."""
    return {
        "sha256": entry["sha256"],
        "total_size": entry["total_size"],
        "file_count": len(entry["files"]),
    }


def compact_hashes(full: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Compact every full fixture hash entry for the run manifest."""
    return {name: _compact_entry(entry) for name, entry in full.items()}


def compute_fixture_hashes(config: Config, tasks: list[Task]) -> dict[str, dict[str, Any]]:
    """Per-fixture identity hashes for the run manifest (reproducibility audit).

    Compact by design — the full per-file breakdown lives in the lockfile; the
    manifest only needs each fixture's ``sha256`` (plus size/file counts for a
    quick eyeball) recorded alongside the runs that consumed it.
    """
    root = fixtures_dir(config)
    hashes: dict[str, dict[str, Any]] = {}
    for name in referenced_fixtures(tasks):
        path = root / name
        if not path.is_dir():
            continue
        hashes[name] = _compact_entry(hash_fixture(path))
    return hashes
