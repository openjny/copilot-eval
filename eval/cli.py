"""CLI entry point for the eval framework."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

import click
import requests

from eval.config import Config, Task, load_config
from eval.runner import RunResult, get_github_token, run_one
from eval.trace import RunMetrics, Trace, extract_conversation, extract_metrics, fetch_traces, filter_by_run
from eval.report import build_report, format_table, format_json, format_markdown


def _ensure_jaeger(config: Config) -> None:
    """Check if Jaeger is reachable, start it via docker compose if not."""
    url = config.runner.otel_endpoint.replace("host.docker.internal", "localhost").replace(":4318", "")
    jaeger_url = f"http://localhost:16686"
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
    import time
    for _ in range(10):
        try:
            requests.get(f"{jaeger_url}/api/services", timeout=2)
            click.echo("Jaeger started.", err=True)
            return
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(1)
    raise click.ClickException("Failed to start Jaeger. Check docker compose logs.")


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
        p = config.get_pattern(task)
        if not p:
            raise click.ClickException(f"Task '{task}' not found. Use 'list' to see available tasks.")
        tasks = [p]
    else:
        tasks = config.enabled_patterns()

    if not tasks:
        click.echo("No tasks to run. Use --task NAME or enable tasks in config.")
        return

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
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
    click.echo(f" Variants:")
    for v in config.variants:
        click.echo(f"   - {v.name}")
    click.echo(f" Tasks:")
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
            futures = {pool.submit(_run_task_serial, t): t.name for t in tasks}
            for future in as_completed(futures):
                results.extend(future.result())
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
    click.echo("=" * 50)
    click.echo(f" Run complete: {run_id}")
    click.echo(f" Results: {passed} passed, {failed} failed")
    click.echo(f" Jaeger:  http://localhost:16686")
    analyze_cmd = f"uv run copilot-eval analyze --run-id {run_id}"
    if config_dir:
        analyze_cmd += f" --config-dir {config_dir}"
    click.echo(f" Analyze: {analyze_cmd}")
    click.echo("=" * 50)


@main.command()
@click.option("--run-id", required=True, help="Run ID to analyze")
@click.option("--output", "-o", type=click.Choice(["table", "json", "markdown"]), default="table", help="Output format")
@click.option("--aggregate", "-a", type=click.Choice(["paired", "median", "mean"]), default="paired", help="Aggregation method")
@click.option("--jaeger-url", default=None, help="Jaeger URL (default: http://localhost:16686)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
@click.option("--skip-eval", is_flag=True, help="Skip judge evaluation, use existing scores")
@click.option("--re-eval", is_flag=True, help="Force re-run judge evaluation (ignore cached scores)")
def analyze(run_id: str, output: str, aggregate: str, jaeger_url: str | None,
            config_dir: str | None, skip_eval: bool, re_eval: bool) -> None:
    """Analyze traces from a previous eval run."""
    config = load_config(Path(config_dir) if config_dir else None)
    jaeger = jaeger_url or "http://localhost:16686"

    click.echo(f"Fetching traces from {jaeger} for run {run_id}...", err=True)
    traces = fetch_traces(jaeger)
    traces = filter_by_run(traces, run_id)

    metrics: list[RunMetrics] = [m for m in (extract_metrics(t) for t in traces) if m is not None]

    if not metrics:
        click.echo("No traces found for this run ID.", err=True)
        return

    results_dir = config.results_dir / run_id

    # Run judge evaluators if not skipped
    if not skip_eval and results_dir.exists():
        if re_eval:
            # Delete existing judge scores to force re-evaluation
            for sf in results_dir.glob("*.scores.json"):
                try:
                    import json as _json
                    existing = _json.loads(sf.read_text())
                    non_judge = [s for s in existing if s.get("type") != "judge"]
                    if non_judge:
                        sf.write_text(_json.dumps(non_judge, indent=2, ensure_ascii=False))
                    else:
                        sf.unlink()
                except (ValueError, OSError):
                    sf.unlink(missing_ok=True)
        _run_judges(config, traces, results_dir)

    variant_order = [v.name for v in config.variants]
    reports = build_report(metrics, results_dir if results_dir.exists() else None, variant_order, aggregate)
    if not reports:
        click.echo("No reports generated.", err=True)
        return

    formatters = {"table": format_table, "json": format_json, "markdown": format_markdown}
    click.echo(formatters[output](reports))


def _run_judges(config: "Config", traces: list[Trace], results_dir: Path) -> None:
    """Run judge evaluators using OTel traces + output files."""
    import json
    import subprocess

    from eval.runner import _read_output_files, _parse_json

    github_token = get_github_token()
    tasks_by_name = {t.name: t for t in config.tasks}

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
        if scores_file.exists():
            continue  # already scored

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
        output_files_text = None
        if output_dir.is_dir():
            output_files_text = _read_output_files_from_dir(output_dir, max_chars=8000)

        # Run each judge evaluator
        scores = []
        for ev in judge_evaluators:
            sections = [f"--- COPILOT OUTPUT ---\n{conversation}\n--- END OUTPUT ---"]
            if output_files_text:
                sections.append(f"--- OUTPUT FILES ---\n{output_files_text}\n--- END FILES ---")
            prompt = (
                f"You are an eval judge. Score the following Copilot output.\n\n"
                f"{ev.prompt}\n\n"
                f"{chr(10).join(sections)}\n\n"
                f'Output ONLY valid JSON: {{"score": N, "reason": "..."}}'
            )
            click.echo(f"    [{scenario}/{variant}/e{epoch}] Evaluating: {ev.name} (judge)...", err=True)
            cmd = ["copilot", "-p", prompt, "-s"]
            if config.runner.judge_model:
                cmd.extend(["--model", config.runner.judge_model])
            judge_env = {**os.environ, "GITHUB_TOKEN": github_token, "COPILOT_OTEL_ENABLED": "false"}
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=judge_env)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                scores.append({"name": ev.name, "type": "judge", "score": None, "reason": "timeout", "passed": True})
                continue
            data = _parse_json(proc.stdout)
            if data:
                score = int(data.get("score", 0))
                reason = str(data.get("reason", ""))
                click.echo(f"    ✓ {ev.name}: {score} — {reason[:60]}", err=True)
                scores.append({"name": ev.name, "type": "judge", "score": score, "reason": reason, "passed": True})
            else:
                scores.append({"name": ev.name, "type": "judge", "score": None, "reason": "parse_error", "passed": True})

        # Also include any existing non-judge scores from the run
        existing_scores = []
        log_scores_file = results_dir / f"{scenario}_{variant}_epoch{epoch}.scores.json"
        if log_scores_file.exists():
            try:
                existing_scores = [s for s in json.loads(log_scores_file.read_text()) if s.get("type") != "judge"]
            except (json.JSONDecodeError, KeyError):
                pass

        all_scores = existing_scores + scores
        if all_scores:
            scores_file.write_text(json.dumps(all_scores, indent=2, ensure_ascii=False))


def _read_output_files_from_dir(output_dir: Path, max_chars: int = 8000) -> str | None:
    """Read all files from a directory and return as concatenated text."""
    parts: list[str] = []
    total = 0
    for f in sorted(output_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(output_dir)
        try:
            content = f.read_text(errors="replace")
        except OSError:
            continue
        if total + len(content) > max_chars:
            remaining = max_chars - total
            if remaining > 0:
                parts.append(f"=== {rel} ===\n{content[:remaining]}\n... (truncated)")
            break
        parts.append(f"=== {rel} ===\n{content}")
        total += len(content)
    return "\n\n".join(parts) if parts else None


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


def _build_images(config: Config, variants: list, token: str) -> None:
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
        raise click.ClickException(f"Base image build failed")
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
            "--secret", f"id=github_token,env=GITHUB_TOKEN",
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
def list_patterns(config_dir: str | None) -> None:
    """List available tasks and variants."""
    config = load_config(Path(config_dir) if config_dir else None)

    click.echo("Tasks:")
    click.echo(f"  {'Name':<25} {'Enabled':<8} {'Evals':>5} Prompt")
    click.echo("  " + "-" * 75)
    for p in config.tasks:
        prompt_preview = p.prompt[:40] + "..." if len(p.prompt) > 40 else p.prompt
        click.echo(f"  {p.name:<25} {'✓' if p.enabled else '−':<8} {len(p.evaluators):>5} {prompt_preview}")

    click.echo(f"\nVariants:")
    click.echo(f"  {'Name':<25} {'Build':<8} {'Run':<8} Description")
    click.echo("  " + "-" * 75)
    for v in config.variants:
        has_build = "✓" if v.dockerfile else "−"
        has_run = "✓" if v.run_script else "−"
        click.echo(f"  {v.name:<25} {has_build:<8} {has_run:<8} {v.description[:40]}")
