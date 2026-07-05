"""Tests for the offline replay/mock runner (issue #132).

The replay runner is a *test/dev-only* harness: it drives the runner →
evaluator → report pipeline entirely offline (no Docker, no Copilot auth) by
replaying pre-recorded agent outputs + OTel traces, and every output it produces
is stamped as replayed/synthetic so it can never be mistaken for a real,
isolated A/B measurement.

These tests exercise:

* the runner in isolation (tag rewriting, output/trace/log replay, error case);
* an end-to-end runner → contains/regex/metric → report pass with NO Docker and
  NO auth, asserting the synthetic labelling is present in the manifest and the
  rendered report;
* driving judge evaluators offline (with the Copilot judge call stubbed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.config import Config, Evaluator, RunnerConfig, Task, Variant
from eval.exceptions import ReplayError
from eval.protocols import EvalScore, RunContext, RunStatus
from eval.runners import RUNNER_REGISTRY, ReplayRunner
from eval.runners.replay_runner import SYNTHETIC_LOG_BANNER

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A minimal Copilot file-exporter trace: an invoke_agent root (turn_count/cost),
# one chat span (token usage), one execute_tool span. Resource tags are dummies
# on purpose — the replay runner rewrites them to the current run.
_RECORDED_TRACE = [
    {
        "type": "span",
        "traceId": "trace-abc",
        "spanId": "root",
        "name": "invoke_agent",
        "startTime": [1000, 0],
        "endTime": [1002, 0],
        "attributes": {"github.copilot.turn_count": 2, "github.copilot.cost": 0.01},
        "resource": {"attributes": {"eval.run_id": "OLD-RUN", "eval.scenario": "stale"}},
    },
    {
        "type": "span",
        "traceId": "trace-abc",
        "spanId": "chat1",
        "parentSpanId": "root",
        "name": "chat claude",
        "startTime": [1000, 0],
        "endTime": [1001, 0],
        "attributes": {
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 20,
        },
        "resource": {"attributes": {"eval.run_id": "OLD-RUN"}},
    },
    {
        "type": "span",
        "traceId": "trace-abc",
        "spanId": "tool1",
        "parentSpanId": "root",
        "name": "execute_tool read",
        "startTime": [1000, 0],
        "endTime": [1000, 500000000],
        "attributes": {"gen_ai.tool.name": "read"},
        "resource": {"attributes": {"eval.run_id": "OLD-RUN"}},
    },
]


def _write_recording(replay_dir: Path, *, transcript: str, exit_code: int = 0) -> None:
    """Materialize a `.replay/` recording (transcript + trace + output + meta)."""
    replay_dir.mkdir(parents=True, exist_ok=True)
    (replay_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
    (replay_dir / "traces.jsonl").write_text(
        "\n".join(json.dumps(rec) for rec in _RECORDED_TRACE) + "\n", encoding="utf-8"
    )
    output_dir = replay_dir / "output"
    output_dir.mkdir(exist_ok=True)
    (output_dir / "answer.md").write_text("The answer is 42.\n", encoding="utf-8")
    (replay_dir / "meta.json").write_text(json.dumps({"exit_code": exit_code}), encoding="utf-8")


def _config(tmp_path: Path, task: Task) -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(backend="replay", collector="file"),
        tasks=[task],
        variants=[Variant(name="baseline")],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _replay_task() -> Task:
    return Task(
        name="replay_task",
        prompt="Answer the question.",
        evaluators=[
            Evaluator(name="mentions_answer", type="contains", value="answer"),
            Evaluator(name="has_number", type="regex", value=r"\d+"),
            Evaluator(name="cheap", type="metric", metric="turn_count", op="<=", threshold=5),
        ],
    )


def _run_context(tmp_path: Path, config: Config, run_dir: Path, work_dir: Path) -> RunContext:
    return RunContext(
        run_id="RUN-123",
        test_id="test-abcdef01",
        epoch=1,
        run_dir=run_dir,
        task=config.tasks[0],
        variant=config.variants[0],
        config=config,
        work_dir=work_dir,
        fixture="replay_task",
        fixture_label="",
    )


# ---------------------------------------------------------------------------
# Runner registration
# ---------------------------------------------------------------------------


def test_replay_backend_registered():
    assert RUNNER_REGISTRY["replay"] is ReplayRunner


def test_replay_runner_only_supports_file_collector():
    assert ReplayRunner().supported_collectors == ("file",)


def test_replay_backend_validates_in_config(tmp_path: Path):
    from tests.conftest import load_inline

    cfg = load_inline(tmp_path, {"runner": {"backend": "replay"}})
    assert cfg.runner.backend == "replay"


# ---------------------------------------------------------------------------
# Runner behavior in isolation
# ---------------------------------------------------------------------------


def test_run_replays_outputs_trace_and_marks_log_synthetic(tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    _write_recording(work_dir / ".replay", transcript="The answer is 42.")

    task = _replay_task()
    config = _config(tmp_path, task)
    artifacts = ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))

    assert artifacts.exit_code == 0
    assert artifacts.status == RunStatus.SUCCESS

    # Output file replayed into work_dir/output/.
    assert (work_dir / "output" / "answer.md").read_text().strip() == "The answer is 42."

    # Log carries the loud synthetic banner + the recorded transcript.
    log_text = artifacts.log_file.read_text()
    assert "REPLAYED / SYNTHETIC RUN" in log_text
    assert SYNTHETIC_LOG_BANNER in log_text
    assert "The answer is 42." in log_text

    # Trace written with resource tags rewritten to THIS run (not the stale ids).
    trace_text = (work_dir / ".traces" / "traces.jsonl").read_text()
    records = [json.loads(line) for line in trace_text.splitlines() if line.strip()]
    for rec in records:
        tags = rec["resource"]["attributes"]
        assert tags["eval.run_id"] == "RUN-123"
        assert tags["eval.scenario"] == "replay_task"
        assert tags["eval.variant"] == "baseline"
        assert tags["eval.test_id"] == "test-abcdef01"
    assert all(t["resource"]["attributes"]["eval.run_id"] != "OLD-RUN" for t in records)


def test_run_honors_recorded_exit_code(tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    _write_recording(work_dir / ".replay", transcript="boom", exit_code=1)

    config = _config(tmp_path, _replay_task())
    artifacts = ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))

    assert artifacts.exit_code == 1
    assert artifacts.status == RunStatus.FAILED


def test_run_missing_replay_dir_raises(tmp_path: Path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    # No `.replay/` recording created.
    config = _config(tmp_path, _replay_task())
    with pytest.raises(ReplayError, match="missing"):
        ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))


def test_run_reads_replay_dir_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    external = tmp_path / "recordings"
    _write_recording(external, transcript="external replay")
    monkeypatch.setenv("EVAL_REPLAY_DIR", str(external))

    config = _config(tmp_path, _replay_task())
    artifacts = ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))

    assert "external replay" in artifacts.log_file.read_text()


# ---------------------------------------------------------------------------
# End-to-end: runner -> inline evaluators -> analyze/report, fully offline
# ---------------------------------------------------------------------------


def _drive_run_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, str]:
    """Run one replayed cell through `run_one` (no Docker, no auth) and persist a
    replayed manifest. Returns (config, run_dir, run_id)."""
    from eval import runner as runner_mod
    from eval.services.manifest import write_manifest

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    task = _replay_task()
    config = _config(tmp_path, task)

    # Fixture that run_one copies into the writable work_dir. The recording lives
    # under the fixture's `.replay/` subdir, exactly as a committed fixture would.
    fixture_dir = tmp_path / "fixtures" / "replay_task"
    _write_recording(fixture_dir / ".replay", transcript="The answer is 42.")

    run_id = "RUN-E2E"
    run_dir = config.results_dir / run_id
    run_dir.mkdir(parents=True)

    result = runner_mod.run_one(
        task,
        config.variants[0],
        epoch=1,
        config=config,
        run_id=run_id,
        run_dir=run_dir,
        github_token="",  # replay never authenticates
        runner=ReplayRunner(),
    )
    assert result.status == RunStatus.SUCCESS
    write_manifest(run_dir, run_id, [result], replayed=True)
    return config, run_dir, run_id


def test_run_one_offline_scores_contains_and_regex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from eval import runner as runner_mod

    task = _replay_task()
    config = _config(tmp_path, task)
    fixture_dir = tmp_path / "fixtures" / "replay_task"
    _write_recording(fixture_dir / ".replay", transcript="The answer is 42.")
    run_dir = config.results_dir / "R"
    run_dir.mkdir(parents=True)

    result = runner_mod.run_one(
        task,
        config.variants[0],
        epoch=1,
        config=config,
        run_id="R",
        run_dir=run_dir,
        github_token="",
        runner=ReplayRunner(),
    )

    by_name = {s.name: s for s in result.scores}
    assert by_name["mentions_answer"].passed  # contains "answer"
    assert by_name["has_number"].passed  # regex \d+ matches "42"
    assert result.passed


def test_manifest_marked_replayed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _config_obj, run_dir, _run_id = _drive_run_one(tmp_path, monkeypatch)
    manifest = json.loads((run_dir / "results.json").read_text())
    assert manifest["replayed"] is True


def test_end_to_end_report_is_marked_synthetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The full offline pipeline (replay runner -> file traces -> metric
    evaluator -> report) produces a report unmistakably labelled synthetic."""
    from eval.services import analyze_service

    config, _run_dir, run_id = _drive_run_one(tmp_path, monkeypatch)
    monkeypatch.setattr(analyze_service, "load_config", lambda _cd: config)

    # skip_eval=True skips judges (which would need Copilot); metric evaluators
    # and the report still run — all offline.
    analyze_service.run_analysis(
        run_id=run_id,
        output="json",
        aggregate="paired",
        jaeger_url=None,
        config_dir=None,
        skip_eval=True,
        re_eval=False,
        no_progress=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["replayed"] is True
    # The metric gate (turn_count <= 5) passed on the replayed turn_count=2.
    task_report = payload["tasks"][0]
    assert task_report["task"] == "replay_task"


def test_end_to_end_table_report_has_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from eval.services import analyze_service

    config, _run_dir, run_id = _drive_run_one(tmp_path, monkeypatch)
    monkeypatch.setattr(analyze_service, "load_config", lambda _cd: config)

    analyze_service.run_analysis(
        run_id=run_id,
        output="table",
        aggregate="paired",
        jaeger_url=None,
        config_dir=None,
        skip_eval=True,
        re_eval=False,
        no_progress=True,
    )
    out = capsys.readouterr().out
    assert "REPLAYED / SYNTHETIC" in out


# ---------------------------------------------------------------------------
# Driving judge evaluators offline (Copilot judge call stubbed)
# ---------------------------------------------------------------------------


def test_judge_evaluators_driven_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A judge evaluator is scored offline against the replayed run: the replay
    runner supplies the transcript/outputs, and the Copilot judge call is
    stubbed (the judge LLM is inherently online; the harness drives the
    pipeline, not the model)."""
    from eval import runner as runner_mod
    from eval.judge_executor import JudgeExecutor
    from eval.services import judge_service
    from eval.services.trace_service import _collect_file_traces

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    task = Task(
        name="replay_task",
        prompt="Answer the question.",
        evaluators=[Evaluator(name="quality", type="judge", prompt="Rate 1-5.")],
    )
    config = _config(tmp_path, task)
    fixture_dir = tmp_path / "fixtures" / "replay_task"
    _write_recording(fixture_dir / ".replay", transcript="The answer is 42.")
    run_id = "RUN-JUDGE"
    run_dir = config.results_dir / run_id
    run_dir.mkdir(parents=True)

    runner_mod.run_one(
        task,
        config.variants[0],
        epoch=1,
        config=config,
        run_id=run_id,
        run_dir=run_dir,
        github_token="",
        runner=ReplayRunner(),
    )

    # Stub the judge LLM + token so nothing touches the network.
    monkeypatch.setattr(judge_service, "get_github_token", lambda: "tok")

    def _fake_execute_single(self: JudgeExecutor, evaluator: Any, context: Any) -> EvalScore:
        return EvalScore(name=evaluator.name, type="judge", score=5, reason="great", passed=True)

    monkeypatch.setattr(JudgeExecutor, "execute_single", _fake_execute_single)

    traces = _collect_file_traces(config, run_id, run_dir)
    assert traces, "replayed trace should be collected offline"
    judge_service._run_judges(config, traces, run_dir)

    # The judge score was persisted next to the run via the offline pipeline.
    scores_files = list(run_dir.glob("*.scores.json"))
    assert scores_files
    all_scores = [s for f in scores_files for s in json.loads(f.read_text())]
    judge_scores = [s for s in all_scores if s["type"] == "judge"]
    assert judge_scores and judge_scores[0]["score"] == 5


# ---------------------------------------------------------------------------
# Council review fixes (issue #132): non-goal safety + robustness
# ---------------------------------------------------------------------------


def test_is_synthetic_capability_and_helper() -> None:
    """The offline harness is identified by an optional ``is_synthetic``
    capability, not a hardcoded backend string, so orchestration/validation can
    branch on the runner rather than the literal name (and third-party offline
    runners can opt in)."""
    from eval.runners import DockerCLIRunner, runner_is_synthetic

    assert ReplayRunner.is_synthetic is True
    assert DockerCLIRunner.is_synthetic is False
    assert runner_is_synthetic("replay") is True
    assert runner_is_synthetic("docker") is False
    # Unknown/non-declaring backend defaults to False (real, isolated) — the
    # safe default: it keeps the full Docker/token pre-flight.
    assert runner_is_synthetic("does-not-exist") is False


def test_readiness_skips_docker_and_token_for_replay(tmp_path: Path, monkeypatch: Any) -> None:
    """The replay backend is offline, so pre-flight must not require Docker or a
    GitHub token — otherwise it couldn't run without auth."""
    from eval import validation

    def _boom(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("Docker/token pre-flight ran for the offline replay backend")

    monkeypatch.setattr(validation, "check_docker_daemon", _boom)
    monkeypatch.setattr(validation, "check_github_token", _boom)

    task = _replay_task()
    fixture_dir = tmp_path / "fixtures" / "replay_task"
    _write_recording(fixture_dir / ".replay", transcript="The answer is 42.")
    config = _config(tmp_path, task)

    results = validation.validate_readiness(config, tasks=[task], check_build=True)

    check_names = {r.name for r in results}
    assert "docker_daemon" not in check_names
    assert "github_token" not in check_names
    assert not any(r.name == "base_image" for r in results)
    assert any("disk" in r.name for r in results)


def test_replay_with_jaeger_collector_rejected_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthetic runner + jaeger collector must fail loudly BEFORE any Docker/
    Jaeger work — never spin up a container just to reject the config."""
    import click

    from eval.config import Config, RunnerConfig, Variant
    from eval.services import orchestrator

    def _boom_jaeger(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("Jaeger/Docker must not start for a synthetic runner")

    monkeypatch.setattr(orchestrator, "_ensure_jaeger", _boom_jaeger)

    task = _replay_task()
    _write_recording(tmp_path / "fixtures" / "replay_task" / ".replay", transcript="hi")
    config = Config(
        vars={},
        runner=RunnerConfig(backend="replay", collector="jaeger"),
        tasks=[task],
        variants=[Variant(name="baseline")],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    with pytest.raises(click.ClickException, match="jaeger"):
        orchestrator.run_command(
            config,
            task=None,
            epochs=1,
            dry_run=False,
            no_build=True,
            skip_preflight=True,
            config_dir=None,
            no_progress=True,
        )


def test_resume_never_drops_replayed_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming a replayed run with a REAL backend must not launder the
    carried-over synthetic cells: the merged manifest stays replayed."""
    from eval.protocols import RunStatus
    from eval.runner import RunResult
    from eval.services import orchestrator
    from eval.services.manifest import MANIFEST_NAME

    def _fake_run_one(
        task: Any,
        variant: Any,
        epoch: int,
        config: Any,
        run_id: str,
        run_dir: Path,
        github_token: str,
        order_index: Any = None,
        fixture: Any = None,
    ) -> RunResult:
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id="t",
            run_id=run_id,
            log_file=run_dir / "x.log",
            exit_code=0,
            status=RunStatus.SUCCESS,
            duration_seconds=1.0,
        )

    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one)
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")

    from eval.config import Config, RunnerConfig, Variant

    def _mk_config(backend: str) -> Config:
        return Config(
            vars={},
            runner=RunnerConfig(backend=backend, collector="file", parallel="off"),
            tasks=[_replay_task()],
            variants=[Variant(name="baseline")],
            project_dir=tmp_path,
            config_dir=tmp_path,
        )

    kwargs = dict(
        task=None,
        epochs=1,
        dry_run=False,
        no_build=True,
        skip_preflight=True,
        config_dir=None,
        no_progress=True,
    )

    # First pass: replay backend → manifest stamped replayed.
    orchestrator.run_command(_mk_config("replay"), **kwargs)
    run_dir = next((tmp_path / "results").iterdir())
    run_id = run_dir.name
    assert json.loads((run_dir / MANIFEST_NAME).read_text())["replayed"] is True

    # Resume with a REAL (docker) backend. The marker must survive.
    orchestrator.run_command(_mk_config("docker"), resume=True, run_id=run_id, **kwargs)
    assert json.loads((run_dir / MANIFEST_NAME).read_text())["replayed"] is True


def test_save_baseline_refuses_replayed_run(tmp_path: Path) -> None:
    """A baseline snapshotted from a replayed run would leak synthetic numbers
    into a later real comparison — refuse it outright."""
    from eval.services.baseline_service import BaselineError, save_baseline

    config = _config(tmp_path, _replay_task())
    with pytest.raises(BaselineError, match="synthetic"):
        save_baseline(config, "RUN", "b", [], replayed=True)


def test_synthetic_baseline_forces_report_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even a REAL run must render the synthetic banner when compared against a
    replayed/synthetic baseline — synthetic data can never pass as genuine in a
    cross-run comparison."""
    from eval import runner as runner_mod
    from eval.services import analyze_service
    from eval.services.baseline_service import baseline_path, save_baseline
    from eval.services.manifest import write_manifest
    from eval.services.trace_service import load_run_metrics

    task = _replay_task()
    config = _config(tmp_path, task)
    _write_recording(
        tmp_path / "fixtures" / "replay_task" / ".replay", transcript="The answer is 42."
    )

    run_id = "RUN-REAL"
    run_dir = config.results_dir / run_id
    run_dir.mkdir(parents=True)
    result = runner_mod.run_one(
        task,
        config.variants[0],
        epoch=1,
        config=config,
        run_id=run_id,
        run_dir=run_dir,
        github_token="",
        runner=ReplayRunner(),
    )
    # This run is treated as REAL: its own manifest is NOT replayed.
    write_manifest(run_dir, run_id, [result], replayed=False)

    metrics = load_run_metrics(config, run_id)
    save_baseline(config, run_id, "synth", metrics, replayed=False)
    # Simulate a synthetic baseline that slipped in (or an older snapshot):
    bpath = baseline_path(config, "synth")
    data = json.loads(bpath.read_text())
    assert data["replayed"] is False  # save_baseline persisted the marker
    data["replayed"] = True
    bpath.write_text(json.dumps(data))

    monkeypatch.setattr(analyze_service, "load_config", lambda _cd: config)
    analyze_service.run_analysis(
        run_id=run_id,
        output="table",
        aggregate="paired",
        jaeger_url=None,
        config_dir=None,
        skip_eval=True,
        re_eval=False,
        no_progress=True,
        baseline_name="synth",
    )
    out = capsys.readouterr().out
    assert "REPLAYED / SYNTHETIC" in out


def test_cost_history_excludes_replayed_runs(tmp_path: Path) -> None:
    """Replayed token traces must not skew a later real run's cost estimate."""
    from eval.collectors.file_collector import TRACE_FILE
    from eval.services.cost_service import load_historical_costs

    results_dir = tmp_path / "results"

    def _make_run(name: str, replayed: bool) -> None:
        run_dir = results_dir / name
        (run_dir / TRACE_FILE.parent).mkdir(parents=True)
        (run_dir / TRACE_FILE).write_text(
            "\n".join(json.dumps(rec) for rec in _RECORDED_TRACE) + "\n", encoding="utf-8"
        )
        (run_dir / "results.json").write_text(json.dumps({"replayed": replayed}))

    # Only a replayed run present → no usable historical cost data.
    _make_run("run-replay", replayed=True)
    assert load_historical_costs(results_dir) is None

    # Add a real run → it (and only it) contributes.
    _make_run("run-real", replayed=False)
    hist = load_historical_costs(results_dir)
    assert hist is not None
    assert hist.avg_input_tokens_per_cell == 100.0  # from the single real run


# --- replay input robustness -----------------------------------------------


def test_replay_empty_dir_with_only_stray_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording with no recognized artifact (only a stray .DS_Store) must
    fail loudly instead of yielding a silent empty 'passing' run."""
    from eval.exceptions import ReplayError

    work_dir = tmp_path / "work"
    replay = work_dir / ".replay"
    replay.mkdir(parents=True)
    (replay / ".DS_Store").write_text("junk")
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    config = _config(tmp_path, _replay_task())

    with pytest.raises(ReplayError, match="no usable recording"):
        ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))


def test_replay_non_object_meta_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A meta.json that is valid JSON but not an object must fail loudly rather
    than silently defaulting to exit 0."""
    from eval.exceptions import ReplayError

    work_dir = tmp_path / "work"
    replay = work_dir / ".replay"
    replay.mkdir(parents=True)
    (replay / "transcript.txt").write_text("hi")
    (replay / "meta.json").write_text(json.dumps([1, 2, 3]))
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    config = _config(tmp_path, _replay_task())

    with pytest.raises(ReplayError, match="expected a JSON object"):
        ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))


def test_replay_wrong_typed_artifact_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact that exists but is the wrong type (a directory named
    traces.jsonl) must fail loudly, not be silently skipped."""
    from eval.exceptions import ReplayError

    work_dir = tmp_path / "work"
    replay = work_dir / ".replay"
    replay.mkdir(parents=True)
    (replay / "transcript.txt").write_text("hi")
    (replay / "traces.jsonl").mkdir()  # wrong type
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    config = _config(tmp_path, _replay_task())

    with pytest.raises(ReplayError, match="not a regular file"):
        ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))


def test_replay_malformed_trace_line_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt traces.jsonl line must fail loudly rather than being passed
    through and yielding a run with no usable telemetry."""
    from eval.exceptions import ReplayError

    work_dir = tmp_path / "work"
    replay = work_dir / ".replay"
    replay.mkdir(parents=True)
    (replay / "transcript.txt").write_text("hi")
    (replay / "traces.jsonl").write_text('{"type":"span","spanId":"a"}\n{ this is not json\n')
    run_dir = tmp_path / "results"
    run_dir.mkdir()
    config = _config(tmp_path, _replay_task())

    with pytest.raises(ReplayError, match="malformed JSON"):
        ReplayRunner().run(_run_context(tmp_path, config, run_dir, work_dir))
