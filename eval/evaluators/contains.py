"""Contains evaluator: PASS iff a literal substring appears in the run log."""

from __future__ import annotations

from eval.config import Evaluator as EvaluatorConfig
from eval.protocols import EvalContext, EvalScore
from eval.runner import _read_log


class ContainsEvaluator:
    """Deterministic substring match against the run's log file.

    Runs inline during ``run_one``.
    """

    evaluator_type = "contains"

    def __init__(self, config: EvaluatorConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> ContainsEvaluator:
        return cls(config)

    def evaluate(self, context: EvalContext) -> EvalScore | None:
        ev = self.config
        if not ev.value or context.log_file is None:
            return None
        output = _read_log(context.log_file)
        found = ev.value in (output or "")
        return EvalScore(
            name=ev.name,
            type="contains",
            score=1 if found else 0,
            reason="found" if found else "not found",
            passed=found,
        )
