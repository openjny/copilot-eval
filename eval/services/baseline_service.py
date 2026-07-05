"""Baseline snapshot persistence for cross-run regression tracking (issue #65).

A "baseline" is a named, point-in-time snapshot of a run's raw per-task,
per-variant OTel metrics -- the same numeric fields `report.py` aggregates for
within-run A/B comparison (duration, tokens, cost, tool/turn counts). Saved
once (`baseline save`), it lets a later `analyze --run-id <new> --baseline
<name>` compare a *new* run against it even though the two runs share no
epoch to pair on 1:1 (see `eval.report.build_baseline_comparisons`, which
uses unpaired bootstrap resampling instead of the paired-epoch bootstrap used
for within-run A/B comparison).

Baselines are stored as plain JSON under ``<results_dir>/.baselines/<name>.json``
-- deliberately simple (no database, no versioning) per the project's
zero-infrastructure principle.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from eval.config import Config
from eval.report import _METRIC_DEFS, _pair_label
from eval.trace import RunMetrics

BASELINES_DIRNAME = ".baselines"


class BaselineError(click.ClickException):
    """Baseline CRUD error, surfaced as a clean CLI message (non-zero exit)."""


def baselines_dir(config: Config) -> Path:
    return config.results_dir / BASELINES_DIRNAME


def baseline_path(config: Config, name: str) -> Path:
    return baselines_dir(config) / f"{name}.json"


def save_baseline(
    config: Config,
    run_id: str,
    name: str,
    metrics: list[RunMetrics],
    *,
    replayed: bool = False,
) -> Path:
    """Serialize `metrics` (already-extracted RunMetrics for `run_id`) into a
    named baseline snapshot, grouped by task -> variant -> per-epoch metric dict.

    A baseline saved from a *replayed/synthetic* run (``replayed=True``) is
    refused outright (issue #132): a baseline is a cross-run measurement that a
    later real run compares against, so allowing a synthetic snapshot would let
    replayed numbers silently leak into a genuine A/B comparison. The offline
    replay runner exists to test the pipeline, never to produce a baseline.
    """
    if replayed:
        raise BaselineError(
            f"Run {run_id!r} was produced by the offline replay/synthetic runner "
            "(runner.backend: replay) and cannot be saved as a baseline. Baselines "
            "are real cross-run measurements; a synthetic snapshot would leak into "
            "later comparisons as if genuine."
        )
    if not metrics:
        raise BaselineError(f"No metrics found for run {run_id!r}; nothing to save as a baseline.")

    tasks: dict[str, Any] = {}
    for r in metrics:
        variants = tasks.setdefault(r.scenario, {}).setdefault("variants", {})
        runs = variants.setdefault(r.variant, {}).setdefault("runs", [])
        runs.append(
            {
                "epoch": _pair_label(r.fixture, r.epoch),
                **{key: float(getattr(r, key)) for _label, key, _precision in _METRIC_DEFS},
            }
        )

    payload = {
        "name": name,
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        # Persisted defensively (always False here, since replayed snapshots are
        # refused above): a consumer that ever encounters a synthetic baseline
        # can still detect and label it rather than treating it as genuine.
        "replayed": bool(replayed),
        "tasks": tasks,
    }

    out_dir = baselines_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = baseline_path(config, name)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def load_baseline(config: Config, name: str) -> dict[str, Any]:
    """Load a saved baseline snapshot by name."""
    path = baseline_path(config, name)
    if not path.exists():
        raise BaselineError(
            f"No baseline named {name!r} (looked in {path}). "
            "Run `baseline list` to see available baselines."
        )
    try:
        data: Any = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise BaselineError(f"Baseline {name!r} is corrupt ({path}): {e}") from e
    if not isinstance(data, dict):
        raise BaselineError(f"Baseline {name!r} is corrupt ({path}): expected a JSON object.")
    return data


def list_baselines(config: Config) -> list[dict[str, Any]]:
    """List saved baselines with summary metadata (name, run_id, created_at,
    and task/variant/run counts). Skips unreadable/corrupt files rather than
    failing the whole listing.
    """
    out_dir = baselines_dir(config)
    if not out_dir.is_dir():
        return []

    entries: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        tasks = data.get("tasks", {}) or {}
        variants_per_task = [t.get("variants", {}) or {} for t in tasks.values()]
        variant_count = len({v for variants in variants_per_task for v in variants})
        run_count = sum(
            len(v.get("runs", []) or [])
            for variants in variants_per_task
            for v in variants.values()
        )
        entries.append(
            {
                "name": data.get("name", path.stem),
                "run_id": data.get("run_id", ""),
                "created_at": data.get("created_at", ""),
                "tasks": len(tasks),
                "variants": variant_count,
                "runs": run_count,
            }
        )
    return entries


def delete_baseline(config: Config, name: str) -> None:
    """Delete a saved baseline by name. Raises `BaselineError` if not found."""
    path = baseline_path(config, name)
    if not path.exists():
        raise BaselineError(f"No baseline named {name!r} (looked in {path}).")
    path.unlink()
