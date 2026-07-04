"""Regex evaluator: PASS iff a pattern matches somewhere in the run log."""

from __future__ import annotations

import re

from eval.config import Evaluator as EvaluatorConfig
from eval.protocols import EvalContext, EvalScore
from eval.runner import _read_log


class RegexEvaluator:
    """Deterministic regex search against the run's log file.

    Runs inline during ``run_one``.
    """

    evaluator_type = "regex"

    def __init__(self, config: EvaluatorConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> RegexEvaluator:
        return cls(config)

    def evaluate(self, context: EvalContext) -> EvalScore | None:
        ev = self.config
        if not ev.value or context.log_file is None:
            return None
        output = _read_log(context.log_file)
        match = bool(re.search(ev.value, output or ""))
        return EvalScore(
            name=ev.name,
            type="regex",
            score=1 if match else 0,
            reason="matched" if match else "no match",
            passed=match,
        )
