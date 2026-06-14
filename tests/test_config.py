"""Tests for configuration loading, validation, and backward compatibility."""
from __future__ import annotations

import pytest

from eval.config import ConfigError, load_config
from tests.conftest import load_inline


def _base(**runner):
    cfg = {"runner": runner} if runner else {}
    return cfg


# --- Happy path + parsing ---

def test_inline_tasks_and_variants(tmp_path):
    cfg = load_inline(tmp_path, {
        "variants": [{"name": "a"}, {"name": "b", "model": "x"}],
        "tasks": [{
            "name": "t1", "prompt": "do {x}",
            "evaluators": [
                {"name": "j", "type": "judge", "prompt": "rate"},
                {"name": "s", "type": "script", "script": "check.sh"},
                {"name": "c", "type": "contains", "value": "OK"},
                {"name": "r", "type": "regex", "value": r"\d+"},
            ],
        }],
    })
    assert [v.name for v in cfg.variants] == ["a", "b"]
    assert cfg.get_variant("b").model == "x"
    task = cfg.get_task("t1")
    assert len(task.evaluators) == 4
    assert {e.type for e in task.evaluators} == {"judge", "script", "contains", "regex"}


def test_default_variant_when_none(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "hi"}]})
    assert [v.name for v in cfg.variants] == ["baseline"]


# --- Backward compatibility ---

def test_backward_compat_judges_and_verify(tmp_path):
    cfg = load_inline(tmp_path, {
        "tasks": [{
            "name": "t1", "prompt": "p",
            "judges": [{"name": "quality", "prompt": "rate it"}],
            "verify": "verify.sh",
        }],
    })
    task = cfg.get_task("t1")
    by_name = {e.name: e for e in task.evaluators}
    assert by_name["quality"].type == "judge"
    assert by_name["verify"].type == "script"
    assert by_name["verify"].script == "verify.sh"


def test_backward_compat_metrics_judges_and_reset_script(tmp_path):
    cfg = load_inline(tmp_path, {
        "tasks": [{
            "name": "t1", "prompt": "p",
            "metrics": {"judges": [{"name": "q", "prompt": "rate"}]},
            "reset_script": "reset.sh",
        }],
    })
    task = cfg.get_task("t1")
    assert task.evaluators[0].name == "q"
    assert task.hooks.before_run == "reset.sh"


def test_malformed_judges_raises(tmp_path):
    with pytest.raises(ConfigError, match="malformed 'judges'"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p", "judges": [{"name": "x"}]}]})


# --- Evaluator validation ---

def test_evaluator_missing_name(tmp_path):
    with pytest.raises(ConfigError, match="missing a required 'name'"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p",
                   "evaluators": [{"type": "judge", "prompt": "x"}]}]})


def test_evaluator_invalid_type(tmp_path):
    with pytest.raises(ConfigError, match="invalid type 'bogus'"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p",
                   "evaluators": [{"name": "e", "type": "bogus"}]}]})


@pytest.mark.parametrize("ev", [
    {"name": "e", "type": "judge"},
    {"name": "e", "type": "script"},
    {"name": "e", "type": "contains"},
    {"name": "e", "type": "regex"},
])
def test_evaluator_missing_required_field(tmp_path, ev):
    with pytest.raises(ConfigError, match="requires a"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p", "evaluators": [ev]}]})


def test_evaluator_duplicate_name(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate evaluator name 'dup'"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p", "evaluators": [
            {"name": "dup", "type": "judge", "prompt": "a"},
            {"name": "dup", "type": "judge", "prompt": "b"},
        ]}]})


def test_evaluator_invalid_regex(tmp_path):
    with pytest.raises(ConfigError, match="invalid regex"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p",
                   "evaluators": [{"name": "e", "type": "regex", "value": "("}]}]})


# --- Runner validation ---

@pytest.mark.parametrize("runner,msg", [
    ({"parallel": "sometimes"}, "runner.parallel"),
    ({"output_format": "yaml"}, "runner.output_format"),
    ({"epochs": 0}, "runner.epochs"),
    ({"timeout_seconds": 0}, "runner.timeout_seconds"),
    ({"max_workers": 0}, "runner.max_workers"),
    ({"judge_timeout_seconds": 0}, "runner.judge_timeout_seconds"),
    ({"epochs": "two"}, "runner.epochs"),
    ({"max_turns": 0}, "runner.max_turns"),
    ({"output_instruction": 123}, "runner.output_instruction"),
])
def test_runner_validation(tmp_path, runner, msg):
    with pytest.raises(ConfigError, match=msg):
        load_inline(tmp_path, {"runner": runner, "tasks": [{"name": "t1", "prompt": "p"}]})


def test_runner_valid_values(tmp_path):
    cfg = load_inline(tmp_path, {
        "runner": {"parallel": "full", "output_format": "json", "epochs": 3, "max_workers": 4,
                   "judge_timeout_seconds": 120},
        "tasks": [{"name": "t1", "prompt": "p"}],
    })
    assert cfg.runner.parallel == "full"
    assert cfg.runner.epochs == 3
    assert cfg.runner.judge_timeout_seconds == 120


def test_runner_judge_timeout_default(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p"}]})
    assert cfg.runner.judge_timeout_seconds == 60


# --- Output instruction (resolve_prompt) ---

def test_resolve_prompt_default_appends_instruction(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "Do it."}]})
    task, variant = cfg.tasks[0], cfg.variants[0]
    assert cfg.runner.output_instruction == "Save all output files under /workspace/output/."
    assert cfg.resolve_prompt(task, variant) == (
        "Do it.\n\nSave all output files under /workspace/output/."
    )


def test_resolve_prompt_empty_instruction_disables(tmp_path):
    cfg = load_inline(tmp_path, {
        "runner": {"output_instruction": ""},
        "tasks": [{"name": "t1", "prompt": "Do it."}],
    })
    task, variant = cfg.tasks[0], cfg.variants[0]
    assert cfg.resolve_prompt(task, variant) == "Do it."


def test_resolve_prompt_custom_instruction_interpolates_vars(tmp_path):
    cfg = load_inline(tmp_path, {
        "runner": {"output_instruction": "Respond in {language}."},
        "variants": [{"name": "ja", "vars": {"language": "Japanese"}}],
        "tasks": [{"name": "t1", "prompt": "Review {language} code."}],
    })
    task, variant = cfg.tasks[0], cfg.variants[0]
    assert cfg.resolve_prompt(task, variant) == (
        "Review Japanese code.\n\nRespond in Japanese."
    )


# --- Name validation + duplicates ---

def test_duplicate_task_name(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate task name 't1'"):
        load_inline(tmp_path, {"tasks": [
            {"name": "t1", "prompt": "a"}, {"name": "t1", "prompt": "b"},
        ]})


def test_duplicate_variant_name(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate variant name 'v'"):
        load_inline(tmp_path, {
            "variants": [{"name": "v"}, {"name": "v"}],
            "tasks": [{"name": "t1", "prompt": "p"}],
        })


def test_invalid_task_name(tmp_path):
    with pytest.raises(ConfigError, match="invalid"):
        load_inline(tmp_path, {"tasks": [{"name": "bad name!", "prompt": "p"}]})


def test_missing_task_prompt(tmp_path):
    with pytest.raises(ConfigError, match="missing a required 'prompt'"):
        load_inline(tmp_path, {"tasks": [{"name": "t1"}]})


def test_blank_task_prompt(tmp_path):
    with pytest.raises(ConfigError, match="missing a required 'prompt'"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "   "}]})


def test_missing_config_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path)
