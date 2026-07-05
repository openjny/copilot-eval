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
    """Regular files under `fixture_path`, sorted by relative POSIX path so the
    fixture digest is deterministic across machines and filesystems."""
    files = [p for p in fixture_path.rglob("*") if p.is_file() and not p.is_symlink()]
    return sorted(files, key=lambda p: p.relative_to(fixture_path).as_posix())


def hash_fixture(fixture_path: Path) -> dict[str, Any]:
    """Content hash for a single fixture directory.

    Returns a dict with the per-fixture ``sha256`` (computed over the sorted
    ``path\\0sha256\\0size`` of every file), the per-file breakdown, and the
    total byte size — the shape written per fixture in the lockfile.
    """
    hasher = hashlib.sha256()
    files: list[dict[str, Any]] = []
    total_size = 0
    for file_path in _iter_fixture_files(fixture_path):
        rel = file_path.relative_to(fixture_path).as_posix()
        file_sha, size = _hash_file(file_path)
        files.append({"path": rel, "sha256": file_sha, "size": size})
        total_size += size
        hasher.update(rel.encode("utf-8"))
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
    """Load and shallow-validate ``fixtures.lock``. Returns None if absent."""
    path = lockfile_path(config)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise FixtureLockError(f"{path} is malformed: expected a YAML mapping.")
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, dict):
        raise FixtureLockError(f"{path} is malformed: missing 'fixtures' mapping.")
    return data


class FixtureLockError(ValueError):
    """Raised when the lockfile exists but cannot be parsed/understood."""


@dataclass
class FixtureVerification:
    """Result of comparing on-disk fixtures against the lockfile."""

    lockfile_present: bool
    matched: list[str] = field(default_factory=list)
    # Human-readable messages describing each detected problem.
    drifted: list[str] = field(default_factory=list)
    unpinned: list[str] = field(default_factory=list)

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

    Never raises on drift — it classifies each referenced fixture as matched,
    drifted (present in the lockfile but hashing differently, or missing on
    disk), or unpinned (referenced but absent from the lockfile). The caller
    decides whether to warn or fail (``--strict-fixtures``).
    """
    tasks = config.tasks if tasks is None else tasks
    lock = load_lockfile(config)
    if lock is None:
        return FixtureVerification(lockfile_present=False)

    locked_fixtures: dict[str, Any] = lock.get("fixtures", {})
    root = fixtures_dir(config)
    result = FixtureVerification(lockfile_present=True)
    for name in referenced_fixtures(tasks):
        entry = locked_fixtures.get(name)
        path = root / name
        if entry is None:
            # A fixture with no directory *and* no lockfile entry is simply an
            # unused task-name fallback; only flag it when it exists on disk.
            if path.is_dir():
                result.unpinned.append(
                    f"fixture '{name}' is not pinned in {FIXTURE_LOCKFILE_NAME}"
                )
            continue
        if not path.is_dir():
            result.drifted.append(
                f"fixture '{name}' is pinned but missing on disk (expected at {path})"
            )
            continue
        current = hash_fixture(path)
        if current["sha256"] == entry.get("sha256"):
            result.matched.append(name)
        else:
            detail = _describe_file_drift(entry.get("files", []), current["files"])
            result.drifted.append(f"fixture '{name}' changed since pinning ({detail})")
    return result


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
        entry = hash_fixture(path)
        hashes[name] = {
            "sha256": entry["sha256"],
            "total_size": entry["total_size"],
            "file_count": len(entry["files"]),
        }
    return hashes
