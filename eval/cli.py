"""CLI entry point for the eval framework."""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
import uuid
from datetime import datetime
from logging import getLogger
from pathlib import Path
from typing import Any

import click
import requests

from eval.collectors import create_collector
from eval.config import Config, Task, Variant, load_config
from eval.evaluators import load_evaluator_plugins
from eval.logging_config import LOG_FORMATS, LOG_LEVELS, configure_logging
from eval.naming import run_slug
from eval.protocols import EvalContext, RunContext, RunStatus
from eval.report import Report, build_report, format_json, format_markdown, format_table
from eval.runner import RunResult, get_github_token, run_one
from eval.trace import (
    RunMetrics,
    Trace,
    extract_conversation,
    extract_metrics,
    fetch_traces,
    filter_by_run,
)
from eval.validation import (
    CheckResult,
    any_failed,
    check_config_schema,
    check_fixtures,
    check_script_references,
    check_var_interpolation,
    validate_readiness,
)

logger = getLogger(__name__)


def _ensure_jaeger(config: Config, jaeger_url: str | None = None) -> None:
    """Check if Jaeger is reachable, start it via docker compose if not."""
    jaeger_url = jaeger_url or config.runner.jaeger_url
    try:
        requests.get(f"{jaeger_url}/api/services", timeout=3)
        return  # already running
    except (requests.ConnectionError, requests.Timeout):
        pass
    click.echo("Jaeger not running. Starting via docker compose...", err=True)
    compose_file = config.project_dir / "docker-compose.yml"
    if not compose_file.exists():
        raise click.ClickException(
            "Jaeger not running and docker-compose.yml not found. Start Jaeger manually."
        )
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"], check=True, capture_output=True
    )
    # Wait for Jaeger to be ready
    for _ in range(10):
        try:
            requests.get(f"{jaeger_url}/api/services", timeout=2)
            click.echo("Jaeger started.", err=True)
            return
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(1)
    raise click.ClickException("Failed to start Jaeger. Check docker compose logs.")


MANIFEST_NAME = "results.json"


def order_variants(
    variants: list[Variant],
    epoch: int,
    strategy: str,
    rng: random.Random,
) -> list[Variant]:
    """Order variants for a given epoch to reduce order-effect bias.

    - ``fixed``: original config order (backward compatible).
    - ``counterbalance``: cyclic rotation by ``epoch``. Each variant occupies
      every position once per complete cycle of ``len(variants)`` epochs
      (position-balanced; not a full permutation/carryover counterbalance).
    - ``random``: shuffle using the supplied RNG. Pass a seeded RNG (see
      ``_ordering_rng``) for a reproducible schedule.
    """
    if len(variants) <= 1 or strategy == "fixed":
        return list(variants)
    if strategy == "counterbalance":
        k = (epoch - 1) % len(variants)
        return variants[k:] + variants[:k]
    if strategy == "random":
        ordered = list(variants)
        rng.shuffle(ordered)
        return ordered
    return list(variants)


def _ordering_rng(seed: int | None, *parts: object) -> random.Random:
    """Build a per-context RNG for variant ordering.

    A fresh ``random.Random`` is returned per call (so concurrent schedulers in
    ``per_task`` mode never share mutable RNG state — ``random.Random`` is not
    thread-safe). When ``seed`` is set, the RNG is derived deterministically
    from ``(seed, *parts)`` so the schedule is reproducible regardless of thread
    timing; when ``seed`` is ``None`` the RNG is non-deterministic.
    """
    if seed is None:
        return random.Random()
    return random.Random("|".join(str(p) for p in (seed, *parts)))


def _write_manifest(
    run_dir: Path, run_id: str, results: list[RunResult], schedule: dict[str, Any] | None = None
) -> None:
    """Persist the full set of runs so `analyze` can detect missing/failed ones."""
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "schedule": schedule or {},
        "runs": [r.to_dict() for r in results],
    }
    try:
        (run_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    except OSError as e:
        click.echo(f"WARNING: failed to write run manifest: {e}", err=True)


def _load_manifest(results_dir: Path) -> list[dict[str, Any]] | None:
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


def _print_check_results(results: list[CheckResult], title: str) -> None:
    """Print a validation/readiness report, one line per check, to stderr."""
    passed = sum(1 for r in results if r.passed)
    warnings = sum(1 for r in results if not r.passed and not r.blocking)
    summary = f"{title}: {passed}/{len(results)} passed"
    if warnings:
        summary += f" ({warnings} warning{'s' if warnings != 1 else ''})"
    click.echo(summary, err=True)
    for r in results:
        click.echo(r.format(), err=True)


@click.group()
@click.option(
    "--log-level",
    default=None,
    type=click.Choice(LOG_LEVELS, case_sensitive=False),
    help="Diagnostic log level (default: INFO or $EVAL_LOG_LEVEL).",
)
@click.option(
    "--log-format",
    default=None,
    type=click.Choice(LOG_FORMATS, case_sensitive=False),
    help="Diagnostic log format (default: plain or $EVAL_LOG_FORMAT).",
)
def main(log_level: str | None, log_format: str | None) -> None:
    """Copilot CLI A/B evaluation framework."""
    # Discover third-party evaluator types (entry-point group
    # "copilot_eval.evaluators", see issue #66) before any command loads a
    # config, so plugin-defined `type:` strings validate and dispatch.
    load_evaluator_plugins()
    try:
        configure_logging(log_level, log_format)
    except ValueError as exc:
        # Invalid EVAL_LOG_LEVEL / EVAL_LOG_FORMAT env vars reach here (the CLI
        # flags are already guarded by click.Choice). Surface a clean usage error
        # (exit code 2, matching click.Choice) instead of an uncaught traceback.
        raise click.UsageError(str(exc)) from exc


def _safe_run_one(
    task: Task,
    variant: Variant,
    epoch: int,
    config: Config,
    run_id: str,
    run_dir: Path,
    github_token: str,
    order_index: int | None = None,
    fixture: str | None = None,
) -> RunResult:
    """Run a single eval, guaranteeing the batch is never aborted by one run.

    `run_one` already converts internal errors into a setup_failed RunResult, but
    this boundary catches any truly unexpected exception (so one bad run cannot
    take down the whole batch or prevent the manifest from being written) and
    turns it into a synthetic setup_failed result instead of re-raising.
    """
    fixture_dir_name = fixture if fixture is not None else task.fixture_names()[0]
    fixture_label = task.fixture_label(fixture_dir_name)
    try:
        return run_one(
            task,
            variant,
            epoch,
            config,
            run_id,
            run_dir,
            github_token,
            order_index,
            fixture=fixture_dir_name,
        )
    except Exception as exc:  # noqa: BLE001 - isolate per-run failures from the batch
        suffix = f" fixture={fixture_label}" if fixture_label else ""
        logger.error(
            "[%s] epoch=%s variant=%s%s errored: %s",
            task.name,
            epoch,
            variant.name,
            suffix,
            exc,
        )
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id=uuid.uuid4().hex,
            run_id=run_id,
            log_file=run_dir / (run_slug(task.name, variant.name, epoch, fixture_label) + ".log"),
            exit_code=-1,
            status=RunStatus.SETUP_FAILED,
            order_index=order_index,
            fixture=fixture_label,
        )


@main.command()
@click.option("--task", "-p", default=None, help="Run a specific task (overrides enabled flag)")
@click.option(
    "--epochs",
    "-n",
    default=None,
    type=int,
    help="Number of epochs (default: from config, typically 1)",
)
@click.option("--dry-run", is_flag=True, help="Show plan without executing")
@click.option("--no-build", is_flag=True, help="Skip auto-build of Docker images")
@click.option(
    "--skip-preflight",
    is_flag=True,
    help="Skip pre-flight readiness checks (Docker/auth/fixtures/disk space)",
)
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
def run(
    task: str | None,
    epochs: int | None,
    dry_run: bool,
    no_build: bool,
    skip_preflight: bool,
    config_dir: str | None,
) -> None:
    """Run A/B eval for one or more tasks."""
    config = load_config(Path(config_dir) if config_dir else None)
    epochs = epochs or config.runner.epochs

    # Select tasks
    if task:
        p = config.get_task(task)
        if not p:
            raise click.ClickException(
                f"Task '{task}' not found. Use 'list' to see available tasks."
            )
        tasks = [p]
    else:
        tasks = config.enabled_tasks()

    if not tasks:
        click.echo("No tasks to run. Use --task NAME or enable tasks in config.")
        return

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = config.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Print plan
    click.echo("=" * 50)
    click.echo(" Copilot Eval Runner")
    click.echo("=" * 50)
    click.echo(f" Model:    {config.runner.model or 'default'}")
    click.echo(f" Effort:   {config.runner.reasoning_effort or 'default'}")
    click.echo(f" Max turns:{config.runner.max_turns or 'unlimited'}")
    click.echo(f" Epochs:   {epochs}")
    click.echo(f" Timeout:  {config.runner.timeout_seconds}s")
    click.echo(f" Parallel: {config.runner.parallel}")
    click.echo(f" Collector: {config.runner.collector}")
    order_desc = config.runner.variant_order
    if config.runner.variant_order == "random" and config.runner.seed is not None:
        order_desc += f" (seed={config.runner.seed})"
    click.echo(f" Order:    {order_desc}")
    click.echo(f" Run ID:   {run_id}")
    if config.vars:
        click.echo(f" Vars:     {config.vars}")
    click.echo(" Variants:")
    for v in config.variants:
        click.echo(f"   - {v.name}")
    click.echo(" Tasks:")
    for p in tasks:
        click.echo(f"   - {p.name}")
    click.echo("=" * 50)

    if dry_run:
        total = sum(len(p.fixture_names()) for p in tasks) * epochs * len(config.variants)
        click.echo(
            f"[dry-run] Would run {epochs} epoch(s) × {len(config.variants)} variants × "
            f"fixtures for each task ({total} runs total)."
        )
        return

    # Pre-flight: fail fast on missing Docker/auth/disk space before doing any
    # Docker work (build, pull, or jaeger startup). Non-blocking warnings
    # (e.g. a missing fixture dir, which the runner itself tolerates) are
    # printed but never abort the run; only blocking failures do.
    if skip_preflight:
        click.echo("Pre-flight checks: skipped (--skip-preflight)", err=True)
    else:
        # The base-image check only makes sense when builds are disabled
        # (--no-build): if auto-build is enabled, _ensure_images() below will
        # build the image itself, so a missing image pre-flight isn't a
        # failure — the default first-run flow must not be blocked here.
        preflight = validate_readiness(config, tasks=tasks, check_build=no_build)
        if any(not r.passed for r in preflight):
            _print_check_results(preflight, "Pre-flight checks")
        if any_failed(preflight):
            raise click.ClickException(
                "Pre-flight validation failed. Fix the issues above and re-run."
            )

    if config.runner.collector == "jaeger":
        _ensure_jaeger(config)
    github_token = get_github_token()
    if not no_build:
        _ensure_images(config, github_token)
    results: list[RunResult] = []

    order = config.runner.variant_order
    seed = config.runner.seed

    if config.runner.parallel == "full":
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # variant_order is applied per epoch so the submission schedule is
        # balanced; actual start times are still recorded per run since the
        # thread pool decides true concurrency. A per-(task, epoch) RNG keeps the
        # schedule reproducible under a seed without sharing RNG state.
        work = [
            (t, v, e, f)
            for t in tasks
            for f in t.fixture_names()
            for e in range(1, epochs + 1)
            for v in order_variants(config.variants, e, order, _ordering_rng(seed, t.name, e))
        ]
        click.echo(
            f"Running {len(work)} runs in full parallel (max_workers={config.runner.max_workers})"
        )
        with ThreadPoolExecutor(max_workers=config.runner.max_workers) as pool:
            futures = {
                pool.submit(
                    _safe_run_one, t, v, e, config, run_id, run_dir, github_token, i, f
                ): f"{t.name}/{v.name}/e{e}"
                for i, (t, v, e, f) in enumerate(work)
            }
            for future in as_completed(futures):
                results.append(future.result())

    elif config.runner.parallel == "per_task" and len(tasks) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _run_task_serial(task: Task) -> list[RunResult]:
            """Run all fixtures × epochs × variants for a single task sequentially."""
            task_results: list[RunResult] = []
            order_index = 0
            for fixture in task.fixture_names():
                for epoch in range(1, epochs + 1):
                    # Each worker thread uses its own RNG (random.Random is not
                    # thread-safe); derived from the seed for reproducibility.
                    ordered = order_variants(
                        config.variants, epoch, order, _ordering_rng(seed, task.name, epoch)
                    )
                    for variant in ordered:
                        task_results.append(
                            _safe_run_one(
                                task,
                                variant,
                                epoch,
                                config,
                                run_id,
                                run_dir,
                                github_token,
                                order_index,
                                fixture,
                            )
                        )
                        order_index += 1
            return task_results

        click.echo(f"Running {len(tasks)} tasks in parallel (variants serial within each task)")
        with ThreadPoolExecutor(max_workers=min(len(tasks), config.runner.max_workers)) as pool:
            task_futures = {pool.submit(_run_task_serial, t): t.name for t in tasks}
            for task_future in as_completed(task_futures):
                results.extend(task_future.result())
    else:
        order_index = 0
        for p in tasks:
            prompt = config.resolve_prompt(p, config.variants[0])
            click.echo(f"\n>>> Task: {p.name}")
            click.echo(f">>> Prompt:  {prompt}\n")

            for fixture in p.fixture_names():
                if p.is_multi_fixture:
                    click.echo(f">>> Fixture: {fixture}")
                for epoch in range(1, epochs + 1):
                    for variant in order_variants(
                        config.variants, epoch, order, _ordering_rng(seed, p.name, epoch)
                    ):
                        result = _safe_run_one(
                            p,
                            variant,
                            epoch,
                            config,
                            run_id,
                            run_dir,
                            github_token,
                            order_index,
                            fixture,
                        )
                        results.append(result)
                        order_index += 1

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    timed_out = sum(1 for r in results if r.status == RunStatus.TIMEOUT)
    errored = sum(1 for r in results if r.status in (RunStatus.FAILED, RunStatus.SETUP_FAILED))

    # Persist a run manifest so `analyze` knows the full expected set of runs
    # (including failed/timeout runs that may have produced no trace).
    schedule = {
        "parallel": config.runner.parallel,
        "max_workers": config.runner.max_workers,
        "variant_order": config.runner.variant_order,
        "seed": config.runner.seed,
    }
    _write_manifest(run_dir, run_id, results, schedule)

    click.echo("=" * 50)
    click.echo(f" Run complete: {run_id}")
    click.echo(f" Results: {passed} passed, {failed} failed")
    if timed_out or errored:
        click.echo(f"   (of which {timed_out} timed out, {errored} errored)")
    if config.runner.collector == "jaeger":
        click.echo(f" Jaeger:  {config.runner.jaeger_url}")
    else:
        click.echo(f" Collector: {config.runner.collector}")
    analyze_cmd = f"uv run copilot-eval analyze --run-id {run_id}"
    if config_dir:
        analyze_cmd += f" --config-dir {config_dir}"
    click.echo(f" Analyze: {analyze_cmd}")
    click.echo("=" * 50)


def _gate_epochs(report: Report) -> int:
    """Epoch count `--min-epochs` should gate on.

    Paired reports gate on the shared paired-epoch count (the number of
    deltas actually being compared); everything else (single variant, or
    median/mean aggregate) falls back to the smallest per-variant sample.
    """
    if report.aggregate == "paired" and len(report.variants) == 2:
        return report.paired_n
    return min(report.variant_n.values(), default=0)


@main.command()
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
def analyze(
    run_id: str,
    output: str,
    aggregate: str,
    jaeger_url: str | None,
    config_dir: str | None,
    skip_eval: bool,
    re_eval: bool,
    min_epochs: int | None,
) -> None:
    """Analyze traces from a previous eval run."""
    config = load_config(Path(config_dir) if config_dir else None)
    results_dir = config.results_dir / run_id

    manifest_runs = _load_manifest(results_dir)

    collector_type = "jaeger" if jaeger_url else config.runner.collector
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
        _run_judges(config, traces, results_dir, force=re_eval)
        _warn_unscored_judges(config, traces, results_dir)
    # Metric evaluators are deterministic (no LLM), so they run every analyze —
    # even with --skip-eval — so CI gates always reflect the current telemetry.
    # No results_dir.exists() guard: _merge_scores_file creates the directory as
    # needed, so gating still runs when `run` and `analyze` are separate CI jobs
    # (or a non-file collector) and the results dir wasn't pre-created. A skipped
    # gate here would silently exit 0 and defeat the CI gate.
    failed_gates = _run_metric_evaluators(config, traces, results_dir)
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
    )
    if not reports:
        click.echo("No reports generated.", err=True)
        return

    formatters = {"table": format_table, "json": format_json, "markdown": format_markdown}
    click.echo(formatters[output](reports))

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

    if gate_failures:
        raise click.ClickException("\n".join(gate_failures))


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


def _warn_unscored_judges(config: Config, traces: list[Trace], results_dir: Path) -> None:
    """Surface judge reproducibility issues: unusable scores, outcome-rate
    breakdown, host Copilot version mismatches, and truncated context."""
    tasks_by_name = {t.name: t for t in config.tasks}
    problems: list[str] = []
    outcomes: dict[str, int] = {}
    judge_total = 0
    mismatches: set[str] = set()
    versions: set[str] = set()
    truncated: list[str] = []
    seen_files: set[Path] = set()
    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        fixture = trace.resource_tags.get("eval.fixture", "")
        task = tasks_by_name.get(scenario)
        if not task or not any(ev.type == "judge" for ev in task.evaluators):
            continue
        scores_file = results_dir / f"{run_slug(scenario, variant, epoch, fixture)}.scores.json"
        if not scores_file.exists() or scores_file in seen_files:
            continue
        seen_files.add(scores_file)
        try:
            scores = json.loads(scores_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        label = (
            f"{scenario}/{fixture}/{variant}/e{epoch}"
            if fixture
            else f"{scenario}/{variant}/e{epoch}"
        )
        for s in scores:
            if s.get("type") != "judge":
                continue
            judge_total += 1
            meta = s.get("meta") or {}
            outcome = meta.get("outcome") or ("ok" if s.get("score") is not None else "unknown")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if v := meta.get("judge_version"):
                versions.add(str(v))
            if mm := meta.get("judge_version_mismatch"):
                mismatches.add(f"expected {mm.get('expected')} got {mm.get('actual')}")
            if meta.get("truncation"):
                truncated.append(f"{label}:{s.get('name')}")
            if s.get("score") is None:
                reason = s.get("reason", "no score")
                problems.append(f"{label}:{s.get('name')} ({reason})")

    if judge_total:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
        click.echo(f"  Judge outcomes ({judge_total} total): {breakdown}", err=True)
    if versions:
        click.echo(f"  Judge host Copilot version(s): {', '.join(sorted(versions))}", err=True)
    if mismatches:
        click.echo(
            f"  WARNING: judge Copilot version mismatch — {'; '.join(sorted(mismatches))}", err=True
        )
    if truncated:
        click.echo(
            f"  WARNING: {len(truncated)} judge(s) saw truncated context "
            f"(raise runner.judge_max_conversation_chars / judge_max_output_chars): "
            f"{', '.join(truncated)}",
            err=True,
        )
    if problems:
        click.echo(
            f"  WARNING: {len(problems)} judge score(s) unavailable: {', '.join(problems)}",
            err=True,
        )


def _report_judge_reliability(results_dir: Path) -> None:
    """Summarize judge self-consistency + parse/error/timeout rates for a run.

    Reads every persisted ``*.scores.json`` in the run directory, aggregates
    judge sample outcomes (ok/parse_error/timeout/error) and per-evaluator score
    spread (stddev), and prints a compact reliability summary so noisy or
    failure-prone judges are visible alongside the metrics.
    """
    outcomes: dict[str, int] = {"ok": 0, "parse_error": 0, "timeout": 0, "error": 0}
    judge_evals = 0  # number of judge score records
    sampled_evals = 0  # records that ran >1 sample
    stddevs: list[float] = []
    no_score = 0

    for jf in sorted(results_dir.glob("*.scores.json")):
        try:
            scores = json.loads(jf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for s in scores:
            if s.get("type") != "judge":
                continue
            judge_evals += 1
            for k, v in (s.get("outcomes") or {}).items():
                outcomes[k] = outcomes.get(k, 0) + int(v)
            n = s.get("n_samples") or 0
            if n and n > 1:
                sampled_evals += 1
            sd = s.get("score_stddev")
            if sd is not None and s.get("score") is not None:
                stddevs.append(float(sd))
            if s.get("score") is None:
                no_score += 1

    if judge_evals == 0:
        return

    total_samples = sum(outcomes.values())
    click.echo("Judge reliability:", err=True)
    click.echo(
        f"  {judge_evals} judge evaluation(s), {total_samples} sample(s)"
        f"{f', {sampled_evals} multi-sampled' if sampled_evals else ''}.",
        err=True,
    )
    if total_samples:

        def rate(k: str) -> str:
            c = outcomes.get(k, 0)
            return f"{c} ({c / total_samples * 100:.0f}%)"

        click.echo(
            f"  Sample outcomes: ok {rate('ok')}, parse_error {rate('parse_error')}, "
            f"timeout {rate('timeout')}, error {rate('error')}.",
            err=True,
        )
    if stddevs:
        mean_sd = sum(stddevs) / len(stddevs)
        max_sd = max(stddevs)
        click.echo(f"  Score spread (σ): mean {mean_sd:.2f}, max {max_sd:.2f}.", err=True)
    if no_score:
        click.echo(f"  WARNING: {no_score} judge evaluation(s) produced no usable score.", err=True)


def _is_truncated(text: str | None) -> bool:
    """Whether judge context text was cut to its char budget by the readers."""
    return bool(text and text.rstrip().endswith("(truncated)"))


def _run_judges(
    config: Config, traces: list[Trace], results_dir: Path, force: bool = False
) -> None:
    """Run judge evaluators using OTel traces + output files.

    Skips judge evaluators that already have a recorded score (judge presence,
    not file existence — non-judge scores share the same file). Pass force=True
    to re-run every judge regardless of cached scores.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from eval.evaluators import JudgeEvaluator
    from eval.runner import read_files_from_dir, run_judges_batch, score_to_dict

    github_token = get_github_token()
    tasks_by_name = {t.name: t for t in config.tasks}

    # Phase 1: build per-trace context and collect the judge work items. Each
    # trace owns a dedicated scores file; contexts are keyed by that file so
    # duplicate traces for the same scenario/variant/epoch coalesce into one
    # writer and don't clobber each other. In batch mode (runner.judge_batch)
    # a context's judges share one work item (one LLM call); otherwise each
    # judge is its own item so failures stay isolated.
    contexts: dict[str, dict[str, Any]] = {}
    work: list[tuple[str, list[Any]]] = []  # (context key, evaluators)

    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        fixture = trace.resource_tags.get("eval.fixture", "")
        task = tasks_by_name.get(scenario)
        if not task:
            continue

        judge_evaluators = [ev for ev in task.evaluators if ev.type == "judge" and ev.prompt]
        if not judge_evaluators:
            continue

        slug = run_slug(scenario, variant, epoch, fixture)
        scores_file = results_dir / f"{slug}.scores.json"
        key = str(scores_file)
        if key in contexts:
            continue  # already collected work for this scores file

        # Load existing scores and decide which judges still need scoring.
        existing_scores: list[dict[str, Any]] = []
        if scores_file.exists():
            try:
                existing_scores = json.loads(scores_file.read_text())
            except (json.JSONDecodeError, OSError):
                existing_scores = []
        existing_judge_names = {s.get("name") for s in existing_scores if s.get("type") == "judge"}
        pending = (
            judge_evaluators
            if force
            else [ev for ev in judge_evaluators if ev.name not in existing_judge_names]
        )
        if not pending:
            continue  # all judge scores already present

        # Extract conversation from OTel trace
        conv_limit = config.runner.judge_max_conversation_chars
        out_limit = config.runner.judge_max_output_chars
        conversation = extract_conversation(trace, max_chars=conv_limit)

        # Fall back to log file if OTel content not available
        if not conversation:
            log_file = results_dir / f"{slug}.log"
            if log_file.exists():
                text = log_file.read_text()
                conversation = (
                    text[:conv_limit] + "\n... (truncated)" if len(text) > conv_limit else text
                )

        # Read output files from persisted outputs. The judge can score on output
        # files alone (e.g. file-writing tasks), so only skip when neither the
        # conversation nor any output file is available.
        output_dir = results_dir / "outputs" / slug
        output_files_text = read_files_from_dir(output_dir, max_chars=out_limit)

        truncation: dict[str, Any] = {}
        if _is_truncated(conversation):
            truncation["conversation"] = conv_limit
        if _is_truncated(output_files_text):
            truncation["output_files"] = out_limit

        if not conversation and not output_files_text:
            continue

        contexts[key] = {
            "scenario": scenario,
            "variant": variant,
            "epoch": epoch,
            "fixture": fixture,
            "scores_file": scores_file,
            "existing_scores": existing_scores,
            "conversation": conversation,
            "output_files_text": output_files_text,
            "truncation": truncation,
            "pending_names": {ev.name for ev in pending},
            "order": {ev.name: i for i, ev in enumerate(pending)},
            "remaining": len(pending),
            "scores": [],
        }
        # Batch mode groups all of a context's judges into one call; otherwise
        # each judge is its own work item so a failure only affects that judge.
        if config.runner.judge_batch:
            work.append((key, list(pending)))
        else:
            for ev in pending:
                work.append((key, [ev]))

    if not work:
        return

    def _write_ctx(ctx: dict[str, Any]) -> None:
        """Merge a trace's collected judge scores with kept scores and persist."""
        rerun_names = ctx["pending_names"]
        kept = [
            s
            for s in ctx["existing_scores"]
            if s.get("type") != "judge" or s.get("name") not in rerun_names
        ]
        scores = sorted(ctx["scores"], key=lambda s: ctx["order"].get(s.get("name"), 0))
        all_scores = kept + scores
        if all_scores:
            ctx["scores_file"].write_text(json.dumps(all_scores, indent=2, ensure_ascii=False))

    # Phase 2: run judge evaluators in parallel (each work item invokes Copilot).
    # Results are collected per trace context on the main thread; a context's
    # scores file is written as soon as all of its judges complete, so an
    # interrupt or crash only loses traces still in flight.
    def _judge(key: str, evs: list[Any]) -> tuple[str, list[dict[str, Any]]]:
        ctx = contexts[key]
        label = ", ".join(ev.name for ev in evs)
        fx = f"/{ctx['fixture']}" if ctx.get("fixture") else ""
        click.echo(
            f"    [{ctx['scenario']}{fx}/{ctx['variant']}/e{ctx['epoch']}] Evaluating: {label} (judge)...",
            err=True,
        )
        extra_meta = {"truncation": ctx["truncation"]} if ctx["truncation"] else None
        if config.runner.judge_batch and len(evs) > 1:
            scored = run_judges_batch(
                evs,
                ctx["conversation"],
                config,
                github_token,
                ctx["output_files_text"],
                extra_meta=extra_meta,
            )
        else:
            score = JudgeEvaluator.from_config(evs[0]).evaluate(
                EvalContext(
                    evaluator=evs[0],
                    config=config,
                    token=github_token,
                    conversation=ctx["conversation"],
                    output_files_text=ctx["output_files_text"],
                    extra_meta=extra_meta,
                )
            )
            # JudgeEvaluator only returns None when both conversation and
            # output_files_text are empty; the collection phase above already
            # guarantees at least one is present for every context here.
            assert score is not None
            scored = [score]
        for s in scored:
            if s.score is not None:
                click.echo(f"    ✓ {s.name}: {s.score} — {s.reason[:60]}", err=True)
            else:
                click.echo(f"    ! {s.name}: {s.reason}", err=True)
        return key, [score_to_dict(s) for s in scored]

    with ThreadPoolExecutor(max_workers=config.runner.max_workers) as pool:
        futures = {pool.submit(_judge, key, evs): (key, evs) for key, evs in work}
        for future in as_completed(futures):
            key, evs = futures[future]
            try:
                key, scores = future.result()
            except Exception as exc:  # never let one judge abort the whole batch
                click.echo(f"    ! {', '.join(ev.name for ev in evs)}: error — {exc}", err=True)
                n = max(1, config.runner.judge_samples)
                scores = [
                    {
                        "name": ev.name,
                        "type": "judge",
                        "score": None,
                        "reason": f"error: {exc}",
                        "passed": False,
                        "samples": [],
                        "score_stddev": None,
                        "n_samples": n,
                        "outcomes": {"ok": 0, "parse_error": 0, "timeout": 0, "error": n},
                        "judge_model": config.runner.judge_model,
                        "judge_version": None,
                    }
                    for ev in evs
                ]
            ctx = contexts[key]
            ctx["scores"].extend(scores)
            ctx["remaining"] -= len(scores)
            if ctx["remaining"] <= 0:
                _write_ctx(ctx)


def _run_metric_evaluators(config: Config, traces: list[Trace], results_dir: Path) -> list[str]:
    """Score type=metric evaluators from parsed telemetry.

    Deterministic and LLM-free: for each trace, thresholds the requested
    ``RunMetrics`` fields and merges the 1/0 pass/fail scores into the run's
    ``*.scores.json`` file (alongside judge/contains/regex scores). Recomputed on
    every ``analyze`` since it's cheap and telemetry-driven.

    Returns a list of human-readable labels for every metric gate that did **not**
    pass — including gates whose value was unavailable (``score is None``), which
    count as failures — so ``analyze`` can exit non-zero for CI gating.
    """
    from eval.evaluators import MetricEvaluator
    from eval.runner import EvalScore

    tasks_by_name = {t.name: t for t in config.tasks}
    seen: set[Path] = set()
    failed_gates: list[str] = []

    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        fixture = trace.resource_tags.get("eval.fixture", "")
        task = tasks_by_name.get(scenario)
        if not task:
            continue

        metric_evaluators = [ev for ev in task.evaluators if ev.type == "metric"]
        if not metric_evaluators:
            continue

        scores_file = results_dir / f"{run_slug(scenario, variant, epoch, fixture)}.scores.json"
        if scores_file in seen:
            continue  # one trace per scores file is enough
        seen.add(scores_file)

        fx = f"/{fixture}" if fixture else ""

        run_metrics = extract_metrics(trace)
        if run_metrics is None:
            # Fail CLOSED: a metric-gated task whose trace can't yield metrics must
            # not silently pass. Emit an unavailable (score=None, passed=False)
            # score for each metric evaluator so the gate below counts as failed.
            new_scores = [
                EvalScore(
                    name=ev.name,
                    type="metric",
                    score=None,
                    reason="metrics unavailable in trace",
                    passed=False,
                )
                for ev in metric_evaluators
            ]
        else:
            new_scores = []
            for ev in metric_evaluators:
                score = MetricEvaluator.from_config(ev).evaluate(
                    EvalContext(evaluator=ev, config=config, metrics=run_metrics)
                )
                # MetricEvaluator only returns None when metrics is None, which
                # is excluded by the branch above (run_metrics is not None here).
                assert score is not None
                new_scores.append(score)

        _merge_scores_file(scores_file, new_scores, replace_type="metric")
        for s in new_scores:
            status = "PASS" if s.passed else ("n/a" if s.score is None else "FAIL")
            click.echo(
                f"    [{scenario}{fx}/{variant}/e{epoch}] {s.name} (metric): {status} — {s.reason}",
                err=True,
            )
            # A gate fails when it does not pass — this deliberately includes the
            # unavailable-value case (score is None), which must NOT silently pass.
            if not s.passed:
                reason = s.reason.split(" → ", 1)[0]  # drop the trailing "→ FAIL"
                failed_gates.append(f"{s.name} [{scenario}{fx}/{variant}/e{epoch}]: {reason}")

    return failed_gates


def _merge_scores_file(scores_file: Path, new_scores: list[Any], replace_type: str) -> None:
    """Merge freshly computed scores into a run's scores file.

    Keeps existing scores except those of ``replace_type`` whose name is being
    recomputed, so re-running ``analyze`` refreshes metric scores idempotently
    without clobbering judge/contains/regex scores.
    """
    from eval.runner import score_to_dict

    existing: list[dict[str, Any]] = []
    if scores_file.exists():
        try:
            existing = json.loads(scores_file.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []

    new_dicts = [score_to_dict(s) for s in new_scores]
    new_names = {d["name"] for d in new_dicts}
    kept = [
        s for s in existing if not (s.get("type") == replace_type and s.get("name") in new_names)
    ]
    all_scores = kept + new_dicts
    scores_file.parent.mkdir(parents=True, exist_ok=True)
    scores_file.write_text(json.dumps(all_scores, indent=2, ensure_ascii=False))


@main.command()
@click.option("--variant", "-v", default=None, help="Build specific variant (default: all)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def build(variant: str | None, config_dir: str | None) -> None:
    """Build Docker images for all (or specific) variants."""
    config = load_config(Path(config_dir) if config_dir else None)
    variants = [config.get_variant(variant)] if variant else config.variants
    variants = [v for v in variants if v is not None]

    if not variants:
        raise click.ClickException(f"Variant '{variant}' not found.")

    github_token = get_github_token()
    _build_images(config, variants, github_token)


def _build_images(config: Config, variants: list[Variant], token: str) -> None:
    """Build base + variant Docker images."""
    base_dockerfile = config.project_dir / "docker" / "Dockerfile"
    base_image = f"{config.runner.container_image_base}:base"
    env = {**os.environ, "DOCKER_BUILDKIT": "1", "GITHUB_TOKEN": token}

    # Step 1: Build base image
    click.echo(f"Building {base_image}...")
    cmd = [
        "docker",
        "build",
        "-f",
        str(base_dockerfile),
        "--build-arg",
        f"COPILOT_VERSION={config.runner.copilot_version}",
        "-t",
        base_image,
        str(config.project_dir),
    ]
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        raise click.ClickException("Base image build failed")
    click.echo(f"✓ {base_image}")

    # Step 2: Build variant images
    for v in variants:
        image = config.image_name(v)
        click.echo(f"Building {image}...")

        if v.dockerfile:
            df = (config.project_dir / v.dockerfile).resolve()
        else:
            # No Dockerfile — variant is just the base image
            cmd = ["docker", "tag", base_image, image]
            result = subprocess.run(cmd)
            if result.returncode != 0:
                raise click.ClickException(f"Tag failed for {image}")
            click.echo(f"✓ {image} (tagged from base)")
            continue

        cmd = [
            "docker",
            "build",
            "-f",
            str(df),
            "--secret",
            "id=github_token,env=GITHUB_TOKEN",
            "-t",
            image,
            str(config.project_dir),
        ]
        result = subprocess.run(cmd, env=env)
        if result.returncode != 0:
            raise click.ClickException(f"Build failed for {image}")
        click.echo(f"✓ {image}")

    click.echo(f"\nBuilt {len(variants)} variant image(s).")


def _ensure_images(config: Config, token: str) -> None:
    """Check if Docker images exist for all variants, build if missing."""
    missing = []
    base_image = f"{config.runner.container_image_base}:base"

    # Check base image
    result = subprocess.run(["docker", "image", "inspect", base_image], capture_output=True)
    if result.returncode != 0:
        missing.append("base")

    # Check variant images
    for v in config.variants:
        image = config.image_name(v)
        result = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if result.returncode != 0:
            missing.append(v.name)

    if not missing:
        return

    click.echo(f"Missing images: {', '.join(missing)}. Building...", err=True)
    _build_images(config, config.variants, token)


@main.command(name="list")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def list_tasks(config_dir: str | None) -> None:
    """List available tasks and variants."""
    config = load_config(Path(config_dir) if config_dir else None)

    click.echo("Tasks:")
    click.echo(f"  {'Name':<25} {'Enabled':<8} {'Evals':>5} Prompt")
    click.echo("  " + "-" * 75)
    for p in config.tasks:
        prompt_preview = p.prompt[:40] + "..." if len(p.prompt) > 40 else p.prompt
        click.echo(
            f"  {p.name:<25} {'✓' if p.enabled else '−':<8} {len(p.evaluators):>5} {prompt_preview}"
        )

    click.echo("\nVariants:")
    click.echo(f"  {'Name':<25} {'Build':<8} {'Run':<8} Description")
    click.echo("  " + "-" * 75)
    for v in config.variants:
        has_build = "✓" if v.dockerfile else "−"
        has_run = "✓" if v.run_script else "−"
        click.echo(f"  {v.name:<25} {has_build:<8} {has_run:<8} {v.description[:40]}")


@main.command()
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
def validate(config_dir: str | None) -> None:
    """Validate an eval-config.yaml: schema, fixtures, script/variant references, and vars.

    Checks (independent of Docker/auth, unlike `run`'s pre-flight checks):
    - YAML syntax and schema validity
    - Referenced fixture directories exist on disk (warning: a missing
      fixture dir doesn't fail a run, since eval.runner tolerates it)
    - Variant/task script references (Dockerfile, run script, hooks,
      health_check, script evaluators) exist on disk
    - {var} prompt/output_instruction placeholders resolve for every variant
      (warning: unresolved placeholders are left as literal text at run time)

    Exits 0 if all *blocking* checks pass (warnings don't affect the exit
    code), 1 otherwise.
    """
    config, schema_result = check_config_schema(Path(config_dir) if config_dir else None)
    results: list[CheckResult] = [schema_result]
    if config is not None:
        results += check_fixtures(config)
        results += check_script_references(config)
        results += check_var_interpolation(config)

    _print_check_results(results, "Validation")
    if any_failed(results):
        raise click.exceptions.Exit(1)
    if any(not r.passed for r in results):
        click.echo("All blocking checks passed (see warnings above).", err=True)
    else:
        click.echo("All checks passed.", err=True)
