"""Judge evaluator: LLM-as-judge scoring of a run's conversation transcript."""

from __future__ import annotations

from eval.config import Evaluator as EvaluatorConfig
from eval.judge_executor import JudgeContext, JudgeExecutor
from eval.protocols import EvalContext, EvalScore


class JudgeEvaluator:
    """Scores a run via a Copilot-as-judge call.

    Delegates to :class:`eval.judge_executor.JudgeExecutor`, which implements
    prompt construction, the Copilot CLI call, response parsing, and
    self-consistency sampling (``runner.judge_samples``) with aggregation
    (``runner.judge_aggregate``). Judge evaluators are scored during
    ``analyze`` (against the captured OTel trace / log), not inline during
    ``run_one``, since that's when a full conversation transcript is
    available.

    ``runner.judge_batch`` scores every judge of a run in a single LLM call
    (see :meth:`eval.judge_executor.JudgeExecutor.execute_batch` and
    ``eval.cli._run_judges``); that batched path doesn't fit this protocol's
    one-evaluator-per-call shape, so it's driven directly by the CLI rather
    than through this class.
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
        executor = JudgeExecutor(context.config)
        judge_context = JudgeContext(
            conversation=context.conversation or "",
            output_files_text=context.output_files_text,
            token=context.token,
            extra_meta=context.extra_meta,
        )
        return executor.execute_single(self.config, judge_context)
