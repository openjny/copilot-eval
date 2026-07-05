"""Business logic for the `analyze` CLI command: fetch traces (Jaeger or file
collector), run judge + metric evaluators, build the A/B report, and gate CI
on metric thresholds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from eval.config import load_config
from eval.progress import create_reporter
from eval.report import (
    BaselineComparison,
    Report,
    baseline_comparisons_json,
    build_baseline_comparisons,
    build_report,
    format_baseline_table,
    format_gha_summary,
    format_html,
    format_json,
    format_junit,
    format_markdown,
    format_markdown_compact,
    format_table,
    write_gha_summary,
)
from eval.services.baseline_service import load_baseline
from eval.services.judge_service import (
    _report_judge_reliability,
    _run_judges,
    _warn_unscored_judges,
)
from eval.services.manifest import load_manifest
from eval.services.metrics_service import _run_metric_evaluators
from eval.services.orchestrator import _ensure_jaeger
from eval.services.trace_service import (
    _collect_file_traces,
    _fetch_traces_for_run,
    _report_run_coverage,
)
from eval.trace import RunMetrics, Trace, extract_metrics

_FORMATTERS = {
    "table": format_table,
    "json": format_json,
    "markdown": format_markdown,
    "junit": format_junit,
    "gha-summary": format_gha_summary,
    "html": format_html,
}


def _gate_epochs(report: Report) -> int:
    """Epoch count `--min-epochs` should gate on.

    Paired reports gate on the shared paired-epoch count (the number of
    deltas actually being compared); everything else (single variant, or
    median/mean aggregate) falls back to the smallest per-variant sample.
    """
    if report.aggregate == "paired" and len(report.variants) == 2:
        return report.paired_n
    return min(report.variant_n.values(), default=0)


def run_analysis(
    *,
    run_id: str,
    output: str,
    aggregate: str,
    jaeger_url: str | None,
    config_dir: str | None,
    skip_eval: bool,
    re_eval: bool,
    min_epochs: int | None = None,
    mc_correction: str = "holm",
    compact: bool = False,
    no_progress: bool = False,
    baseline_name: str | None = None,
    fail_on_regression: bool | None = None,
) -> None:
    """Analyze traces from a previous eval run and print the A/B report."""
    config = load_config(Path(config_dir) if config_dir else None)
    results_dir = config.results_dir / run_id

    manifest_runs = load_manifest(results_dir)

    collector_type = "jaeger" if jaeger_url else config.runner.collector
    traces: list[Trace]
    if collector_type == "jaeger":
        jaeger = jaeger_url or config.runner.jaeger_url
        _ensure_jaeger(config, jaeger)
        click.echo(f"Fetching traces from {jaeger} for run {run_id}...", err=True)
        traces = _fetch_traces_for_run(config, jaeger, run_id, manifest_runs)
    else:
        click.echo(f"Reading traces from file collector for run {run_id}...", err=True)
        traces = _collect_file_traces(config, run_id, results_dir)

    metrics: list[RunMetrics] = [m for m in (extract_metrics(t) for t in traces) if m is not None]

    # Reconcile against the persisted manifest so failed/timeout/missing runs
    # are surfaced instead of silently dropped (survivorship bias).
    if manifest_runs is not None:
        _report_run_coverage(manifest_runs, traces)
    elif not metrics:
        click.echo(
            "No traces found for this run ID, and no manifest to reconcile against.", err=True
        )
        return

    # With no surviving traces we can't show metrics, but a manifest still lets us
    # report reliability (success/failure rates) — which is exactly when a run
    # with all-failed/timed-out variants needs it most. Only bail when there is
    # neither trace data nor a manifest to fall back on.
    if not metrics and manifest_runs is None:
        click.echo("No traces found for this run ID.", err=True)
        return
    if not metrics:
        click.echo("No surviving traces; reporting reliability from the manifest only.", err=True)

    # Run judge evaluators if not skipped
    if not skip_eval and results_dir.exists():
        reporter = create_reporter(no_progress=no_progress)
        _run_judges(config, traces, results_dir, force=re_eval, reporter=reporter)
        _warn_unscored_judges(config, traces, results_dir)
    # Metric evaluators are deterministic (no LLM), so they run every analyze —
    # even with --skip-eval — so CI gates always reflect the current telemetry.
    # No results_dir.exists() guard: _merge_scores_file creates the directory as
    # needed, so gating still runs when `run` and `analyze` are separate CI jobs
    # (or a non-file collector) and the results dir wasn't pre-created. A skipped
    # gate here would silently exit 0 and defeat the CI gate.
    failed_gates = _run_metric_evaluators(config, traces, results_dir, manifest_runs)
    if results_dir.exists():
        _report_judge_reliability(results_dir)

    variant_order = [v.name for v in config.variants]
    raw_tids = {t.resource_tags.get("eval.test_id") for t in traces}
    trace_test_ids = {t for t in raw_tids if t is not None}
    reports = build_report(
        metrics,
        results_dir if results_dir.exists() else None,
        variant_order,
        aggregate,
        manifest_runs=manifest_runs,
        trace_test_ids=trace_test_ids,
        mc_correction=mc_correction,
    )
    if not reports:
        click.echo("No reports generated.", err=True)
        return

    # Cross-run baseline comparison (issue #65): compares this run's raw
    # metrics against a previously saved snapshot (`baseline save`) via
    # unpaired bootstrap, since the two runs share no epoch to pair on.
    baseline_comparisons: list[BaselineComparison] = []
    baseline_missing: list[str] = []
    if baseline_name:
        baseline_data = load_baseline(config, baseline_name)
        baseline_comparisons, baseline_missing = build_baseline_comparisons(
            metrics, baseline_data, variant_order, mc_correction
        )

    formatter = (
        format_markdown_compact if (compact and output == "markdown") else _FORMATTERS[output]
    )
    if output == "json":
        payload = json.loads(formatter(reports))
        if baseline_name:
            payload["baseline"] = baseline_comparisons_json(baseline_comparisons, baseline_missing)
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        content = formatter(reports)
        if baseline_name:
            content = (
                f"{content}\n\n{format_baseline_table(baseline_comparisons, baseline_missing)}"
            )
        if output == "gha-summary" and write_gha_summary(content):
            click.echo("Report appended to $GITHUB_STEP_SUMMARY.", err=True)
        else:
            click.echo(content)

    gate_failures: list[str] = []

    # CI gating: fail the command (non-zero exit) when any metric gate did not
    # pass, so a regression in cost/latency/tokens can block a merge. Runs with no
    # metric evaluators never populate failed_gates, so they stay exit 0.
    if failed_gates:
        gate_failures.append(f"Metric gate failed: {'; '.join(failed_gates)}")

    if min_epochs is not None:
        underpowered = [
            f"{r.task} (n={_gate_epochs(r)})" for r in reports if _gate_epochs(r) < min_epochs
        ]
        if underpowered:
            gate_failures.append(
                f"Insufficient epochs (< {min_epochs}) for reliable conclusions: "
                f"{', '.join(underpowered)}"
            )

    if baseline_name:
        # Default to gating in CI (the $CI env var GitHub Actions and most
        # other CI providers set) unless the user passed an explicit
        # --fail-on-regression/--no-fail-on-regression override.
        resolved_fail_on_regression = (
            fail_on_regression if fail_on_regression is not None else bool(os.environ.get("CI"))
        )
        if resolved_fail_on_regression:
            regressed = [f"{c.task}/{c.variant}" for c in baseline_comparisons if c.has_regression]
            if regressed:
                gate_failures.append(
                    f"Regression vs baseline {baseline_name!r}: {', '.join(regressed)}"
                )

    if gate_failures:
        raise click.ClickException("\n".join(gate_failures))
