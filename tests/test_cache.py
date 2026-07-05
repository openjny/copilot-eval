"""Tests for the opt-in content-hash run cache (issue #131): cache-key
computation and invalidation, the on-disk RunCache round-trip (store →
materialize), the orchestrator `run --cache` flow (miss = fresh run, hit = cell
skipped + result reused), and that `analyze`/`build_report` always surfaces the
fresh/cached sample breakdown (and warns past a high-cache threshold) while
cached cells still count toward the totals and the power gate (Option C).

External execution is fully mocked: `run_one` is monkeypatched and the Docker
image-digest resolver is stubbed, so no Docker daemon is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.config import Config, RunnerConfig, Task, Variant
from eval.protocols import EvalScore, RunStatus
from eval.report import build_report
from eval.runner import RunResult
from eval.services import cache_service, orchestrator
from eval.services.cache_service import (
    CacheKeyInputs,
    RunCache,
    build_cache_key_inputs,
    compute_cache_key,
)
from eval.services.manifest import MANIFEST_NAME
from eval.trace import RunMetrics

# --- shared helpers -----------------------------------------------------


def _config(tmp_path: Path, parallel: str = "off") -> Config:
    return Config(
        vars={},
        runner=RunnerConfig(parallel=parallel, max_workers=4, timeout_seconds=300),
        tasks=[],
        variants=[Variant(name="baseline"), Variant(name="experimental")],
        project_dir=tmp_path,
        config_dir=tmp_path,
    )


def _task(name: str = "code-review") -> Task:
    return Task(name=name, prompt="do the thing")


def _base_inputs(**overrides) -> CacheKeyInputs:
    base = dict(
        task="t1",
        variant="baseline",
        epoch=1,
        fixture="",
        prompt="do the thing",
        image_digest="sha256:abc",
        fixture_sha256="deadbeef",
        model="gpt-5",
        reasoning_effort="high",
        max_turns=10,
        timeout_seconds=300,
        collector="file",
        output_format="text",
        capture_content=True,
        resources_cpus=None,
        resources_memory=None,
        resources_pids_limit=None,
        run_script_sha256=None,
        env_file_sha256=None,
        evaluators_json="[]",
        before_run_sha256=None,
        after_run_sha256=None,
        health_check_sha256=None,
        hooks_on_failure="fail",
    )
    base.update(overrides)
    return CacheKeyInputs(**base)  # type: ignore[arg-type]


# --- cache-key computation & invalidation -------------------------------


def test_compute_cache_key_is_deterministic():
    assert compute_cache_key(_base_inputs()) == compute_cache_key(_base_inputs())


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "something else"),
        ("image_digest", "sha256:different"),
        ("fixture_sha256", "cafef00d"),
        ("model", "gpt-4"),
        ("reasoning_effort", "low"),
        ("max_turns", 99),
        ("timeout_seconds", 600),
        ("collector", "jaeger"),
        ("epoch", 2),
        ("variant", "experimental"),
        ("task", "other-task"),
        ("fixture", "fixture-b"),
        ("output_format", "json"),
        ("capture_content", False),
        ("resources_cpus", "2.0"),
        ("resources_memory", "4g"),
        ("resources_pids_limit", 100),
        ("run_script_sha256", "runscript-hash"),
        ("env_file_sha256", "envfile-hash"),
        ("evaluators_json", '[{"type":"contains","value":"x"}]'),
        ("before_run_sha256", "before-hash"),
        ("after_run_sha256", "after-hash"),
        ("health_check_sha256", "health-hash"),
        ("hooks_on_failure", "warn"),
    ],
)
def test_any_input_change_busts_the_key(field, value):
    """Every environment-complete input must be part of the key: changing any
    one of them yields a different cache key (and so re-executes the cell)."""
    baseline_key = compute_cache_key(_base_inputs())
    changed_key = compute_cache_key(_base_inputs(**{field: value}))
    assert baseline_key != changed_key


def test_build_cache_key_inputs_captures_environment(tmp_path):
    config = _config(tmp_path)
    config.runner.model = "gpt-5"
    config.runner.reasoning_effort = "high"
    config.runner.max_turns = 7
    config.runner.output_format = "json"
    config.runner.capture_content = False
    config.runner.resources.cpus = "2.0"
    task = _task("t1")
    variant = Variant(name="experimental", model="variant-model")

    inputs = build_cache_key_inputs(
        config,
        task,
        variant,
        epoch=2,
        fixture_label="",
        image_digest="sha256:x",
        fixture_sha256="hh",
    )

    # Variant model overrides runner model; runner effort/max-turns flow through.
    assert inputs.model == "variant-model"
    assert inputs.reasoning_effort == "high"
    assert inputs.max_turns == 7
    assert inputs.epoch == 2
    assert inputs.prompt == config.resolve_prompt(task, variant)
    # Container/runtime inputs that aren't baked into the image digest.
    assert inputs.output_format == "json"
    assert inputs.capture_content is False
    assert inputs.resources_cpus == "2.0"


def test_run_script_content_is_part_of_the_key(tmp_path):
    """A variant's run script is bind-mounted at run time, not baked into the
    image, so editing it must bust the cache even though the path is unchanged."""
    script = tmp_path / "setup.sh"
    script.write_text("echo one")
    config = _config(tmp_path)
    config.tasks.append(_task("t1"))
    variant = Variant(name="baseline", run_script="setup.sh")

    def key_for() -> str:
        return compute_cache_key(
            build_cache_key_inputs(config, config.tasks[0], variant, 1, "", "sha256:x", "hh")
        )

    before = key_for()
    script.write_text("echo two")  # same path, different content
    assert key_for() != before


def test_runtime_evaluator_change_busts_key_but_judge_does_not(tmp_path):
    """Inline runtime evaluators (contains/regex/script) score at run time and
    their verdicts are cached, so changing them must bust the key. Judge/metric
    evaluators run in `analyze` off the reused trace, so they must NOT."""
    from eval.config import Evaluator

    config = _config(tmp_path)
    task = _task("t1")
    task.evaluators = [Evaluator(name="c", type="contains", value="foo")]
    config.tasks.append(task)
    variant = Variant(name="baseline")

    def key_for() -> str:
        return compute_cache_key(
            build_cache_key_inputs(config, task, variant, 1, "", "sha256:x", "hh")
        )

    before = key_for()
    task.evaluators[0].value = "bar"  # runtime evaluator changed
    assert key_for() != before

    # Adding a judge (deferred to analyze) must not change the run key.
    after_runtime = key_for()
    task.evaluators.append(Evaluator(name="j", type="judge", prompt="grade it"))
    assert key_for() == after_runtime


# --- RunCache store / lookup / materialize round-trip -------------------


def _write_cell_artifacts(run_dir: Path, slug: str, *, source_run_id: str = "run-a") -> None:
    (run_dir / f"{slug}.log").write_text("run log contents")
    (run_dir / f"{slug}.scores.json").write_text(
        json.dumps([{"name": "a", "type": "contains", "score": 1, "passed": True}])
    )
    out = run_dir / "outputs" / slug
    out.mkdir(parents=True)
    (out / "answer.txt").write_text("the answer")
    traces = run_dir / ".traces"
    traces.mkdir()
    # A realistic file-exporter span record carrying the *source* run id as an
    # OTel resource tag — this is what materialize must re-home onto the new run.
    span = {
        "type": "span",
        "traceId": "t0",
        "spanId": "s0",
        "name": "copilot.turn",
        "resource": {
            "attributes": {
                "eval.run_id": source_run_id,
                "eval.test_id": "original-test-id",
            }
        },
    }
    (traces / f"{slug}.jsonl").write_text(json.dumps(span) + "\n")


def _success_result(run_dir: Path, slug: str) -> RunResult:
    return RunResult(
        task="t1",
        variant="baseline",
        epoch=1,
        test_id="original-test-id",
        run_id="run-a",
        log_file=run_dir / f"{slug}.log",
        exit_code=0,
        status=RunStatus.SUCCESS,
        scores=[EvalScore(name="a", type="contains", score=1, passed=True)],
        fixture="",
    )


def test_store_and_materialize_round_trip(tmp_path):
    from eval.naming import run_slug

    run_a = tmp_path / "run-a"
    run_a.mkdir()
    slug = run_slug("t1", "baseline", 1, "")
    _write_cell_artifacts(run_a, slug)

    cache = RunCache(tmp_path / ".cache")
    inputs = _base_inputs()
    key = compute_cache_key(inputs)
    cache.store(key, _success_result(run_a, slug), run_a, inputs)

    # A fresh run dir: materialize should recreate every artifact + a cached result.
    run_b = tmp_path / "run-b"
    run_b.mkdir()
    entry = cache.lookup(key)
    assert entry is not None
    result = cache.materialize(key, entry, run_b, run_id="run-b")

    assert result.cached is True
    assert result.run_id == "run-b"
    assert result.test_id == "original-test-id"  # preserved so trace tags line up
    assert result.passed is True
    assert (run_b / f"{slug}.log").read_text() == "run log contents"
    assert (run_b / "outputs" / slug / "answer.txt").read_text() == "the answer"
    assert (run_b / ".traces" / f"{slug}.jsonl").exists()
    assert [s.name for s in result.scores] == ["a"]

    # The reused trace was re-homed onto the new run id (its test_id preserved),
    # so the file collector — which filters by run_id — will ingest it.
    from eval.collectors.file_collector import parse_file_traces

    traces = parse_file_traces(run_b / ".traces" / f"{slug}.jsonl")
    assert len(traces) == 1
    assert traces[0].resource_tags["eval.run_id"] == "run-b"
    assert traces[0].resource_tags["eval.test_id"] == "original-test-id"


def test_cached_trace_is_ingested_by_file_collector(tmp_path):
    """End-to-end proof of the re-homing fix: a materialized cached cell's trace
    survives the FileCollector's run-id filter under the *new* run id."""
    from eval.collectors.file_collector import FileCollector
    from eval.naming import run_slug
    from eval.protocols import RunContext

    run_a = tmp_path / "run-a"
    run_a.mkdir()
    slug = run_slug("t1", "baseline", 1, "")
    _write_cell_artifacts(run_a, slug, source_run_id="run-a")

    cache = RunCache(tmp_path / ".cache")
    inputs = _base_inputs()
    key = compute_cache_key(inputs)
    cache.store(key, _success_result(run_a, slug), run_a, inputs)

    run_b = tmp_path / "run-b"
    run_b.mkdir()
    cache.materialize(key, cache.lookup(key), run_b, run_id="run-b")

    collected = FileCollector().collect(
        RunContext(
            run_id="run-b",
            test_id="",
            epoch=1,
            run_dir=run_b,
            task=_task("t1"),
            variant=Variant(name="baseline"),
            config=_config(tmp_path),
        )
    )
    # Filtered by run_id="run-b"; the re-homed trace passes the filter.
    assert len(collected) == 1
    assert collected[0].resource_tags["eval.test_id"] == "original-test-id"


def test_lookup_miss_returns_none(tmp_path):
    cache = RunCache(tmp_path / ".cache")
    assert cache.lookup(compute_cache_key(_base_inputs())) is None


def test_store_skips_non_successful_cells(tmp_path):
    from eval.naming import run_slug

    run_a = tmp_path / "run-a"
    run_a.mkdir()
    slug = run_slug("t1", "baseline", 1, "")
    _write_cell_artifacts(run_a, slug)

    cache = RunCache(tmp_path / ".cache")
    inputs = _base_inputs()
    key = compute_cache_key(inputs)
    failed = _success_result(run_a, slug)
    failed.status = RunStatus.FAILED
    failed.exit_code = 1
    cache.store(key, failed, run_a, inputs)

    assert cache.lookup(key) is None  # an infra-failed cell is never cached


def test_store_caches_completed_cell_even_when_eval_failed(tmp_path):
    """A cell whose container ran to completion but whose evaluator failed is
    still cached and reused one-for-one — caching only passing cells would bias
    point estimates across repeated cached runs."""
    from eval.naming import run_slug

    run_a = tmp_path / "run-a"
    run_a.mkdir()
    slug = run_slug("t1", "baseline", 1, "")
    _write_cell_artifacts(run_a, slug)

    cache = RunCache(tmp_path / ".cache")
    inputs = _base_inputs()
    key = compute_cache_key(inputs)
    completed_but_failed = _success_result(run_a, slug)
    completed_but_failed.scores = [EvalScore(name="a", type="contains", score=0, passed=False)]
    assert completed_but_failed.status == RunStatus.SUCCESS
    assert completed_but_failed.passed is False

    cache.store(key, completed_but_failed, run_a, inputs)
    entry = cache.lookup(key)
    assert entry is not None  # completed cells are cached regardless of pass/fail


def test_lookup_ignores_malformed_entry(tmp_path):
    """A corrupt/partial result.json is treated as a miss (re-execute), never a
    crash."""
    cache = RunCache(tmp_path / ".cache")
    key = compute_cache_key(_base_inputs())
    entry_dir = (tmp_path / ".cache") / key
    entry_dir.mkdir(parents=True)
    (entry_dir / "result.json").write_text('{"unexpected": "shape"}')  # missing keys
    assert cache.lookup(key) is None


# --- orchestrator run --cache end-to-end --------------------------------


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


def _fake_run_one_writing_log(calls: list[str]):
    """A fake run_one that records the cells it executed and writes a minimal
    log artifact so the cache has something to store."""

    def _run(
        task, variant, epoch, config, run_id, run_dir, github_token, order_index=None, fixture=None
    ):
        from eval.naming import run_slug

        calls.append(variant.name)
        label = task.fixture_label(fixture if fixture is not None else task.fixture_names()[0])
        slug = run_slug(task.name, variant.name, epoch, label)
        (run_dir / f"{slug}.log").write_text("fresh log")
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id=f"tid-{variant.name}-{epoch}",
            run_id=run_id,
            log_file=run_dir / f"{slug}.log",
            exit_code=0,
            status=RunStatus.SUCCESS,
            order_index=order_index,
            fixture=label,
            duration_seconds=1.0,
        )

    return _run


def test_first_run_populates_cache_second_run_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(cache_service, "resolve_image_digest", lambda image: f"digest::{image}")

    config = _config(tmp_path, parallel="off")
    config.tasks.append(_task("t1"))
    cache_dir = str(tmp_path / "my-cache")

    # First run with --cache: cache is empty, so both cells execute afresh and
    # are stored.
    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)
    assert sorted(calls) == ["baseline", "experimental"]

    first_run_dir = next((tmp_path / "results").iterdir())
    first_manifest = json.loads((first_run_dir / MANIFEST_NAME).read_text())
    assert all(r["cached"] is False for r in first_manifest["runs"])

    # Second run with --cache: nothing changed, so BOTH cells are cache hits and
    # run_one is never called.
    calls.clear()
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)
    assert calls == []  # no cell re-executed

    run_dirs = sorted((tmp_path / "results").iterdir())
    assert len(run_dirs) == 2  # a distinct new run dir
    second_run_dir = [d for d in run_dirs if d != first_run_dir][0]
    second_manifest = json.loads((second_run_dir / MANIFEST_NAME).read_text())
    assert len(second_manifest["runs"]) == 2
    assert all(r["cached"] is True for r in second_manifest["runs"])
    # Reused artifacts were materialized into the new run dir.
    from eval.naming import run_slug

    assert (second_run_dir / f"{run_slug('t1', 'baseline', 1, '')}.log").exists()


def test_cache_busts_when_prompt_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(cache_service, "resolve_image_digest", lambda image: f"digest::{image}")

    config = _config(tmp_path, parallel="off")
    config.tasks.append(_task("t1"))
    cache_dir = str(tmp_path / "cache")

    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)
    assert sorted(calls) == ["baseline", "experimental"]

    # Change the prompt: the key is busted, so both cells re-execute.
    config.tasks[0].prompt = "do something different"
    calls.clear()
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)
    assert sorted(calls) == ["baseline", "experimental"]


def test_cache_disabled_by_default_never_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(cache_service, "resolve_image_digest", lambda image: f"digest::{image}")

    config = _config(tmp_path, parallel="off")
    config.tasks.append(_task("t1"))

    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs())  # no cache flag
    orchestrator.run_command(config, **_run_command_kwargs())

    # Default path: every cell runs both times (2 variants × 2 runs = 4).
    assert len(calls) == 4


def test_cache_skipped_when_image_digest_unresolved(tmp_path, monkeypatch):
    """If the image digest can't be resolved, cells must NOT be cached or reused
    — keying on the mutable tag could reuse a result from a different image."""
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(cache_service, "resolve_image_digest", lambda image: None)

    config = _config(tmp_path, parallel="off")
    config.tasks.append(_task("t1"))
    cache_dir = str(tmp_path / "cache")

    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)
    assert sorted(calls) == ["baseline", "experimental"]

    # Second run: nothing was cached (digest unresolved), so both re-execute.
    calls.clear()
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)
    assert sorted(calls) == ["baseline", "experimental"]


def test_cache_disabled_for_non_file_collector(tmp_path, monkeypatch):
    """Caching re-homes the exported trace file; the Jaeger backend keeps traces
    server-side and can't be re-homed, so caching is disabled (not silently
    dropping reused cells at analyze)."""
    monkeypatch.setattr(orchestrator, "get_github_token", lambda: "tok")
    monkeypatch.setattr(cache_service, "resolve_image_digest", lambda image: f"digest::{image}")
    monkeypatch.setattr(orchestrator, "_ensure_jaeger", lambda config, jaeger_url=None: None)
    config = _config(tmp_path, parallel="off")
    config.runner.collector = "jaeger"
    config.tasks.append(_task("t1"))
    cache_dir = str(tmp_path / "cache")

    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "run_one", _fake_run_one_writing_log(calls))
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)
    orchestrator.run_command(config, **_run_command_kwargs(), cache=True, cache_dir=cache_dir)

    # Cache disabled → every cell runs both times (no reuse).
    assert len(calls) == 4


# --- effective sample size reporting ------------------------------------


def _metric(variant: str, epoch: str, test_id: str) -> RunMetrics:
    return RunMetrics(
        scenario="t1",
        variant=variant,
        epoch=epoch,
        test_id=test_id,
        total_spans=1,
        duration=1.0,
        turn_count=1,
        tool_count=0,
        tool_names=[],
        tool_duration=0.0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_tokens=0,
        model="m",
        cost=0.0,
    )


def _manifest_row(variant: str, epoch: int, test_id: str, cached: bool) -> dict:
    return {
        "task": "t1",
        "variant": variant,
        "epoch": epoch,
        "fixture": "",
        "test_id": test_id,
        "status": "completed",
        "passed": True,
        "cached": cached,
    }


def test_report_surfaces_fresh_cached_breakdown():
    """A run that reused 1 of 2 epochs per variant must surface the fresh/cached
    breakdown (issue #131, Option C). Cached cells still count toward the totals
    and the power gate; at exactly 50% cached the high-cache warning stays quiet."""
    from eval.report import _cache_composition, format_json
    from eval.services.analyze_service import _gate_epochs

    metrics = [
        _metric("baseline", "1", "aaaaaaaa"),
        _metric("baseline", "2", "bbbbbbbb"),
        _metric("experimental", "1", "cccccccc"),
        _metric("experimental", "2", "dddddddd"),
    ]
    manifest = [
        _manifest_row("baseline", 1, "aaaaaaaa", cached=True),
        _manifest_row("baseline", 2, "bbbbbbbb", cached=False),
        _manifest_row("experimental", 1, "cccccccc", cached=True),
        _manifest_row("experimental", 2, "dddddddd", cached=False),
    ]

    reports = build_report(
        metrics,
        variant_order=["baseline", "experimental"],
        manifest_runs=manifest,
        trace_test_ids={"aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"},
    )
    report = reports[0]

    # Totals count ALL real samples (fresh + cached); the split is disclosed.
    assert report.variant_n == {"baseline": 2, "experimental": 2}
    assert report.cached_variant_n == {"baseline": 1, "experimental": 1}
    assert report.effective_variant_n == {"baseline": 1, "experimental": 1}
    assert report.paired_n == 2
    assert report.effective_paired_n == 1  # only epoch 2 is fresh on both sides

    # Power gate counts all real samples (Option C), not just the fresh ones.
    assert _gate_epochs(report) == 2

    # The fresh/cached breakdown is always surfaced when the cache was used.
    comp = _cache_composition(report)
    assert "1 fresh, 1 cached" in comp
    assert "fully fresh" in comp

    # Exactly 50% cached is not "high", so no warning fires.
    assert not any(w["type"] == "high_cache_fraction" for w in report.warnings)

    # Machine-readable JSON carries the breakdown.
    payload = json.loads(format_json(reports))
    task_json = payload["tasks"][0]
    assert task_json["cached_variant_n"] == {"baseline": 1, "experimental": 1}
    assert task_json["fresh_variant_n"] == {"baseline": 1, "experimental": 1}
    assert task_json["fresh_paired_n"] == 1


def test_reuse_baseline_workflow_passes_min_epochs_gate():
    """The primary workflow — reuse the baseline from cache, freshly re-run only
    the experiment — must satisfy a satisfiable `--min-epochs` gate (issue #131,
    Option C). Before Option C the both-sides-fresh restriction forced the paired
    effective count to 0 and blocked this gate."""
    from eval.services.analyze_service import _gate_epochs

    metrics = [
        _metric("baseline", "1", "aaaaaaaa"),
        _metric("baseline", "2", "bbbbbbbb"),
        _metric("experimental", "1", "cccccccc"),
        _metric("experimental", "2", "dddddddd"),
    ]
    manifest = [
        # baseline fully reused from a prior run; experiment freshly produced.
        _manifest_row("baseline", 1, "aaaaaaaa", cached=True),
        _manifest_row("baseline", 2, "bbbbbbbb", cached=True),
        _manifest_row("experimental", 1, "cccccccc", cached=False),
        _manifest_row("experimental", 2, "dddddddd", cached=False),
    ]
    reports = build_report(
        metrics,
        variant_order=["baseline", "experimental"],
        manifest_runs=manifest,
        trace_test_ids={"aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"},
    )
    report = reports[0]

    # 2 paired epochs are compared; the gate counts them all despite the reuse.
    assert report.paired_n == 2
    assert _gate_epochs(report) == 2
    assert _gate_epochs(report) >= 2  # a --min-epochs 2 gate would pass
    """Past the high-cache threshold (>50% reused) `analyze` must warn so a
    mostly-reused run isn't misread as fully fresh (issue #131, Option C)."""
    metrics = [
        _metric("baseline", "1", "aaaaaaaa"),
        _metric("baseline", "2", "bbbbbbbb"),
        _metric("baseline", "3", "eeeeeeee"),
        _metric("experimental", "1", "cccccccc"),
        _metric("experimental", "2", "dddddddd"),
        _metric("experimental", "3", "ffffffff"),
    ]
    manifest = [
        _manifest_row("baseline", 1, "aaaaaaaa", cached=True),
        _manifest_row("baseline", 2, "bbbbbbbb", cached=True),
        _manifest_row("baseline", 3, "eeeeeeee", cached=False),
        _manifest_row("experimental", 1, "cccccccc", cached=True),
        _manifest_row("experimental", 2, "dddddddd", cached=True),
        _manifest_row("experimental", 3, "ffffffff", cached=False),
    ]
    reports = build_report(
        metrics,
        variant_order=["baseline", "experimental"],
        manifest_runs=manifest,
        trace_test_ids={
            "aaaaaaaa",
            "bbbbbbbb",
            "eeeeeeee",
            "cccccccc",
            "dddddddd",
            "ffffffff",
        },
    )
    report = reports[0]

    warning = next((w for w in report.warnings if w["type"] == "high_cache_fraction"), None)
    assert warning is not None
    assert "count toward" in warning["message"]


def test_report_no_cache_effective_equals_total():
    from eval.report import _has_cached_samples

    metrics = [
        _metric("baseline", "1", "aaaaaaaa"),
        _metric("experimental", "1", "cccccccc"),
    ]
    manifest = [
        _manifest_row("baseline", 1, "aaaaaaaa", cached=False),
        _manifest_row("experimental", 1, "cccccccc", cached=False),
    ]
    reports = build_report(
        metrics,
        variant_order=["baseline", "experimental"],
        manifest_runs=manifest,
        trace_test_ids={"aaaaaaaa", "cccccccc"},
    )
    report = reports[0]

    assert report.effective_variant_n == report.variant_n
    assert report.cached_variant_n == {"baseline": 0, "experimental": 0}
    assert not any(w["type"] == "high_cache_fraction" for w in report.warnings)
    assert not _has_cached_samples(report)
