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
    cfg = load_inline(
        tmp_path,
        {
            "variants": [{"name": "a"}, {"name": "b", "model": "x"}],
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "do {x}",
                    "evaluators": [
                        {"name": "j", "type": "judge", "prompt": "rate"},
                        {"name": "s", "type": "script", "script": "check.sh"},
                        {"name": "c", "type": "contains", "value": "OK"},
                        {"name": "r", "type": "regex", "value": r"\d+"},
                    ],
                }
            ],
        },
    )
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
    cfg = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "p",
                    "judges": [{"name": "quality", "prompt": "rate it"}],
                    "verify": "verify.sh",
                }
            ],
        },
    )
    task = cfg.get_task("t1")
    by_name = {e.name: e for e in task.evaluators}
    assert by_name["quality"].type == "judge"
    assert by_name["verify"].type == "script"
    assert by_name["verify"].script == "verify.sh"


def test_backward_compat_metrics_judges_and_reset_script(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "p",
                    "metrics": {"judges": [{"name": "q", "prompt": "rate"}]},
                    "reset_script": "reset.sh",
                }
            ],
        },
    )
    task = cfg.get_task("t1")
    assert task.evaluators[0].name == "q"
    assert task.hooks.before_run == "reset.sh"


def test_malformed_judges_raises(tmp_path):
    with pytest.raises(ConfigError, match="malformed 'judges'"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p", "judges": [{"name": "x"}]}]})


# --- Hooks failure policy ---


def test_hooks_on_failure_default_is_fail(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "tasks": [{"name": "t1", "prompt": "p", "hooks": {"before_run": "b.sh"}}],
        },
    )
    assert cfg.get_task("t1").hooks.on_failure == "fail"


def test_hooks_on_failure_warn(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "tasks": [
                {"name": "t1", "prompt": "p", "hooks": {"before_run": "b.sh", "on_failure": "warn"}}
            ],
        },
    )
    assert cfg.get_task("t1").hooks.on_failure == "warn"


def test_hooks_on_failure_invalid_raises(tmp_path):
    with pytest.raises(ConfigError, match="hooks.on_failure"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "hooks": {"before_run": "b.sh", "on_failure": "boom"},
                    }
                ],
            },
        )


# --- Evaluator validation ---


def test_evaluator_missing_name(tmp_path):
    with pytest.raises(ConfigError, match="missing a required 'name'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {"name": "t1", "prompt": "p", "evaluators": [{"type": "judge", "prompt": "x"}]}
                ]
            },
        )


def test_evaluator_invalid_type(tmp_path):
    with pytest.raises(ConfigError, match="invalid type 'bogus'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {"name": "t1", "prompt": "p", "evaluators": [{"name": "e", "type": "bogus"}]}
                ]
            },
        )


@pytest.mark.parametrize(
    "ev",
    [
        {"name": "e", "type": "judge"},
        {"name": "e", "type": "script"},
        {"name": "e", "type": "contains"},
        {"name": "e", "type": "regex"},
    ],
)
def test_evaluator_missing_required_field(tmp_path, ev):
    with pytest.raises(ConfigError, match="requires a"):
        load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p", "evaluators": [ev]}]})


def test_evaluator_duplicate_name(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate evaluator name 'dup'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [
                            {"name": "dup", "type": "judge", "prompt": "a"},
                            {"name": "dup", "type": "judge", "prompt": "b"},
                        ],
                    }
                ]
            },
        )


def test_evaluator_invalid_regex(tmp_path):
    with pytest.raises(ConfigError, match="invalid regex"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [{"name": "e", "type": "regex", "value": "("}],
                    }
                ]
            },
        )


# --- Structured judge rubric ---


def _rubric_task(**ev):
    return {
        "tasks": [
            {"name": "t1", "prompt": "p", "evaluators": [{"name": "j", "type": "judge", **ev}]}
        ]
    }


def test_rubric_composes_prompt(tmp_path):
    cfg = load_inline(
        tmp_path,
        _rubric_task(
            criterion="How thorough is it?",
            rubric={"10": "Complete", "4": "Partial", "1": "Minimal"},
        ),
    )
    ev = cfg.get_task("t1").evaluators[0]
    assert ev.criterion == "How thorough is it?"
    assert ev.rubric == {10: "Complete", 4: "Partial", 1: "Minimal"}
    assert ev.prompt == (
        "How thorough is it?\n\n"
        "Score from 1 to 10 using these anchors:\n"
        "- 10: Complete\n- 4: Partial\n- 1: Minimal"
    )


def test_rubric_accepts_integer_keys_and_sorts_descending(tmp_path):
    cfg = load_inline(
        tmp_path,
        _rubric_task(
            criterion="Rate it.",
            rubric={1: "low", 7: "mid", 10: "high"},
        ),
    )
    ev = cfg.get_task("t1").evaluators[0]
    assert ev.prompt.splitlines()[3:] == ["- 10: high", "- 7: mid", "- 1: low"]


def test_rubric_and_prompt_mutually_exclusive(tmp_path):
    with pytest.raises(ConfigError, match="cannot set both 'prompt' and 'rubric'"):
        load_inline(tmp_path, _rubric_task(prompt="rate", criterion="c", rubric={"10": "good"}))


def test_rubric_requires_criterion(tmp_path):
    with pytest.raises(ConfigError, match="requires a non-empty 'criterion'"):
        load_inline(tmp_path, _rubric_task(rubric={"10": "good"}))


def test_criterion_without_rubric_rejected(tmp_path):
    with pytest.raises(ConfigError, match="sets 'criterion' without a 'rubric'"):
        load_inline(tmp_path, _rubric_task(criterion="c"))


def test_rubric_must_be_non_empty_mapping(tmp_path):
    with pytest.raises(ConfigError, match="non-empty mapping"):
        load_inline(tmp_path, _rubric_task(criterion="c", rubric={}))


def test_rubric_non_integer_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="non-integer score key"):
        load_inline(tmp_path, _rubric_task(criterion="c", rubric={"high": "good"}))


def test_rubric_empty_anchor_rejected(tmp_path):
    with pytest.raises(ConfigError, match="must be a non-empty string"):
        load_inline(tmp_path, _rubric_task(criterion="c", rubric={"10": "  "}))


def test_judge_without_prompt_or_rubric_rejected(tmp_path):
    with pytest.raises(ConfigError, match="requires a 'prompt' or a 'rubric'"):
        load_inline(tmp_path, _rubric_task())


# --- Metric evaluator validation ---


def test_metric_evaluator_parsed(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "tasks": [
                {
                    "name": "t1",
                    "prompt": "p",
                    "evaluators": [
                        {
                            "name": "cost-budget",
                            "type": "metric",
                            "metric": "cost",
                            "op": "<",
                            "value": 0.5,
                        },
                        {
                            "name": "latency",
                            "type": "metric",
                            "metric": "duration",
                            "op": "<=",
                            "value": 60,
                        },
                    ],
                }
            ]
        },
    )
    evs = {e.name: e for e in cfg.get_task("t1").evaluators}
    assert evs["cost-budget"].metric == "cost"
    assert evs["cost-budget"].op == "<"
    assert evs["cost-budget"].threshold == 0.5
    assert evs["latency"].threshold == 60.0


def test_metric_evaluator_missing_metric(tmp_path):
    with pytest.raises(ConfigError, match="requires a 'metric'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [{"name": "e", "type": "metric", "op": "<", "value": 1}],
                    }
                ]
            },
        )


def test_metric_evaluator_invalid_metric(tmp_path):
    with pytest.raises(ConfigError, match="invalid metric 'bogus'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [
                            {
                                "name": "e",
                                "type": "metric",
                                "metric": "bogus",
                                "op": "<",
                                "value": 1,
                            }
                        ],
                    }
                ]
            },
        )


def test_metric_evaluator_missing_op(tmp_path):
    with pytest.raises(ConfigError, match="requires an 'op'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [
                            {"name": "e", "type": "metric", "metric": "cost", "value": 1}
                        ],
                    }
                ]
            },
        )


def test_metric_evaluator_invalid_op(tmp_path):
    with pytest.raises(ConfigError, match="invalid op '=<'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [
                            {
                                "name": "e",
                                "type": "metric",
                                "metric": "cost",
                                "op": "=<",
                                "value": 1,
                            }
                        ],
                    }
                ]
            },
        )


def test_metric_evaluator_missing_value(tmp_path):
    with pytest.raises(ConfigError, match="requires a numeric 'value'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [
                            {"name": "e", "type": "metric", "metric": "cost", "op": "<"}
                        ],
                    }
                ]
            },
        )


def test_metric_evaluator_non_numeric_value(tmp_path):
    with pytest.raises(ConfigError, match="requires a numeric 'value'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {
                        "name": "t1",
                        "prompt": "p",
                        "evaluators": [
                            {
                                "name": "e",
                                "type": "metric",
                                "metric": "cost",
                                "op": "<",
                                "value": "cheap",
                            }
                        ],
                    }
                ]
            },
        )


# --- Runner validation ---


@pytest.mark.parametrize(
    "runner,msg",
    [
        ({"parallel": "sometimes"}, "runner.parallel"),
        ({"output_format": "yaml"}, "runner.output_format"),
        ({"epochs": 0}, "runner.epochs"),
        ({"timeout_seconds": 0}, "runner.timeout_seconds"),
        ({"max_workers": 0}, "runner.max_workers"),
        ({"judge_timeout_seconds": 0}, "runner.judge_timeout_seconds"),
        ({"judge_samples": 0}, "runner.judge_samples"),
        ({"judge_aggregate": "mode"}, "runner.judge_aggregate"),
        ({"judge_max_conversation_chars": 0}, "runner.judge_max_conversation_chars"),
        ({"judge_max_output_chars": 0}, "runner.judge_max_output_chars"),
        ({"epochs": "two"}, "runner.epochs"),
        ({"max_turns": 0}, "runner.max_turns"),
        ({"variant_order": "shuffle"}, "runner.variant_order"),
        ({"seed": "abc"}, "runner.seed"),
        ({"output_instruction": 123}, "runner.output_instruction"),
    ],
)
def test_runner_validation(tmp_path, runner, msg):
    with pytest.raises(ConfigError, match=msg):
        load_inline(tmp_path, {"runner": runner, "tasks": [{"name": "t1", "prompt": "p"}]})


def test_runner_valid_values(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "runner": {
                "parallel": "full",
                "output_format": "json",
                "epochs": 3,
                "max_workers": 4,
                "judge_timeout_seconds": 120,
                "variant_order": "counterbalance",
                "seed": 7,
            },
            "tasks": [{"name": "t1", "prompt": "p"}],
        },
    )
    assert cfg.runner.parallel == "full"
    assert cfg.runner.epochs == 3
    assert cfg.runner.judge_timeout_seconds == 120
    assert cfg.runner.variant_order == "counterbalance"
    assert cfg.runner.seed == 7


def test_runner_variant_order_default(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p"}]})
    assert cfg.runner.variant_order == "fixed"
    assert cfg.runner.seed is None


def test_runner_judge_timeout_default(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p"}]})
    assert cfg.runner.judge_timeout_seconds == 60


def test_runner_judge_sampling_defaults_and_values(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p"}]})
    assert cfg.runner.judge_samples == 1
    assert cfg.runner.judge_aggregate == "median"
    cfg = load_inline(
        tmp_path,
        {
            "runner": {"judge_samples": 5, "judge_aggregate": "majority"},
            "tasks": [{"name": "t1", "prompt": "p"}],
        },
    )
    assert cfg.runner.judge_samples == 5
    assert cfg.runner.judge_aggregate == "majority"


def test_runner_judge_context_defaults(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "p"}]})
    assert cfg.runner.judge_max_conversation_chars == 8000
    assert cfg.runner.judge_max_output_chars == 8000
    assert cfg.runner.judge_copilot_version is None


def test_runner_judge_context_overrides(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "runner": {
                "judge_max_conversation_chars": 20000,
                "judge_max_output_chars": 15000,
                "judge_copilot_version": "copilot/1.0.18",
            },
            "tasks": [{"name": "t1", "prompt": "p"}],
        },
    )
    assert cfg.runner.judge_max_conversation_chars == 20000
    assert cfg.runner.judge_max_output_chars == 15000
    assert cfg.runner.judge_copilot_version == "copilot/1.0.18"


# --- Output instruction (resolve_prompt) ---


def test_resolve_prompt_default_appends_instruction(tmp_path):
    cfg = load_inline(tmp_path, {"tasks": [{"name": "t1", "prompt": "Do it."}]})
    task, variant = cfg.tasks[0], cfg.variants[0]
    assert cfg.runner.output_instruction == "Save all output files under /workspace/output/."
    assert cfg.resolve_prompt(task, variant) == (
        "Do it.\n\nSave all output files under /workspace/output/."
    )


def test_resolve_prompt_empty_instruction_disables(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "runner": {"output_instruction": ""},
            "tasks": [{"name": "t1", "prompt": "Do it."}],
        },
    )
    task, variant = cfg.tasks[0], cfg.variants[0]
    assert cfg.resolve_prompt(task, variant) == "Do it."


def test_resolve_prompt_null_instruction_uses_default(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "runner": {"output_instruction": None},
            "tasks": [{"name": "t1", "prompt": "Do it."}],
        },
    )
    assert cfg.runner.output_instruction == "Save all output files under /workspace/output/."


def test_resolve_prompt_custom_instruction_interpolates_vars(tmp_path):
    cfg = load_inline(
        tmp_path,
        {
            "runner": {"output_instruction": "Respond in {language}."},
            "variants": [{"name": "ja", "vars": {"language": "Japanese"}}],
            "tasks": [{"name": "t1", "prompt": "Review {language} code."}],
        },
    )
    task, variant = cfg.tasks[0], cfg.variants[0]
    assert cfg.resolve_prompt(task, variant) == ("Review Japanese code.\n\nRespond in Japanese.")


# --- Name validation + duplicates ---


def test_duplicate_task_name(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate task name 't1'"):
        load_inline(
            tmp_path,
            {
                "tasks": [
                    {"name": "t1", "prompt": "a"},
                    {"name": "t1", "prompt": "b"},
                ]
            },
        )


def test_duplicate_variant_name(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate variant name 'v'"):
        load_inline(
            tmp_path,
            {
                "variants": [{"name": "v"}, {"name": "v"}],
                "tasks": [{"name": "t1", "prompt": "p"}],
            },
        )


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
