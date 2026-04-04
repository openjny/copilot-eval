"""CLI entry point for the eval framework."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import click

from eval.config import load_config
from eval.runner import RunResult, get_github_token, run_one
from eval.trace import extract_metrics, fetch_traces, filter_by_run
from eval.report import build_report, format_table, format_json, format_markdown


@click.group()
def main() -> None:
    """Copilot CLI A/B evaluation framework."""


@main.command()
@click.option("--pattern", "-p", default=None, help="Run a specific pattern (overrides enabled flag)")
@click.option("--epochs", "-n", default=None, type=int, help="Number of epochs (default: from config, typically 1)")
@click.option("--dry-run", is_flag=True, help="Show plan without executing")
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
def run(pattern: str | None, epochs: int | None, dry_run: bool, config_dir: str | None) -> None:
    """Run A/B eval for one or more patterns."""
    config = load_config(Path(config_dir) if config_dir else None)
    epochs = epochs or config.runner.epochs

    # Select patterns
    if pattern:
        p = config.get_pattern(pattern)
        if not p:
            raise click.ClickException(f"Pattern '{pattern}' not found. Use 'list' to see available patterns.")
        patterns = [p]
    else:
        patterns = config.enabled_patterns()

    if not patterns:
        click.echo("No patterns to run. Use --pattern NAME or enable patterns in config.")
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
    click.echo(f" Patterns:")
    for p in patterns:
        click.echo(f"   - {p.name} ({p.type})")
    click.echo("=" * 50)

    if dry_run:
        click.echo(f"[dry-run] Would run {epochs} epoch(s) × {len(config.variants)} variants for each pattern.")
        return

    github_token = get_github_token()
    results: list[RunResult] = []

    for p in patterns:
        prompt = config.resolve_prompt(p)
        click.echo(f"\n>>> Pattern: {p.name} ({p.type})")
        click.echo(f">>> Prompt:  {prompt}\n")

        for epoch in range(1, epochs + 1):
            for variant in config.variants:
                result = run_one(p, variant, epoch, config, run_id, run_dir, github_token)
                results.append(result)

    # Summary
    passed = sum(1 for r in results if r.result == "PASS")
    failed = sum(1 for r in results if r.result == "FAIL")
    click.echo("=" * 50)
    click.echo(f" Run complete: {run_id}")
    click.echo(f" Results: {passed} PASS, {failed} FAIL, {len(results) - passed - failed} SKIP")
    click.echo(f" Jaeger:  http://localhost:16686")
    click.echo(f" Analyze: python -m eval analyze --run-id {run_id}")
    click.echo("=" * 50)


@main.command()
@click.option("--run-id", required=True, help="Run ID to analyze")
@click.option("--output", "-o", type=click.Choice(["table", "json", "markdown"]), default="table", help="Output format")
@click.option("--jaeger-url", default=None, help="Jaeger URL (default: http://localhost:16686)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def analyze(run_id: str, output: str, jaeger_url: str | None, config_dir: str | None) -> None:
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
    report = build_report(metrics, results_dir if results_dir.exists() else None)
    if not report:
        return

    formatters = {"table": format_table, "json": format_json, "markdown": format_markdown}
    click.echo(formatters[output](report))


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
    dockerfile = config.project_dir / "docker" / "Dockerfile"

    for v in variants:
        image = config.image_name(v)
        click.echo(f"Building {image}...")

        build_args = [
            "--build-arg", f"COPILOT_VERSION={config.runner.copilot_version}",
            "--build-arg", f"VARIANT_NAME={v.name}",
        ]

        if v.build_script:
            build_args.extend(["--build-arg", f"VARIANT_BUILD_SCRIPT={v.build_script}"])

        cmd = [
            "docker", "build",
            "-f", str(dockerfile),
            *build_args,
            "--secret", f"id=github_token,env=GITHUB_TOKEN",
            "-t", image,
            str(config.project_dir),
        ]

        env = {**os.environ, "DOCKER_BUILDKIT": "1", "GITHUB_TOKEN": github_token}
        result = subprocess.run(cmd, env=env)
        if result.returncode != 0:
            raise click.ClickException(f"Build failed for {image}")
        click.echo(f"✓ {image}")

    click.echo(f"\nBuilt {len(variants)} image(s).")


@main.command(name="list")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def list_patterns(config_dir: str | None) -> None:
    """List available patterns and variants."""
    config = load_config(Path(config_dir) if config_dir else None)

    click.echo("Patterns:")
    click.echo(f"  {'Name':<25} {'Type':<8} {'Enabled':<8} Prompt")
    click.echo("  " + "-" * 75)
    for p in config.patterns:
        prompt_preview = p.prompt[:40] + "..." if len(p.prompt) > 40 else p.prompt
        click.echo(f"  {p.name:<25} {p.type:<8} {'✓' if p.enabled else '−':<8} {prompt_preview}")

    click.echo(f"\nVariants:")
    click.echo(f"  {'Name':<25} {'Build':<8} {'Run':<8} Description")
    click.echo("  " + "-" * 75)
    for v in config.variants:
        has_build = "✓" if v.build_script else "−"
        has_run = "✓" if v.run_script else "−"
        click.echo(f"  {v.name:<25} {has_build:<8} {has_run:<8} {v.description[:40]}")
