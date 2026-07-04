"""Tests for the `type: python` evaluator (issue #66): in-process `module:func`
evaluators, the first consumer of the plugin extension path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.config import Config, ConfigError, RunnerConfig, Task, Variant
from eval.config import Evaluator as EvaluatorConfig
from eval.evaluators import EVALUATOR_REGISTRY, PythonEvaluator
from eval.evaluators.python_eval import PythonEvalError, _load_callable
from eval.protocols import EvalContext
from tests.conftest import load_inline


def _config(tmp_path: Path) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[],
        variants=[],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _write_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, body: str
) -> None:
    (tmp_path / filename).write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))


# --- Registry wiring ---


def test_registry_has_python_type():
    assert EVALUATOR_REGISTRY["python"] is PythonEvaluator


# --- Happy path ---


def test_python_evaluator_calls_function_and_returns_score(tmp_path, monkeypatch):
    _write_module(
        tmp_path,
        monkeypatch,
        "my_eval_mod.py",
        "from eval.protocols import EvalScore\n\n"
        "def my_func(context):\n"
        "    return EvalScore(name='e', type='python', score=1, passed=True, reason='ok')\n",
    )
    ev = EvaluatorConfig(name="e", type="python", script="my_eval_mod:my_func")
    evaluator = PythonEvaluator.from_config(ev)
    context = EvalContext(evaluator=ev, config=_config(tmp_path))

    score = evaluator.evaluate(context)

    assert score is not None
    assert score.name == "e"
    assert score.passed is True
    assert score.reason == "ok"


def test_python_evaluator_receives_full_context(tmp_path, monkeypatch):
    """The function receives the EvalContext as-is (task/variant/log_file/etc),
    unlike `script`, which only sees the log file via env/exit code."""
    _write_module(
        tmp_path,
        monkeypatch,
        "context_check_mod.py",
        "from eval.protocols import EvalScore\n\n"
        "def check(context):\n"
        "    passed = context.task is not None and context.task.name == 't1'\n"
        "    return EvalScore(name='e', type='python', score=int(passed), passed=passed)\n",
    )
    ev = EvaluatorConfig(name="e", type="python", script="context_check_mod:check")
    evaluator = PythonEvaluator.from_config(ev)
    task = Task(name="t1", prompt="p")
    context = EvalContext(evaluator=ev, config=_config(tmp_path), task=task)

    score = evaluator.evaluate(context)

    assert score is not None
    assert score.passed is True


def test_python_evaluator_returns_none_when_func_returns_none(tmp_path, monkeypatch):
    _write_module(
        tmp_path,
        monkeypatch,
        "none_mod.py",
        "def not_applicable(context):\n    return None\n",
    )
    ev = EvaluatorConfig(name="e", type="python", script="none_mod:not_applicable")
    evaluator = PythonEvaluator.from_config(ev)
    context = EvalContext(evaluator=ev, config=_config(tmp_path))

    assert evaluator.evaluate(context) is None


def test_python_evaluator_returns_none_when_script_unset(tmp_path):
    ev = EvaluatorConfig(name="e", type="python", script=None)
    evaluator = PythonEvaluator.from_config(ev)
    context = EvalContext(evaluator=ev, config=_config(tmp_path))

    assert evaluator.evaluate(context) is None


# --- Error handling ---


def test_python_evaluator_raises_on_non_evalscore_return(tmp_path, monkeypatch):
    _write_module(
        tmp_path,
        monkeypatch,
        "bad_return_mod.py",
        "def wrong_type(context):\n    return {'not': 'an EvalScore'}\n",
    )
    ev = EvaluatorConfig(name="e", type="python", script="bad_return_mod:wrong_type")
    evaluator = PythonEvaluator.from_config(ev)
    context = EvalContext(evaluator=ev, config=_config(tmp_path))

    with pytest.raises(PythonEvalError, match="must return an EvalScore or None"):
        evaluator.evaluate(context)


def test_load_callable_missing_colon_raises():
    with pytest.raises(PythonEvalError, match="module:func"):
        _load_callable("no_colon_here")


def test_load_callable_missing_module_raises():
    with pytest.raises(PythonEvalError, match="Failed to import module"):
        _load_callable("totally_nonexistent_module_xyz:func")


def test_load_callable_missing_attribute_raises():
    with pytest.raises(PythonEvalError, match="has no attribute"):
        _load_callable("eval.config:totally_nonexistent_func_xyz")


def test_load_callable_non_callable_raises():
    with pytest.raises(PythonEvalError, match="not a callable value|resolved to a non-callable"):
        _load_callable("eval.config:EVALUATOR_TYPES")


def test_load_callable_resolves_valid_function():
    func = _load_callable("eval.config:_check_duplicate_names")
    assert callable(func)


# --- Config validation ---


def test_config_requires_script_for_python_type(tmp_path):
    with pytest.raises(ConfigError, match="requires a 'script'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [{"name": "e", "type": "python"}],
                    }
                ]
            },
        )


def test_config_requires_module_colon_func_format(tmp_path):
    with pytest.raises(ConfigError, match="module:func"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [{"name": "e", "type": "python", "script": "no_colon_here"}],
                    }
                ]
            },
        )


def test_config_accepts_valid_python_evaluator(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "p",
                    "evaluators": [{"name": "e", "type": "python", "script": "my_module:my_func"}],
                }
            ]
        },
    )
    assert cfg.tasks[0].evaluators[0].type == "python"
    assert cfg.tasks[0].evaluators[0].script == "my_module:my_func"


# --- Inline dispatch (like script/contains/regex, not deferred like judge/metric) ---


def test_python_evaluator_dispatches_inline_via_run_evaluators(tmp_path, monkeypatch):
    from eval import runner as runner_mod

    _write_module(
        tmp_path,
        monkeypatch,
        "inline_dispatch_mod.py",
        "from eval.protocols import EvalScore\n\n"
        "def always_pass(context):\n"
        "    return EvalScore(name='e', type='python', score=1, passed=True)\n",
    )
    config = _config(tmp_path)
    log_file = tmp_path / "run.log"
    log_file.write_text("anything\n")
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            EvaluatorConfig(name="e", type="python", script="inline_dispatch_mod:always_pass")
        ],
    )

    scores = runner_mod._run_evaluators(
        task, variant=Variant(name="v"), config=config, log_file=log_file, token="tok"
    )

    assert len(scores) == 1
    assert scores[0].name == "e"
    assert scores[0].passed is True
