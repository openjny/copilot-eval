"""Shared presentation helper for `CheckResult` lists.

Used by both the `run` command's pre-flight checks (`eval.services.orchestrator`)
and the `validate` command (`eval.cli.validate_cmd`), so the two share one
printed report format instead of duplicating it.
"""

from __future__ import annotations

import click

from eval.validation import CheckResult


def print_check_results(results: list[CheckResult], title: str, strict: bool = False) -> None:
    """Print a validation/readiness report, one line per check, to stderr.

    When `strict` is set, warning-level results are rendered as failures and
    counted as such in the summary (issue #128) — mirroring `validate --strict`,
    which promotes warnings to a non-zero CI gate.
    """
    passed = sum(1 for r in results if r.passed)
    warnings = sum(1 for r in results if not r.passed and not r.blocking)
    summary = f"{title}: {passed}/{len(results)} passed"
    if warnings:
        if strict:
            noun = "warning" if warnings == 1 else "warnings"
            summary += f" ({warnings} {noun} promoted to failure by --strict)"
        else:
            summary += f" ({warnings} warning{'s' if warnings != 1 else ''})"
    click.echo(summary, err=True)
    for r in results:
        click.echo(r.format(strict=strict), err=True)
