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


def test_build_report_surfaces_cost_metric():
    results = [
        make_metrics("t1", "a", "1", cost=10.0),
        make_metrics("t1", "a", "2", cost=20.0),
        make_metrics("t1", "b", "1", cost=12.0),
        make_metrics("t1", "b", "2", cost=24.0),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    cost_row = next(r for r in reports[0].summary if r.metric == "Cost ($)")
    assert cost_row.values["a"] == 15.0  # median(10, 20)
    assert cost_row.values["b"] == 18.0  # median(12, 24)
    assert cost_row.precision == 4  # small fractional costs need extra decimals
    # paired deltas: (12-10)=2, (24-20)=4 -> median 3; baseline median([10,20])=15 -> +20.0%
    assert cost_row.delta == "+20.0%"


def test_cost_renders_with_higher_precision():
    # Realistic sub-dollar costs would collapse to "0.0" at 1-decimal precision;
    # they must survive into the summary and per-run tables with real digits.
    from eval.report import format_markdown, format_table

    results = [
        make_metrics("t1", "a", "1", cost=0.0412),
        make_metrics("t1", "a", "2", cost=0.0388),
        make_metrics("t1", "b", "1", cost=0.0611),
        make_metrics("t1", "b", "2", cost=0.0589),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")

    table = format_table(reports)
    cost_line = next(line for line in table.splitlines() if line.startswith("Cost ($)"))
    assert "0.0400" in cost_line  # median(0.0412, 0.0388), not "0.0"
    assert "0.0600" in cost_line  # median(0.0611, 0.0589)

    md = format_markdown(reports)
    cost_md = next(line for line in md.splitlines() if line.startswith("| Cost ($)"))
    assert "0.0400" in cost_md and "0.0600" in cost_md

    # Per-run tables (table + markdown) carry a Cost($) column with real digits.
    assert "Cost($)" in table
    assert any("0.0412" in line for line in table.splitlines())
    assert "Cost($)" in md
    assert any("0.0412" in line for line in md.splitlines())


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
    _write_scores(
        tmp_path,
        "t1",
        "a",
        "1",
        [
            {"name": "quality", "type": "judge", "score": 8, "reason": "good"},
            {"name": "speed", "type": "judge", "score": 5, "reason": "ok"},
        ],
    )
    _write_scores(
        tmp_path,
        "t1",
        "b",
        "1",
        [
            {"name": "quality", "type": "judge", "score": 6, "reason": "meh"},
        ],
    )
    epoch_data, reasons, names, stddevs = _load_judge_raw(tmp_path, ["a", "b"], "t1")
    assert names == ["quality", "speed"]
    assert epoch_data[("a", "1")] == {"quality": 8, "speed": 5}
    assert epoch_data[("b", "1")] == {"quality": 6}
    assert reasons[("a", "1")]["quality"] == "good"


def test_load_judge_raw_skips_null_scores(tmp_path):
    _write_scores(
        tmp_path,
        "t1",
        "a",
        "1",
        [
            {"name": "quality", "type": "judge", "score": None, "reason": "timeout"},
            {"name": "speed", "type": "judge", "score": 7},
        ],
    )
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


# --- Multiple fixtures (input-coverage axis) ---


def _write_scores_fx(d, task, variant, epoch, fixture, scores):
    from eval.naming import run_slug

    f = d / f"{run_slug(task, variant, epoch, fixture)}.scores.json"
    f.write_text(json.dumps(scores), encoding="utf-8")


def test_single_fixture_runs_unchanged():
    # Empty fixture label -> report epoch labels are the bare epoch (legacy).
    results = [make_metrics("t1", "a", "1"), make_metrics("t1", "a", "2")]
    reports = build_report(results, variant_order=["a"], aggregate="median")
    assert sorted(r.epoch for r in reports[0].runs) == ["1", "2"]


def test_multi_fixture_pooled_paired_delta():
    # Two fixtures, plugin consistently faster within each (fixture, epoch) cell.
    results = [
        make_metrics("cr", "base", "1", duration=10, fixture="fixA"),
        make_metrics("cr", "plugin", "1", duration=8, fixture="fixA"),
        make_metrics("cr", "base", "1", duration=20, fixture="fixB"),
        make_metrics("cr", "plugin", "1", duration=16, fixture="fixB"),
    ]
    reports = build_report(results, None, ["base", "plugin"], "paired")
    report = reports[0]
    # Runs are labelled by fixture#epoch so the per-fixture breakdown is visible.
    assert {r.epoch for r in report.runs} == {"fixA#1", "fixB#1"}
    # Paired delta pools across both fixtures: 2 paired cells.
    assert report.paired_n == 2
    dur = next(s for s in report.summary if s.metric == "Duration (s)")
    # deltas: -2/10=-20%, -4/20=-20% -> median -20.0%
    assert dur.delta == "-20.0%"


def test_multi_fixture_does_not_pair_across_fixtures():
    # base only ran fixA, plugin only ran fixB -> no shared (fixture, epoch) cell.
    results = [
        make_metrics("cr", "base", "1", duration=10, fixture="fixA"),
        make_metrics("cr", "plugin", "1", duration=8, fixture="fixB"),
    ]
    reports = build_report(results, None, ["base", "plugin"], "paired")
    assert reports[0].paired_n == 0
    dur = next(s for s in reports[0].summary if s.metric == "Duration (s)")
    assert dur.delta == ""


def test_multi_fixture_judge_scores_pool_by_fixture(tmp_path):
    _write_scores_fx(tmp_path, "cr", "base", "1", "fixA", [{"name": "q", "score": 4}])
    _write_scores_fx(tmp_path, "cr", "plugin", "1", "fixA", [{"name": "q", "score": 6}])
    _write_scores_fx(tmp_path, "cr", "base", "1", "fixB", [{"name": "q", "score": 8}])
    _write_scores_fx(tmp_path, "cr", "plugin", "1", "fixB", [{"name": "q", "score": 10}])
    epoch_data, _, names, _ = _load_judge_raw(tmp_path, ["base", "plugin"], "cr")
    assert names == ["q"]
    # Keyed by (variant, fixture#epoch) so each fixture stays distinct.
    assert epoch_data[("base", "fixA#1")] == {"q": 4}
    assert epoch_data[("plugin", "fixB#1")] == {"q": 10}

    results = [
        make_metrics("cr", "base", "1", fixture="fixA"),
        make_metrics("cr", "plugin", "1", fixture="fixA"),
        make_metrics("cr", "base", "1", fixture="fixB"),
        make_metrics("cr", "plugin", "1", fixture="fixB"),
    ]
    reports = build_report(results, tmp_path, ["base", "plugin"], "paired")
    judge_row = next(r for r in reports[0].judge_scores if r.metric == "q")
    # deltas: +2 over baseline 4 (fixA) and +2 over baseline 8 (fixB)
    # -> pct uses paired baseline median([4,8])=6 -> +2/6 = +33.3%
    assert judge_row.delta == "+33.3%"


# --- Judge runtime aggregation ---
def test_load_judge_runtime_aggregates(tmp_path):
    from eval.report import _load_judge_runtime

    _write_scores(
        tmp_path,
        "t1",
        "a",
        "1",
        [
            {
                "name": "q",
                "type": "judge",
                "score": 8,
                "meta": {"outcome": "ok", "judge_version": "copilot/1.0.18"},
            },
            {
                "name": "s",
                "type": "judge",
                "score": None,
                "meta": {
                    "outcome": "parse_error",
                    "judge_version": "copilot/1.0.18",
                    "truncation": {"conversation": 8000},
                },
            },
        ],
    )
    _write_scores(
        tmp_path,
        "t1",
        "b",
        "1",
        [
            {
                "name": "q",
                "type": "judge",
                "score": None,
                "meta": {
                    "outcome": "timeout",
                    "judge_version": "copilot/2.0.0",
                    "judge_version_mismatch": {
                        "expected": "copilot/1.0.18",
                        "actual": "copilot/2.0.0",
                    },
                },
            },
        ],
    )
    rt = _load_judge_runtime(tmp_path, ["a", "b"], "t1")
    assert rt["total"] == 3
    assert rt["outcomes"] == {"ok": 1, "parse_error": 1, "timeout": 1}
    assert rt["versions"] == ["copilot/1.0.18", "copilot/2.0.0"]
    assert rt["truncated"] == 1
    assert rt["version_mismatch"] is True


def test_load_judge_runtime_empty_when_no_judges(tmp_path):
    from eval.report import _load_judge_runtime

    _write_scores(tmp_path, "t1", "a", "1", [{"name": "c", "type": "contains", "score": 1}])
    assert _load_judge_runtime(tmp_path, ["a"], "t1") == {}


def test_load_judge_runtime_infers_outcome_without_meta(tmp_path):
    from eval.report import _load_judge_runtime

    _write_scores(
        tmp_path,
        "t1",
        "a",
        "1",
        [
            {"name": "q", "type": "judge", "score": 7},
            {"name": "s", "type": "judge", "score": None, "reason": "timeout"},
        ],
    )
    rt = _load_judge_runtime(tmp_path, ["a"], "t1")
    assert rt["outcomes"] == {"ok": 1, "unknown": 1}


def test_build_report_attaches_judge_runtime(tmp_path):
    _write_scores(
        tmp_path,
        "t1",
        "a",
        "1",
        [
            {"name": "q", "type": "judge", "score": 8, "meta": {"outcome": "ok"}},
        ],
    )
    results = [make_metrics("t1", "a", "1")]
    reports = build_report(results, tmp_path, ["a"], "median")
    assert reports[0].judge_runtime["outcomes"] == {"ok": 1}


# --- Dispersion + significance helpers ---


def test_stddev_and_min_max():
    from eval.report import _min_max, _stddev

    assert _stddev([5.0]) == 0.0  # single sample
    assert round(_stddev([2.0, 4.0, 6.0]), 4) == 2.0
    assert _min_max([3.0, 1.0, 2.0]) == (1.0, 3.0)
    assert _min_max([]) == (0.0, 0.0)


def test_bootstrap_ci_deterministic_and_excludes_zero():
    from eval.report import _bootstrap_ci, _ci_significant

    # All deltas clearly positive -> CI should exclude 0 -> significant.
    deltas = [4.0, 5.0, 6.0, 5.0, 4.0, 6.0]
    ci1 = _bootstrap_ci(deltas)
    ci2 = _bootstrap_ci(deltas)
    assert ci1 == ci2  # deterministic (seeded)
    assert ci1 is not None and ci1[0] > 0
    assert _ci_significant(ci1) is True


def test_bootstrap_ci_includes_zero_not_significant():
    from eval.report import _bootstrap_ci, _ci_significant

    deltas = [-5.0, 6.0, -4.0, 5.0]  # straddles 0
    ci = _bootstrap_ci(deltas)
    assert ci is not None and ci[0] < 0 < ci[1]
    assert _ci_significant(ci) is False


def test_bootstrap_ci_insufficient_samples():
    from eval.report import _bootstrap_ci, _ci_significant

    assert _bootstrap_ci([3.0]) is None
    assert _ci_significant(None) is None


def test_summary_row_carries_n_and_dispersion():
    results = [
        make_metrics("t1", "a", "1", duration=10.0),
        make_metrics("t1", "a", "2", duration=12.0),
        make_metrics("t1", "a", "3", duration=14.0),
        make_metrics("t1", "b", "1", duration=20.0),
        make_metrics("t1", "b", "2", duration=22.0),
        make_metrics("t1", "b", "3", duration=24.0),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    row = next(r for r in reports[0].summary if r.metric == "Duration (s)")
    assert row.n == {"a": 3, "b": 3}
    assert row.vmin["a"] == 10.0 and row.vmax["a"] == 14.0
    assert row.stddev["a"] > 0
    assert row.paired_n == 3
    # CI is computed, but with only 3 paired samples (<MIN_RELIABLE_N) significance
    # must NOT be asserted — that would re-create the "decisive at n=3" failure.
    assert row.ci_low is not None
    assert row.significant is None
    assert reports[0].variant_n == {"a": 3, "b": 3}
    assert reports[0].paired_n == 3


def test_significance_asserted_only_above_threshold():
    from eval.report import MIN_RELIABLE_N

    results = []
    for e in range(1, MIN_RELIABLE_N + 1):
        results.append(make_metrics("t1", "a", str(e), duration=10.0))
        results.append(make_metrics("t1", "b", str(e), duration=20.0))  # uniformly +10
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    row = next(r for r in reports[0].summary if r.metric == "Duration (s)")
    assert row.paired_n == MIN_RELIABLE_N
    # With enough paired samples and a consistent gap, the CI excludes 0.
    assert row.significant is True


def test_low_n_significance_not_rendered_as_star():
    from eval.report import _fmt_delta

    results = [
        make_metrics("t1", "a", "1", duration=10.0),
        make_metrics("t1", "a", "2", duration=10.0),
        make_metrics("t1", "b", "1", duration=20.0),
        make_metrics("t1", "b", "2", duration=20.0),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    row = next(r for r in reports[0].summary if r.metric == "Duration (s)")
    rendered = _fmt_delta(row)
    assert "low-n" in rendered
    assert "*" not in rendered
    assert "abs" in rendered  # CI labelled as absolute units


# --- Small-n warnings ---


def test_small_sample_warning_emitted():
    results = [
        make_metrics("t1", "a", "1"),
        make_metrics("t1", "b", "1"),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    assert any("Small sample size" in w for w in reports[0].warnings)
    assert any("paired epoch" in w for w in reports[0].warnings)


def test_no_warning_with_enough_samples():
    results = []
    for e in range(1, 6):
        results.append(make_metrics("t1", "a", str(e), duration=float(e)))
        results.append(make_metrics("t1", "b", str(e), duration=float(e) + 1))
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    assert reports[0].warnings == []


# --- Reliability (survivorship bias) ---


def _manifest(task, variant, epoch, status="completed", test_id=None, scores=None):
    return {
        "task": task,
        "variant": variant,
        "epoch": epoch,
        "status": status,
        "test_id": test_id or f"{variant}{epoch}",
        "scores": scores or [],
    }


def test_reliability_success_and_timeout_rates():
    manifest = [
        _manifest("t1", "a", 1, "completed", "a1"),
        _manifest("t1", "a", 2, "completed", "a2"),
        _manifest("t1", "b", 1, "completed", "b1"),
        _manifest("t1", "b", 2, "timeout", "b2"),
    ]
    results = [
        make_metrics("t1", "a", "1", test_id="a1"),
        make_metrics("t1", "a", "2", test_id="a2"),
        make_metrics("t1", "b", "1", test_id="b1"),
    ]
    trace_ids = {"a1", "a2", "b1"}
    reports = build_report(
        results,
        variant_order=["a", "b"],
        aggregate="paired",
        manifest_runs=manifest,
        trace_test_ids=trace_ids,
    )
    rel = {row.metric: row.values for row in reports[0].reliability}
    assert rel["Success rate"] == {"a": "100.0%", "b": "50.0%"}
    assert rel["Timeout rate"] == {"a": "0.0%", "b": "50.0%"}


def test_reliability_missing_trace_rate():
    # b epoch 2 completed but produced no trace -> missing, not success.
    manifest = [
        _manifest("t1", "b", 1, "completed", "b1"),
        _manifest("t1", "b", 2, "completed", "b2"),
    ]
    results = [make_metrics("t1", "b", "1", test_id="b1")]
    reports = build_report(
        results,
        variant_order=["b"],
        aggregate="median",
        manifest_runs=manifest,
        trace_test_ids={"b1"},
    )
    rel = {row.metric: row.values for row in reports[0].reliability}
    assert rel["Missing-trace rate"]["b"] == "50.0%"
    assert rel["Success rate"]["b"] == "50.0%"


def test_reliability_judge_coverage(tmp_path):
    # Two successful runs; epoch 1 has a usable judge score, epoch 2's judge
    # produced only a null score (timeout). Coverage = 1/2 successful = 50%.
    manifest = [
        _manifest("t1", "a", 1, "completed", "a1"),
        _manifest("t1", "a", 2, "completed", "a2"),
    ]
    results = [
        make_metrics("t1", "a", "1", test_id="a1"),
        make_metrics("t1", "a", "2", test_id="a2"),
    ]
    _write_scores(
        tmp_path,
        "t1",
        "a",
        "1",
        [
            {"name": "q", "type": "judge", "score": 8, "reason": "ok"},
        ],
    )
    _write_scores(
        tmp_path,
        "t1",
        "a",
        "2",
        [
            {"name": "q", "type": "judge", "score": None, "reason": "timeout"},
        ],
    )
    reports = build_report(
        results, tmp_path, ["a"], "median", manifest_runs=manifest, trace_test_ids={"a1", "a2"}
    )
    rel = {row.metric: row.values for row in reports[0].reliability}
    assert rel["Judge-score coverage"]["a"] == "50.0%"


def test_zero_trace_variant_still_appears_in_reliability():
    # variant b timed out on every epoch -> no traces. It must NOT vanish from
    # the report; reliability should expose its 0% success / 100% timeout.
    manifest = [
        _manifest("t1", "a", 1, "completed", "a1"),
        _manifest("t1", "b", 1, "timeout", "b1"),
        _manifest("t1", "b", 2, "timeout", "b2"),
    ]
    results = [make_metrics("t1", "a", "1", test_id="a1")]
    reports = build_report(
        results,
        variant_order=["a", "b"],
        aggregate="paired",
        manifest_runs=manifest,
        trace_test_ids={"a1"},
    )
    rep = reports[0]
    assert "b" in rep.variants
    assert rep.variant_n["b"] == 0
    rel = {row.metric: row.values for row in rep.reliability}
    assert rel["Success rate"]["b"] == "0.0%"
    assert rel["Timeout rate"]["b"] == "100.0%"


def test_manifest_only_report_when_no_traces():
    # Every run failed -> no surviving metrics. A manifest-only report should
    # still be produced (build_report seeds the task from the manifest).
    manifest = [
        _manifest("t1", "a", 1, "timeout", "a1"),
        _manifest("t1", "b", 1, "failed", "b1"),
    ]
    reports = build_report(
        [],
        variant_order=["a", "b"],
        aggregate="paired",
        manifest_runs=manifest,
        trace_test_ids=set(),
    )
    assert len(reports) == 1
    rel = {row.metric: row.values for row in reports[0].reliability}
    assert rel["Success rate"] == {"a": "0.0%", "b": "0.0%"}
    assert rel["Timeout rate"]["a"] == "100.0%"
    assert rel["Failed rate"]["b"] == "100.0%"


def test_reliability_degrades_without_manifest():
    results = [make_metrics("t1", "a", "1"), make_metrics("t1", "a", "2")]
    reports = build_report(results, variant_order=["a"], aggregate="median")
    rel = {row.metric: row.values for row in reports[0].reliability}
    assert rel["Runs (traces)"]["a"] == "2"


# --- Formatter smoke tests ---


def test_format_json_includes_stats_and_reliability():
    manifest = [
        _manifest("t1", "a", 1, "completed", "a1"),
        _manifest("t1", "b", 1, "completed", "b1"),
    ]
    results = [
        make_metrics("t1", "a", "1", test_id="a1", duration=10.0),
        make_metrics("t1", "b", "1", test_id="b1", duration=12.0),
    ]
    reports = build_report(
        results,
        variant_order=["a", "b"],
        aggregate="paired",
        manifest_runs=manifest,
        trace_test_ids={"a1", "b1"},
    )
    from eval.report import format_json

    data = json.loads(format_json(reports))
    task = data["tasks"][0]
    assert task["variant_n"] == {"a": 1, "b": 1}
    assert "reliability" in task and task["reliability"]
    srow = task["summary"][0]
    assert set(["n", "stddev", "min", "max", "ci_low", "ci_high", "significant"]).issubset(srow)
    assert task["warnings"]  # small-n warning present


def test_format_table_and_markdown_render_reliability():
    manifest = [
        _manifest("t1", "a", 1, "completed", "a1"),
        _manifest("t1", "b", 1, "timeout", "b1"),
    ]
    results = [make_metrics("t1", "a", "1", test_id="a1")]
    reports = build_report(
        results,
        variant_order=["a", "b"],
        aggregate="paired",
        manifest_runs=manifest,
        trace_test_ids={"a1"},
    )
    from eval.report import format_markdown, format_table

    table = format_table(reports)
    md = format_markdown(reports)
    assert "Reliability" in table and "Success rate" in table
    assert "Samples:" in table
    assert "### Reliability" in md and "Success rate" in md
