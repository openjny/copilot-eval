"""`analyze` command: routing only — delegates to eval.services.analyze_service."""

from __future__ import annotations

import click

from eval.services.analyze_service import run_analysis


@click.command()
@click.option("--run-id", required=True, help="Run ID to analyze")
@click.option(
    "--output",
    "-o",
    type=click.Choice(["table", "json", "markdown"]),
    default="table",
    help="Output format",
)
@click.option(
    "--aggregate",
    "-a",
    type=click.Choice(["paired", "median", "mean"]),
    default="paired",
    help="Aggregation method",
)
@click.option("--jaeger-url", default=None, help="Jaeger URL override (forces jaeger collector)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
@click.option("--skip-eval", is_flag=True, help="Skip judge evaluation, use existing scores")
@click.option(
    "--re-eval", is_flag=True, help="Force re-run judge evaluation (ignore cached scores)"
)
@click.option(
    "--min-epochs",
    type=int,
    default=None,
    help=(
        "CI gate: exit non-zero if any task has fewer than N (paired) epochs. "
        "Use e.g. --min-epochs 10 to require enough data for reliable conclusions."
    ),
)
@click.option(
    "--no-mc-correction",
    is_flag=True,
    help=(
        "Disable the Holm-Bonferroni multiple-comparison correction applied to "
        "significance markers (`*`) across each task's family of metrics/judge "
        "criteria. Off by default only when you explicitly want the old, "
        "uncorrected per-metric significance check."
    ),
)
@click.option(
    "--compact",
    is_flag=True,
    help=(
        "With '-o markdown': condensed, PR-comment-friendly output (headline "
        "table + CI summary + warnings, no per-run detail). Fits GitHub's "
        "65KB comment limit -- pipe straight into `gh pr comment --body`."
    ),
)
@click.option(
    "--no-progress",
    is_flag=True,
    help="Disable live progress reporting for judge scoring",
)
def analyze(
    run_id: str,
    output: str,
    aggregate: str,
    jaeger_url: str | None,
    config_dir: str | None,
    skip_eval: bool,
    re_eval: bool,
    min_epochs: int | None,
    no_mc_correction: bool,
    compact: bool,
    no_progress: bool,
) -> None:
    """Analyze traces from a previous eval run."""
    run_analysis(
        run_id=run_id,
        output=output,
        aggregate=aggregate,
        jaeger_url=jaeger_url,
        config_dir=config_dir,
        skip_eval=skip_eval,
        re_eval=re_eval,
        min_epochs=min_epochs,
        mc_correction="none" if no_mc_correction else "holm",
        compact=compact,
        no_progress=no_progress,
    )
