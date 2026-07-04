"""Judge evaluator: LLM-as-judge scoring of a run's conversation transcript."""

from __future__ import annotations

from eval import runner as _runner
from eval.config import Evaluator as EvaluatorConfig
from eval.protocols import EvalContext, EvalScore


class JudgeEvaluator:
    """Scores a run via a Copilot-as-judge call.

    Delegates to :func:`eval.runner.run_judge`, which implements
    self-consistency sampling (``runner.judge_samples``) and aggregation
    (``runner.judge_aggregate``). Judge evaluators are scored during
    ``analyze`` (against the captured OTel trace / log), not inline during
    ``run_one``, since that's when a full conversation transcript is
    available.

    ``runner.judge_batch`` scores every judge of a run in a single LLM call
    (see :func:`eval.runner.run_judges_batch` and ``eval.cli._run_judges``);
    that batched path doesn't fit this protocol's one-evaluator-per-call
    shape, so it's driven directly by the CLI rather than through this class.
    """

    evaluator_type = "judge"

    def __init__(self, config: EvaluatorConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> JudgeEvaluator:
        return cls(config)

    def evaluate(self, context: EvalContext) -> EvalScore | None:
        if context.conversation is None and context.output_files_text is None:
            return None
        return _runner.run_judge(
            self.config,
            context.conversation or "",
            context.config,
            context.token,
            context.output_files_text,
            context.extra_meta,
        )
