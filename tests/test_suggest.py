"""Tests for `suggest-evaluators` (issue #93): the meta-prompt, response parsing,
coverage guarantees, YAML serialization, and the CLI — with the judge model call
mocked. Also asserts the generated task file passes `copilot-eval validate`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from eval.cli import main
from eval.config import Config, Evaluator, RunnerConfig
from eval.exceptions import JudgeParseError
from eval.services import suggest_service as svc

# A realistic judge response: two judge rubrics + regex + contains + metric.
_GOOD_RESPONSE = json.dumps(
    {
        "evaluators": [
            {
                "name": "vulnerability-coverage",
                "type": "judge",
                "criterion": "Rate how thoroughly the review covers OWASP Top 10 risks.",
                "rubric": {
                    "1": "Misses critical vulnerability classes.",
                    "5": "Covers some but not all relevant categories.",
                    "10": "Exhaustive, with remediation suggestions.",
                },
            },
            {
                "name": "has-line-references",
                "type": "regex",
                "value": r"line \d+",
            },
            {
                "name": "mentions-sql-injection",
                "type": "contains",
                "value": "SQL injection",
            },
            {
                "name": "cost-gate",
                "type": "metric",
                "metric": "cost",
                "op": "<=",
                "value": 0.10,
            },
        ]
    }
)


class _FakeExecutor:
    """Stand-in for JudgeExecutor.complete: returns a canned response."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt: str | None = None

    def complete(self, prompt: str, token: str | None) -> str:
        self.last_prompt = prompt
        return self.response


def _config(tmp_path: Path) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[],
        variants=[],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


# --- meta-prompt ----------------------------------------------------------


def test_build_meta_prompt_prompt_only_omits_evidence_sections():
    prompt = svc.build_meta_prompt("Do the thing")
    assert "Do the thing" in prompt
    assert "FIXTURE" not in prompt
    assert "SAMPLE OUTPUT" not in prompt
    assert "evaluators" in prompt  # the JSON output contract


def test_build_meta_prompt_includes_fixture_and_samples():
    prompt = svc.build_meta_prompt("Task", "some fixture text", ["a sample output"])
    assert "FIXTURE" in prompt
    assert "some fixture text" in prompt
    assert "SAMPLE OUTPUT 1" in prompt
    assert "a sample output" in prompt


# --- parsing / normalization ----------------------------------------------


def test_parse_suggestions_extracts_all_types():
    parsed = svc.parse_suggestions(_GOOD_RESPONSE)
    types = {e["type"] for e in parsed}
    assert {"judge", "regex", "contains", "metric"} <= types


def test_parse_suggestions_handles_code_fence_and_prose():
    noisy = "Here you go:\n```json\n" + _GOOD_RESPONSE + "\n```\nDone."
    parsed = svc.parse_suggestions(noisy)
    assert any(e["type"] == "judge" for e in parsed)


def test_parse_suggestions_coerces_range_rubric_keys():
    resp = json.dumps(
        {
            "evaluators": [
                {
                    "name": "quality",
                    "type": "judge",
                    "criterion": "Rate quality.",
                    "rubric": {"1-3": "bad", "7-9": "good", "10": "great"},
                }
            ]
        }
    )
    parsed = svc.parse_suggestions(resp)
    rubric = parsed[0]["rubric"]
    assert set(rubric.keys()) == {1, 7, 10}


def test_parse_suggestions_drops_unsupported_and_duplicate_names():
    resp = json.dumps(
        {
            "evaluators": [
                {"name": "a", "type": "script", "script": "check.sh"},
                {"name": "b", "type": "regex", "value": "x"},
                {"name": "b", "type": "regex", "value": "y"},  # duplicate name
            ]
        }
    )
    parsed = svc.parse_suggestions(resp)
    names = [e["name"] for e in parsed]
    assert names == ["b"]  # script dropped, duplicate dropped


def test_parse_suggestions_raises_without_evaluators():
    with pytest.raises(JudgeParseError):
        svc.parse_suggestions("no json here at all")


# --- validation + coverage ------------------------------------------------


def test_validate_evaluators_drops_invalid_keeps_valid():
    raw = [
        {"name": "good", "type": "regex", "value": "ok"},
        {"name": "bad", "type": "metric", "metric": "not_a_metric", "op": "<=", "value": 1},
    ]
    valid = svc._validate_evaluators(raw)
    assert [e.name for e in valid] == ["good"]


def test_ensure_coverage_adds_judge_when_missing():
    only_regex = [Evaluator(name="r", type="regex", value="x")]
    covered = svc.ensure_coverage(only_regex)
    assert any(e.type == "judge" for e in covered)
    assert any(e.type in ("regex", "contains", "metric") for e in covered)


def test_ensure_coverage_adds_deterministic_when_missing():
    only_judge = [
        Evaluator(name="j", type="judge", criterion="Rate.", rubric={1: "bad", 10: "good"})
    ]
    covered = svc.ensure_coverage(only_judge)
    assert any(e.type == "judge" for e in covered)
    assert any(e.type in ("regex", "contains", "metric") for e in covered)


# --- orchestration --------------------------------------------------------


def test_suggest_evaluators_writes_yaml_with_both_kinds(tmp_path: Path):
    out = tmp_path / "tasks" / "security-review.yaml"
    result = svc.suggest_evaluators(
        task_prompt="Review this PR for security vulnerabilities",
        task_name="security-review",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        executor=_FakeExecutor(_GOOD_RESPONSE),
    )
    assert out.is_file()
    doc = yaml.safe_load(out.read_text())
    assert doc["name"] == "security-review"
    assert doc["prompt"].strip().startswith("Review this PR")
    types = {e["type"] for e in doc["evaluators"]}
    assert "judge" in types
    assert types & {"regex", "contains", "metric"}  # at least one deterministic
    # A judge evaluator uses the structured rubric form with integer anchors.
    judge = next(e for e in doc["evaluators"] if e["type"] == "judge")
    assert "criterion" in judge
    assert all(isinstance(k, int) for k in judge["rubric"])
    assert result.prompt_only is True


def test_suggest_evaluators_prompt_only_flag(tmp_path: Path):
    out = tmp_path / "t.yaml"
    result = svc.suggest_evaluators(
        task_prompt="Task",
        task_name="t",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        executor=_FakeExecutor(_GOOD_RESPONSE),
    )
    assert result.prompt_only is True


def test_suggest_evaluators_with_sample_outputs_not_prompt_only(tmp_path: Path):
    sample = tmp_path / "sample.md"
    sample.write_text("Found a SQL injection on line 42")
    out = tmp_path / "t.yaml"
    fake = _FakeExecutor(_GOOD_RESPONSE)
    result = svc.suggest_evaluators(
        task_prompt="Task",
        task_name="t",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        sample_output_paths=[sample],
        executor=fake,
    )
    assert result.prompt_only is False
    assert "SAMPLE OUTPUT 1" in (fake.last_prompt or "")
    assert "line 42" in (fake.last_prompt or "")


def test_suggest_evaluators_coverage_fallback_on_sparse_response(tmp_path: Path):
    # Model returns only a single regex — service must still emit a judge rubric.
    sparse = json.dumps({"evaluators": [{"name": "only-regex", "type": "regex", "value": "x"}]})
    out = tmp_path / "t.yaml"
    result = svc.suggest_evaluators(
        task_prompt="Task",
        task_name="t",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        executor=_FakeExecutor(sparse),
    )
    types = {e.type for e in result.evaluators}
    assert "judge" in types
    assert types & {"regex", "contains", "metric"}


# --- generated YAML passes `validate` -------------------------------------


def _scaffold_project(config_dir: Path) -> None:
    (config_dir / "eval-config.yaml").write_text("vars: {}\nrunner: {}\n")
    (config_dir / "tasks").mkdir(parents=True, exist_ok=True)


def test_generated_yaml_passes_validate(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    _scaffold_project(project)

    out = project / "tasks" / "security-review.yaml"
    svc.suggest_evaluators(
        task_prompt="Review this PR for security vulnerabilities",
        task_name="security-review",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        executor=_FakeExecutor(_GOOD_RESPONSE),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(project)])
    assert result.exit_code == 0, result.output


def test_generated_yaml_with_fallbacks_passes_validate(tmp_path: Path):
    # Even when the model returns nothing usable, the defaults must validate.
    project = tmp_path / "proj"
    project.mkdir()
    _scaffold_project(project)

    out = project / "tasks" / "generated.yaml"
    svc.suggest_evaluators(
        task_prompt="Do something useful",
        task_name="generated",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        executor=_FakeExecutor(json.dumps({"evaluators": []})),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config-dir", str(project)])
    assert result.exit_code == 0, result.output


# --- CLI ------------------------------------------------------------------


def test_cli_suggest_evaluators_writes_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        "eval.judge_executor.JudgeExecutor.complete",
        lambda self, prompt, token: _GOOD_RESPONSE,
    )
    out = tmp_path / "tasks" / "sec.yaml"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "suggest-evaluators",
            "--task-prompt",
            "Review this PR for security vulnerabilities",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert "judge" in result.output
    doc = yaml.safe_load(out.read_text())
    assert doc["name"] == "sec"
    assert any(e["type"] == "judge" for e in doc["evaluators"])


def test_cli_dry_run_prints_prompt_without_calling_model(tmp_path: Path, monkeypatch):
    def _boom(self, prompt, token):  # pragma: no cover - must not be called
        raise AssertionError("model should not be called in --dry-run")

    monkeypatch.setattr("eval.judge_executor.JudgeExecutor.complete", _boom)
    out = tmp_path / "t.yaml"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["suggest-evaluators", "--task-prompt", "Do the thing", "--output", str(out), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "Do the thing" in result.output
    assert not out.exists()


def test_cli_requires_exactly_one_prompt_source(tmp_path: Path):
    out = tmp_path / "t.yaml"
    runner = CliRunner()
    result = runner.invoke(main, ["suggest-evaluators", "--output", str(out)])
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower()


def test_cli_reads_prompt_from_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        "eval.judge_executor.JudgeExecutor.complete",
        lambda self, prompt, token: _GOOD_RESPONSE,
    )
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Task prompt from a file")
    out = tmp_path / "t.yaml"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "suggest-evaluators",
            "--task-prompt-file",
            str(prompt_file),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(out.read_text())
    assert "Task prompt from a file" in doc["prompt"]


# --- council fixes --------------------------------------------------------


def test_slugify_task_name_coerces_invalid_names():
    assert svc.slugify_task_name("Security Review") == "Security-Review"
    assert svc.slugify_task_name("  spaced  out  ") == "spaced-out"
    assert svc.slugify_task_name("--weird--") == "weird"
    assert svc.slugify_task_name("!!!") == "suggested-task"
    assert svc.slugify_task_name("") == "suggested-task"


def test_suggest_evaluators_slugifies_name_and_passes_validate(tmp_path: Path):
    # A human-friendly --task-name with spaces would otherwise fail validate.
    project = tmp_path / "proj"
    project.mkdir()
    _scaffold_project(project)

    out = project / "tasks" / "review.yaml"
    result = svc.suggest_evaluators(
        task_prompt="Review this PR",
        task_name="Security Review",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        executor=_FakeExecutor(_GOOD_RESPONSE),
    )
    assert result.task_name == "Security-Review"
    doc = yaml.safe_load(out.read_text())
    assert doc["name"] == "Security-Review"

    runner = CliRunner()
    validated = runner.invoke(main, ["validate", "--config-dir", str(project)])
    assert validated.exit_code == 0, validated.output


def test_ensure_coverage_dedupes_default_judge_name():
    # Model already used "overall-quality" (the default judge name) on a regex,
    # and there is no judge — the fallback judge must not collide.
    evs = [Evaluator(name="overall-quality", type="regex", value="x")]
    covered = svc.ensure_coverage(evs)
    names = [e.name for e in covered]
    assert len(names) == len(set(names)), names
    assert any(e.type == "judge" for e in covered)


def test_ensure_coverage_dedupes_default_gate_name():
    # Model used "cost-gate" on a judge, and there is no deterministic anchor —
    # the fallback metric gate must not collide.
    evs = [
        Evaluator(name="cost-gate", type="judge", criterion="Rate.", rubric={1: "no", 10: "yes"})
    ]
    covered = svc.ensure_coverage(evs)
    names = [e.name for e in covered]
    assert len(names) == len(set(names)), names
    assert any(e.type in ("regex", "contains", "metric") for e in covered)


def test_name_collision_response_passes_validate(tmp_path: Path):
    # Model returns a single regex literally named "overall-quality"; the added
    # judge fallback must be renamed so the generated file still validates.
    resp = json.dumps({"evaluators": [{"name": "overall-quality", "type": "regex", "value": "ok"}]})
    project = tmp_path / "proj"
    project.mkdir()
    _scaffold_project(project)
    out = project / "tasks" / "gen.yaml"
    result = svc.suggest_evaluators(
        task_prompt="Task",
        task_name="gen",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        executor=_FakeExecutor(resp),
    )
    names = [e.name for e in result.evaluators]
    assert len(names) == len(set(names)), names
    runner = CliRunner()
    validated = runner.invoke(main, ["validate", "--config-dir", str(project)])
    assert validated.exit_code == 0, validated.output


def test_validate_evaluators_drops_invalid_regex():
    raw = [
        {"name": "good", "type": "contains", "value": "ok"},
        {"name": "bad-regex", "type": "regex", "value": "([unclosed"},
    ]
    valid = svc._validate_evaluators(raw)
    assert [e.name for e in valid] == ["good"]


def test_parse_suggestions_handles_bare_top_level_array():
    resp = json.dumps(
        [
            {"name": "r", "type": "regex", "value": "x"},
            {
                "name": "j",
                "type": "judge",
                "criterion": "Rate.",
                "rubric": {"1": "no", "10": "yes"},
            },
        ]
    )
    parsed = svc.parse_suggestions(resp)
    types = {e["type"] for e in parsed}
    assert {"regex", "judge"} <= types


def test_parse_suggestions_handles_fenced_top_level_array():
    inner = json.dumps([{"name": "r", "type": "regex", "value": "x"}])
    resp = "Sure:\n```json\n" + inner + "\n```"
    parsed = svc.parse_suggestions(resp)
    assert parsed[0]["type"] == "regex"


def test_suggest_evaluators_emits_fixture_field(tmp_path: Path):
    fixture = tmp_path / "my-fixture"
    fixture.mkdir()
    (fixture / "input.md").write_text("some input")
    out = tmp_path / "tasks" / "t.yaml"
    svc.suggest_evaluators(
        task_prompt="Task",
        task_name="t",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        fixture_dir=fixture,
        executor=_FakeExecutor(_GOOD_RESPONSE),
    )
    doc = yaml.safe_load(out.read_text())
    assert doc["fixture"] == "my-fixture"


def test_summarize_fixture_skips_hidden_files(tmp_path: Path):
    fixture = tmp_path / "fx"
    fixture.mkdir()
    (fixture / "visible.md").write_text("visible content")
    (fixture / ".secret").write_text("hidden content")
    summary = svc.summarize_fixture(fixture)
    assert "visible.md" in summary
    assert ".secret" not in summary


def test_suggest_evaluators_masks_secrets_in_prompt(tmp_path: Path):
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sample = tmp_path / "sample.md"
    sample.write_text(f"the token is {secret} do not leak it")
    out = tmp_path / "t.yaml"
    fake = _FakeExecutor(_GOOD_RESPONSE)
    svc.suggest_evaluators(
        task_prompt="Task",
        task_name="t",
        output_path=out,
        config=_config(tmp_path),
        token=secret,
        sample_output_paths=[sample],
        executor=fake,
    )
    assert secret not in (fake.last_prompt or "")
    assert "REDACTED" in (fake.last_prompt or "")


def test_suggest_evaluators_empty_sample_is_prompt_only(tmp_path: Path):
    sample = tmp_path / "empty.md"
    sample.write_text("   \n\t  \n")
    out = tmp_path / "t.yaml"
    result = svc.suggest_evaluators(
        task_prompt="Task",
        task_name="t",
        output_path=out,
        config=_config(tmp_path),
        token=None,
        sample_output_paths=[sample],
        executor=_FakeExecutor(_GOOD_RESPONSE),
    )
    assert result.prompt_only is True
