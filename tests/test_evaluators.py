"""Tests for the Evaluator protocol, EVALUATOR_REGISTRY, and plugin discovery.

Covers the extensibility story documented in ``eval/evaluators/__init__.py``:
a third-party evaluator type registered in ``EVALUATOR_REGISTRY`` (whether by
hand or via the ``copilot_eval.evaluators`` entry-point group) must (1) pass
``eval.config`` validation and (2) actually get dispatched by
``eval.runner._run_evaluators``, without any change to either module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.config import Config, RunnerConfig, Task, Variant
from eval.config import Evaluator as EvaluatorConfig
from eval.evaluators import EVALUATOR_REGISTRY
from eval.protocols import EvalContext, EvalScore
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


# --- EVALUATOR_REGISTRY ---


def test_registry_has_all_built_in_types():
    assert set(EVALUATOR_REGISTRY) == {"judge", "script", "contains", "regex", "metric"}


# --- Third-party evaluator type: registry dispatch end-to-end ---


class _AlwaysPassEvaluator:
    """Minimal third-party Evaluator implementation used only by this test."""

    evaluator_type = "always_pass"

    def __init__(self, config: EvaluatorConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> _AlwaysPassEvaluator:
        return cls(config)

    def evaluate(self, context: EvalContext) -> EvalScore | None:
        return EvalScore(name=self.name, type=self.evaluator_type, score=1, passed=True)


@pytest.fixture
def custom_evaluator_type(monkeypatch):
    """Register a fake third-party evaluator type for the duration of a test."""
    monkeypatch.setitem(EVALUATOR_REGISTRY, "always_pass", _AlwaysPassEvaluator)
    yield "always_pass"


def test_custom_evaluator_type_passes_config_validation(tmp_path, custom_evaluator_type):
    """A type registered in EVALUATOR_REGISTRY (not in the static built-in list)
    must validate in eval-config.yaml without any eval.config change."""
    cfg = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "p",
                    "evaluators": [{"name": "e", "type": custom_evaluator_type}],
                }
            ]
        },
    )
    assert cfg.tasks[0].evaluators[0].type == "always_pass"


def test_custom_evaluator_type_dispatches_via_run_evaluators(tmp_path, custom_evaluator_type):
    """A registered non-judge/metric type runs inline via _run_evaluators, just
    like the built-in script/contains/regex types."""
    from eval import runner as runner_mod

    config = _config(tmp_path)
    log_file = tmp_path / "run.log"
    log_file.write_text("anything\n")
    task = Task(
        name="t",
        prompt="p",
        evaluators=[EvaluatorConfig(name="e", type=custom_evaluator_type)],
    )

    scores = runner_mod._run_evaluators(
        task, variant=Variant(name="v"), config=config, log_file=log_file, token="tok"
    )

    assert len(scores) == 1
    assert scores[0].name == "e"
    assert scores[0].passed is True


def test_unknown_evaluator_type_is_skipped_not_crashed(tmp_path):
    """If ev.type somehow isn't in the registry (e.g. a plugin type whose entry
    point failed to load after config was parsed), dispatch skips it instead of
    raising, matching the previous _eval_* dispatch's tolerant behavior."""
    from eval import runner as runner_mod

    config = _config(tmp_path)
    log_file = tmp_path / "run.log"
    log_file.write_text("anything\n")
    task = Task(
        name="t",
        prompt="p",
        evaluators=[EvaluatorConfig(name="e", type="totally_unregistered")],
    )

    scores = runner_mod._run_evaluators(
        task, variant=Variant(name="v"), config=config, log_file=log_file, token="tok"
    )

    assert scores == []


# --- Entry-point plugin discovery ---


class _FakeEntryPoint:
    def __init__(self, name: str, cls: type) -> None:
        self.name = name
        self._cls = cls

    def load(self) -> type:
        return self._cls


def test_load_evaluator_plugins_registers_entry_points(monkeypatch):
    """load_evaluator_plugins() discovers a fake entry point and adds it to
    EVALUATOR_REGISTRY, exercising the mechanism issue #66 depends on."""
    import eval.evaluators as evaluators_mod

    monkeypatch.setattr(evaluators_mod, "_plugins_loaded", False)
    monkeypatch.delitem(EVALUATOR_REGISTRY, "plugin_type", raising=False)

    fake_ep = _FakeEntryPoint("plugin_type", _AlwaysPassEvaluator)

    def fake_entry_points(*, group: str):
        assert group == evaluators_mod.ENTRY_POINT_GROUP
        return [fake_ep]

    monkeypatch.setattr(evaluators_mod.importlib_metadata, "entry_points", fake_entry_points)

    evaluators_mod.load_evaluator_plugins()

    assert EVALUATOR_REGISTRY["plugin_type"] is _AlwaysPassEvaluator
    del EVALUATOR_REGISTRY["plugin_type"]


def test_load_evaluator_plugins_is_idempotent(monkeypatch):
    """A second call is a no-op (doesn't re-scan entry points)."""
    import eval.evaluators as evaluators_mod

    monkeypatch.setattr(evaluators_mod, "_plugins_loaded", True)
    calls = []

    def fake_entry_points(*, group: str):
        calls.append(group)
        return []

    monkeypatch.setattr(evaluators_mod.importlib_metadata, "entry_points", fake_entry_points)

    evaluators_mod.load_evaluator_plugins()

    assert calls == []


def test_load_evaluator_plugins_skips_broken_plugin(monkeypatch, caplog):
    """A plugin whose entry point fails to load is logged and skipped, not
    fatal to CLI startup."""
    import eval.evaluators as evaluators_mod

    monkeypatch.setattr(evaluators_mod, "_plugins_loaded", False)

    class _BrokenEntryPoint:
        name = "broken"

        def load(self) -> type:
            raise ImportError("boom")

    def fake_entry_points(*, group: str):
        return [_BrokenEntryPoint()]

    monkeypatch.setattr(evaluators_mod.importlib_metadata, "entry_points", fake_entry_points)

    evaluators_mod.load_evaluator_plugins()  # must not raise

    assert "broken" not in EVALUATOR_REGISTRY
