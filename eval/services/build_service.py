"""Docker image build orchestration for eval variants.

Builds the shared base image (``docker/Dockerfile``) plus one image per
variant, and can lazily build anything missing before a `run`.
"""

from __future__ import annotations

import os
import subprocess

import click

from eval.config import Config, Variant
from eval.runner import get_github_token


def _build_images(config: Config, variants: list[Variant], token: str) -> None:
    """Build base + variant Docker images."""
    base_dockerfile = config.project_dir / "docker" / "Dockerfile"
    base_image = f"{config.runner.container_image_base}:base"
    env = {**os.environ, "DOCKER_BUILDKIT": "1", "GITHUB_TOKEN": token}

    # Step 1: Build base image
    click.echo(f"Building {base_image}...")
    cmd = [
        "docker",
        "build",
        "-f",
        str(base_dockerfile),
        "--build-arg",
        f"COPILOT_VERSION={config.runner.copilot_version}",
        "-t",
        base_image,
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
            "docker",
            "build",
            "-f",
            str(df),
            "--secret",
            "id=github_token,env=GITHUB_TOKEN",
            "-t",
            image,
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
    result = subprocess.run(["docker", "image", "inspect", base_image], capture_output=True)
    if result.returncode != 0:
        missing.append("base")

    # Check variant images
    for v in config.variants:
        image = config.image_name(v)
        result = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if result.returncode != 0:
            missing.append(v.name)

    if not missing:
        return

    click.echo(f"Missing images: {', '.join(missing)}. Building...", err=True)
    _build_images(config, config.variants, token)


def build_command(config: Config, variant: str | None) -> None:
    """Business logic for the `build` CLI command.

    Resolves which variant(s) to build (all by default) and builds them,
    raising a `click.ClickException` if an explicitly named variant doesn't
    exist in the config.
    """
    variants = [config.get_variant(variant)] if variant else config.variants
    resolved = [v for v in variants if v is not None]

    if not resolved:
        raise click.ClickException(f"Variant '{variant}' not found.")

    github_token = get_github_token()
    _build_images(config, resolved, github_token)
