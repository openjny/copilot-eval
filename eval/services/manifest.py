"""Run manifest persistence.

The manifest (``results.json``) records the full set of runs a `run` invocation
scheduled — including failed/timeout ones that may never produce a trace — so
`analyze` can reconcile against it instead of silently dropping missing runs
(survivorship bias).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from eval.runner import RunResult

MANIFEST_NAME = "results.json"


def write_manifest(
    run_dir: Path,
    run_id: str,
    results: list[RunResult],
    schedule: dict[str, Any] | None = None,
    cost_estimate: dict[str, Any] | None = None,
    fixtures: dict[str, Any] | None = None,
) -> None:
    """Persist the full set of runs so `analyze` can detect missing/failed ones."""
    write_manifest_dicts(
        run_dir, run_id, [r.to_dict() for r in results], schedule, cost_estimate, fixtures
    )


def write_manifest_dicts(
    run_dir: Path,
    run_id: str,
    runs: list[dict[str, Any]],
    schedule: dict[str, Any] | None = None,
    cost_estimate: dict[str, Any] | None = None,
    fixtures: dict[str, Any] | None = None,
) -> None:
    """Same as :func:`write_manifest`, but takes already-serialized run dicts.

    Used by `run --resume` (see ``eval.services.resume_service``), which merges
    freshly executed :class:`RunResult` dicts with rows carried over verbatim
    from the prior manifest -- there's no single ``list[RunResult]`` to hand
    ``write_manifest`` in that case.
    """
    judge_tokens_in, judge_tokens_out = _aggregate_judge_tokens(runs)
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "schedule": schedule or {},
        # Fixture identity (issue #89): content hashes of the fixtures consumed
        # by this run, so a later audit can prove two runs used identical
        # inputs even if the fixture directories were modified afterwards.
        "fixtures": fixtures or {},
        # Cost governance (issue #70): the pre-flight estimate computed before
        # this run started, plus the judge token usage actually observed
        # across this run's scores (see eval.judge_executor).
        "cost": {
            "estimate": cost_estimate,
            "judge_tokens_in": judge_tokens_in,
            "judge_tokens_out": judge_tokens_out,
        },
        "runs": runs,
    }
    try:
        (run_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    except OSError as e:
        click.echo(f"WARNING: failed to write run manifest: {e}", err=True)


def _aggregate_judge_tokens(runs: list[dict[str, Any]]) -> tuple[int, int]:
    """Sum ``judge_tokens_in``/``judge_tokens_out`` across every judge score's
    ``meta`` in the manifest's serialized runs (see ``eval.judge_executor``,
    which records per-evaluator judge token usage)."""
    tokens_in = 0
    tokens_out = 0
    for run in runs:
        for score in run.get("scores") or []:
            if not isinstance(score, dict) or score.get("type") != "judge":
                continue
            meta = score.get("meta") or {}
            tokens_in += meta.get("judge_tokens_in") or 0
            tokens_out += meta.get("judge_tokens_out") or 0
    return tokens_in, tokens_out


def load_manifest(results_dir: Path) -> list[dict[str, Any]] | None:
    """Load persisted runs from a run's manifest. Returns None if not present."""
    manifest_file = results_dir / MANIFEST_NAME
    if not manifest_file.exists():
        return None
    try:
        data = json.loads(manifest_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    runs = data.get("runs") if isinstance(data, dict) else None
    return runs if isinstance(runs, list) else None


def load_manifest_fixtures(results_dir: Path) -> dict[str, Any]:
    """Load the top-level ``fixtures`` block from a run's manifest (empty if
    absent/unreadable). Used by `run --resume` to carry forward the fixture
    hashes of cells that already completed in the original run instead of
    overwriting them with values recomputed at resume time (issue #89)."""
    manifest_file = results_dir / MANIFEST_NAME
    if not manifest_file.exists():
        return {}
    try:
        data = json.loads(manifest_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    fixtures = data.get("fixtures") if isinstance(data, dict) else None
    return fixtures if isinstance(fixtures, dict) else {}
