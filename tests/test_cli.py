"""Tests for CLI scheduling helpers (variant ordering for bias reduction)."""

import json
import random
from pathlib import Path

from click.testing import CliRunner

from eval.cli import main
from eval.config import Config, RunnerConfig, Variant
from eval.services.metrics_service import _run_metric_evaluators
from eval.services.orchestrator import _ordering_rng, order_variants
from eval.services.trace_service import _collect_file_traces

FIXTURE = Path(__file__).parent / "fixtures" / "file-exporter-sample.jsonl"


def _variants(*names: str) -> list[Variant]:
    return [Variant(name=n) for n in names]


def _names(variants: list[Variant]) -> list[str]:
    return [v.name for v in variants]


def test_fixed_preserves_order():
    vs = _variants("a", "b", "c")
    rng = random.Random(0)
    for epoch in range(1, 5):
        assert _names(order_variants(vs, epoch, "fixed", rng)) == ["a", "b", "c"]


def test_counterbalance_rotates_by_epoch():
    vs = _variants("a", "b", "c")
    rng = random.Random(0)
    assert _names(order_variants(vs, 1, "counterbalance", rng)) == ["a", "b", "c"]
    assert _names(order_variants(vs, 2, "counterbalance", rng)) == ["b", "c", "a"]
    assert _names(order_variants(vs, 3, "counterbalance", rng)) == ["c", "a", "b"]
    # Wraps around after a full cycle.
    assert _names(order_variants(vs, 4, "counterbalance", rng)) == ["a", "b", "c"]


def test_counterbalance_balances_positions_across_cycle():
    vs = _variants("a", "b", "c")
    rng = random.Random(0)
    first_positions = [_names(order_variants(vs, e, "counterbalance", rng))[0] for e in range(1, 4)]
    assert sorted(first_positions) == ["a", "b", "c"]


def test_random_is_reproducible_with_same_seed():
    vs = _variants("a", "b", "c", "d")
    out1 = _names(order_variants(vs, 1, "random", random.Random(42)))
    out2 = _names(order_variants(vs, 1, "random", random.Random(42)))
    assert out1 == out2
    assert sorted(out1) == ["a", "b", "c", "d"]


def test_random_differs_across_seeds():
    vs = _variants("a", "b", "c", "d", "e", "f")
    out1 = _names(order_variants(vs, 1, "random", random.Random(1)))
    out2 = _names(order_variants(vs, 1, "random", random.Random(2)))
    assert out1 != out2


def test_does_not_mutate_input():
    vs = _variants("a", "b", "c")
    original = _names(vs)
    order_variants(vs, 2, "counterbalance", random.Random(0))
    order_variants(vs, 1, "random", random.Random(0))
    assert _names(vs) == original


def test_single_variant_is_noop():
    vs = _variants("only")
    for strategy in ("fixed", "counterbalance", "random"):
        assert _names(order_variants(vs, 2, strategy, random.Random(0))) == ["only"]


# --- _ordering_rng (per-context, thread-safe, reproducible) ---


def test_ordering_rng_seeded_is_reproducible_per_context():
    vs = _variants("a", "b", "c", "d")
    out1 = _names(order_variants(vs, 1, "random", _ordering_rng(7, "task", 1)))
    out2 = _names(order_variants(vs, 1, "random", _ordering_rng(7, "task", 1)))
    assert out1 == out2


def test_ordering_rng_differs_by_context():
    vs = _variants("a", "b", "c", "d", "e")
    a = _names(order_variants(vs, 1, "random", _ordering_rng(7, "task-a", 1)))
    b = _names(order_variants(vs, 1, "random", _ordering_rng(7, "task-b", 1)))
    c = _names(order_variants(vs, 2, "random", _ordering_rng(7, "task-a", 2)))
    assert a != b or a != c  # different task/epoch contexts produce distinct schedules


def test_ordering_rng_returns_fresh_instance_each_call():
    # Distinct objects => no shared mutable state across concurrent schedulers.
    assert _ordering_rng(1, "x") is not _ordering_rng(1, "x")


def test_ordering_rng_none_seed_is_nondeterministic():
    r = _ordering_rng(None, "x")
    assert isinstance(r, random.Random)


def test_collect_file_traces_reads_all_per_run_files(tmp_path: Path):
    results_dir = tmp_path / "results"
    traces_dir = results_dir / ".traces"
    traces_dir.mkdir(parents=True)
    traces_dir.joinpath("task_a_epoch1.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    traces_dir.joinpath("task_b_epoch1.jsonl").write_text(
        FIXTURE.read_text(encoding="utf-8")
        .replace("spike-run", "run-2")
        .replace("spike-001", "test-2")
        .replace("c5b55d939c5df4939aa20c7090a13cc9", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        encoding="utf-8",
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[],
        variants=[],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    traces = _collect_file_traces(config, "run-2", results_dir)

    assert len(traces) == 1
    assert traces[0].trace_id == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert traces[0].resource_tags["eval.run_id"] == "run-2"


def test_invalid_log_level_env_var_yields_clean_error():
    """A bogus EVAL_LOG_LEVEL must fail as a ClickException, not a traceback."""
    result = CliRunner().invoke(
        main,
        ["list", "--config-dir", "examples/prompt-language"],
        env={"EVAL_LOG_LEVEL": "bogus"},
    )
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Invalid log level" in result.output


def test_invalid_log_format_env_var_yields_clean_error():
    """A bogus EVAL_LOG_FORMAT must fail as a ClickException, not a traceback."""
    result = CliRunner().invoke(
        main,
        ["list", "--config-dir", "examples/prompt-language"],
        env={"EVAL_LOG_FORMAT": "bogus"},
    )
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Invalid log format" in result.output


# --- Metric evaluators at analyze time ---


def _metric_config(project_dir: Path, config_dir: Path) -> Config:
    from eval.config import Evaluator, Task

    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(name="latency", type="metric", metric="duration", op="<", threshold=60.0),
            Evaluator(name="budget", type="metric", metric="cost", op="<", threshold=0.5),
        ],
    )
    return Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=project_dir,
        config_dir=config_dir,
    )


def _metric_trace():
    from eval.trace import Span, Trace

    root = Span(
        name="invoke_agent",
        duration_s=42.0,
        span_id="r",
        parent_id=None,
        tags={
            "github.copilot.turn_count": 3,
            "github.copilot.cost": "0.42",
            "gen_ai.request.model": "m",
        },
    )
    return Trace(
        trace_id="tr",
        spans=[root],
        resource_tags={
            "eval.scenario": "t",
            "eval.variant": "v",
            "eval.epoch": "0",
            "eval.test_id": "abc",
        },
    )


def test_run_metric_evaluators_writes_scores(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    config = _metric_config(tmp_path, tmp_path)

    _run_metric_evaluators(config, [_metric_trace()], results_dir)

    scores_file = results_dir / "t_v_epoch0.scores.json"
    assert scores_file.exists()
    scores = {s["name"]: s for s in json.loads(scores_file.read_text())}
    assert scores["latency"]["type"] == "metric"
    assert scores["latency"]["passed"] is True and scores["latency"]["score"] == 1
    assert scores["budget"]["passed"] is True and scores["budget"]["score"] == 1


def test_run_metric_evaluators_preserves_other_scores_and_is_idempotent(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    scores_file = results_dir / "t_v_epoch0.scores.json"
    scores_file.write_text(
        json.dumps(
            [
                {"name": "quality", "type": "judge", "score": 8, "reason": "r", "passed": True},
                {
                    "name": "latency",
                    "type": "metric",
                    "score": 0,
                    "reason": "stale",
                    "passed": False,
                },
            ]
        )
    )
    config = _metric_config(tmp_path, tmp_path)

    _run_metric_evaluators(config, [_metric_trace()], results_dir)
    _run_metric_evaluators(config, [_metric_trace()], results_dir)  # idempotent

    scores = json.loads(scores_file.read_text())
    by_name = {s["name"]: s for s in scores}
    # Judge score preserved, stale metric recomputed (now passing), no duplicates.
    assert by_name["quality"]["type"] == "judge"
    assert by_name["latency"]["passed"] is True
    assert len([s for s in scores if s["name"] == "latency"]) == 1


def test_run_metric_evaluators_returns_failed_gates(tmp_path: Path):
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(name="latency", type="metric", metric="duration", op="<", threshold=60.0),
            Evaluator(name="budget", type="metric", metric="cost", op="<", threshold=0.3),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    failed = _run_metric_evaluators(config, [_metric_trace()], results_dir)

    # latency passes (42 < 60); budget fails (0.42 not < 0.3).
    assert len(failed) == 1
    assert "budget" in failed[0]
    assert "→" not in failed[0]  # arrow suffix stripped for the summary


def test_run_metric_evaluators_unavailable_counts_as_failed(tmp_path: Path, monkeypatch):
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(name="budget", type="metric", metric="cost", op="<", threshold=0.5),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )
    # Simulate a metric that can't be derived from the trace (value is None):
    # eval_metric must score it None/not-passing, and the gate must count as failed.
    monkeypatch.setattr("eval.runner.metric_value", lambda *a, **k: None)

    failed = _run_metric_evaluators(config, [_metric_trace()], results_dir)

    assert len(failed) == 1
    assert "budget" in failed[0]
    assert "unavailable" in failed[0]


def _metric_trace_no_root():
    """A trace with resource tags but no `invoke_agent` span → extract_metrics None."""
    from eval.trace import Trace

    return Trace(
        trace_id="tr",
        spans=[],
        resource_tags={
            "eval.scenario": "t",
            "eval.variant": "v",
            "eval.epoch": "0",
            "eval.test_id": "abc",
        },
    )


def test_run_metric_evaluators_fails_closed_when_metrics_absent(tmp_path: Path):
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(name="budget", type="metric", metric="cost", op="<", threshold=0.5),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    # A trace that yields no metrics for a metric-gated task must fail CLOSED.
    failed = _run_metric_evaluators(config, [_metric_trace_no_root()], results_dir)

    assert len(failed) == 1
    assert "budget" in failed[0]
    assert "unavailable" in failed[0]

    # A null (score=None, passed=False) score is persisted so the report reflects it.
    scores = json.loads((results_dir / "t_v_epoch0.scores.json").read_text())
    budget = next(s for s in scores if s["name"] == "budget")
    assert budget["type"] == "metric"
    assert budget["score"] is None
    assert budget["passed"] is False


def _metric_trace_no_cost():
    """A metric trace whose root has NO `github.copilot.cost` tag.

    Drives the real `extract_metrics` path where an absent cost tag must be
    treated as unavailable for gating (not coerced to 0.0).
    """
    from eval.trace import Span, Trace

    root = Span(
        name="invoke_agent",
        duration_s=42.0,
        span_id="r",
        parent_id=None,
        tags={
            "github.copilot.turn_count": 3,
            "gen_ai.request.model": "m",
        },
    )
    return Trace(
        trace_id="tr",
        spans=[root],
        resource_tags={
            "eval.scenario": "t",
            "eval.variant": "v",
            "eval.epoch": "0",
            "eval.test_id": "abc",
        },
    )


def test_run_metric_evaluators_cost_absent_fails_closed(tmp_path: Path):
    """Case 1 (issue #64): an absent `github.copilot.cost` tag must make a
    `cost < X` gate fail CLOSED via the real extract_metrics path — not silently
    pass because cost was coerced to 0.0 (0.0 < 0.5)."""
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(name="budget", type="metric", metric="cost", op="<", threshold=0.5),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    failed = _run_metric_evaluators(config, [_metric_trace_no_cost()], results_dir)

    assert len(failed) == 1
    assert "budget" in failed[0]
    assert "unavailable" in failed[0]

    # The persisted score is null (not a passing 0 < 0.5), so the report reflects it.
    scores = json.loads((results_dir / "t_v_epoch0.scores.json").read_text())
    budget = next(s for s in scores if s["name"] == "budget")
    assert budget["score"] is None
    assert budget["passed"] is False


def test_run_metric_evaluators_missing_telemetry_run_fails_closed(tmp_path: Path):
    """Case 2 (issue #64): a metric-gated run recorded in the manifest that
    produced no usable trace (telemetry entirely missing) must fail CLOSED rather
    than be silently skipped."""
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(name="budget", type="metric", metric="cost", op="<", threshold=0.5),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    # Manifest records a metric-gated run, but no trace was ingested for it.
    manifest_runs = [
        {
            "task": "t",
            "variant": "v",
            "epoch": 0,
            "fixture": "",
            "test_id": "gone",
            "status": "success",
        }
    ]

    failed = _run_metric_evaluators(config, [], results_dir, manifest_runs)

    assert len(failed) == 1
    assert "budget" in failed[0]
    assert "telemetry missing" in failed[0]

    # A null score is persisted so the report reflects the missing run.
    scores = json.loads((results_dir / "t_v_epoch0.scores.json").read_text())
    budget = next(s for s in scores if s["name"] == "budget")
    assert budget["score"] is None
    assert budget["passed"] is False


def test_run_metric_evaluators_manifest_run_with_trace_not_double_counted(tmp_path: Path):
    """A manifest run that DID produce a usable trace must be scored once (from the
    trace) and not re-failed by the missing-telemetry manifest cross-reference."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    config = _metric_config(tmp_path, tmp_path)

    manifest_runs = [
        {
            "task": "t",
            "variant": "v",
            "epoch": 0,
            "fixture": "",
            "test_id": "abc",
            "status": "success",
        }
    ]

    # _metric_trace passes both gates (duration 42 < 60, cost 0.42 < 0.5).
    failed = _run_metric_evaluators(config, [_metric_trace()], results_dir, manifest_runs)

    assert failed == []
    scores = {
        s["name"]: s for s in json.loads((results_dir / "t_v_epoch0.scores.json").read_text())
    }
    assert scores["budget"]["passed"] is True
    # Exactly one score per metric (no duplicate from the manifest pass).
    all_scores = json.loads((results_dir / "t_v_epoch0.scores.json").read_text())
    assert len([s for s in all_scores if s["name"] == "budget"]) == 1


def _metric_trace_int_tags_absent():
    """A trace whose root lacks `github.copilot.turn_count` and whose chat span
    carries NO token-usage tags. Drives the real `extract_metrics` path where the
    int-tag metrics coerce to 0 but are flagged unavailable (#121) — so a
    `turn_count <= N` / `total_tokens <= N` gate must fail CLOSED, not pass on a
    coerced 0 <= N."""
    from eval.trace import Span, Trace

    root = Span(
        name="invoke_agent",
        duration_s=42.0,
        span_id="r",
        parent_id=None,
        tags={"gen_ai.request.model": "m"},  # no turn_count tag
    )
    chat = Span(
        name="chat",
        duration_s=1.0,
        span_id="c1",
        parent_id="r",
        tags={},  # no gen_ai.usage.* token tags → token telemetry absent
    )
    return Trace(
        trace_id="tr",
        spans=[root, chat],
        resource_tags={
            "eval.scenario": "t",
            "eval.variant": "v",
            "eval.epoch": "0",
            "eval.test_id": "abc",
        },
    )


def test_run_metric_evaluators_int_tag_absent_fails_closed(tmp_path: Path):
    """#121: absent int-tag telemetry (tokens / turn_count) must make a `<=` gate
    fail CLOSED via the real extract_metrics path — not silently pass on a coerced
    0 (0 <= N)."""
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(
                name="tokens", type="metric", metric="total_tokens", op="<=", threshold=1000.0
            ),
            Evaluator(name="turns", type="metric", metric="turn_count", op="<=", threshold=10.0),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    failed = _run_metric_evaluators(config, [_metric_trace_int_tags_absent()], results_dir)

    assert len(failed) == 2
    joined = " ".join(failed)
    assert "tokens" in joined and "turns" in joined
    assert "unavailable" in joined

    # Both gates persist a null (score=None, passed=False) score, not a passing 0.
    scores = {
        s["name"]: s for s in json.loads((results_dir / "t_v_epoch0.scores.json").read_text())
    }
    assert scores["tokens"]["score"] is None and scores["tokens"]["passed"] is False
    assert scores["turns"]["score"] is None and scores["turns"]["passed"] is False


def _metric_trace_fx(fixture: str, duration: float):
    """A metric trace tagged with eval.fixture (multi-fixture input-coverage axis)."""
    from eval.trace import Span, Trace

    root = Span(
        name="invoke_agent",
        duration_s=duration,
        span_id="r",
        parent_id=None,
        tags={
            "github.copilot.turn_count": 3,
            "github.copilot.cost": "0.10",
            "gen_ai.request.model": "m",
        },
    )
    return Trace(
        trace_id=f"tr-{fixture}",
        spans=[root],
        resource_tags={
            "eval.scenario": "t",
            "eval.variant": "v",
            "eval.epoch": "0",
            "eval.test_id": f"abc-{fixture}",
            "eval.fixture": fixture,
        },
    )


def test_run_metric_evaluators_multi_fixture_writes_separate_files_and_catches_failure(
    tmp_path: Path,
):
    """Each fixture of a multi-fixture task must get its own fixture-suffixed scores
    file, and a metric that fails on one fixture must not be dedup-skipped by another
    fixture that shares scenario/variant/epoch."""
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        fixtures=["fixA", "fixB"],
        evaluators=[
            Evaluator(name="latency", type="metric", metric="duration", op="<", threshold=60.0),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )

    # fixA passes (42 < 60); fixB fails (90 not < 60). Same scenario/variant/epoch.
    traces = [_metric_trace_fx("fixA", 42.0), _metric_trace_fx("fixB", 90.0)]
    failed = _run_metric_evaluators(config, traces, results_dir)

    # Each fixture wrote its own fixture-suffixed scores file (no orphaning/misattribution).
    file_a = results_dir / "t_v_epoch0__fixture__fixA.scores.json"
    file_b = results_dir / "t_v_epoch0__fixture__fixB.scores.json"
    assert file_a.exists() and file_b.exists()
    assert not (results_dir / "t_v_epoch0.scores.json").exists()
    assert json.loads(file_a.read_text())[0]["passed"] is True
    assert json.loads(file_b.read_text())[0]["passed"] is False

    # fixB's failure is caught (not skipped by the dedup `seen` set) and is labelled.
    assert len(failed) == 1
    assert "latency" in failed[0]
    assert "fixB" in failed[0]


# --- analyze exit code (CI gating) ---
def _analyze_gate_config(tmp_path: Path, budget_threshold: float) -> Config:
    """Build a Config (rooted at tmp_path) with a single cost-budget metric gate."""
    from eval.config import Evaluator, Task

    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(
                name="budget", type="metric", metric="cost", op="<", threshold=budget_threshold
            )
        ],
    )
    return Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _patch_analyze(
    tmp_path: Path, monkeypatch, budget_threshold: float, create_results_dir: bool = True
) -> str:
    """Point analyze at a tmp-rooted config + results dir with a synthetic trace."""
    from eval.services import analyze_service

    run_id = "run-1"
    config = _analyze_gate_config(tmp_path, budget_threshold)
    if create_results_dir:
        (config.results_dir / run_id).mkdir(parents=True)
    monkeypatch.setattr(analyze_service, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(analyze_service, "_collect_file_traces", lambda *a, **k: [_metric_trace()])
    return run_id


def test_analyze_exits_nonzero_when_metric_gate_fails(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from eval.cli import main

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.3)

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0
    assert "Metric gate failed" in result.output
    assert "budget" in result.output


def test_analyze_exits_zero_when_metric_gate_passes(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from eval.cli import main

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code == 0, result.output
    assert "Metric gate failed" not in result.output


def test_analyze_gate_runs_without_preexisting_results_dir(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from eval.cli import main

    # results/<run_id> does NOT exist (e.g. `run` and `analyze` in separate CI jobs).
    # Metric gating must still run and fail the command, not silently exit 0.
    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.3, create_results_dir=False)

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0
    assert "Metric gate failed" in result.output
    assert "budget" in result.output


def test_analyze_exits_nonzero_when_one_fixture_fails_metric_gate(tmp_path: Path, monkeypatch):
    """A multi-fixture task whose metric gate passes on fixA but fails on fixB must
    make `analyze` exit non-zero — the failing fixture is not masked by the passer."""
    from click.testing import CliRunner

    from eval.cli import main
    from eval.config import Evaluator, Task
    from eval.services import analyze_service

    run_id = "run-1"
    task = Task(
        name="t",
        prompt="p",
        fixtures=["fixA", "fixB"],
        evaluators=[
            Evaluator(name="latency", type="metric", metric="duration", op="<", threshold=60.0),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )
    (config.results_dir / run_id).mkdir(parents=True)
    traces = [_metric_trace_fx("fixA", 42.0), _metric_trace_fx("fixB", 90.0)]
    monkeypatch.setattr(analyze_service, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(analyze_service, "_collect_file_traces", lambda *a, **k: traces)

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0
    assert "Metric gate failed" in result.output
    assert "fixB" in result.output


def _patch_analyze_with(tmp_path: Path, monkeypatch, config: Config, traces, manifest=None) -> str:
    """Point analyze at a config + results dir with arbitrary traces and an
    optional persisted manifest (results.json)."""
    from eval.services import analyze_service

    run_id = "run-1"
    results_dir = config.results_dir / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (results_dir / "results.json").write_text(json.dumps({"runs": manifest}))
    monkeypatch.setattr(analyze_service, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(analyze_service, "_collect_file_traces", lambda *a, **k: traces)
    return run_id


def test_analyze_exits_nonzero_when_cost_tag_absent(tmp_path: Path, monkeypatch):
    """Case 1 (issue #64) end-to-end: a cost gate whose `github.copilot.cost` tag
    is absent must make `analyze` exit non-zero — even with a threshold the coerced
    0.0 would have passed (0.0 < 0.5)."""
    from click.testing import CliRunner

    from eval.cli import main

    config = _analyze_gate_config(tmp_path, budget_threshold=0.5)
    run_id = _patch_analyze_with(tmp_path, monkeypatch, config, [_metric_trace_no_cost()])

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0, result.output
    assert "Metric gate failed" in result.output
    assert "budget" in result.output


def test_analyze_exits_nonzero_when_trace_yields_no_metrics_and_no_manifest(
    tmp_path: Path, monkeypatch
):
    """Regression (issue #64): an ingested-but-unparseable trace for a metric-gated
    task, with no manifest, must fail CLOSED — not slip through the "no traces"
    early return with exit 0."""
    from click.testing import CliRunner

    from eval.cli import main

    config = _analyze_gate_config(tmp_path, budget_threshold=0.5)
    run_id = _patch_analyze_with(tmp_path, monkeypatch, config, [_metric_trace_no_root()])

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0, result.output
    assert "Metric gate failed" in result.output


def test_analyze_exits_nonzero_when_no_telemetry_and_no_manifest(tmp_path: Path, monkeypatch):
    """Regression (issue #64): a metric-gated config that produced zero telemetry
    and has no manifest can verify its gates from neither source, so `analyze` must
    fail CLOSED rather than exit 0 on entirely-absent telemetry."""
    from click.testing import CliRunner

    from eval.cli import main

    config = _analyze_gate_config(tmp_path, budget_threshold=0.5)
    run_id = _patch_analyze_with(tmp_path, monkeypatch, config, [])

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0, result.output
    assert "Metric gate failed" in result.output


def test_analyze_exits_nonzero_when_manifest_run_missing_telemetry(tmp_path: Path, monkeypatch):
    """Case 2 (issue #64) end-to-end: a metric-gated run recorded in the manifest
    that produced no trace must make `analyze` exit non-zero."""
    from click.testing import CliRunner

    from eval.cli import main

    config = _analyze_gate_config(tmp_path, budget_threshold=0.5)
    manifest = [
        {
            "task": "t",
            "variant": "v",
            "epoch": 0,
            "fixture": "",
            "test_id": "gone",
            "status": "success",
        }
    ]
    run_id = _patch_analyze_with(tmp_path, monkeypatch, config, [], manifest=manifest)

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0, result.output
    assert "Metric gate failed" in result.output
    assert "budget" in result.output


def test_analyze_exits_nonzero_when_int_tag_absent(tmp_path: Path, monkeypatch):
    """#121 end-to-end: a `total_tokens <= N` gate whose token telemetry is absent
    must make `analyze` exit non-zero — even with a threshold the coerced 0 would
    have passed (0 <= 1000)."""
    from click.testing import CliRunner

    from eval.cli import main
    from eval.config import Evaluator, Task

    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(
                name="tokens", type="metric", metric="total_tokens", op="<=", threshold=1000.0
            )
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )
    run_id = _patch_analyze_with(tmp_path, monkeypatch, config, [_metric_trace_int_tags_absent()])

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0, result.output
    assert "Metric gate failed" in result.output
    assert "tokens" in result.output


def test_run_metric_evaluators_missing_telemetry_fails_closed_regardless_of_status(tmp_path: Path):
    """The manifest cross-reference fails CLOSED for a metric-gated run with no
    trace even when its recorded status is timeout/failed (a dropped/timed-out run
    still can't verify its gate)."""
    from eval.config import Evaluator, Task

    results_dir = tmp_path / "results"
    results_dir.mkdir()
    task = Task(
        name="t",
        prompt="p",
        evaluators=[
            Evaluator(name="budget", type="metric", metric="cost", op="<", threshold=0.5),
        ],
    )
    config = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )
    manifest_runs = [
        {
            "task": "t",
            "variant": "v",
            "epoch": 0,
            "fixture": "",
            "test_id": "x",
            "status": "timeout",
        }
    ]

    failed = _run_metric_evaluators(config, [], results_dir, manifest_runs)

    assert len(failed) == 1
    assert "budget" in failed[0]
    assert "telemetry missing" in failed[0]


# --- analyze fail-closed on unknown / empty run (issue #126) ---
def _no_gate_config(tmp_path: Path) -> Config:
    """Build a Config with a single task and NO metric evaluators, so exit-code
    behaviour is driven purely by run existence, not by a metric gate."""
    from eval.config import Task

    task = Task(name="t", prompt="p", evaluators=[])
    return Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[task],
        variants=_variants("v"),
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def test_analyze_exits_nonzero_when_run_id_unknown(tmp_path: Path, monkeypatch):
    """A mistyped/never-executed --run-id whose results dir does not exist must
    fail CLOSED (non-zero exit + "not found") instead of silently exiting 0, so a
    typo'd run can't pass a CI gate as green."""
    from eval.services import analyze_service

    config = _no_gate_config(tmp_path)
    # results/<run_id> deliberately NOT created — the run is unknown.
    monkeypatch.setattr(analyze_service, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(analyze_service, "_collect_file_traces", lambda *a, **k: [])

    result = CliRunner().invoke(main, ["analyze", "--run-id", "typo-run", "--skip-eval"])

    assert result.exit_code != 0, result.output
    assert "not found" in result.output
    assert "typo-run" in result.output


def test_analyze_exits_nonzero_when_known_run_has_no_analyzable_data(tmp_path: Path, monkeypatch):
    """A known run (results dir exists) that produced no traces and no manifest
    can't be analyzed, so `analyze` must fail CLOSED instead of exiting 0."""
    from eval.services import analyze_service

    config = _no_gate_config(tmp_path)
    run_id = "empty-run"
    (config.results_dir / run_id).mkdir(parents=True)
    monkeypatch.setattr(analyze_service, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(analyze_service, "_collect_file_traces", lambda *a, **k: [])

    result = CliRunner().invoke(main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code != 0, result.output
    assert "no analyzable data" in result.output


def test_analyze_allow_empty_exits_zero_on_unknown_run(tmp_path: Path, monkeypatch):
    """--allow-empty is the explicit escape hatch: an unknown/empty run exits 0
    for the rare "I know it's empty and that's fine" case."""
    from eval.services import analyze_service

    config = _no_gate_config(tmp_path)
    monkeypatch.setattr(analyze_service, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(analyze_service, "_collect_file_traces", lambda *a, **k: [])

    result = CliRunner().invoke(
        main, ["analyze", "--run-id", "typo-run", "--skip-eval", "--allow-empty"]
    )

    assert result.exit_code == 0, result.output


# --- analyze --min-epochs (statistical power CI gate) ---


def test_analyze_exits_nonzero_when_below_min_epochs(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from eval import cli

    # budget_threshold=0.5 keeps the metric gate passing so only --min-epochs
    # decides the exit code below.
    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)

    result = CliRunner().invoke(
        cli.main, ["analyze", "--run-id", run_id, "--skip-eval", "--min-epochs", "2"]
    )

    assert result.exit_code != 0
    assert "Insufficient epochs" in result.output
    assert "n=1" in result.output


def test_analyze_exits_zero_when_min_epochs_satisfied(tmp_path: Path, monkeypatch):
    from click.testing import CliRunner

    from eval import cli

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)

    result = CliRunner().invoke(
        cli.main, ["analyze", "--run-id", run_id, "--skip-eval", "--min-epochs", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "Insufficient epochs" not in result.output


def test_analyze_min_epochs_omitted_does_not_gate(tmp_path: Path, monkeypatch):
    """Without --min-epochs, a low sample size must not affect the exit code."""
    from click.testing import CliRunner

    from eval import cli

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)

    result = CliRunner().invoke(cli.main, ["analyze", "--run-id", run_id, "--skip-eval"])

    assert result.exit_code == 0, result.output


# --- analyze --no-mc-correction (multiple-comparison correction opt-out) ---


def test_analyze_default_applies_holm_correction(monkeypatch):
    """By default, `analyze` must pass mc_correction="holm" through to the
    report builder (issue #71's Holm-Bonferroni correction is on by default)."""
    from click.testing import CliRunner

    from eval.cli import analyze_cmd

    captured = {}
    monkeypatch.setattr(
        analyze_cmd, "run_analysis", lambda **kwargs: captured.update(kwargs) or None
    )

    result = CliRunner().invoke(analyze_cmd.analyze, ["--run-id", "run-1"])

    assert result.exit_code == 0, result.output
    assert captured["mc_correction"] == "holm"


def test_analyze_no_mc_correction_flag_disables_correction(monkeypatch):
    """--no-mc-correction must opt out of the correction entirely (mc_correction="none")."""
    from click.testing import CliRunner

    from eval.cli import analyze_cmd

    captured = {}
    monkeypatch.setattr(
        analyze_cmd, "run_analysis", lambda **kwargs: captured.update(kwargs) or None
    )

    result = CliRunner().invoke(analyze_cmd.analyze, ["--run-id", "run-1", "--no-mc-correction"])

    assert result.exit_code == 0, result.output
    assert captured["mc_correction"] == "none"


# --- analyze --compact (PR comment markdown) ---


def test_analyze_compact_flag_defaults_to_false(monkeypatch):
    from click.testing import CliRunner

    from eval.cli import analyze_cmd

    captured = {}
    monkeypatch.setattr(
        analyze_cmd, "run_analysis", lambda **kwargs: captured.update(kwargs) or None
    )

    result = CliRunner().invoke(analyze_cmd.analyze, ["--run-id", "run-1"])

    assert result.exit_code == 0, result.output
    assert captured["compact"] is False


def test_analyze_compact_flag_passed_through(monkeypatch):
    from click.testing import CliRunner

    from eval.cli import analyze_cmd

    captured = {}
    monkeypatch.setattr(
        analyze_cmd, "run_analysis", lambda **kwargs: captured.update(kwargs) or None
    )

    result = CliRunner().invoke(
        analyze_cmd.analyze, ["--run-id", "run-1", "-o", "markdown", "--compact"]
    )

    assert result.exit_code == 0, result.output
    assert captured["compact"] is True
    assert captured["output"] == "markdown"


def test_analyze_compact_selects_compact_formatter(tmp_path: Path, monkeypatch):
    """`analyze -o markdown --compact` must render via format_markdown_compact,
    not the full format_markdown (which includes per-run/tool-usage detail)."""
    from click.testing import CliRunner

    from eval import cli

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)

    result = CliRunner().invoke(
        cli.main, ["analyze", "--run-id", run_id, "--skip-eval", "-o", "markdown", "--compact"]
    )

    assert result.exit_code == 0, result.output
    assert "📊 copilot-eval:" in result.output
    assert "### Per-Run Details" not in result.output
    assert "### Tool Usage" not in result.output


# --- CI-native output formats ---


def test_analyze_junit_output_is_valid_xml(tmp_path: Path, monkeypatch):
    import xml.etree.ElementTree as ET

    from eval import cli

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)

    result = CliRunner().invoke(
        cli.main, ["analyze", "--run-id", run_id, "--skip-eval", "-o", "junit"]
    )

    assert result.exit_code == 0, result.output
    xml_start = result.output.index("<?xml")
    root = ET.fromstring(result.output[xml_start:])
    assert root.tag == "testsuites"


def test_analyze_html_output_is_self_contained(tmp_path: Path, monkeypatch):
    from eval import cli

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)

    result = CliRunner().invoke(
        cli.main, ["analyze", "--run-id", run_id, "--skip-eval", "-o", "html"]
    )

    assert result.exit_code == 0, result.output
    assert "<!DOCTYPE html>" in result.output
    assert "<style>" in result.output


def test_analyze_gha_summary_writes_to_step_summary_file(tmp_path: Path, monkeypatch):
    from eval import cli

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)
    summary_path = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    result = CliRunner().invoke(
        cli.main, ["analyze", "--run-id", run_id, "--skip-eval", "-o", "gha-summary"]
    )

    assert result.exit_code == 0, result.output
    assert "GITHUB_STEP_SUMMARY" in result.output
    assert "📊 copilot-eval:" in summary_path.read_text(encoding="utf-8")


def test_analyze_gha_summary_falls_back_to_stdout_without_env_var(tmp_path: Path, monkeypatch):
    from eval import cli

    run_id = _patch_analyze(tmp_path, monkeypatch, budget_threshold=0.5)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    result = CliRunner().invoke(
        cli.main, ["analyze", "--run-id", run_id, "--skip-eval", "-o", "gha-summary"]
    )

    assert result.exit_code == 0, result.output
    assert "📊 copilot-eval:" in result.output
