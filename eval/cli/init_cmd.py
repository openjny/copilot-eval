"""`init` command: scaffold a minimal, runnable eval project.

Copies a static template tree (``eval/templates/init/<template>/``) to
``--config-dir``, rewriting two path-dependent placeholders along the way:

- the generated config's ``$schema`` header, made relative to the target dir
- the ``experimental`` variant's Dockerfile path, made relative to the repo
  root (``config.project_dir`` — see ``eval.config.load_config`` — is always
  the repo checkout, not ``--config-dir``, so ``build.dockerfile`` values must
  be expressed relative to it; this mirrors ``examples/azure-skills``, whose
  paths are written the same way).

No templating engine is used deliberately (issue #81): plain string
replacement keeps this dependency-free and the templates readable as-is.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

# eval/cli/init_cmd.py -> eval/cli -> eval -> repo root.
_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
_TEMPLATES_ROOT = _PROJECT_DIR / "eval" / "templates" / "init"
_SCHEMA_PATH = _PROJECT_DIR / "schemas" / "eval-config.schema.json"

_SUPPORTED_TEMPLATES = ("minimal",)


@click.command()
@click.option(
    "--config-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Target directory to scaffold (created if it doesn't exist).",
)
@click.option(
    "--template",
    default="minimal",
    type=click.Choice(_SUPPORTED_TEMPLATES),
    show_default=True,
    help="Scaffold template to use.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite files that already exist in --config-dir.",
)
def init(config_dir: Path, template: str, force: bool) -> None:
    """Scaffold a minimal, runnable eval project under --config-dir.

    Generates eval-config.yaml, tasks/hello-world.yaml,
    variants/{baseline,experimental}.yaml, fixtures/hello-world/README.md,
    docker/Dockerfile.experimental, .env.example, and .gitignore — a
    first-green-run eval you can validate and dry-run immediately:

    \b
        uv run copilot-eval init --config-dir my-eval
        cp my-eval/.env.example my-eval/.env   # then edit it
        uv run copilot-eval validate --config-dir my-eval
        uv run copilot-eval run --config-dir my-eval --dry-run

    Fails if any target file already exists, unless --force is given.
    """
    template_dir = _TEMPLATES_ROOT / template
    if not template_dir.is_dir():
        raise click.ClickException(f"Unknown template '{template}' (looked in {template_dir}).")

    target_dir = config_dir.resolve()
    template_files = sorted(p for p in template_dir.rglob("*") if p.is_file())

    if not force:
        existing = [
            target_dir / p.relative_to(template_dir)
            for p in template_files
            if (target_dir / p.relative_to(template_dir)).exists()
        ]
        if existing:
            listing = "\n  ".join(str(p) for p in existing)
            raise click.ClickException(
                f"Refusing to overwrite existing file(s):\n  {listing}\n"
                "Re-run with --force to overwrite."
            )

    substitutions = {
        "__SCHEMA_PATH__": _posix_relpath(_SCHEMA_PATH, target_dir),
        "__DOCKERFILE_PATH__": _posix_relpath(
            target_dir / "docker" / "Dockerfile.experimental", _PROJECT_DIR
        ),
    }

    created: list[Path] = []
    for src in template_files:
        rel = src.relative_to(template_dir)
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text()
        for token, value in substitutions.items():
            text = text.replace(token, value)
        dest.write_text(text)
        created.append(dest)

    click.echo(f"Created {target_dir}/")
    for path in created:
        click.echo(f"  {path.relative_to(target_dir)}")

    click.echo("\nNext steps:")
    click.echo(
        f"  1. cp {config_dir}/.env.example {config_dir}/.env   (then add COPILOT_GITHUB_TOKEN)"
    )
    click.echo(f"  2. uv run copilot-eval validate --config-dir {config_dir}")
    click.echo(f"  3. uv run copilot-eval run --config-dir {config_dir} --dry-run")
    click.echo(f"  4. uv run copilot-eval run --config-dir {config_dir}")


def _posix_relpath(path: Path, start: Path) -> str:
    """`os.path.relpath` with forward slashes, for YAML values on any OS."""
    return Path(os.path.relpath(path, start)).as_posix()
