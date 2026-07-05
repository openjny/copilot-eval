"""Tests for report aggregation, pairing, and judge score loading."""

from __future__ import annotations

import json

from eval.report import (
    MIN_RELIABLE_K,
    MIN_RELIABLE_N,
    _aggregate_values,
    _approx_power,
    _build_pass_k_rows,
    _epoch_sort_key,
    _load_judge_raw,
    _task_run_key,
    build_report,
)
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
    epoch_data, reasons, names, stddevs, passed = _load_judge_raw(tmp_path, ["a", "b"], "t1")
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
    epoch_data, _, names, _, _ = _load_judge_raw(tmp_path, ["a"], "t1")
    assert names == ["speed"]
    assert epoch_data[("a", "1")] == {"speed": 7}


def test_load_judge_raw_missing_dir(tmp_path):
    assert _load_judge_raw(tmp_path / "nope", ["a"], "t1") == ({}, {}, [], {}, {})


def test_load_judge_raw_matches_longest_variant(tmp_path):
    # variants "v" and "my_v": a file for "my_v" must not be claimed by "v".
    _write_scores(tmp_path, "t1", "my_v", "1", [{"name": "q", "score": 9}])
    epoch_data, _, _, _, _ = _load_judge_raw(tmp_path, ["v", "my_v"], "t1")
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
    epoch_data, _, names, _, _ = _load_judge_raw(tmp_path, ["base", "plugin"], "cr")
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


# --- Multiple-comparison correction (issue #71) ---


def test_holm_bonferroni_rejects_only_a_significant_prefix():
    from eval.report import _holm_bonferroni

    # Sorted p-values 0.001, 0.01, 0.02, 0.5 against alpha=0.05 and m=4:
    # thresholds are 0.0125, 0.01667, 0.025, 0.05 -- 0.001 and 0.01 clear their
    # thresholds, 0.02 clears 0.025, 0.5 fails, so all three smallest reject.
    p_values = [0.5, 0.001, 0.02, 0.01]
    decisions = _holm_bonferroni(p_values)
    assert decisions == [False, True, True, True]


def test_holm_bonferroni_step_down_stops_at_first_failure():
    from eval.report import _holm_bonferroni

    # Even though the third p-value (0.05) alone would clear a Bonferroni-style
    # 0.05/3 cutoff for *some* rank, Holm's step-down property means once an
    # earlier-ranked hypothesis fails, nothing after it can be rejected either.
    p_values = [0.5, 0.5, 0.02]
    decisions = _holm_bonferroni(p_values)
    # sorted: 0.02 (thr=0.05/3=0.0167 -> fails) -> stop; nothing rejected.
    assert decisions == [False, False, False]


def test_holm_bonferroni_empty_input():
    from eval.report import _holm_bonferroni

    assert _holm_bonferroni([]) == []


def test_benjamini_hochberg_more_permissive_than_holm():
    from eval.report import _benjamini_hochberg, _holm_bonferroni

    # A classic case where BH (FDR) rejects more than Holm (FWER) for the same
    # p-values -- BH is intentionally less conservative.
    p_values = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205]
    holm = sum(_holm_bonferroni(p_values))
    bh = sum(_benjamini_hochberg(p_values))
    assert bh >= holm


def test_benjamini_hochberg_empty_input():
    from eval.report import _benjamini_hochberg

    assert _benjamini_hochberg([]) == []


def test_apply_mc_correction_only_downgrades_never_upgrades():
    from eval.report import SummaryRow, _apply_mc_correction

    # Two "significant" rows (small p) and one "not significant" row (p=1).
    # Correction may turn a True into False, but must never turn False/None
    # into True -- that would grant significance a raw CI check didn't support.
    rows = [
        SummaryRow(metric="a", values={}, paired_n=5, p_value=0.001, significant=True),
        SummaryRow(metric="b", values={}, paired_n=5, p_value=0.04, significant=True),
        SummaryRow(metric="c", values={}, paired_n=5, p_value=1.0, significant=False),
    ]
    tests = _apply_mc_correction(rows, "holm")
    assert tests == 3
    assert rows[0].significant is True  # p=0.001 survives alpha/3
    assert rows[2].significant is False  # was already False; stays False


def test_apply_mc_correction_skips_untestable_rows():
    from eval.report import MIN_RELIABLE_N, SummaryRow, _apply_mc_correction

    # A row below MIN_RELIABLE_N (significant=None) or with no p-value at all
    # must not count towards the family size or be touched by correction.
    rows = [
        SummaryRow(metric="a", values={}, paired_n=5, p_value=0.001, significant=True),
        SummaryRow(metric="b", values={}, paired_n=2, p_value=0.001, significant=None),
        SummaryRow(metric="c", values={}, paired_n=0, p_value=None, significant=None),
    ]
    tests = _apply_mc_correction(rows, "holm")
    assert tests == 1
    assert rows[0].significant is True
    assert rows[1].significant is None
    assert rows[2].significant is None
    assert rows[1].paired_n < MIN_RELIABLE_N  # sanity: below-threshold row untouched


def test_apply_mc_correction_none_method_is_a_no_op():
    from eval.report import SummaryRow, _apply_mc_correction

    rows = [SummaryRow(metric="a", values={}, paired_n=5, p_value=0.04, significant=True)]
    tests = _apply_mc_correction(rows, "none")
    assert tests == 0
    assert rows[0].significant is True  # untouched


def test_apply_mc_correction_rejects_unknown_method():
    from eval.report import SummaryRow, _apply_mc_correction

    rows = [SummaryRow(metric="a", values={}, paired_n=5, p_value=0.04, significant=True)]
    try:
        _apply_mc_correction(rows, "not-a-real-method")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_mc_correction_suppresses_isolated_false_positive():
    """A single borderline-significant metric among many independently tested
    OTel metrics loses its raw `*` once Holm-Bonferroni is applied across the
    task's family -- the false-positive-by-chance scenario issue #71 exists to
    fix (a lone metric "looking" significant purely because ~9-11 tests ran at
    alpha=0.05 each).
    """
    results = []
    # 7 epochs of a clean +1 delta, one epoch of a -1 delta: raw CI (1.0, 1.0)
    # excludes 0 with p ~= 0.014 -- individually "significant" at alpha=0.05,
    # but not once corrected alongside the other 8 (constant, p=1) OTel metrics.
    for e in range(1, 8):
        results.append(make_metrics("t1", "a", str(e), duration=10.0))
        results.append(make_metrics("t1", "b", str(e), duration=11.0))
    results.append(make_metrics("t1", "a", "8", duration=10.0))
    results.append(make_metrics("t1", "b", "8", duration=9.0))

    corrected = build_report(results, variant_order=["a", "b"], aggregate="paired")
    uncorrected = build_report(
        results, variant_order=["a", "b"], aggregate="paired", mc_correction="none"
    )

    dur_uncorrected = next(r for r in uncorrected[0].summary if r.metric == "Duration (s)")
    dur_corrected = next(r for r in corrected[0].summary if r.metric == "Duration (s)")

    assert dur_uncorrected.significant is True  # raw CI-excludes-zero check
    assert dur_corrected.significant is False  # corrected away
    assert corrected[0].mc_correction == "holm"
    assert corrected[0].mc_tests == 9
    assert uncorrected[0].mc_correction == "none"
    assert uncorrected[0].mc_tests == 0


def test_mc_correction_default_is_holm():
    results = []
    for e in range(1, 6):
        results.append(make_metrics("t1", "a", str(e), duration=float(e)))
        results.append(make_metrics("t1", "b", str(e), duration=float(e) + 5.0))
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    assert reports[0].mc_correction == "holm"


def test_mc_correction_benjamini_hochberg_selectable():
    results = []
    for e in range(1, 6):
        results.append(make_metrics("t1", "a", str(e), duration=float(e)))
        results.append(make_metrics("t1", "b", str(e), duration=float(e) + 5.0))
    reports = build_report(
        results, variant_order=["a", "b"], aggregate="paired", mc_correction="benjamini-hochberg"
    )
    assert reports[0].mc_correction == "benjamini-hochberg"
    dur = next(r for r in reports[0].summary if r.metric == "Duration (s)")
    assert dur.significant is True  # consistent +5 gap survives either correction


# --- Small-n warnings ---


def test_small_sample_warning_emitted():
    results = [
        make_metrics("t1", "a", "1"),
        make_metrics("t1", "b", "1"),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    messages = [w["message"] for w in reports[0].warnings]
    assert any("Small sample size" in m for m in messages)
    assert any(w["type"] == "low_power" for w in reports[0].warnings)
    low_power = next(w for w in reports[0].warnings if w["type"] == "low_power")
    assert low_power["paired_n"] == 1
    assert "0.5" in low_power["power"]


def test_no_warning_with_enough_samples():
    results = []
    for e in range(1, 6):
        results.append(make_metrics("t1", "a", str(e), duration=float(e)))
        results.append(make_metrics("t1", "b", str(e), duration=float(e) + 1))
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    assert reports[0].warnings == []


# --- Power approximation ---


def test_approx_power_increases_with_n_and_effect_size():
    # Power should monotonically increase as N grows for a fixed effect size...
    powers_by_n = [_approx_power(n, 0.5) for n in (2, 5, 10, 30)]
    assert powers_by_n == sorted(powers_by_n)
    # ...and as the effect size grows for a fixed N.
    powers_by_d = [_approx_power(10, d) for d in (0.2, 0.5, 0.8)]
    assert powers_by_d == sorted(powers_by_d)


def test_approx_power_bounds():
    assert _approx_power(1, 0.5) == 0.0  # undefined below n=2
    for n in (2, 3, 5, 10, 50):
        for d in (0.2, 0.5, 0.8, 2.0):
            p = _approx_power(n, d)
            assert 0.0 <= p <= 1.0


def test_low_power_warning_includes_power_for_medium_effect():
    results = [
        make_metrics("t1", "a", "1"),
        make_metrics("t1", "a", "2"),
        make_metrics("t1", "a", "3"),
        make_metrics("t1", "b", "1"),
        make_metrics("t1", "b", "2"),
        make_metrics("t1", "b", "3"),
    ]
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    assert reports[0].paired_n == 3 < MIN_RELIABLE_N
    low_power = next(w for w in reports[0].warnings if w["type"] == "low_power")
    assert set(low_power["power"]) == {"0.2", "0.5", "0.8"}
    assert "LOW STATISTICAL POWER" in low_power["message"]
    assert "N=3 paired epochs" in low_power["message"]


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


# --- pass@k / pass^k reliability ---


def test_task_run_key_single_fixture_pools_into_one_group():
    assert _task_run_key("1") == ""
    assert _task_run_key("2") == ""


def test_task_run_key_multi_fixture_groups_by_fixture():
    assert _task_run_key("fixA#1") == "fixA"
    assert _task_run_key("fixA#2") == "fixA"
    assert _task_run_key("fixB#1") == "fixB"


def test_build_pass_k_rows_single_fixture_is_binary(tmp_path):
    # One task-run (no fixture) with k=3 epochs: 2 pass, 1 fail -> pass@3=100%
    # (any succeeded), pass^3=0% (not all succeeded).
    _write_scores(tmp_path, "t1", "a", "1", [{"name": "q", "type": "judge", "score": 1}])
    _write_scores(tmp_path, "t1", "a", "2", [{"name": "q", "type": "judge", "score": 1}])
    _write_scores(tmp_path, "t1", "a", "3", [{"name": "q", "type": "judge", "score": 0}])
    _, _, names, _, epoch_passed = _load_judge_raw(tmp_path, ["a"], "t1")
    rows, min_k = _build_pass_k_rows(epoch_passed, ["a"], names, "paired")
    assert min_k == 3
    at_k = next(r for r in rows if r.metric == "pass@3 (q)")
    all_k = next(r for r in rows if r.metric == "pass^3 (q)")
    assert at_k.values["a"] == 100.0
    assert all_k.values["a"] == 0.0


def test_build_pass_k_rows_multi_fixture_rate_and_ci(tmp_path):
    # 3 fixtures (task-runs) x k=3 epochs each, for two variants.
    # baseline: 2/3 fixtures pass every epoch (pass^k=1), 1/3 has a failure.
    # experimental: all 3/3 fixtures pass every epoch.
    fixtures = {
        "fixA": {"baseline": [1, 1, 1], "candidate": [1, 1, 1]},
        "fixB": {"baseline": [1, 1, 1], "candidate": [1, 1, 1]},
        "fixC": {"baseline": [1, 1, 0], "candidate": [1, 1, 1]},
    }
    for fx, by_variant in fixtures.items():
        for variant, scores in by_variant.items():
            for i, s in enumerate(scores, start=1):
                _write_scores_fx(
                    tmp_path,
                    "t1",
                    variant,
                    str(i),
                    fx,
                    [{"name": "q", "type": "judge", "score": s}],
                )
    _, _, names, _, epoch_passed = _load_judge_raw(tmp_path, ["baseline", "candidate"], "t1")
    rows, min_k = _build_pass_k_rows(epoch_passed, ["baseline", "candidate"], names, "paired")
    assert min_k == 3
    at_k = next(r for r in rows if r.metric == "pass@3 (q)")
    all_k = next(r for r in rows if r.metric == "pass^3 (q)")
    # pass@3: every fixture had at least one success in all 3 epochs -> 100% both.
    assert at_k.values == {"baseline": 100.0, "candidate": 100.0}
    assert at_k.delta == "+0%"
    # pass^3: baseline 2/3 fixtures fully passed -> 66.7%; candidate 3/3 -> 100%.
    assert round(all_k.values["baseline"], 1) == 66.7
    assert all_k.values["candidate"] == 100.0
    assert all_k.delta == "+33%"
    # 3 paired task-runs (fixtures) -> bootstrap CI is computable.
    assert all_k.paired_n == 3
    assert all_k.ci_low is not None
    assert all_k.ci_high is not None


def test_build_pass_k_rows_no_data_skips_evaluator():
    rows, min_k = _build_pass_k_rows({}, ["a", "b"], ["q"], "paired")
    assert rows == []
    assert min_k is None


def test_build_report_warns_when_k_below_minimum(tmp_path):
    # Only k=2 epochs scored -> below MIN_RELIABLE_K (3), warning expected.
    _write_scores(tmp_path, "t1", "a", "1", [{"name": "q", "type": "judge", "score": 1}])
    _write_scores(tmp_path, "t1", "a", "2", [{"name": "q", "type": "judge", "score": 1}])
    results = [make_metrics("t1", "a", "1"), make_metrics("t1", "a", "2")]
    reports = build_report(results, tmp_path, ["a"], "median")
    assert reports[0].pass_k  # rows are still produced, just flagged as low-k
    insufficient = [w for w in reports[0].warnings if w["type"] == "insufficient_k"]
    assert insufficient
    assert insufficient[0]["k"] == 2
    assert insufficient[0]["min_reliable_k"] == MIN_RELIABLE_K


def test_build_report_no_warning_when_k_meets_minimum(tmp_path):
    for i in range(1, 4):
        _write_scores(tmp_path, "t1", "a", str(i), [{"name": "q", "type": "judge", "score": 1}])
    results = [make_metrics("t1", "a", str(i)) for i in range(1, 4)]
    reports = build_report(results, tmp_path, ["a"], "median")
    assert not [w for w in reports[0].warnings if w["type"] == "insufficient_k"]


def test_build_report_pass_k_missing_passed_defaults_to_score_truthiness(tmp_path):
    # Hand-written scores.json without a "passed" key: falls back to bool(score),
    # matching production for deterministic evaluator types (contains/regex/
    # script/metric all derive score from passed 1:1).
    _write_scores(tmp_path, "t1", "a", "1", [{"name": "lint", "type": "metric", "score": 1}])
    _write_scores(tmp_path, "t1", "a", "2", [{"name": "lint", "type": "metric", "score": 0}])
    _write_scores(tmp_path, "t1", "a", "3", [{"name": "lint", "type": "metric", "score": 1}])
    results = [make_metrics("t1", "a", str(i)) for i in range(1, 4)]
    reports = build_report(results, tmp_path, ["a"], "median")
    all_k = next(r for r in reports[0].pass_k if r.metric == "pass^3 (lint)")
    assert all_k.values["a"] == 0.0  # epoch 2 failed -> not all-passing
    at_k = next(r for r in reports[0].pass_k if r.metric == "pass@3 (lint)")
    assert at_k.values["a"] == 100.0  # epochs 1 and 3 passed


# --- Compact markdown (PR comment) ---


def _paired_results(scenario: str, n: int, base: float, delta_per_epoch: float) -> list:
    """n paired epochs of tight-variance metrics so the paired delta is CI-significant."""
    results = []
    for i in range(1, n + 1):
        jitter = (i % 3) * 0.01
        results.append(make_metrics(scenario, "baseline", str(i), duration=base + jitter))
        results.append(
            make_metrics(scenario, "candidate", str(i), duration=base + delta_per_epoch + jitter)
        )
    return results


def test_format_markdown_compact_header_and_table():
    from eval.report import format_markdown_compact

    results = _paired_results("code_review", 10, base=20.0, delta_per_epoch=-3.0)
    reports = build_report(results, variant_order=["baseline", "candidate"], aggregate="paired")

    out = format_markdown_compact(reports)
    assert out.startswith("## 📊 copilot-eval: code_review")
    assert "| Metric | baseline | candidate | Δ |" in out
    assert "Reliability" not in out  # per-run/reliability detail dropped in compact mode
    assert "Per-Run Details" not in out


def test_format_markdown_compact_marks_improvement_and_regression():
    from eval.report import format_markdown_compact

    # Duration DOWN is an improvement (lower-is-better metric) -> ✅.
    improved = build_report(
        _paired_results("t_improve", 10, base=20.0, delta_per_epoch=-5.0),
        variant_order=["baseline", "candidate"],
        aggregate="paired",
    )
    out_improved = format_markdown_compact(improved)
    row = next(ln for ln in out_improved.splitlines() if ln.startswith("| Duration"))
    assert "✅" in row and "**-" in row

    # Duration UP is a regression (lower-is-better metric) -> ❌.
    regressed = build_report(
        _paired_results("t_regress", 10, base=20.0, delta_per_epoch=5.0),
        variant_order=["baseline", "candidate"],
        aggregate="paired",
    )
    out_regressed = format_markdown_compact(regressed)
    row = next(ln for ln in out_regressed.splitlines() if ln.startswith("| Duration"))
    assert "❌" in row and "**+" in row


def test_format_markdown_compact_no_marker_when_not_significant():
    from eval.report import format_markdown_compact

    # n=3 paired epochs is below MIN_RELIABLE_N -> never asserted significant.
    results = _paired_results("t_lown", 3, base=20.0, delta_per_epoch=-5.0)
    reports = build_report(results, variant_order=["baseline", "candidate"], aggregate="paired")

    out = format_markdown_compact(reports)
    row = next(ln for ln in out.splitlines() if ln.startswith("| Duration"))
    assert "✅" not in row and "❌" not in row and "**" not in row


def test_format_markdown_compact_ci_summary_line():
    from eval.report import format_markdown_compact

    reports = build_report(
        _paired_results("t_ci", 10, base=20.0, delta_per_epoch=-5.0),
        variant_order=["baseline", "candidate"],
        aggregate="paired",
    )
    out = format_markdown_compact(reports)
    assert "95% CI excludes zero for Duration (s)" in out
    assert "N=10 paired epochs" in out


def test_format_markdown_compact_no_significant_metrics_still_summarizes():
    from eval.report import format_markdown_compact

    results = [make_metrics("t_flat", "baseline", str(i), duration=20.0) for i in range(1, 6)] + [
        make_metrics("t_flat", "candidate", str(i), duration=20.0) for i in range(1, 6)
    ]
    reports = build_report(results, variant_order=["baseline", "candidate"], aggregate="paired")

    out = format_markdown_compact(reports)
    assert "No metric's 95% CI excludes zero" in out


def test_format_markdown_compact_single_variant_has_no_delta_column():
    from eval.report import format_markdown_compact

    results = [make_metrics("t_solo", "only", str(i), duration=5.0) for i in range(1, 4)]
    reports = build_report(results, variant_order=["only"], aggregate="median")

    out = format_markdown_compact(reports)
    assert "Δ" not in out


def test_format_markdown_compact_omits_per_run_and_tool_usage_sections():
    from eval.report import format_markdown, format_markdown_compact

    results = _paired_results("t_size", 5, base=20.0, delta_per_epoch=-3.0)
    reports = build_report(results, variant_order=["baseline", "candidate"], aggregate="paired")

    full = format_markdown(reports)
    compact = format_markdown_compact(reports)
    assert "### Tool Usage" in full and "### Tool Usage" not in compact
    assert "### Per-Run Details" in full and "### Per-Run Details" not in compact
    assert len(compact) < len(full)


def test_truncate_for_pr_comment_under_limit_is_unchanged():
    from eval.report import _truncate_for_pr_comment

    text = "short report"
    assert _truncate_for_pr_comment(text, limit=1000) == text


def test_truncate_for_pr_comment_enforces_char_limit():
    from eval.report import _truncate_for_pr_comment

    text = "\n".join(f"line {i} of a very long report" for i in range(10_000))
    assert len(text) > 5000

    out = _truncate_for_pr_comment(text, limit=5000)
    assert len(out) <= 5000
    assert "truncated" in out.lower()
    assert out.startswith("line 0 of a very long report")


def test_format_markdown_compact_stays_within_github_comment_limit():
    from eval.report import PR_COMMENT_CHAR_LIMIT, format_markdown_compact

    # Many tasks in one run, each with paired judge scores -- the kind of
    # config that would blow past 65KB in the full (non-compact) format.
    many_reports = []
    for t in range(60):
        results = _paired_results(f"task_{t}", 8, base=20.0, delta_per_epoch=-1.0)
        many_reports.extend(
            build_report(results, variant_order=["baseline", "candidate"], aggregate="paired")
        )

    out = format_markdown_compact(many_reports)
    assert len(out) <= PR_COMMENT_CHAR_LIMIT


# --- Colored terminal output for significant deltas (issue #91) ---


def _duration_delta_line(table: str) -> str:
    return next(ln for ln in table.splitlines() if ln.startswith("Duration (s)"))


def test_format_table_colors_significant_improvement_green():

    from eval.report import format_table

    reports = build_report(
        _paired_results("t_improve", 10, base=20.0, delta_per_epoch=-5.0),
        variant_order=["baseline", "candidate"],
        aggregate="paired",
    )
    row = next(r for r in reports[0].summary if r.metric == "Duration (s)")
    assert row.significant is True  # guard: the fixture is genuinely significant

    out = format_table(reports, color=True)
    line = _duration_delta_line(out)
    # Lower-is-better metric going down -> green + bold.
    assert "\x1b[32m" in line and "\x1b[1m" in line
    assert "\x1b[0m" in line  # reset present


def test_format_table_colors_significant_regression_red():

    from eval.report import format_table

    reports = build_report(
        _paired_results("t_regress", 10, base=20.0, delta_per_epoch=5.0),
        variant_order=["baseline", "candidate"],
        aggregate="paired",
    )
    out = format_table(reports, color=True)
    line = _duration_delta_line(out)
    # Lower-is-better metric going up -> red + bold.
    assert "\x1b[31m" in line and "\x1b[1m" in line


def test_format_table_dims_non_significant_delta():

    from eval.report import format_table

    # 6 paired epochs where the sign of the delta flips epoch to epoch, so the
    # bootstrap CI straddles zero -> not significant (ns), but still testable
    # (>= MIN_RELIABLE_N paired samples, so significant is False, not None).
    diffs = [1.0, -1.2, 0.8, -0.9, 1.1, -1.0]
    results = []
    for i, d in enumerate(diffs, start=1):
        results.append(make_metrics("t1", "a", str(i), duration=10.0))
        results.append(make_metrics("t1", "b", str(i), duration=10.0 + d))
    reports = build_report(results, variant_order=["a", "b"], aggregate="paired")
    row = next(r for r in reports[0].summary if r.metric == "Duration (s)")
    assert row.significant is False

    out = format_table(reports, color=True)
    line = _duration_delta_line(out)
    assert "\x1b[2m" in line  # dim escape present


def test_format_table_no_color_when_disabled():
    from eval.report import format_table

    reports = build_report(
        _paired_results("t_improve", 10, base=20.0, delta_per_epoch=-5.0),
        variant_order=["baseline", "candidate"],
        aggregate="paired",
    )
    out = format_table(reports, color=False)
    assert "\x1b[" not in out  # no ANSI escape codes at all


def test_format_table_auto_detect_disables_color_when_piped():
    from eval.report import format_table

    # Default (color=None) auto-detects; pytest captures stdout (not a TTY),
    # so no color is emitted even without passing color explicitly.
    reports = build_report(
        _paired_results("t_improve", 10, base=20.0, delta_per_epoch=-5.0),
        variant_order=["baseline", "candidate"],
        aggregate="paired",
    )
    assert "\x1b[" not in format_table(reports)


def test_format_table_colors_higher_is_better_metric_direction():
    # Judge scores are higher-is-better: a significant *increase* is an
    # improvement (green), a significant *decrease* is a regression (red).
    # This covers the `lower_is_better=False` branch of _colorize_delta, which
    # the summary-metric tests above (lower-is-better) never exercise.
    from eval.report import Report, SummaryRow, format_table

    def _report_with_judge(row: SummaryRow) -> Report:
        return Report(
            task="t",
            runs=[],
            variants=["baseline", "candidate"],
            summary=[],
            tool_patterns={},
            judge_scores=[row],
        )

    improved = SummaryRow(
        metric="thoroughness",
        values={"baseline": 6.9, "candidate": 8.1},
        delta="+17.4%",
        n={"baseline": 5, "candidate": 5},
        paired_n=5,
        ci_low=0.8,
        ci_high=1.6,  # CI entirely > 0 -> score increased
        significant=True,
    )
    out = format_table([_report_with_judge(improved)], color=True)
    line = next(ln for ln in out.splitlines() if ln.startswith("thoroughness"))
    assert "\x1b[32m" in line and "\x1b[1m" in line  # green + bold

    regressed = SummaryRow(
        metric="thoroughness",
        values={"baseline": 8.1, "candidate": 6.9},
        delta="-14.8%",
        n={"baseline": 5, "candidate": 5},
        paired_n=5,
        ci_low=-1.6,
        ci_high=-0.8,  # CI entirely < 0 -> score decreased
        significant=True,
    )
    out = format_table([_report_with_judge(regressed)], color=True)
    line = next(ln for ln in out.splitlines() if ln.startswith("thoroughness"))
    assert "\x1b[31m" in line and "\x1b[1m" in line  # red + bold


def test_stdout_supports_color_empty_no_color_does_not_disable(monkeypatch):
    # Per the no-color.org spec, NO_COLOR disables color only when present AND
    # non-empty. An empty value must NOT disable it. Lock that behavior in.
    from eval.report import _stdout_supports_color

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("NO_COLOR", "")
    assert _stdout_supports_color(_Tty()) is True


def test_stdout_supports_color_honors_no_color(monkeypatch):
    from eval.report import _stdout_supports_color

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert _stdout_supports_color(_Tty()) is True

    monkeypatch.setenv("NO_COLOR", "1")
    assert _stdout_supports_color(_Tty()) is False


def test_stdout_supports_color_honors_term_dumb_and_non_tty(monkeypatch):
    from eval.report import _stdout_supports_color

    class _Tty:
        def isatty(self) -> bool:
            return True

    class _Pipe:
        def isatty(self) -> bool:
            return False

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert _stdout_supports_color(_Tty()) is False

    monkeypatch.setenv("TERM", "xterm")
    assert _stdout_supports_color(_Pipe()) is False
