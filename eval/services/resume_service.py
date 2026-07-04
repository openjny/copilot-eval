"""Resume support for ``run --resume`` (issue #67): detect which
(task, variant, epoch, fixture) cells of a previous run already succeeded so a
resumed run only re-executes what's missing or failed, then merges the new
results back into the original run directory.

Cell status is derived from the run's manifest (``results.json``, written by
``eval.services.manifest.write_manifest``) rather than by scanning individual
``.log``/``.scores.json`` files: the manifest already records the full
scheduled set with a normalized :class:`~eval.protocols.RunStatus`, which is
exactly what `analyze`'s failed/missing reconciliation (see
``eval.services.trace_service._report_run_coverage``) also relies on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from eval.config import Config
from eval.protocols import RunStatus
from eval.runner import RunResult
from eval.services.manifest import MANIFEST_NAME, load_manifest

# Identifies one scheduled matrix cell. `fixture` is "" for single-fixture
# tasks, matching `RunResult.fixture` / `eval.naming.run_slug`.
CellKey = tuple[str, str, int, str]


def cell_key(task: str, variant: str, epoch: int | str, fixture: str | None = "") -> CellKey:
    """Build a `CellKey`, tolerating the loose typing raw manifest JSON has
    (epoch may arrive as ``str`` or ``int``; fixture may be ``None``)."""
    return (task, variant, int(epoch), fixture or "")


def scan_run_results(run_dir: Path) -> dict[CellKey, dict[str, Any]]:
    """Index a previous run's manifest by (task, variant, epoch, fixture).

    Returns an empty dict when the manifest is missing or unreadable (corrupt
    JSON, unexpected shape, etc.) -- ``load_manifest`` already swallows those
    errors and returns ``None``. Treating "unreadable" the same as "no prior
    run" is the safe default for resume: every cell is then considered
    missing and gets re-executed, rather than silently skipped based on state
    that couldn't be parsed.
    """
    runs = load_manifest(run_dir)
    if not runs:
        return {}
    index: dict[CellKey, dict[str, Any]] = {}
    for record in runs:
        if not isinstance(record, dict):
            continue
        try:
            key = cell_key(
                record["task"], record["variant"], record["epoch"], record.get("fixture")
            )
        except (KeyError, TypeError, ValueError):
            # Malformed row -- skip it rather than let one bad record abort resume.
            continue
        index[key] = record
    return index


def is_cell_complete(record: dict[str, Any] | None) -> bool:
    """A cell only counts as done if it recorded a successful status.

    Anything else -- failed, timeout, setup_failed, or genuinely missing
    (``record is None``) -- must be re-run.
    """
    return record is not None and record.get("status") == str(RunStatus.SUCCESS)


def completed_cells(index: dict[CellKey, dict[str, Any]]) -> set[CellKey]:
    """Cells to skip on resume: those that recorded a successful run."""
    return {key for key, record in index.items() if is_cell_complete(record)}


def filter_schedule(
    work_items: list[tuple[Any, Any, int, str]], completed: set[CellKey]
) -> list[tuple[Any, Any, int, str]]:
    """Drop already-completed cells from a flat ``(task, variant, epoch,
    fixture)`` work list (as built by the ``full`` parallel strategy in
    ``eval.services.orchestrator._execute_schedule``).

    ``task``/``variant`` may be ``Task``/``Variant`` objects or bare name
    strings; only their name identity feeds the `CellKey` lookup. ``fixture``
    in the work item is the *fixture directory name* (needed downstream to
    actually run the cell), which for single-fixture tasks differs from the
    *reporting* label ("") recorded in the manifest / ``completed`` -- when
    ``task`` is a real ``Task`` its ``fixture_label`` is used to bridge that,
    so a bare fixture-dir string still matches the manifest's cell key.
    """
    if not completed:
        return list(work_items)

    def _name(x: Any) -> str:
        return str(x.name) if hasattr(x, "name") else str(x)

    def _label(task: Any, fixture: str) -> str:
        return task.fixture_label(fixture) if hasattr(task, "fixture_label") else fixture

    return [
        item
        for item in work_items
        if cell_key(_name(item[0]), _name(item[1]), item[2], _label(item[0], item[3]))
        not in completed
    ]


def merge_manifest_runs(
    index: dict[CellKey, dict[str, Any]], new_results: list[RunResult]
) -> list[dict[str, Any]]:
    """Combine a prior run's manifest rows with freshly executed results.

    New results always win for their cell (a re-run supersedes the old
    failed/missing record); every other previously recorded cell -- including
    ones this resume skipped because they'd already succeeded -- is preserved
    verbatim so the merged manifest still covers the full matrix.
    """
    merged = dict(index)
    for result in new_results:
        merged[cell_key(result.task, result.variant, result.epoch, result.fixture)] = (
            result.to_dict()
        )
    return list(merged.values())


def warn_if_schedule_changed(run_dir: Path, config: Config) -> None:
    """Best-effort, non-fatal warning when the scheduling strategy recorded in
    the prior manifest differs from the current config.

    Resume re-executes cells against the *current* config, so a changed
    prompt/evaluator/variant definition is silently picked up -- usually what
    you want when iterating -- but a changed parallelism/ordering/seed can
    make the merged manifest inconsistent (e.g. two different variant orders
    for the same epoch across the original and resumed runs), so it's worth
    surfacing.
    """
    manifest_file = run_dir / MANIFEST_NAME
    if not manifest_file.exists():
        return
    try:
        data = json.loads(manifest_file.read_text())
    except (OSError, ValueError):
        return
    old_schedule = data.get("schedule") if isinstance(data, dict) else None
    if not isinstance(old_schedule, dict) or not old_schedule:
        return
    current = {
        "parallel": config.runner.parallel,
        "max_workers": config.runner.max_workers,
        "variant_order": config.runner.variant_order,
        "seed": config.runner.seed,
    }
    changed = {k: (old_schedule.get(k), v) for k, v in current.items() if old_schedule.get(k) != v}
    if changed:
        details = ", ".join(f"{k}: {old!r} -> {new!r}" for k, (old, new) in changed.items())
        click.echo(
            f"WARNING: --resume config differs from the original run's schedule ({details}). "
            "The merged manifest may mix scheduling strategies across the original and "
            "resumed cells.",
            err=True,
        )
