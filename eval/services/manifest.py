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
    run_dir: Path, run_id: str, results: list[RunResult], schedule: dict[str, Any] | None = None
) -> None:
    """Persist the full set of runs so `analyze` can detect missing/failed ones."""
    write_manifest_dicts(run_dir, run_id, [r.to_dict() for r in results], schedule)


def write_manifest_dicts(
    run_dir: Path, run_id: str, runs: list[dict[str, Any]], schedule: dict[str, Any] | None = None
) -> None:
    """Same as :func:`write_manifest`, but takes already-serialized run dicts.

    Used by `run --resume` (see ``eval.services.resume_service``), which merges
    freshly executed :class:`RunResult` dicts with rows carried over verbatim
    from the prior manifest -- there's no single ``list[RunResult]`` to hand
    ``write_manifest`` in that case.
    """
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "schedule": schedule or {},
        "runs": runs,
    }
    try:
        (run_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    except OSError as e:
        click.echo(f"WARNING: failed to write run manifest: {e}", err=True)


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
