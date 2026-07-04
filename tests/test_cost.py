"""Tests for cost governance (issue #70): pre-flight cost estimate, budget cap,
and judge-cost tracking.

Covers:
- ``eval.config``: ``runner.budget_limit`` parsing/validation.
- ``eval.services.cost_service``: judge-call counting, historical-cost
  averaging from persisted file-collector traces, the estimate calculation,
  and the human-readable report.
- ``eval.services.orchestrator.run_command``: the budget-cap abort and the
  ``--estimate``/``--yes`` confirmation gate.
- ``eval.judge_executor``: per-evaluator judge token tracking (CLI-reported
  usage when present, chars/4 fallback estimate otherwise).
- ``eval.services.manifest``: cost summary persisted in the run manifest.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click
import pytest

from eval.config import Config, ConfigError, Evaluator, RunnerConfig, Task, Variant
from eval.judge_executor import JudgeExecutor
from eval.protocols import RunStatus
from eval.runner import RunResult, run_judge
from eval.services import cost_service, orchestrator
from eval.services.cost_service import (
    CostEstimate,
    estimate_run_cost,
    format_cost_report,
    judge_calls_per_cell,
    load_historical_costs,
)
from eval.services.manifest import load_manifest

from .conftest import load_inline

FIXTURE = Path(__file__).parent / "fixtures" / "file-exporter-sample.jsonl"

# --- eval.config: runner.budget_limit ---------------------------------------


def test_budget_limit_defaults_to_none(tmp_path: Path):
    config = load_inline(tmp_path, {"runner": {}, "tasks": [], "variants": []})
    assert config.runner.budget_limit is None


def test_budget_limit_parses_float(tmp_path: Path):
    config = load_inline(tmp_path, {"runner": {"budget_limit": 10.5}, "tasks": [], "variants": []})
    assert config.runner.budget_limit == 10.5


def test_budget_limit_rejects_non_number(tmp_path: Path):
    with pytest.raises(ConfigError, match="budget_limit"):
        load_inline(tmp_path, {"runner": {"budget_limit": "lots"}, "tasks": [], "variants": []})


def test_budget_limit_rejects_negative(tmp_path: Path):
    with pytest.raises(ConfigError, match="budget_limit"):
        load_inline(tmp_path, {"runner": {"budget_limit": -1}, "tasks": [], "variants": []})


# --- eval.services.cost_service: judge_calls_per_cell -----------------------


def _task(name: str = "t", evaluators: list[Evaluator] | None = None) -> Task:
    return Task(name=name, prompt="do it", evaluators=evaluators or [])


def _judge_ev(name: str) -> Evaluator:
    return Evaluator(name=name, type="judge", prompt="rate it")


def test_judge_calls_per_cell_no_judges():
    assert judge_calls_per_cell(_task(), RunnerConfig()) == 0


def test_judge_calls_per_cell_single_judge_single_sample():
    task = _task(evaluators=[_judge_ev("a")])
    assert judge_calls_per_cell(task, RunnerConfig(judge_samples=1)) == 1


def test_judge_calls_per_cell_multiple_judges_and_samples():
    task = _task(evaluators=[_judge_ev("a"), _judge_ev("b"), _judge_ev("c")])
    assert judge_calls_per_cell(task, RunnerConfig(judge_samples=3)) == 9


def test_judge_calls_per_cell_batched_collapses_to_samples():
    task = _task(evaluators=[_judge_ev("a"), _judge_ev("b"), _judge_ev("c")])
    runner = RunnerConfig(judge_samples=3, judge_batch=True)
    assert judge_calls_per_cell(task, runner) == 3


def test_judge_calls_per_cell_batched_single_judge_not_collapsed():
    # judge_batch only matters with >1 judge evaluator (a single one delegates
    # to the non-batched path, see JudgeExecutor.execute_batch).
    task = _task(evaluators=[_judge_ev("a")])
    runner = RunnerConfig(judge_samples=2, judge_batch=True)
    assert judge_calls_per_cell(task, runner) == 2


def test_judge_calls_per_cell_ignores_non_judge_evaluators():
    task = _task(
        evaluators=[
            _judge_ev("a"),
            Evaluator(name="b", type="contains", value="x"),
        ]
    )
    assert judge_calls_per_cell(task, RunnerConfig(judge_samples=1)) == 1


# --- eval.services.cost_service: load_historical_costs ----------------------


def test_load_historical_costs_no_results_dir(tmp_path: Path):
    assert load_historical_costs(tmp_path / "results") is None


def test_load_historical_costs_no_trace_files(tmp_path: Path):
    run_dir = tmp_path / "results" / "20240101-000000-aaaaaa"
    run_dir.mkdir(parents=True)
    assert load_historical_costs(tmp_path / "results") is None


def _write_run_trace(run_dir: Path, name: str = "cell.jsonl") -> None:
    traces_dir = run_dir / ".traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, traces_dir / name)


def test_load_historical_costs_averages_past_runs(tmp_path: Path):
    results_dir = tmp_path / "results"
    run_dir = results_dir / "run-1"
    run_dir.mkdir(parents=True)
    _write_run_trace(run_dir)

    history = load_historical_costs(results_dir)

    assert history is not None
    assert history.sample_size == 1
    assert history.avg_input_tokens_per_cell == pytest.approx(31416)
    assert history.avg_output_tokens_per_cell == pytest.approx(4)


def test_load_historical_costs_averages_across_multiple_cells(tmp_path: Path):
    results_dir = tmp_path / "results"
    run_dir = results_dir / "run-1"
    run_dir.mkdir(parents=True)
    _write_run_trace(run_dir, "cell-1.jsonl")
    _write_run_trace(run_dir, "cell-2.jsonl")

    history = load_historical_costs(results_dir)

    assert history is not None
    assert history.sample_size == 2
    # Both cells are copies of the same fixture, so the average equals the
    # per-cell value.
    assert history.avg_input_tokens_per_cell == pytest.approx(31416)


def test_load_historical_costs_respects_max_runs(tmp_path: Path):
    import os
    import time

    results_dir = tmp_path / "results"
    for i in range(3):
        run_dir = results_dir / f"run-{i}"
        run_dir.mkdir(parents=True)
        _write_run_trace(run_dir)
        # Ensure distinct mtimes so "most recent" ordering is deterministic.
        os.utime(run_dir, (time.time() + i, time.time() + i))

    history = load_historical_costs(results_dir, max_runs=2)

    assert history is not None
    assert history.sample_size == 2


# --- eval.services.cost_service: estimate_run_cost --------------------------


def _config(tmp_path: Path, tasks: list[Task], variants: list[Variant], **runner_kw) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(**runner_kw),
        tasks=tasks,
        variants=variants,
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def test_estimate_run_cost_counts_cells_and_judge_calls(tmp_path: Path):
    task = _task(evaluators=[_judge_ev("a"), _judge_ev("b")])
    variants = [Variant(name="baseline"), Variant(name="experimental")]
    config = _config(tmp_path, [task], variants, judge_samples=1)

    estimate = estimate_run_cost(config, [task], variants, epochs=3)

    # 1 task x 1 fixture x 3 epochs x 2 variants
    assert estimate.cells == 6
    # 2 judge evaluators x 1 sample x 6 cells
    assert estimate.judge_calls == 12
    assert estimate.based_on_history is False
    assert estimate.history_sample_size == 0


def test_estimate_run_cost_uses_defaults_without_history(tmp_path: Path):
    task = _task()
    variants = [Variant(name="baseline")]
    config = _config(tmp_path, [task], variants)

    estimate = estimate_run_cost(config, [task], variants, epochs=1)

    assert estimate.est_input_tokens == cost_service.DEFAULT_AVG_INPUT_TOKENS_PER_CELL
    assert estimate.est_output_tokens == cost_service.DEFAULT_AVG_OUTPUT_TOKENS_PER_CELL
    assert estimate.cost_total > 0
    assert estimate.cost_total == pytest.approx(estimate.cost_agent + estimate.cost_judge)


def test_estimate_run_cost_uses_historical_average(tmp_path: Path):
    results_dir = tmp_path / "results"
    run_dir = results_dir / "run-1"
    run_dir.mkdir(parents=True)
    _write_run_trace(run_dir)

    task = _task()
    variants = [Variant(name="baseline")]
    config = _config(tmp_path, [task], variants)

    estimate = estimate_run_cost(config, [task], variants, epochs=1)

    assert estimate.based_on_history is True
    assert estimate.history_sample_size == 1
    assert estimate.est_input_tokens == 31416


def test_cost_estimate_over_budget():
    estimate = CostEstimate(
        cells=1,
        judge_calls=0,
        est_input_tokens=1,
        est_output_tokens=1,
        est_judge_input_tokens=0,
        est_judge_output_tokens=0,
        cost_agent=5.0,
        cost_judge=0.0,
        cost_total=5.0,
        based_on_history=False,
        history_sample_size=0,
    )
    assert estimate.over_budget(1.0) is True
    assert estimate.over_budget(10.0) is False
    assert estimate.over_budget(None) is False


def test_format_cost_report_includes_budget_status():
    estimate = CostEstimate(
        cells=2,
        judge_calls=1,
        est_input_tokens=100,
        est_output_tokens=50,
        est_judge_input_tokens=10,
        est_judge_output_tokens=5,
        cost_agent=1.0,
        cost_judge=0.1,
        cost_total=1.1,
        based_on_history=False,
        history_sample_size=0,
    )
    report_over = format_cost_report(estimate, budget_limit=1.0)
    assert "OVER BUDGET" in report_over

    report_under = format_cost_report(estimate, budget_limit=10.0)
    assert "within budget" in report_under

    report_no_limit = format_cost_report(estimate, budget_limit=None)
    assert "Budget limit" not in report_no_limit


# --- eval.services.orchestrator.run_command: budget cap + --estimate -------


def _run_command_kwargs(**overrides):
    base = dict(
        task=None,
        epochs=1,
        dry_run=False,
        no_build=True,
        skip_preflight=True,
        config_dir=None,
        no_progress=True,
    )
    base.update(overrides)
    return base


def _fake_run_one(
    task, variant, epoch, config, run_id, run_dir, github_token, order_index=None, fixture=None
):
    return RunResult(
        task=task.name,
        variant=variant.name,
        epoch=epoch,
        test_id="t",
        run_id=run_id,
        log_file=run_dir / "x.log",
        exit_code=0,
        status=RunStatus.SUCCESS,
        order_index=order_index,
        fixture=fixture or "",
    )


def test_run_command_aborts_when_over_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    called = {"n": 0}

    def _tracking_run_one(*a, **k):
        called["n"] += 1
        return _fake_run_one(*a, **k)

    monkeypatch.setattr(orchestrator, "run_one", _tracking_run_one)

    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")])

    with pytest.raises(click.ClickException, match="exceeds budget limit"):
        orchestrator.run_command(config, **_run_command_kwargs(budget_limit=0.0000001))

    assert called["n"] == 0
    assert not (tmp_path / "results").exists()


def test_run_command_config_budget_limit_is_used_when_no_override(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one)

    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")], budget_limit=0.0000001)

    with pytest.raises(click.ClickException, match="exceeds budget limit"):
        orchestrator.run_command(config, **_run_command_kwargs())


def test_run_command_cli_budget_limit_overrides_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one)

    # config allows a huge budget, but the CLI override is tiny -> should abort.
    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")], budget_limit=1000.0)

    with pytest.raises(click.ClickException, match="exceeds budget limit"):
        orchestrator.run_command(config, **_run_command_kwargs(budget_limit=0.0000001))


def test_run_command_within_budget_proceeds(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one)

    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")], budget_limit=1000.0)

    orchestrator.run_command(config, **_run_command_kwargs())

    run_dirs = list((tmp_path / "results").iterdir())
    assert len(run_dirs) == 1


def test_run_command_estimate_yes_skips_confirmation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one)

    def _boom(*a, **k):
        raise AssertionError("click.confirm should not be called when --yes is set")

    monkeypatch.setattr(click, "confirm", _boom)

    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")])
    orchestrator.run_command(config, **_run_command_kwargs(estimate=True, yes=True))

    run_dirs = list((tmp_path / "results").iterdir())
    assert len(run_dirs) == 1


def test_run_command_estimate_declined_aborts_without_running(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    called = {"n": 0}

    def _tracking_run_one(*a, **k):
        called["n"] += 1
        return _fake_run_one(*a, **k)

    monkeypatch.setattr(orchestrator, "run_one", _tracking_run_one)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)

    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")])
    orchestrator.run_command(config, **_run_command_kwargs(estimate=True, yes=False))

    assert called["n"] == 0
    assert not (tmp_path / "results").exists()


def test_run_command_estimate_confirmed_proceeds(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)

    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")])
    orchestrator.run_command(config, **_run_command_kwargs(estimate=True, yes=False))

    run_dirs = list((tmp_path / "results").iterdir())
    assert len(run_dirs) == 1


def test_run_command_persists_cost_estimate_in_manifest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one)

    config = _config(tmp_path, [_task("t1")], [Variant(name="baseline")])
    orchestrator.run_command(config, **_run_command_kwargs())

    run_dir = next((tmp_path / "results").iterdir())
    manifest = load_manifest(run_dir)
    assert manifest is not None

    import json

    raw = json.loads((run_dir / "results.json").read_text())
    assert "cost" in raw
    assert raw["cost"]["estimate"]["cells"] == 1
    assert raw["cost"]["judge_tokens_in"] == 0
    assert raw["cost"]["judge_tokens_out"] == 0


# --- eval.judge_executor: judge_tokens_in/out tracking ----------------------


def _judge_config(tmp_path: Path, **runner_kw) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(**runner_kw),
        tasks=[],
        variants=[],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_judge(monkeypatch, proc, version="copilot/1.0.18"):
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: version)
    monkeypatch.setattr(je_mod.subprocess, "run", lambda *a, **k: proc)


def test_judge_records_estimated_tokens_without_cli_usage(tmp_path: Path, monkeypatch):
    _patch_judge(monkeypatch, _FakeProc(stdout='{"score": 8, "reason": "good"}'))
    ev = Evaluator(name="quality", type="judge", prompt="Rate it.")

    score = run_judge(ev, "some conversation text", _judge_config(tmp_path), token=None)

    assert score.meta["judge_tokens_in"] > 0
    assert score.meta["judge_tokens_out"] > 0
    assert score.meta["judge_tokens_estimated"] is True


def test_judge_uses_cli_reported_usage_when_present(tmp_path: Path, monkeypatch):
    _patch_judge(
        monkeypatch,
        _FakeProc(
            stdout='{"score": 8, "reason": "good", '
            '"usage": {"input_tokens": 1234, "output_tokens": 56}}'
        ),
    )
    ev = Evaluator(name="quality", type="judge", prompt="Rate it.")

    score = run_judge(ev, "conversation", _judge_config(tmp_path), token=None)

    assert score.meta["judge_tokens_in"] == 1234
    assert score.meta["judge_tokens_out"] == 56
    assert score.meta["judge_tokens_estimated"] is False


def test_judge_tokens_recorded_even_on_parse_error(tmp_path: Path, monkeypatch):
    _patch_judge(monkeypatch, _FakeProc(stdout="not json", returncode=0))
    ev = Evaluator(name="quality", type="judge", prompt="Rate it.")

    score = run_judge(ev, "conversation", _judge_config(tmp_path), token=None)

    assert score.score is None
    assert score.meta["judge_tokens_in"] > 0
    assert score.meta["judge_tokens_out"] > 0


def test_judge_tokens_summed_across_self_consistency_samples(tmp_path: Path, monkeypatch):
    from eval import judge_executor as je_mod

    monkeypatch.setattr(je_mod, "host_copilot_version", lambda: "copilot/1.0.18")
    stdouts = []

    def fake_run(*a, **k):
        i = len(stdouts) + 1
        stdout = f'{{"score": {i}, "reason": "r{i}"}}'
        stdouts.append(stdout)
        return _FakeProc(stdout=stdout)

    monkeypatch.setattr(je_mod.subprocess, "run", fake_run)
    ev = Evaluator(name="quality", type="judge", prompt="Rate it.")
    config = _judge_config(tmp_path, judge_samples=3)

    score = run_judge(ev, "conversation text", config, token=None)

    prompt = JudgeExecutor(config)._build_single_prompt(ev, "conversation text", None)
    expected_in = je_mod._estimate_tokens(prompt) * 3
    expected_out = sum(je_mod._estimate_tokens(s) for s in stdouts)

    assert score.n_samples == 3
    assert score.meta["judge_tokens_in"] == expected_in
    assert score.meta["judge_tokens_out"] == expected_out
