"""Tests for CLI scheduling helpers (variant ordering for bias reduction)."""

import random
from pathlib import Path

from eval.cli import _fetch_traces_from_files, _ordering_rng, order_variants
from eval.config import Config, RunnerConfig, Variant

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


def test_fetch_traces_from_files_reads_all_per_run_files(tmp_path: Path):
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

    traces = _fetch_traces_from_files(config, "run-2", results_dir, manifest_runs=None)

    assert len(traces) == 1
    assert traces[0].trace_id == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert traces[0].resource_tags["eval.run_id"] == "run-2"
