"""`baseline` command group: save/list/delete named cross-run baseline
snapshots used by `analyze --baseline <name>` for regression tracking.
Routing only -- business logic lives in eval.services.baseline_service.
"""

from __future__ import annotations

from pathlib import Path

import click

from eval.config import load_config
from eval.services.baseline_service import delete_baseline, list_baselines, save_baseline
from eval.services.trace_service import load_run_metrics


@click.group(name="baseline")
def baseline() -> None:
    """Manage named baseline snapshots for cross-run regression tracking."""


@baseline.command(name="save")
@click.option("--run-id", required=True, help="Run ID to snapshot as a baseline")
@click.option("--name", "name", required=True, help="Name to save the baseline under")
@click.option("--jaeger-url", default=None, help="Jaeger URL override (forces jaeger collector)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def save(run_id: str, name: str, jaeger_url: str | None, config_dir: str | None) -> None:
    """Save a run's OTel metrics as a named baseline snapshot."""
    config = load_config(Path(config_dir) if config_dir else None)
    metrics = load_run_metrics(config, run_id, jaeger_url)
    path = save_baseline(config, run_id, name, metrics)
    click.echo(f"Saved baseline {name!r} from run {run_id} -> {path}")


@baseline.command(name="list")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def list_(config_dir: str | None) -> None:
    """List saved baselines."""
    config = load_config(Path(config_dir) if config_dir else None)
    entries = list_baselines(config)
    if not entries:
        click.echo("No baselines saved.")
        return

    click.echo(
        f"{'Name':<20} {'Run ID':<28} {'Created':<20} {'Tasks':>6} {'Variants':>9} {'Runs':>6}"
    )
    click.echo("-" * 95)
    for e in entries:
        click.echo(
            f"{e['name']:<20} {e['run_id']:<28} {e['created_at']:<20} "
            f"{e['tasks']:>6} {e['variants']:>9} {e['runs']:>6}"
        )


@baseline.command(name="delete")
@click.option("--name", "name", required=True, help="Baseline name to delete")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def delete(name: str, config_dir: str | None) -> None:
    """Delete a saved baseline."""
    config = load_config(Path(config_dir) if config_dir else None)
    delete_baseline(config, name)
    click.echo(f"Deleted baseline {name!r}")
