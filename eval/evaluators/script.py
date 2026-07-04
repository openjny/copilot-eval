"""Script evaluator: runs an external script; PASS iff it exits 0."""

from __future__ import annotations

import os
import subprocess
from logging import getLogger

from eval.config import Evaluator as EvaluatorConfig
from eval.env_utils import load_env_file
from eval.protocols import EvalContext, EvalScore

logger = getLogger(__name__)


class ScriptEvaluator:
    """Runs ``evaluator.script`` with ``EVAL_*`` vars in its environment.

    The script's stdout/stderr is appended to the run's log file so it's
    visible alongside the Copilot transcript. Runs inline during ``run_one``.
    """

    evaluator_type = "script"

    def __init__(self, config: EvaluatorConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> ScriptEvaluator:
        return cls(config)

    def evaluate(self, context: EvalContext) -> EvalScore | None:
        ev = self.config
        if not ev.script:
            return None
        if context.task is None or context.variant is None or context.log_file is None:
            return None
        config = context.config
        resolved = (config.config_dir / ev.script).resolve()
        if not resolved.exists():
            resolved = (config.project_dir / ev.script).resolve()
        if not resolved.exists():
            return None
        logger.info("Evaluating: %s (script)...", ev.name)
        merged_vars = config.resolve_vars(context.task, context.variant)
        env = {
            **os.environ,
            **load_env_file(config.env_file),
            **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()},
        }
        with open(context.log_file, "a") as lf:
            proc = subprocess.run([str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)
        passed = proc.returncode == 0
        return EvalScore(
            name=ev.name,
            type="script",
            score=1 if passed else 0,
            reason="PASS" if passed else "FAIL",
            passed=passed,
        )
