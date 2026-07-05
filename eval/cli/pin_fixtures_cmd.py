"""`pin-fixtures` command: routing only — delegates to eval.services.fixtures_service.

Generates a content-hash lockfile (``fixtures.lock``) so later runs can verify
that fixtures were not silently modified (issue #89).
"""

from __future__ import annotations

from pathlib import Path

import click

from eval.config import load_config
from eval.services.fixtures_service import pin_fixtures


@click.command(name="pin-fixtures")
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
def pin_fixtures_cmd(config_dir: str | None) -> None:
    """Pin fixtures: write fixtures.lock with per-fixture content hashes."""
    config = load_config(Path(config_dir) if config_dir else None)
    result = pin_fixtures(config)

    count = len(result.fixtures)
    click.echo(f"Pinned {count} fixture(s) -> {result.path}")
    for name, entry in result.fixtures.items():
        click.echo(
            f"  {name}: {entry['sha256'][:12]}… "
            f"({len(entry['files'])} file(s), {entry['total_size']} bytes)"
        )
    for name in result.missing:
        click.echo(
            f"  WARNING: fixture '{name}' referenced but not found on disk; skipped.",
            err=True,
        )
