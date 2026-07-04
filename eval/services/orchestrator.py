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

import click
import requests

from eval.config import Config, Task, Variant
from eval.naming import run_slug
from eval.protocols import RunStatus
from eval.runner import RunResult, get_github_token, run_one
from eval.services.build_service import _ensure_images
from eval.services.check_report import print_check_results
from eval.services.manifest import write_manifest
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
) -> list[RunResult]:
    """Run every (task, fixture, epoch, variant) combination per the configured
    parallelism strategy (``full``, ``per_task``, or serial) and variant order."""
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
) -> None:
    """Business logic for the `run` CLI command: select tasks, print the plan,
    pre-flight, build/ensure images, schedule runs, and persist the manifest."""
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

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = config.results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    _print_plan(config, tasks, resolved_epochs, run_id)

    if dry_run:
        total = sum(len(p.fixture_names()) for p in tasks) * resolved_epochs * len(config.variants)
        click.echo(
            f"[dry-run] Would run {resolved_epochs} epoch(s) × {len(config.variants)} variants × "
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

    results = _execute_schedule(config, tasks, resolved_epochs, run_id, run_dir, github_token)

    schedule = {
        "parallel": config.runner.parallel,
        "max_workers": config.runner.max_workers,
        "variant_order": config.runner.variant_order,
        "seed": config.runner.seed,
    }
    write_manifest(run_dir, run_id, results, schedule)

    _print_summary(config, run_id, results, config_dir)
