"""Tests for report aggregation, pairing, and judge score loading."""
from __future__ import annotations

import json

from eval.report import _aggregate_values, _epoch_sort_key, _load_judge_raw, build_report
from tests.conftest import make_metrics

# --- Paired aggregation pairs by epoch key, not list index ---

def test_aggregate_paired_pairs_by_epoch():
    # variant b is missing epoch "2"; deltas must use common epochs {1,3}.
    vals = {
        "a": {"1": 10.0, "2": 20.0, "3": 30.0},
        "b": {"1": 11.0, "3": 33.0},
    }
    agg, delta = _aggregate_values(vals, ["a", "b"], "paired")
    # displayed values: per-variant median
    assert agg["a"] == 20.0
    assert agg["b"] == 22.0
    # paired deltas: (11-10)=1, (33-30)=3 -> median 2; ref0=20 -> +10.0%
    assert delta == "+10.0%"


def test_aggregate_paired_no_common_epoch():
    vals = {"a": {"1": 5.0}, "b": {"2": 9.0}}
    _, delta = _aggregate_values(vals, ["a", "b"], "paired")
    assert delta == ""


def test_aggregate_paired_denominator_uses_common_epochs():
    # baseline epoch "2" is an outlier and unpaired; it must not skew the percent.
    vals = {
        "a": {"1": 10.0, "2": 1000.0},
        "b": {"1": 12.0},
    }
    agg, delta = _aggregate_values(vals, ["a", "b"], "paired")
    # delta over common {1}: 12-10=2; paired baseline median([10])=10 -> +20.0%
    assert delta == "+20.0%"
    # displayed value still reflects all epochs
    assert agg["a"] == 505.0


def test_aggregate_paired_ignores_unknown_epoch_sentinel():
    # Both variants have a "?" epoch from missing OTel tags; it must not pair.
    vals = {"a": {"?": 5.0}, "b": {"?": 50.0}}
    _, delta = _aggregate_values(vals, ["a", "b"], "paired")
    assert delta == ""


def test_aggregate_median_and_mean():
    vals = {"a": {"1": 2.0, "2": 4.0}, "b": {"1": 10.0, "2": 20.0}}
    agg_med, _ = _aggregate_values(vals, ["a", "b"], "median")
    agg_mean, _ = _aggregate_values(vals, ["a", "b"], "mean")
    assert agg_med == {"a": 3.0, "b": 15.0}
    assert agg_mean == {"a": 3.0, "b": 15.0}


def test_build_report_paired_delta_with_missing_epoch():
    results = [
        make_metrics("t1", "a", "1", duration=10.0),
        make_metrics("t1", "a", "2", duration=20.0),
        make_metrics("t1", "a", "3", duration=30.0),
        make_metrics("t1", "b", "1", duration=11.0),
        make_metrics("t1", "b", "3", duration=33.0),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    assert len(reports) == 1
    dur_row = next(r for r in reports[0].summary if r.metric == "Duration (s)")
    assert dur_row.delta == "+10.0%"


# --- Numeric epoch ordering ---

def test_epoch_sort_key_numeric():
    epochs = ["10", "2", "1"]
    assert sorted(epochs, key=_epoch_sort_key) == ["1", "2", "10"]


def test_epoch_sort_key_non_numeric_fallback():
    assert sorted(["b", "1", "a"], key=_epoch_sort_key) == ["1", "a", "b"]


def test_build_report_runs_sorted_numerically():
    results = [
        make_metrics("t1", "a", "10"),
        make_metrics("t1", "a", "2"),
        make_metrics("t1", "a", "1"),
    ]
    reports = build_report(results, variant_order=["a"], aggregate="median")
    assert [r.epoch for r in reports[0].runs] == ["1", "2", "10"]


# --- Judge score loading ---

def _write_scores(d, task, variant, epoch, scores):
    f = d / f"{task}_{variant}_epoch{epoch}.scores.json"
    f.write_text(json.dumps(scores), encoding="utf-8")


def test_load_judge_raw(tmp_path):
    _write_scores(tmp_path, "t1", "a", "1", [
        {"name": "quality", "type": "judge", "score": 8, "reason": "good"},
        {"name": "speed", "type": "judge", "score": 5, "reason": "ok"},
    ])
    _write_scores(tmp_path, "t1", "b", "1", [
        {"name": "quality", "type": "judge", "score": 6, "reason": "meh"},
    ])
    epoch_data, reasons, names, stddevs = _load_judge_raw(tmp_path, ["a", "b"], "t1")
    assert names == ["quality", "speed"]
    assert epoch_data[("a", "1")] == {"quality": 8, "speed": 5}
    assert epoch_data[("b", "1")] == {"quality": 6}
    assert reasons[("a", "1")]["quality"] == "good"


def test_load_judge_raw_skips_null_scores(tmp_path):
    _write_scores(tmp_path, "t1", "a", "1", [
        {"name": "quality", "type": "judge", "score": None, "reason": "timeout"},
        {"name": "speed", "type": "judge", "score": 7},
    ])
    epoch_data, _, names, _ = _load_judge_raw(tmp_path, ["a"], "t1")
    assert names == ["speed"]
    assert epoch_data[("a", "1")] == {"speed": 7}


def test_load_judge_raw_missing_dir(tmp_path):
    assert _load_judge_raw(tmp_path / "nope", ["a"], "t1") == ({}, {}, [], {})


def test_load_judge_raw_matches_longest_variant(tmp_path):
    # variants "v" and "my_v": a file for "my_v" must not be claimed by "v".
    _write_scores(tmp_path, "t1", "my_v", "1", [{"name": "q", "score": 9}])
    epoch_data, _, _, _ = _load_judge_raw(tmp_path, ["v", "my_v"], "t1")
    assert ("my_v", "1") in epoch_data
    assert ("v", "1") not in epoch_data


def test_build_report_judge_paired_by_epoch(tmp_path):
    # variant b missing epoch 2 -> paired judge delta uses common epoch {1}
    _write_scores(tmp_path, "t1", "a", "1", [{"name": "q", "score": 4}])
    _write_scores(tmp_path, "t1", "a", "2", [{"name": "q", "score": 10}])
    _write_scores(tmp_path, "t1", "b", "1", [{"name": "q", "score": 6}])
    results = [
        make_metrics("t1", "a", "1"),
        make_metrics("t1", "a", "2"),
        make_metrics("t1", "b", "1"),
    ]
    reports = build_report(results, tmp_path, ["a", "b"], "paired")
    judge_row = next(r for r in reports[0].judge_scores if r.metric == "q")
    # common epoch {1}: delta 6-4=2; paired baseline = median([4]) = 4 -> +50.0%
    assert judge_row.delta == "+50.0%"
