"""Python evaluator: in-process `module:func` call (issue #66).

Unlike ``script`` (which shells out to an executable and only sees an exit
code), ``python`` evaluators run in-process and are handed an ``EvalContext``,
returning an ``EvalScore`` directly. Like ``script``/``contains``/``regex``,
``python`` runs inline right after each task execution (it is not in
``eval.runner._DEFERRED_EVALUATOR_TYPES``), so only the inline fields are
populated: ``task``, ``variant``, ``log_file``, ``work_dir``, ``token``.
``conversation``, ``output_files_text``, and ``metrics`` are always ``None``
for ``python`` evaluators — those are only filled in for ``judge``/``metric``
evaluators, which run later during ``analyze``. This is the first consumer of
the plugin extension path from issue #66: any callable matching
``(EvalContext) -> EvalScore | None`` can be referenced by
``script: module:func`` without registering a whole new evaluator type via
entry points.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from logging import getLogger
from typing import cast

from eval.config import Evaluator as EvaluatorConfig
from eval.exceptions import EvalError
from eval.protocols import EvalContext, EvalScore

logger = getLogger(__name__)


class PythonEvalError(EvalError):
    """Raised when a `type: python` evaluator's `module:func` can't be loaded
    or called, or returns something other than `EvalScore | None`."""


class PythonEvaluator:
    """Calls ``evaluator.script`` (``"module:func"``) in-process.

    Runs inline during ``run_one`` like script/contains/regex (see
    ``eval.runner._DEFERRED_EVALUATOR_TYPES``); the function is free to return
    ``None`` to opt out for a given context, mirroring the other inline
    evaluators.
    """

    evaluator_type = "python"

    def __init__(self, config: EvaluatorConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> PythonEvaluator:
        return cls(config)

    def evaluate(self, context: EvalContext) -> EvalScore | None:
        ev = self.config
        if not ev.script:
            return None
        logger.info("Evaluating: %s (python: %s)...", ev.name, ev.script)
        func = _load_callable(ev.script)
        result = func(context)
        if result is None:
            return None
        if not isinstance(result, EvalScore):
            raise PythonEvalError(
                f"Evaluator '{ev.name}' (type=python): '{ev.script}' must return an "
                f"EvalScore or None, got {type(result).__name__}."
            )
        return result


def _load_callable(script: str) -> Callable[[EvalContext], EvalScore | None]:
    """Resolve a `module:func` reference to a callable via dynamic import."""
    if ":" not in script:
        raise PythonEvalError(
            f"type=python evaluator script '{script}' must be in 'module:func' format."
        )
    module_name, func_name = script.rsplit(":", 1)
    if not module_name or not func_name:
        raise PythonEvalError(
            f"type=python evaluator script '{script}' must be in 'module:func' format "
            "with both a non-empty module and function name."
        )
    try:
        module = importlib.import_module(module_name)
    except (ImportError, ValueError) as exc:
        raise PythonEvalError(f"Failed to import module '{module_name}': {exc}") from exc
    try:
        func = getattr(module, func_name)
    except AttributeError as exc:
        raise PythonEvalError(f"Module '{module_name}' has no attribute '{func_name}'.") from exc
    if not callable(func):
        raise PythonEvalError(f"'{script}' resolved to a non-callable value.")
    return cast("Callable[[EvalContext], EvalScore | None]", func)
