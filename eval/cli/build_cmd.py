"""`build` command: routing only — delegates to eval.services.build_service."""

from __future__ import annotations

from pathlib import Path

import click

from eval.config import load_config
from eval.services.build_service import build_command


@click.command()
@click.option("--variant", "-v", default=None, help="Build specific variant (default: all)")
@click.option("--config-dir", default=None, type=click.Path(exists=True))
def build(variant: str | None, config_dir: str | None) -> None:
    """Build Docker images for all (or specific) variants."""
    config = load_config(Path(config_dir) if config_dir else None)
    build_command(config, variant)
