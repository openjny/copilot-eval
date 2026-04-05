"""CLI entry point for the eval framework."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import click

from eval.config import Task, load_config
from eval.runner import RunResult, get_github_token, run_one
from eval.trace import extract_metrics, fetch_traces, filter_by_run
from eval.report import build_report, format_table, format_json, format_markdown


@click.group()
def main() -> None:
    """Copilot CLI A/B evaluation framework."""


@main.command()
@click.option("--task", "-p", default=None, help="Run a specific task (overrides enabled flag)")
@click.option("--epochs", "-n", default=None, type=int, help="Number of epochs (default: from config, typically 1)")
@click.option("--dry-run", is_flag=True, help="Show plan without executing")
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
def run(task: str | None, epochs: int | None, dry_run: bool, config_dir: str | None) -> None:
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

    github_token = get_github_token()
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
    click.echo(f" Analyze: python -m eval analyze --run-id {run_id}")
    click.echo("=" * 50)


@main.command()
@click.option("--run-id", required=True, help="Run ID to analyze")
@click.option("--output", "-o", type=click.Choice(["table", "json", "markdown"]), default="table", help="Output format")
@click.option("--aggregate", "-a", type=click.Choice(["paired", "median", "mean"]), default="paired", help="Aggregation method")
@click.option("--jaeger-url", default=None, help="Jaeger URL (default: http://localhost:16686)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def analyze(run_id: str, output: str, aggregate: str, jaeger_url: str | None, config_dir: str | None) -> None:
    """Analyze traces from a previous eval run."""
    config = load_config(Path(config_dir) if config_dir else None)
    jaeger = jaeger_url or "http://localhost:16686"

    click.echo(f"Fetching traces from {jaeger} for run {run_id}...", err=True)
    traces = fetch_traces(jaeger)
    traces = filter_by_run(traces, run_id)

    metrics = [extract_metrics(t) for t in traces]
    metrics = [m for m in metrics if m is not None]

    if not metrics:
        click.echo("No traces found for this run ID.", err=True)
        return

    results_dir = config.results_dir / run_id
    variant_order = [v.name for v in config.variants]
    reports = build_report(metrics, results_dir if results_dir.exists() else None, variant_order, aggregate)
    if not reports:
        click.echo("No reports generated.", err=True)
        return

    formatters = {"table": format_table, "json": format_json, "markdown": format_markdown}
    click.echo(formatters[output](reports))


@main.command()
@click.option("--variant", "-v", default=None, help="Build specific variant (default: all)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def build(variant: str | None, config_dir: str | None) -> None:
    """Build Docker images for all (or specific) variants."""
    import subprocess

    config = load_config(Path(config_dir) if config_dir else None)
    variants = [config.get_variant(variant)] if variant else config.variants
    variants = [v for v in variants if v is not None]

    if not variants:
        raise click.ClickException(f"Variant '{variant}' not found.")

    github_token = get_github_token()
    base_dockerfile = config.project_dir / "docker" / "Dockerfile"
    base_image = f"{config.runner.container_image_base}:base"
    env = {**os.environ, "DOCKER_BUILDKIT": "1", "GITHUB_TOKEN": github_token}

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
