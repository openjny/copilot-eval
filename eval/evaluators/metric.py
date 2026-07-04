"""Metric evaluator: deterministic pass/fail gate on parsed OTel RunMetrics."""

from __future__ import annotations

from eval import runner as _runner
from eval.config import Evaluator as EvaluatorConfig
from eval.protocols import EvalContext, EvalScore


class MetricEvaluator:
    """Thresholds a ``RunMetrics`` field (e.g. cost, duration, tool_count).

    Delegates to :func:`eval.runner.eval_metric`. Like judge evaluators,
    metric evaluators are scored during ``analyze`` once traces are parsed
    into ``RunMetrics``, not inline during ``run_one``.
    """

    evaluator_type = "metric"

    def __init__(self, config: EvaluatorConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> MetricEvaluator:
        return cls(config)

    def evaluate(self, context: EvalContext) -> EvalScore | None:
        if context.metrics is None:
            return None
        return _runner.eval_metric(self.config, context.metrics)
