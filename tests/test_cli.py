"""Tests for CLI scheduling helpers (variant ordering for bias reduction)."""
import random

from eval.cli import order_variants
from eval.config import Variant


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
    first_positions = [
        _names(order_variants(vs, e, "counterbalance", rng))[0] for e in range(1, 4)
    ]
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
