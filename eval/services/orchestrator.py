"""Run scheduling service: variant ordering, parallel execution strategies, and
the `run` command's business logic.

Split out of the old monolithic ``eval/cli.py`` (issue #83). ``run_command``
is what ``eval.cli.run_cmd`` delegates to; the rest are scheduling helpers
reused by both `run` and the manifest reconciliation done during `analyze`.
"""

from __future__ import annotations

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

from eval.config import Config, Task, Variant
from eval.exceptions import EvalError
from eval.naming import run_slug
from eval.progress import NullProgress, ProgressReporter, create_reporter
from eval.protocols import RunStatus
from eval.runner import RunResult, get_github_token, run_one
from eval.services.build_service import _ensure_images
from eval.services.check_report import print_check_results
from eval.services.cost_service import estimate_run_cost, format_cost_report
from eval.services.manifest import write_manifest, write_manifest_dicts
from eval.services.resume_service import (
    CellKey,
    cell_key,
    completed_cells,
    filter_schedule,
    merge_manifest_runs,
    scan_run_results,
    warn_if_schedule_changed,
)
from eval.validation import any_failed, validate_readiness

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

    `run_one` already converts its own typed errors (see `eval.exceptions`)
    into a setup_failed RunResult internally, so nothing from that expected
    domain should reach here. This boundary is a last-resort safety net for
    anything that still escapes (e.g. a genuine bug, or a future call path
    that doesn't go through run_one's own handling) -- it isolates that
    failure to this one run instead of aborting the whole batch or preventing
    the manifest from being written.
    """
    fixture_dir_name = fixture if fixture is not None else task.fixture_names()[0]
    fixture_label = task.fixture_label(fixture_dir_name)

    def _errored_result(exc: Exception, description: str) -> RunResult:
        suffix = f" fixture={fixture_label}" if fixture_label else ""
        logger.error(
            "[%s] epoch=%s variant=%s%s %s: %s",
            task.name,
            epoch,
            variant.name,
            suffix,
            description,
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
    except EvalError as exc:
        return _errored_result(exc, "eval error")
    except Exception as exc:  # noqa: BLE001 - last-resort isolation for unexpected bugs
        return _errored_result(exc, "errored")


def _cell_name(task: Task, variant: Variant, epoch: int, fixture: str = "") -> str:
    """Human-readable id for one (task, variant, epoch, fixture) cell, used in
    progress output. Fixture is only included for multi-fixture tasks."""
    if task.is_multi_fixture and fixture:
        return f"{task.name}/{fixture}/{variant.name}/e{epoch}"
    return f"{task.name}/{variant.name}/e{epoch}"


def _report_cell_result(
    reporter: ProgressReporter, config: Config, name: str, result: RunResult
) -> None:
    """Forward one cell's outcome to the progress reporter as a completion or
    a failure, deriving a short human-readable reason for failures."""
    if result.status == RunStatus.SUCCESS:
        reporter.cell_completed(name, duration=result.duration_seconds, status=str(result.status))
        return
    if result.status == RunStatus.TIMEOUT:
        reason = f"timeout after {config.runner.timeout_seconds}s"
    elif result.status == RunStatus.SETUP_FAILED:
        reason = "setup failed"
    else:
        reason = f"exit code {result.exit_code}"
    reporter.cell_failed(name, duration=result.duration_seconds, reason=reason)


def _print_plan(config: Config, tasks: list[Task], epochs: int, run_id: str) -> None:
    """Print the run banner (model/effort/epochs/variants/tasks) before executing."""
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


def _execute_schedule(
    config: Config,
    tasks: list[Task],
    epochs: int,
    run_id: str,
    run_dir: Path,
    github_token: str,
    reporter: ProgressReporter | None = None,
    skip_cells: set[CellKey] | None = None,
) -> list[RunResult]:
    """Run every (task, fixture, epoch, variant) combination per the configured
    parallelism strategy (``full``, ``per_task``, or serial) and variant order.

    ``reporter`` (default: a silent :class:`NullProgress`) is fed a
    start/cell_started/cell_completed|cell_failed/finish stream so callers get
    live progress without any of this scheduling logic depending on how (or
    whether) it's rendered.

    ``skip_cells`` (issue #67, ``run --resume``) is a set of
    ``(task, variant, epoch, fixture)`` keys that already succeeded in a prior
    run and should not be re-executed; only cells outside this set are
    submitted/iterated, so the returned ``results`` cover exactly what this
    call ran (callers merge that back into the prior manifest themselves --
    see ``eval.services.resume_service.merge_manifest_runs``).
    """
    reporter = reporter or NullProgress()
    results: list[RunResult] = []
    order = config.runner.variant_order
    seed = config.runner.seed
    skip_cells = skip_cells or set()
    total = sum(
        1
        for t in tasks
        for f in t.fixture_names()
        for e in range(1, epochs + 1)
        for v in config.variants
        if cell_key(t.name, v.name, e, t.fixture_label(f)) not in skip_cells
    )
    if skip_cells:
        click.echo(f"Resume: skipping {len(skip_cells)} previously completed cell(s).", err=True)

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
        work = filter_schedule(work, skip_cells)
        click.echo(
            f"Running {len(work)} runs in full parallel (max_workers={config.runner.max_workers})"
        )

        def _run_and_report(t: Task, v: Variant, e: int, f: str, i: int, name: str) -> RunResult:
            # cell_started is reported from inside the worker thread so it
            # reflects the true concurrent start time, not submission order.
            reporter.cell_started(name)
            result = _safe_run_one(t, v, e, config, run_id, run_dir, github_token, i, f)
            _report_cell_result(reporter, config, name, result)
            return result

        reporter.start(len(work), label="eval matrix", workers=config.runner.max_workers)
        try:
            with ThreadPoolExecutor(max_workers=config.runner.max_workers) as pool:
                futures = [
                    pool.submit(_run_and_report, t, v, e, f, i, _cell_name(t, v, e, f))
                    for i, (t, v, e, f) in enumerate(work)
                ]
                for future in as_completed(futures):
                    results.append(future.result())
        finally:
            reporter.finish()

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
                        skip_key = cell_key(
                            task.name, variant.name, epoch, task.fixture_label(fixture)
                        )
                        if skip_key in skip_cells:
                            order_index += 1
                            continue
                        name = _cell_name(task, variant, epoch, fixture)
                        reporter.cell_started(name)
                        result = _safe_run_one(
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
                        _report_cell_result(reporter, config, name, result)
                        task_results.append(result)
                        order_index += 1
            return task_results

        click.echo(f"Running {len(tasks)} tasks in parallel (variants serial within each task)")
        workers = min(len(tasks), config.runner.max_workers)
        reporter.start(total, label="eval matrix", workers=workers)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                task_futures = {pool.submit(_run_task_serial, t): t.name for t in tasks}
                for task_future in as_completed(task_futures):
                    results.extend(task_future.result())
        finally:
            reporter.finish()
    else:
        reporter.start(total, label="eval matrix", workers=1)
        try:
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
                            skip_key = cell_key(
                                p.name, variant.name, epoch, p.fixture_label(fixture)
                            )
                            if skip_key in skip_cells:
                                order_index += 1
                                continue
                            name = _cell_name(p, variant, epoch, fixture)
                            reporter.cell_started(name)
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
                            _report_cell_result(reporter, config, name, result)
                            results.append(result)
                            order_index += 1
        finally:
            reporter.finish()

    return results


def _print_summary(
    config: Config, run_id: str, results: list[RunResult], config_dir: str | None
) -> None:
    """Print the pass/fail/timeout/errored summary and the follow-up `analyze` command."""
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    timed_out = sum(1 for r in results if r.status == RunStatus.TIMEOUT)
    errored = sum(1 for r in results if r.status in (RunStatus.FAILED, RunStatus.SETUP_FAILED))

    click.echo("=" * 50)
    click.echo(f" Run complete: {run_id}")
    click.echo(f" Results: {passed} passed, {failed} failed")
    if timed_out or errored:
        click.echo(f"   (of which {timed_out} timed out, {errored} errored)")
    judge_tokens_in = sum(
        (s.meta.get("judge_tokens_in") or 0) for r in results for s in r.scores if s.type == "judge"
    )
    judge_tokens_out = sum(
        (s.meta.get("judge_tokens_out") or 0)
        for r in results
        for s in r.scores
        if s.type == "judge"
    )
    if judge_tokens_in or judge_tokens_out:
        click.echo(f" Judge tokens: {judge_tokens_in:,} in / {judge_tokens_out:,} out")
    if config.runner.collector == "jaeger":
        click.echo(f" Jaeger:  {config.runner.jaeger_url}")
    else:
        click.echo(f" Collector: {config.runner.collector}")
    analyze_cmd = f"uv run copilot-eval analyze --run-id {run_id}"
    if config_dir:
        analyze_cmd += f" --config-dir {config_dir}"
    click.echo(f" Analyze: {analyze_cmd}")
    click.echo("=" * 50)


def run_command(
    config: Config,
    *,
    task: str | None,
    epochs: int | None,
    dry_run: bool,
    no_build: bool,
    skip_preflight: bool,
    config_dir: str | None,
    no_progress: bool = False,
    resume: bool = False,
    run_id: str | None = None,
    estimate: bool = False,
    yes: bool = False,
    budget_limit: float | None = None,
) -> None:
    """Business logic for the `run` CLI command: select tasks, print the plan,
    pre-flight, build/ensure images, schedule runs, and persist the manifest.

    When ``resume`` is set (issue #67), ``run_id`` must name an existing run
    directory: its manifest is scanned for cells that already succeeded, only
    the remaining (failed/missing) cells are executed, and the new results are
    merged back into that same run directory instead of starting a fresh
    run-id.

    Cost governance (issue #70): a pre-flight :class:`CostEstimate` is always
    computed. ``budget_limit`` (falling back to ``config.runner.budget_limit``)
    aborts the run before any Docker/agent work if the estimate exceeds it.
    ``estimate=True`` additionally prints the full cost breakdown and, unless
    ``yes`` is set, asks for interactive confirmation before proceeding.
    """
    resolved_epochs = epochs or config.runner.epochs

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

    effective_budget_limit = (
        budget_limit if budget_limit is not None else config.runner.budget_limit
    )
    cost_estimate = estimate_run_cost(config, tasks, config.variants, resolved_epochs)
    if estimate:
        click.echo(format_cost_report(cost_estimate, effective_budget_limit))
    if cost_estimate.over_budget(effective_budget_limit):
        if not estimate:
            click.echo(format_cost_report(cost_estimate, effective_budget_limit), err=True)
        raise click.ClickException(
            f"Estimated cost ${cost_estimate.cost_total:.4f} exceeds budget limit "
            f"${effective_budget_limit:.4f}. Adjust runner.budget_limit / --budget-limit, "
            "or reduce the run's scope (tasks/epochs/variants)."
        )
    if (
        estimate
        and not dry_run
        and not yes
        and not click.confirm("Proceed with this run?", default=True)
    ):
        click.echo("Aborted (cost estimate not confirmed).")
        return

    existing_index: dict[CellKey, dict[str, Any]] = {}
    skip_cells: set[CellKey] = set()

    if resume:
        if not run_id:
            raise click.ClickException("--resume requires --run-id <existing run id>")
        run_dir = config.results_dir / run_id
        if not run_dir.exists():
            raise click.ClickException(
                f"Run '{run_id}' not found under {config.results_dir}. "
                "Pass an existing --run-id to resume."
            )
        existing_index = scan_run_results(run_dir)
        skip_cells = completed_cells(existing_index)
        warn_if_schedule_changed(run_dir, config)
    else:
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        run_dir = config.results_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

    _print_plan(config, tasks, resolved_epochs, run_id)
    if resume:
        remaining = len(existing_index) - len(skip_cells)
        click.echo(
            f" Resume:   {len(skip_cells)} cell(s) already completed, "
            f"{remaining} to retry (plus any missing cells)"
        )

    if dry_run:
        full_total = (
            sum(len(p.fixture_names()) for p in tasks) * resolved_epochs * len(config.variants)
        )
        if resume:
            remaining = sum(
                1
                for p in tasks
                for f in p.fixture_names()
                for e in range(1, resolved_epochs + 1)
                for v in config.variants
                if cell_key(p.name, v.name, e, p.fixture_label(f)) not in skip_cells
            )
            click.echo(
                f"[dry-run] Would run {remaining} cell(s) out of {full_total} in the matrix "
                f"(skipping {len(skip_cells)} already completed)."
            )
        else:
            click.echo(
                f"[dry-run] Would run {resolved_epochs} epoch(s) × {len(config.variants)} variants × "
                f"fixtures for each task ({full_total} runs total)."
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
            print_check_results(preflight, "Pre-flight checks")
        if any_failed(preflight):
            raise click.ClickException(
                "Pre-flight validation failed. Fix the issues above and re-run."
            )

    if config.runner.collector == "jaeger":
        _ensure_jaeger(config)
    github_token = get_github_token()
    if not no_build:
        _ensure_images(config, github_token)

    reporter = create_reporter(no_progress=no_progress)
    results = _execute_schedule(
        config, tasks, resolved_epochs, run_id, run_dir, github_token, reporter, skip_cells
    )

    schedule = {
        "parallel": config.runner.parallel,
        "max_workers": config.runner.max_workers,
        "variant_order": config.runner.variant_order,
        "seed": config.runner.seed,
    }
    if resume:
        merged_runs = merge_manifest_runs(existing_index, results)
        write_manifest_dicts(run_dir, run_id, merged_runs, schedule, cost_estimate.to_dict())
        passed = sum(1 for r in merged_runs if r.get("passed"))
        click.echo(
            f" Resume merged: {passed}/{len(merged_runs)} cell(s) passing overall "
            f"({len(results)} re-executed this run)"
        )
    else:
        write_manifest(run_dir, run_id, results, schedule, cost_estimate.to_dict())

    _print_summary(config, run_id, results, config_dir)
