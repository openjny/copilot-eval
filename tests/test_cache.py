"""Tests for the opt-in content-hash run cache (issue #131): cache-key
computation and invalidation, the on-disk RunCache round-trip (store →
materialize), the orchestrator `run --cache` flow (miss = fresh run, hit = cell
skipped + result reused), and that `analyze`/`build_report` reports the
effective (non-cached) sample size so cached reuse can't fake-inflate confidence.

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


# --- RunCache store / lookup / materialize round-trip -------------------


def _write_cell_artifacts(run_dir: Path, slug: str) -> None:
    (run_dir / f"{slug}.log").write_text("run log contents")
    (run_dir / f"{slug}.scores.json").write_text(
        json.dumps([{"name": "a", "type": "contains", "score": 1, "passed": True}])
    )
    out = run_dir / "outputs" / slug
    out.mkdir(parents=True)
    (out / "answer.txt").write_text("the answer")
    traces = run_dir / ".traces"
    traces.mkdir()
    (traces / f"{slug}.jsonl").write_text('{"span": 1}\n')


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

    assert cache.lookup(key) is None  # a failed cell is never cached


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


def test_report_surfaces_effective_non_cached_sample_size():
    """A run that reused 1 of 2 epochs per variant must report the effective
    (non-cached) sample size, not just the inflated total."""
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

    assert report.variant_n == {"baseline": 2, "experimental": 2}
    assert report.cached_variant_n == {"baseline": 1, "experimental": 1}
    assert report.effective_variant_n == {"baseline": 1, "experimental": 1}
    assert report.paired_n == 2
    assert report.effective_paired_n == 1  # only epoch 2 is fresh on both sides
    assert any(w["type"] == "cached_samples" for w in report.warnings)


def test_report_no_cache_effective_equals_total():
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
    assert not any(w["type"] == "cached_samples" for w in report.warnings)
