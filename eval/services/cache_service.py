"""Opt-in content-addressed run cache (issue #131).

Skips a matrix cell when an *identical* cell — same environment-complete
inputs — was already executed and stored by a prior `run`. On a hit the whole
prior cell result (log, scores, output artifacts, and trace) is reused verbatim
for the new run instead of launching another container.

Design guardrails (from the review that approved the issue):

- **Opt-in only.** The orchestrator wires this in exclusively under
  ``run --cache``; nothing here runs on the default path.
- **Epoch-safe.** The cache key includes the ``epoch`` index, so epoch *e* of a
  new run can only reuse epoch *e* of a prior run. Caching therefore reuses
  whole prior-run cells one-for-one and never collapses or dedupes epochs
  within a run — the per-run sample size is preserved. Statistical honesty is
  then completed in ``eval.report``, which reports the effective (non-cached)
  sample size so CIs/power reflect genuinely independent draws.
- **Environment-complete key.** The key hashes the variant image digest, the
  fully-resolved prompt, the fixture content hash (reusing #89's fixture
  hashing), and the model/effort/max-turns/timeout/collector — every input that
  can change the agent's behavior. Any change busts the affected cells only.

The cache is a plain directory (``results/.cache`` by default, overridable with
``--cache-dir``); no external service is required, preserving the
zero-dependency default path.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Any

from eval.collectors.file_collector import TRACE_FILE
from eval.config import Config, Task, Variant
from eval.naming import run_slug
from eval.protocols import EvalScore, RunStatus
from eval.runner import RunResult
from eval.services.resume_service import CellKey, cell_key

logger = getLogger(__name__)

# Bump when the key/entry layout changes in a backward-incompatible way so old
# cache entries can never be silently mismatched against a new key scheme.
CACHE_VERSION = 1

CACHE_DIRNAME = ".cache"
_ENTRY_RESULT = "result.json"
_ENTRY_META = "meta.json"
_ENTRY_ARTIFACTS = "artifacts"


@dataclass(frozen=True)
class CacheKeyInputs:
    """Every input that must match for a cached cell to be reused.

    Anything that can change the agent's behavior — or the artifacts we score
    later — belongs here. Two cells with identical ``CacheKeyInputs`` are
    considered interchangeable, so a hit reuses the prior result wholesale.
    """

    task: str
    variant: str
    epoch: int
    fixture: str  # reporting label ("" for single-fixture tasks)
    prompt: str
    image_digest: str
    fixture_sha256: str | None
    model: str | None
    reasoning_effort: str | None
    max_turns: int | None
    timeout_seconds: int
    collector: str

    def to_canonical(self) -> dict[str, Any]:
        """Stable, JSON-serializable view used both for hashing and for the
        auditable provenance recorded alongside a stored entry."""
        return {
            "cache_version": CACHE_VERSION,
            "task": self.task,
            "variant": self.variant,
            "epoch": self.epoch,
            "fixture": self.fixture,
            "prompt": self.prompt,
            "image_digest": self.image_digest,
            "fixture_sha256": self.fixture_sha256,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "collector": self.collector,
        }


def compute_cache_key(inputs: CacheKeyInputs) -> str:
    """Deterministic sha256 over the canonical key inputs.

    ``sort_keys`` + a compact separator make the digest independent of dict
    ordering, so the same inputs always hash to the same key.
    """
    payload = json.dumps(inputs.to_canonical(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cell_slug(inputs: CacheKeyInputs) -> str:
    return run_slug(inputs.task, inputs.variant, inputs.epoch, inputs.fixture)


def _cell_artifact_paths(run_dir: Path, slug: str) -> list[tuple[Path, str]]:
    """Locate the persisted artifacts for one cell in ``run_dir``.

    Returns ``(source_path, relative_name)`` pairs for every artifact that
    exists — the run log, its scores sidecar, the per-cell output directory, and
    the exported trace. ``relative_name`` is the artifact's path relative to the
    cache entry's ``artifacts/`` root, so store/materialize stay symmetric.
    """
    pairs: list[tuple[Path, str]] = []
    log = run_dir / f"{slug}.log"
    if log.exists():
        pairs.append((log, f"{slug}.log"))
    scores = run_dir / f"{slug}.scores.json"
    if scores.exists():
        pairs.append((scores, f"{slug}.scores.json"))
    outputs = run_dir / "outputs" / slug
    if outputs.is_dir():
        pairs.append((outputs, f"outputs/{slug}"))
    trace = run_dir / TRACE_FILE.parent / f"{slug}.jsonl"
    if trace.exists():
        pairs.append((trace, f"{TRACE_FILE.parent}/{slug}.jsonl"))
    return pairs


def _score_from_dict(d: dict[str, Any]) -> EvalScore:
    """Rebuild an :class:`EvalScore` from its serialized form (see
    ``eval.protocols.score_to_dict``) so a cached cell's reconstructed
    ``RunResult`` carries the same scores it had when first produced."""
    return EvalScore(
        name=str(d.get("name", "")),
        type=str(d.get("type", "")),
        score=d.get("score"),
        reason=str(d.get("reason", "")),
        passed=bool(d.get("passed", True)),
        samples=list(d.get("samples", []) or []),
        score_stddev=d.get("score_stddev"),
        n_samples=int(d.get("n_samples", 0) or 0),
        outcomes=dict(d.get("outcomes", {}) or {}),
        judge_model=d.get("judge_model"),
        judge_version=d.get("judge_version"),
        meta=dict(d.get("meta", {}) or {}),
    )


def _status_from_str(value: Any) -> RunStatus:
    for status in RunStatus:
        if str(status) == value:
            return status
    return RunStatus.SUCCESS


class RunCache:
    """Filesystem-backed content cache for completed matrix cells.

    Each entry lives under ``<cache_dir>/<key>/`` and holds the serialized
    ``RunResult`` (``result.json``), auditable key provenance (``meta.json``),
    and a copy of the cell's artifacts (``artifacts/``). Lookups are pure reads;
    ``store`` is only ever called for freshly, successfully executed cells.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def _entry_dir(self, key: str) -> Path:
        return self.cache_dir / key

    def lookup(self, key: str) -> dict[str, Any] | None:
        """Return the stored entry's ``result.json`` payload, or ``None`` on a
        miss (or an unreadable/partial entry — treated as a miss so a corrupt
        cache never blocks a run; the cell is simply re-executed)."""
        result_file = self._entry_dir(key) / _ENTRY_RESULT
        if not result_file.exists():
            return None
        try:
            data = json.loads(result_file.read_text())
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable cache entry %s", key)
            return None
        return data if isinstance(data, dict) else None

    def materialize(
        self, key: str, result_dict: dict[str, Any], run_dir: Path, *, run_id: str
    ) -> RunResult:
        """Copy a cached cell's artifacts into ``run_dir`` and rebuild its
        :class:`RunResult`, marked ``cached=True`` and re-homed onto ``run_id``.

        The original ``test_id`` is preserved so the reused trace's OTel tags
        still line up with the manifest during ``analyze``. Because the cell's
        ``(task, variant, epoch, fixture)`` is identical, ``run_slug`` yields the
        same file names the fresh run would have written.
        """
        slug = run_slug(
            str(result_dict["task"]),
            str(result_dict["variant"]),
            result_dict["epoch"],
            str(result_dict.get("fixture") or ""),
        )
        artifacts_root = self._entry_dir(key) / _ENTRY_ARTIFACTS
        for source, rel in _cell_artifact_paths_from_names(artifacts_root, slug):
            dest = run_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(source, dest)

        scores = [_score_from_dict(s) for s in result_dict.get("scores", []) or []]
        return RunResult(
            task=str(result_dict["task"]),
            variant=str(result_dict["variant"]),
            epoch=int(result_dict["epoch"]),
            test_id=str(result_dict.get("test_id", "")),
            run_id=run_id,
            log_file=run_dir / f"{slug}.log",
            exit_code=int(result_dict.get("exit_code", 0)),
            status=_status_from_str(result_dict.get("status")),
            scores=scores,
            order_index=result_dict.get("order_index"),
            started_at=result_dict.get("started_at"),
            finished_at=result_dict.get("finished_at"),
            duration_seconds=result_dict.get("duration_seconds"),
            fixture=str(result_dict.get("fixture") or ""),
            retry_count=int(result_dict.get("retry_count", 0) or 0),
            cached=True,
        )

    def store(self, key: str, result: RunResult, run_dir: Path, inputs: CacheKeyInputs) -> None:
        """Persist a freshly executed, successful cell so a later run can reuse
        it. No-op for anything that isn't a clean success — a failed/timed-out
        cell must be re-run, not cached."""
        if result.status != RunStatus.SUCCESS or not result.passed:
            return
        entry = self._entry_dir(key)
        artifacts_root = entry / _ENTRY_ARTIFACTS
        slug = run_slug(result.task, result.variant, result.epoch, result.fixture)
        try:
            artifacts_root.mkdir(parents=True, exist_ok=True)
            for source, rel in _cell_artifact_paths(run_dir, slug):
                dest = artifacts_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, dest)
            (entry / _ENTRY_RESULT).write_text(
                json.dumps(result.to_dict(), indent=2, ensure_ascii=False)
            )
            (entry / _ENTRY_META).write_text(
                json.dumps(
                    {
                        "key": key,
                        "source_run_id": result.run_id,
                        "inputs": inputs.to_canonical(),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        except OSError as exc:
            # A cache write failure must never fail the run — the result is
            # already produced; caching is a best-effort optimization.
            logger.warning("Failed to write cache entry %s: %s", key, exc)


def _cell_artifact_paths_from_names(artifacts_root: Path, slug: str) -> list[tuple[Path, str]]:
    """Mirror of :func:`_cell_artifact_paths` for the read (materialize) side,
    resolved against a cache entry's ``artifacts/`` root instead of a run dir."""
    return _cell_artifact_paths(artifacts_root, slug)


def resolve_image_digest(image: str) -> str:
    """Resolve a local Docker image reference to its content digest (``.Id``).

    Falls back to the image reference itself when Docker is unavailable or the
    image isn't present locally, so the key stays deterministic (and a cache
    built without a resolvable digest simply keys on the tag). Isolated here so
    tests can monkeypatch it without a Docker daemon.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("Could not resolve image digest for %s; keying on tag", image)
        return image
    digest = proc.stdout.strip()
    return digest or image


def build_cache_key_inputs(
    config: Config,
    task: Task,
    variant: Variant,
    epoch: int,
    fixture_label: str,
    image_digest: str,
    fixture_sha256: str | None,
) -> CacheKeyInputs:
    """Assemble the environment-complete key inputs for one matrix cell."""
    return CacheKeyInputs(
        task=task.name,
        variant=variant.name,
        epoch=epoch,
        fixture=fixture_label,
        prompt=config.resolve_prompt(task, variant),
        image_digest=image_digest,
        fixture_sha256=fixture_sha256,
        model=variant.model or config.runner.model,
        reasoning_effort=config.runner.reasoning_effort,
        max_turns=config.runner.max_turns,
        timeout_seconds=task.timeout_seconds or config.runner.timeout_seconds,
        collector=config.runner.collector,
    )


@dataclass
class CacheResolution:
    """Outcome of consulting the cache for the whole matrix before scheduling."""

    hits: list[RunResult]
    hit_cells: set[CellKey]
    keys: dict[CellKey, str]
    inputs: dict[CellKey, CacheKeyInputs]


def resolve_cache_hits(
    config: Config,
    tasks: list[Task],
    epochs: int,
    run_id: str,
    run_dir: Path,
    cache: RunCache,
    fixture_hashes: dict[str, dict[str, Any]],
    already_skipped: set[CellKey],
) -> CacheResolution:
    """Consult the cache for every matrix cell not already skipped (e.g. by
    ``--resume``), materializing hits into ``run_dir``.

    Returns the materialized cached results, the set of cell keys that hit (so
    the scheduler skips them), and — for the miss cells — the computed cache
    keys and key inputs, so freshly executed results can be stored afterwards
    under exactly the same key.
    """
    hits: list[RunResult] = []
    hit_cells: set[CellKey] = set()
    keys: dict[CellKey, str] = {}
    inputs_by_cell: dict[CellKey, CacheKeyInputs] = {}

    digests: dict[str, str] = {}
    for variant in config.variants:
        digests[variant.name] = resolve_image_digest(config.image_name(variant))

    for task in tasks:
        for fixture in task.fixture_names():
            label = task.fixture_label(fixture)
            fixture_sha = (fixture_hashes.get(fixture) or {}).get("sha256")
            for epoch in range(1, epochs + 1):
                for variant in config.variants:
                    ck = cell_key(task.name, variant.name, epoch, label)
                    if ck in already_skipped:
                        continue
                    key_inputs = build_cache_key_inputs(
                        config,
                        task,
                        variant,
                        epoch,
                        label,
                        digests[variant.name],
                        fixture_sha,
                    )
                    key = compute_cache_key(key_inputs)
                    keys[ck] = key
                    inputs_by_cell[ck] = key_inputs
                    entry = cache.lookup(key)
                    if entry is None:
                        continue
                    result = cache.materialize(key, entry, run_dir, run_id=run_id)
                    hits.append(result)
                    hit_cells.add(ck)

    return CacheResolution(hits=hits, hit_cells=hit_cells, keys=keys, inputs=inputs_by_cell)


def store_fresh_results(
    cache: RunCache,
    run_dir: Path,
    results: list[RunResult],
    keys: dict[CellKey, str],
    inputs: dict[CellKey, CacheKeyInputs],
) -> None:
    """Store every freshly executed cell that has a computed cache key, so a
    subsequent ``run --cache`` can reuse it. Cached (reused) results are skipped
    — they're already in the cache."""
    for result in results:
        if result.cached:
            continue
        ck = cell_key(result.task, result.variant, result.epoch, result.fixture)
        key = keys.get(ck)
        key_inputs = inputs.get(ck)
        if key is None or key_inputs is None:
            continue
        cache.store(key, result, run_dir, key_inputs)
