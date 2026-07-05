"""`validate` command: routing only — delegates to eval.validation checks."""

from __future__ import annotations

import os
from pathlib import Path

import click

from eval.services.check_report import print_check_results
from eval.validation import (
    CheckResult,
    any_failed,
    any_warnings,
    check_config_schema,
    check_fixtures,
    check_json_schema,
    check_script_references,
    check_var_interpolation,
)


@click.command()
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
@click.option(
    "--strict/--no-strict",
    default=None,
    help=(
        "Promote warnings (missing fixtures, unresolved vars, an unreadable/"
        "skipped schema) to a non-zero exit — a fail-closed CI gate. Defaults "
        "to enabled when the CI env var is set, disabled otherwise."
    ),
)
def validate(config_dir: str | None, strict: bool | None) -> None:
    """Validate an eval-config.yaml: schema, fixtures, script/variant references, and vars.

    Checks (independent of Docker/auth, unlike `run`'s pre-flight checks):
    - YAML syntax and schema validity
    - JSON Schema conformance against schemas/eval-config.schema.json — covers
      both the inline eval-config.yaml and the split-file layout (tasks/*.yaml,
      variants/*.yaml)
    - Referenced fixture directories exist on disk (warning: a missing
      fixture dir doesn't fail a run, since eval.runner tolerates it)
    - Variant/task script references (Dockerfile, run script, hooks,
      health_check, script evaluators) exist on disk
    - {var} prompt/output_instruction placeholders resolve for every variant
      (warning: unresolved placeholders are left as literal text at run time)

    By default, exits 0 if all *blocking* checks pass (warnings don't affect the
    exit code), 1 otherwise. With `--strict` (auto-enabled when the CI env var
    is set), warnings are promoted to failures and any warning makes the command
    exit non-zero — a single authoritative pre-run gate for CI.
    """
    if strict is None:
        strict = bool(os.environ.get("CI"))

    config_path = Path(config_dir) if config_dir else None
    config, schema_result = check_config_schema(config_path)
    results: list[CheckResult] = [schema_result, check_json_schema(config_path)]
    if config is not None:
        results += check_fixtures(config)
        results += check_script_references(config)
        results += check_var_interpolation(config)

    print_check_results(results, "Validation", strict=strict)

    blocked = any_failed(results)
    warned = any_warnings(results)
    if blocked or (strict and warned):
        raise click.exceptions.Exit(1)
    if warned:
        click.echo(
            "All blocking checks passed (see warnings above; re-run with --strict "
            "to fail on them).",
            err=True,
        )
    else:
        click.echo("All checks passed.", err=True)
