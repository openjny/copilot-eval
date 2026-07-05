"""`run` command: routing only — delegates scheduling to eval.services.orchestrator."""

from __future__ import annotations

from pathlib import Path

import click

from eval.config import load_config
from eval.services.orchestrator import run_command


@click.command()
@click.option(
    "--task",
    "-p",
    default=None,
    help="Run only this task by name (overrides the per-task 'enabled' flag)",
)
@click.option(
    "--epochs",
    "-n",
    default=None,
    type=int,
    help="Repetitions per variant × fixture cell (default: runner.epochs, typically 1)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the plan and matrix size, then exit without building images or running anything",
)
@click.option(
    "--no-build",
    is_flag=True,
    help="Skip auto-building Docker images (assume they already exist)",
)
@click.option(
    "--skip-preflight",
    is_flag=True,
    help="Skip pre-flight readiness checks (Docker/auth/fixtures/disk space)",
)
@click.option(
    "--no-progress",
    is_flag=True,
    help="Disable live progress reporting (progress bar / per-cell status)",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Re-run only failed/missing matrix cells from an existing run (requires --run-id)",
)
@click.option(
    "--run-id",
    default=None,
    help="Existing run id to resume (with --resume); new results merge into its directory",
)
@click.option(
    "--estimate",
    is_flag=True,
    help="Show a pre-flight cost estimate and confirm before running (see runner.budget_limit)",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the --estimate confirmation prompt",
)
@click.option(
    "--budget-limit",
    default=None,
    type=float,
    help="Abort if the pre-flight cost estimate (USD) exceeds this value (overrides runner.budget_limit)",
)
@click.option(
    "--config-dir",
    default=None,
    type=click.Path(exists=True),
    help="Project directory containing eval-config.yaml (defaults to the repo root)",
)
def run(
    task: str | None,
    epochs: int | None,
    dry_run: bool,
    no_build: bool,
    skip_preflight: bool,
    no_progress: bool,
    resume: bool,
    run_id: str | None,
    estimate: bool,
    yes: bool,
    budget_limit: float | None,
    config_dir: str | None,
) -> None:
    """Run A/B eval for one or more tasks."""
    config = load_config(Path(config_dir) if config_dir else None)
    run_command(
        config,
        task=task,
        epochs=epochs,
        dry_run=dry_run,
        no_build=no_build,
        skip_preflight=skip_preflight,
        config_dir=config_dir,
        no_progress=no_progress,
        resume=resume,
        run_id=run_id,
        estimate=estimate,
        yes=yes,
        budget_limit=budget_limit,
    )
