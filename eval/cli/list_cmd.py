"""`list` command: prints configured tasks and variants."""

from __future__ import annotations

from pathlib import Path

import click

from eval.config import load_config


@click.command(name="list")
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
