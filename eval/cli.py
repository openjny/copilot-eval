"""CLI entry point for the eval framework."""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import requests

from eval.config import Config, Task, Variant, load_config
from eval.report import build_report, format_json, format_markdown, format_table
from eval.runner import RunResult, get_github_token, run_one
from eval.trace import (
    RunMetrics,
    Trace,
    extract_conversation,
    extract_metrics,
    fetch_traces,
    filter_by_run,
)


def _ensure_jaeger(config: Config) -> None:
    """Check if Jaeger is reachable, start it via docker compose if not."""
    jaeger_url = config.runner.jaeger_url
    try:
        requests.get(f"{jaeger_url}/api/services", timeout=3)
        return  # already running
    except (requests.ConnectionError, requests.Timeout):
        pass
    click.echo("Jaeger not running. Starting via docker compose...", err=True)
    compose_file = config.project_dir / "docker-compose.yml"
    if not compose_file.exists():
        raise click.ClickException("Jaeger not running and docker-compose.yml not found. Start Jaeger manually.")
    subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                   check=True, capture_output=True)
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


def _write_manifest(run_dir: Path, run_id: str, results: list[RunResult]) -> None:
    """Persist the full set of runs so `analyze` can detect missing/failed ones."""
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
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


@click.group()
def main() -> None:
    """Copilot CLI A/B evaluation framework."""


@main.command()
@click.option("--task", "-p", default=None, help="Run a specific task (overrides enabled flag)")
@click.option("--epochs", "-n", default=None, type=int, help="Number of epochs (default: from config, typically 1)")
@click.option("--dry-run", is_flag=True, help="Show plan without executing")
@click.option("--no-build", is_flag=True, help="Skip auto-build of Docker images")
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
def run(task: str | None, epochs: int | None, dry_run: bool, no_build: bool, config_dir: str | None) -> None:
    """Run A/B eval for one or more tasks."""
    config = load_config(Path(config_dir) if config_dir else None)
    epochs = epochs or config.runner.epochs

    # Select tasks
    if task:
        p = config.get_task(task)
        if not p:
            raise click.ClickException(f"Task '{task}' not found. Use 'list' to see available tasks.")
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
        click.echo(f"[dry-run] Would run {epochs} epoch(s) × {len(config.variants)} variants for each task.")
        return

    _ensure_jaeger(config)
    github_token = get_github_token()
    if not no_build:
        _ensure_images(config, github_token)
    results: list[RunResult] = []

    if config.runner.parallel == "full":
        from concurrent.futures import ThreadPoolExecutor, as_completed

        work = [(t, v, e) for t in tasks for e in range(1, epochs + 1) for v in config.variants]
        click.echo(f"Running {len(work)} runs in full parallel (max_workers={config.runner.max_workers})")
        with ThreadPoolExecutor(max_workers=config.runner.max_workers) as pool:
            futures = {pool.submit(run_one, t, v, e, config, run_id, run_dir, github_token): f"{t.name}/{v.name}/e{e}" for t, v, e in work}
            for future in as_completed(futures):
                results.append(future.result())

    elif config.runner.parallel == "per_task" and len(tasks) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _run_task_serial(task: Task) -> list[RunResult]:
            """Run all epochs × variants for a single task sequentially."""
            task_results: list[RunResult] = []
            for epoch in range(1, epochs + 1):
                for variant in config.variants:
                    task_results.append(
                        run_one(task, variant, epoch, config, run_id, run_dir, github_token)
                    )
            return task_results

        click.echo(f"Running {len(tasks)} tasks in parallel (variants serial within each task)")
        with ThreadPoolExecutor(max_workers=min(len(tasks), config.runner.max_workers)) as pool:
            task_futures = {pool.submit(_run_task_serial, t): t.name for t in tasks}
            for task_future in as_completed(task_futures):
                results.extend(task_future.result())
    else:
        for p in tasks:
            prompt = config.resolve_prompt(p, config.variants[0])
            click.echo(f"\n>>> Task: {p.name}")
            click.echo(f">>> Prompt:  {prompt}\n")

            for epoch in range(1, epochs + 1):
                for variant in config.variants:
                    result = run_one(p, variant, epoch, config, run_id, run_dir, github_token)
                    results.append(result)

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    timed_out = sum(1 for r in results if r.status == "timeout")
    errored = sum(1 for r in results if r.status in ("failed", "setup_failed"))

    # Persist a run manifest so `analyze` knows the full expected set of runs
    # (including failed/timeout runs that may have produced no trace).
    _write_manifest(run_dir, run_id, results)

    click.echo("=" * 50)
    click.echo(f" Run complete: {run_id}")
    click.echo(f" Results: {passed} passed, {failed} failed")
    if timed_out or errored:
        click.echo(f"   (of which {timed_out} timed out, {errored} errored)")
    click.echo(f" Jaeger:  {config.runner.jaeger_url}")
    analyze_cmd = f"uv run copilot-eval analyze --run-id {run_id}"
    if config_dir:
        analyze_cmd += f" --config-dir {config_dir}"
    click.echo(f" Analyze: {analyze_cmd}")
    click.echo("=" * 50)


@main.command()
@click.option("--run-id", required=True, help="Run ID to analyze")
@click.option("--output", "-o", type=click.Choice(["table", "json", "markdown"]), default="table", help="Output format")
@click.option("--aggregate", "-a", type=click.Choice(["paired", "median", "mean"]), default="paired", help="Aggregation method")
@click.option("--jaeger-url", default=None, help="Jaeger URL (default: runner.jaeger_url from config)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
@click.option("--skip-eval", is_flag=True, help="Skip judge evaluation, use existing scores")
@click.option("--re-eval", is_flag=True, help="Force re-run judge evaluation (ignore cached scores)")
def analyze(run_id: str, output: str, aggregate: str, jaeger_url: str | None,
            config_dir: str | None, skip_eval: bool, re_eval: bool) -> None:
    """Analyze traces from a previous eval run."""
    config = load_config(Path(config_dir) if config_dir else None)
    jaeger = jaeger_url or config.runner.jaeger_url
    results_dir = config.results_dir / run_id

    manifest_runs = _load_manifest(results_dir)

    click.echo(f"Fetching traces from {jaeger} for run {run_id}...", err=True)
    traces = _fetch_traces_for_run(config, jaeger, run_id, manifest_runs)

    metrics: list[RunMetrics] = [m for m in (extract_metrics(t) for t in traces) if m is not None]

    # Reconcile against the persisted manifest so failed/timeout/missing runs
    # are surfaced instead of silently dropped (survivorship bias).
    if manifest_runs is not None:
        _report_run_coverage(manifest_runs, traces)
    elif not metrics:
        click.echo("No traces found for this run ID, and no manifest to reconcile against.", err=True)
        return

    if not metrics:
        click.echo("No traces found for this run ID.", err=True)
        return

    # Run judge evaluators if not skipped
    if not skip_eval and results_dir.exists():
        _run_judges(config, traces, results_dir, force=re_eval)
        _warn_unscored_judges(config, traces, results_dir)
    if results_dir.exists():
        _report_judge_reliability(results_dir)

    variant_order = [v.name for v in config.variants]
    reports = build_report(metrics, results_dir if results_dir.exists() else None, variant_order, aggregate)
    if not reports:
        click.echo("No reports generated.", err=True)
        return

    formatters = {"table": format_table, "json": format_json, "markdown": format_markdown}
    click.echo(formatters[output](reports))


def _fetch_traces_for_run(config: Config, jaeger: str, run_id: str,
                          manifest_runs: list[dict[str, Any]] | None) -> list[Trace]:
    """Fetch traces for a run, retrying while ingestion catches up.

    Uses a server-side tag filter on eval.run_id and a high limit so large runs
    aren't truncated. If a manifest is available, retries until the number of
    fetched traces reaches the number of runs that should have produced one.
    """
    expected = None
    if manifest_runs is not None:
        # Only completed runs are guaranteed to emit a trace; timeout/failed
        # runs may not, so don't let them keep the retry loop waiting forever.
        expected = sum(1 for r in manifest_runs if r.get("status") == "completed")

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


def _report_run_coverage(manifest_runs: list[dict[str, Any]], traces: list[Trace]) -> None:
    """Reconcile persisted runs against ingested traces and warn about gaps."""
    trace_test_ids = {t.resource_tags.get("eval.test_id") for t in traces}

    missing: list[str] = []
    failed: list[str] = []
    for r in manifest_runs:
        label = f"{r.get('task')}/{r.get('variant')}/e{r.get('epoch')}"
        status = r.get("status", "completed")
        has_trace = r.get("test_id") in trace_test_ids
        if status == "timeout":
            failed.append(f"{label} (timeout)")
        elif status == "failed":
            failed.append(f"{label} (exit {r.get('exit_code')})")
        elif status == "setup_failed":
            failed.append(f"{label} (setup_failed)")
        elif not has_trace:
            # Run reported as completed but no trace ingested → silently dropped.
            missing.append(label)

    total = len(manifest_runs)
    ok = total - len(missing) - len(failed)
    click.echo(f"Run coverage: {ok}/{total} ok, {len(failed)} failed/timeout, {len(missing)} missing trace.", err=True)
    if failed:
        click.echo(f"  Failed/timeout runs (excluded from metrics): {', '.join(failed)}", err=True)
    if missing:
        click.echo(f"  WARNING: completed runs with no ingested trace: {', '.join(missing)}", err=True)


def _warn_unscored_judges(config: Config, traces: list[Trace], results_dir: Path) -> None:
    """Surface judge evaluations that produced no usable score (timeout/parse_error)."""
    tasks_by_name = {t.name: t for t in config.tasks}
    problems: list[str] = []
    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        task = tasks_by_name.get(scenario)
        if not task or not any(ev.type == "judge" for ev in task.evaluators):
            continue
        scores_file = results_dir / f"{scenario}_{variant}_epoch{epoch}.scores.json"
        if not scores_file.exists():
            continue
        try:
            scores = json.loads(scores_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for s in scores:
            if s.get("type") == "judge" and s.get("score") is None:
                reason = s.get("reason", "no score")
                problems.append(f"{scenario}/{variant}/e{epoch}:{s.get('name')} ({reason})")
    if problems:
        click.echo(f"  WARNING: {len(problems)} judge score(s) unavailable: {', '.join(problems)}", err=True)


def _report_judge_reliability(results_dir: Path) -> None:
    """Summarize judge self-consistency + parse/error/timeout rates for a run.

    Reads every persisted ``*.scores.json`` in the run directory, aggregates
    judge sample outcomes (ok/parse_error/timeout/error) and per-evaluator score
    spread (stddev), and prints a compact reliability summary so noisy or
    failure-prone judges are visible alongside the metrics.
    """
    outcomes: dict[str, int] = {"ok": 0, "parse_error": 0, "timeout": 0, "error": 0}
    judge_evals = 0          # number of judge score records
    sampled_evals = 0        # records that ran >1 sample
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


def _run_judges(config: Config, traces: list[Trace], results_dir: Path, force: bool = False) -> None:
    """Run judge evaluators using OTel traces + output files.

    Skips judge evaluators that already have a recorded score (judge presence,
    not file existence — non-judge scores share the same file). Pass force=True
    to re-run every judge regardless of cached scores.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from eval.runner import read_files_from_dir, run_judge, score_to_dict

    github_token = get_github_token()
    tasks_by_name = {t.name: t for t in config.tasks}

    # Phase 1: build per-trace context and collect the judge work items. Each
    # trace owns a dedicated scores file; contexts are keyed by that file so
    # duplicate traces for the same scenario/variant/epoch coalesce into one
    # writer and don't clobber each other.
    contexts: dict[str, dict[str, Any]] = {}
    work: list[tuple[str, Any]] = []  # (context key, evaluator)

    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        task = tasks_by_name.get(scenario)
        if not task:
            continue

        judge_evaluators = [ev for ev in task.evaluators if ev.type == "judge" and ev.prompt]
        if not judge_evaluators:
            continue

        scores_file = results_dir / f"{scenario}_{variant}_epoch{epoch}.scores.json"
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
        pending = judge_evaluators if force else [
            ev for ev in judge_evaluators if ev.name not in existing_judge_names
        ]
        if not pending:
            continue  # all judge scores already present

        # Extract conversation from OTel trace
        conversation = extract_conversation(trace)

        # Fall back to log file if OTel content not available
        if not conversation:
            log_file = results_dir / f"{scenario}_{variant}_epoch{epoch}.log"
            if log_file.exists():
                text = log_file.read_text()
                conversation = text[:8000] + "\n... (truncated)" if len(text) > 8000 else text

        if not conversation:
            continue

        # Read output files from persisted outputs
        output_dir = results_dir / "outputs" / f"{scenario}_{variant}_epoch{epoch}"
        output_files_text = read_files_from_dir(output_dir, max_chars=8000)

        contexts[key] = {
            "scenario": scenario,
            "variant": variant,
            "epoch": epoch,
            "scores_file": scores_file,
            "existing_scores": existing_scores,
            "conversation": conversation,
            "output_files_text": output_files_text,
            "pending_names": {ev.name for ev in pending},
            "order": {ev.name: i for i, ev in enumerate(pending)},
            "remaining": len(pending),
            "scores": [],
        }
        for ev in pending:
            work.append((key, ev))

    if not work:
        return

    def _write_ctx(ctx: dict[str, Any]) -> None:
        """Merge a trace's collected judge scores with kept scores and persist."""
        rerun_names = ctx["pending_names"]
        kept = [
            s for s in ctx["existing_scores"]
            if s.get("type") != "judge" or s.get("name") not in rerun_names
        ]
        scores = sorted(ctx["scores"], key=lambda s: ctx["order"].get(s.get("name"), 0))
        all_scores = kept + scores
        if all_scores:
            ctx["scores_file"].write_text(json.dumps(all_scores, indent=2, ensure_ascii=False))

    # Phase 2: run judge evaluators in parallel (each invokes Copilot). Results
    # are collected per trace context on the main thread; a context's scores
    # file is written as soon as all of its judges complete, so an interrupt or
    # crash only loses traces still in flight.
    def _judge(key: str, ev: Any) -> tuple[str, dict[str, Any]]:
        ctx = contexts[key]
        click.echo(
            f"    [{ctx['scenario']}/{ctx['variant']}/e{ctx['epoch']}] Evaluating: {ev.name} (judge)...",
            err=True,
        )
        s = run_judge(ev, ctx["conversation"], config, github_token, ctx["output_files_text"])
        if s.score is not None:
            click.echo(f"    ✓ {ev.name}: {s.score} — {s.reason[:60]}", err=True)
        else:
            click.echo(f"    ! {ev.name}: {s.reason}", err=True)
        return key, score_to_dict(s)

    with ThreadPoolExecutor(max_workers=config.runner.max_workers) as pool:
        futures = {pool.submit(_judge, key, ev): (key, ev) for key, ev in work}
        for future in as_completed(futures):
            key, ev = futures[future]
            try:
                key, score = future.result()
            except Exception as exc:  # never let one judge abort the whole batch
                click.echo(f"    ! {ev.name}: error — {exc}", err=True)
                score = {"name": ev.name, "type": "judge", "score": None,
                         "reason": f"error: {exc}", "passed": False}
            ctx = contexts[key]
            ctx["scores"].append(score)
            ctx["remaining"] -= 1
            if ctx["remaining"] == 0:
                _write_ctx(ctx)


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
        "docker", "build",
        "-f", str(base_dockerfile),
        "--build-arg", f"COPILOT_VERSION={config.runner.copilot_version}",
        "-t", base_image,
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
            "docker", "build",
            "-f", str(df),
            "--secret", "id=github_token,env=GITHUB_TOKEN",
            "-t", image,
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
    result = subprocess.run(["docker", "image", "inspect", base_image],
                           capture_output=True)
    if result.returncode != 0:
        missing.append("base")

    # Check variant images
    for v in config.variants:
        image = config.image_name(v)
        result = subprocess.run(["docker", "image", "inspect", image],
                               capture_output=True)
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
        click.echo(f"  {p.name:<25} {'✓' if p.enabled else '−':<8} {len(p.evaluators):>5} {prompt_preview}")

    click.echo("\nVariants:")
    click.echo(f"  {'Name':<25} {'Build':<8} {'Run':<8} Description")
    click.echo("  " + "-" * 75)
    for v in config.variants:
        has_build = "✓" if v.dockerfile else "−"
        has_run = "✓" if v.run_script else "−"
        click.echo(f"  {v.name:<25} {has_build:<8} {has_run:<8} {v.description[:40]}")
