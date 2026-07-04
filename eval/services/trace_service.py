"""Trace collection for `analyze`: fetching from Jaeger or reading file-exporter
output, plus reconciling ingested traces against a run's manifest."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import click

from eval.collectors import create_collector
from eval.config import Config, Task, Variant
from eval.protocols import RunContext, RunStatus
from eval.trace import Trace, fetch_traces, filter_by_run


def _fetch_traces_for_run(
    config: Config, jaeger: str, run_id: str, manifest_runs: list[dict[str, Any]] | None
) -> list[Trace]:
    """Fetch traces for a run, retrying while ingestion catches up.

    Uses a server-side tag filter on eval.run_id and a high limit so large runs
    aren't truncated. If a manifest is available, retries until the number of
    fetched traces reaches the number of runs that should have produced one.
    """
    expected = None
    if manifest_runs is not None:
        # Only completed runs are guaranteed to emit a trace; timeout/failed
        # runs may not, so don't let them keep the retry loop waiting forever.
        expected = sum(1 for r in manifest_runs if r.get("status") == RunStatus.SUCCESS.value)

    retries = max(1, config.runner.trace_fetch_retries)
    traces: list[Trace] = []
    for attempt in range(1, retries + 1):
        traces = fetch_traces(jaeger, limit=config.runner.trace_fetch_limit, run_id=run_id)
        traces = filter_by_run(traces, run_id)  # safety net for any over-broad matches
        if expected is None or len(traces) >= expected:
            return traces
        if attempt < retries:
            click.echo(
                f"  Waiting for trace ingestion ({len(traces)}/{expected})... "
                f"retry {attempt}/{retries - 1}",
                err=True,
            )
            time.sleep(config.runner.trace_fetch_retry_delay)
    if expected is not None and len(traces) < expected:
        click.echo(
            f"  WARNING: only {len(traces)}/{expected} expected traces ingested "
            f"after {retries} attempt(s).",
            err=True,
        )
    return traces


def _collect_file_traces(config: Config, run_id: str, results_dir: Path) -> list[Trace]:
    """Collect traces from file exporter output stored in results directory."""
    task = config.tasks[0] if config.tasks else Task(name="analyze", prompt="")
    variant = config.variants[0] if config.variants else Variant(name="analyze")
    collector = create_collector("file")
    return collector.collect(
        RunContext(
            run_id=run_id,
            test_id="",
            epoch=0,
            run_dir=results_dir,
            task=task,
            variant=variant,
            config=config,
        )
    )


def _report_run_coverage(manifest_runs: list[dict[str, Any]], traces: list[Trace]) -> None:
    """Reconcile persisted runs against ingested traces and warn about gaps."""
    trace_test_ids = {t.resource_tags.get("eval.test_id") for t in traces}

    missing: list[str] = []
    failed: list[str] = []
    for r in manifest_runs:
        fx = r.get("fixture")
        base = f"{r.get('task')}/{r.get('variant')}/e{r.get('epoch')}"
        label = f"{r.get('task')}[{fx}]/{r.get('variant')}/e{r.get('epoch')}" if fx else base
        status = r.get("status", RunStatus.SUCCESS.value)
        has_trace = r.get("test_id") in trace_test_ids
        if status == RunStatus.TIMEOUT.value:
            failed.append(f"{label} (timeout)")
        elif status == RunStatus.FAILED.value:
            failed.append(f"{label} (exit {r.get('exit_code')})")
        elif status == RunStatus.SETUP_FAILED.value:
            failed.append(f"{label} (setup_failed)")
        elif not has_trace:
            # Run reported as completed but no trace ingested → silently dropped.
            missing.append(label)

    total = len(manifest_runs)
    ok = total - len(missing) - len(failed)
    click.echo(
        f"Run coverage: {ok}/{total} ok, {len(failed)} failed/timeout, {len(missing)} missing trace.",
        err=True,
    )
    if failed:
        click.echo(f"  Failed/timeout runs (excluded from metrics): {', '.join(failed)}", err=True)
    if missing:
        click.echo(
            f"  WARNING: completed runs with no ingested trace: {', '.join(missing)}", err=True
        )
