"""CLI entry point for the eval framework: Click group + shared options.

Subcommands are routing-only wrappers defined in sibling modules
(``list_cmd``, ``build_cmd``, ``run_cmd``, ``analyze_cmd``, ``validate_cmd``);
their business logic lives in :mod:`eval.services` (issue #83).
"""

from __future__ import annotations

import click

from eval.collectors import load_collector_plugins
from eval.evaluators import load_evaluator_plugins
from eval.logging_config import LOG_FORMATS, LOG_LEVELS, configure_logging
from eval.runners import load_runner_plugins


@click.group()
@click.option(
    "--log-level",
    default=None,
    type=click.Choice(LOG_LEVELS, case_sensitive=False),
    help="Diagnostic log level (default: INFO or $EVAL_LOG_LEVEL).",
)
@click.option(
    "--log-format",
    default=None,
    type=click.Choice(LOG_FORMATS, case_sensitive=False),
    help="Diagnostic log format (default: plain or $EVAL_LOG_FORMAT).",
)
def main(log_level: str | None, log_format: str | None) -> None:
    """Copilot CLI A/B evaluation framework."""
    # Discover third-party evaluator types, runner backends, and collectors
    # (entry-point groups "copilot_eval.evaluators"/"copilot_eval.runners"/
    # "copilot_eval.collectors", see issue #66) before any command loads a
    # config, so plugin-defined `type:`/`backend:`/`collector:` strings
    # validate and dispatch.
    load_evaluator_plugins()
    load_runner_plugins()
    load_collector_plugins()
    try:
        configure_logging(log_level, log_format)
    except ValueError as exc:
        # Invalid EVAL_LOG_LEVEL / EVAL_LOG_FORMAT env vars reach here (the CLI
        # flags are already guarded by click.Choice). Surface a clean usage error
        # (exit code 2, matching click.Choice) instead of an uncaught traceback.
        raise click.UsageError(str(exc)) from exc


from eval.cli.analyze_cmd import analyze  # noqa: E402
from eval.cli.build_cmd import build  # noqa: E402
from eval.cli.list_cmd import list_tasks  # noqa: E402
from eval.cli.run_cmd import run  # noqa: E402
from eval.cli.validate_cmd import validate  # noqa: E402

main.add_command(run)
main.add_command(analyze)
main.add_command(build)
main.add_command(list_tasks)
main.add_command(validate)

# --- Backward-compat re-exports ---
#
# `eval/cli.py` used to be a single module containing all of the helpers
# below; they now live in `eval.services.*` (issue #83). Re-exported here so
# any external code that imported them from `eval.cli` keeps working.
from eval.runner import get_github_token, run_one  # noqa: E402, F401
from eval.services.analyze_service import _gate_epochs  # noqa: E402, F401
from eval.services.build_service import _build_images, _ensure_images  # noqa: E402, F401
from eval.services.check_report import (  # noqa: E402, F401
    print_check_results as _print_check_results,
)
from eval.services.judge_service import (  # noqa: E402, F401
    _is_truncated,
    _report_judge_reliability,
    _run_judges,
    _warn_unscored_judges,
)
from eval.services.manifest import MANIFEST_NAME  # noqa: E402, F401
from eval.services.manifest import load_manifest as _load_manifest  # noqa: E402, F401
from eval.services.manifest import write_manifest as _write_manifest  # noqa: E402, F401
from eval.services.metrics_service import (  # noqa: E402, F401
    _merge_scores_file,
    _run_metric_evaluators,
)
from eval.services.orchestrator import (  # noqa: E402, F401
    _ensure_jaeger,
    _ordering_rng,
    _safe_run_one,
    order_variants,
)
from eval.services.trace_service import (  # noqa: E402, F401
    _collect_file_traces,
    _fetch_traces_for_run,
    _report_run_coverage,
)
from eval.validation import (  # noqa: E402, F401
    CheckResult,
    any_failed,
    check_config_schema,
    check_fixtures,
    check_script_references,
    check_var_interpolation,
    validate_readiness,
)

__all__ = ["main"]
