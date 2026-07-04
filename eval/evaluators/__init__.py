"""Evaluator strategy classes + registry.

Provides the concrete implementations of ``eval.protocols.Evaluator`` for each
built-in evaluator ``type`` (judge/script/contains/regex/metric), plus a
name -> class registry so evaluator dispatch (``eval.runner._run_evaluators``
for inline types, ``eval.cli._run_judges``/``_run_metric_evaluators`` for
judge/metric) is a lookup instead of an if/elif chain.

Third-party evaluator types can be added without touching ``eval.runner`` or
``eval.config`` (enabling issue #66): register a class implementing
``eval.protocols.Evaluator`` under the ``copilot_eval.evaluators`` entry-point
group, and it is loaded into ``EVALUATOR_REGISTRY`` by
``load_evaluator_plugins()`` — called once at CLI startup (``eval.cli.main``)
— after which ``eval.config`` accepts the new ``type:`` string and
``eval.runner._run_evaluators`` dispatches to it inline like script/contains/
regex (any type other than judge/metric is treated as inline-capable).
"""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from logging import getLogger

from eval.evaluators.contains import ContainsEvaluator
from eval.evaluators.judge import JudgeEvaluator
from eval.evaluators.metric import MetricEvaluator
from eval.evaluators.regex import RegexEvaluator
from eval.evaluators.script import ScriptEvaluator
from eval.protocols import Evaluator

logger = getLogger(__name__)

# `type: <key>` in an eval-config.yaml evaluator entry selects the
# corresponding class below.
EVALUATOR_REGISTRY: dict[str, type[Evaluator]] = {
    "judge": JudgeEvaluator,
    "script": ScriptEvaluator,
    "contains": ContainsEvaluator,
    "regex": RegexEvaluator,
    "metric": MetricEvaluator,
}

# Entry-point group third-party packages can use to register additional
# evaluator types (enables #66), e.g. in their pyproject.toml:
#
#   [project.entry-points."copilot_eval.evaluators"]
#   my_type = "my_package.evaluators:MyEvaluator"
#
# where `my_package.evaluators.MyEvaluator` implements `eval.protocols.Evaluator`.
ENTRY_POINT_GROUP = "copilot_eval.evaluators"

_plugins_loaded = False


def load_evaluator_plugins() -> None:
    """Discover and register third-party evaluator types via entry points.

    Idempotent — safe to call more than once (e.g. once per CLI invocation).
    A plugin that fails to load is logged and skipped rather than aborting the
    process, since one broken plugin package shouldn't take down the CLI.
    """
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    try:
        entry_points = importlib_metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - defensive against odd metadata backends
        logger.debug("No '%s' entry points available", ENTRY_POINT_GROUP)
        return
    for ep in entry_points:
        try:
            evaluator_cls = ep.load()
        except Exception as exc:  # noqa: BLE001 - one bad plugin shouldn't break the rest
            logger.warning("Failed to load evaluator plugin '%s': %s", ep.name, exc)
            continue
        EVALUATOR_REGISTRY[ep.name] = evaluator_cls


def get_evaluator_class(evaluator_type: str) -> type[Evaluator] | None:
    """Look up a registered evaluator class by its config `type` string."""
    return EVALUATOR_REGISTRY.get(evaluator_type)


__all__ = [
    "EVALUATOR_REGISTRY",
    "ENTRY_POINT_GROUP",
    "ContainsEvaluator",
    "JudgeEvaluator",
    "MetricEvaluator",
    "RegexEvaluator",
    "ScriptEvaluator",
    "get_evaluator_class",
    "load_evaluator_plugins",
]
