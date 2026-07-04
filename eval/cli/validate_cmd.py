"""`validate` command: routing only — delegates to eval.validation checks."""

from __future__ import annotations

from pathlib import Path

import click

from eval.services.check_report import print_check_results
from eval.validation import (
    CheckResult,
    any_failed,
    check_config_schema,
    check_fixtures,
    check_script_references,
    check_var_interpolation,
)


@click.command()
@click.option("--config-dir", default=None, type=click.Path(exists=True), help="Project directory")
def validate(config_dir: str | None) -> None:
    """Validate an eval-config.yaml: schema, fixtures, script/variant references, and vars.

    Checks (independent of Docker/auth, unlike `run`'s pre-flight checks):
    - YAML syntax and schema validity
    - Referenced fixture directories exist on disk (warning: a missing
      fixture dir doesn't fail a run, since eval.runner tolerates it)
    - Variant/task script references (Dockerfile, run script, hooks,
      health_check, script evaluators) exist on disk
    - {var} prompt/output_instruction placeholders resolve for every variant
      (warning: unresolved placeholders are left as literal text at run time)

    Exits 0 if all *blocking* checks pass (warnings don't affect the exit
    code), 1 otherwise.
    """
    config, schema_result = check_config_schema(Path(config_dir) if config_dir else None)
    results: list[CheckResult] = [schema_result]
    if config is not None:
        results += check_fixtures(config)
        results += check_script_references(config)
        results += check_var_interpolation(config)

    print_check_results(results, "Validation")
    if any_failed(results):
        raise click.exceptions.Exit(1)
    if any(not r.passed for r in results):
        click.echo("All blocking checks passed (see warnings above).", err=True)
    else:
        click.echo("All checks passed.", err=True)
