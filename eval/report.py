"""A/B comparison report generation with multiple output formats."""

from __future__ import annotations

import json
import math
import os
import random
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import mean as _mean
from statistics import median
from statistics import stdev as _stdev
from typing import Any

import click

from eval.naming import parse_slug
from eval.protocols import RunStatus
from eval.trace import RunMetrics

# Below this many (paired) samples, A/B deltas are treated as low-confidence: a
# % delta at n=3 is mostly noise. Used to gate "insufficient data" warnings.
MIN_RELIABLE_N = 5

# Recommended minimum paired epochs for a CI gate (stricter than MIN_RELIABLE_N,
# which is the floor for an "exploratory" look at the data).
CI_GATE_RECOMMENDED_N = 10

# Minimum attempts-per-task-run (k) for pass@k/pass^k to be meaningful. Below
# this, "did it ever/always succeed in k tries" is mostly noise -- e.g. at
# k=1, pass@k and pass^k are identical and just restate the raw success rate.
MIN_RELIABLE_K = 3

# Cohen's d effect sizes (small / medium / large) power is reported for. Medium
# (0.5) is the headline number surfaced in the low-power banner.
_EFFECT_SIZES = (0.2, 0.5, 0.8)
_MEDIUM_EFFECT = 0.5

# Two-sided z-critical value for alpha=0.05, used by the power approximation.
_Z_ALPHA_TWO_SIDED = 1.959963985

# Bootstrap settings for the paired-delta confidence interval. The seed keeps
# report output deterministic across runs (same inputs -> same CI).
_BOOTSTRAP_ITERATIONS = 2000
_BOOTSTRAP_SEED = 12345
_CI_CONFIDENCE = 0.95

# GitHub's hard cap on a single issue/PR comment body (65,536 characters). The
# compact markdown formatter truncates with a visible notice rather than
# silently letting `gh pr comment` reject an oversized report.
PR_COMMENT_CHAR_LIMIT = 65536


@dataclass
class SummaryRow:
    metric: str
    values: dict[str, float]
    delta: str = ""
    # Per-variant sample size and dispersion for this metric.
    n: dict[str, int] = field(default_factory=dict)
    stddev: dict[str, float] = field(default_factory=dict)
    vmin: dict[str, float] = field(default_factory=dict)
    vmax: dict[str, float] = field(default_factory=dict)
    # Paired-delta bootstrap confidence interval (None when not computable).
    paired_n: int = 0
    ci_low: float | None = None
    ci_high: float | None = None
    # Two-sided bootstrap p-value proxy for the paired delta (None when not
    # computable). Feeds the multiple-comparison correction across a task's
    # family of tests; see `_apply_mc_correction`.
    p_value: float | None = None
    # True/False when a CI is available (excludes/includes 0) *and* the delta
    # survives the multiple-comparison correction; None otherwise (insufficient
    # paired samples). A raw CI-excludes-zero delta that is knocked out by
    # correction renders as False (`ns`), not True.
    significant: bool | None = None
    # Decimal places used when rendering this metric's value/stddev/CI.
    precision: int = 1


@dataclass
class ReliabilityRow:
    """Per-task reliability metrics, surfaced as first-class report output."""

    metric: str
    values: dict[str, str]


@dataclass
class Report:
    task: str
    runs: list[RunMetrics]
    variants: list[str]
    summary: list[SummaryRow]
    tool_patterns: dict[str, dict[str, int]]
    judge_scores: list[SummaryRow] = field(default_factory=list)
    # Per-epoch judge scores: key = (variant, epoch_str) -> {evaluator: score}
    epoch_judges: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    # Per-epoch judge reasons: key = (variant, epoch_str) -> {evaluator: reason}
    epoch_reasons: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # Per-epoch judge score spread: key = (variant, epoch_str) -> {evaluator: stddev}
    epoch_stddevs: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)
    judge_names: list[str] = field(default_factory=list)
    # pass@k ("succeeded at least once in k tries") / pass^k ("succeeded every
    # time in k tries") reliability rows, one pair per evaluator that produced
    # scores. Empty when no evaluators ran (mirrors judge_scores).
    pass_k: list[SummaryRow] = field(default_factory=list)
    # Per-task judge-runtime aggregate (outcome counts, host Copilot versions,
    # truncated-context count, version-mismatch flag). Empty when no judges ran.
    judge_runtime: dict[str, Any] = field(default_factory=dict)
    aggregate: str = "paired"
    # Per-variant run count (traces with extracted metrics) and shared paired n.
    variant_n: dict[str, int] = field(default_factory=dict)
    paired_n: int = 0
    # Success/failure reliability table (empty when no manifest is available).
    reliability: list[ReliabilityRow] = field(default_factory=list)
    # Data-sufficiency / significance caveats shown at the top of the report.
    # Each entry is a structured warning: {"type": ..., "message": ..., ...}.
    # "type" is one of "small_sample_size", "no_paired_epochs", "low_power".
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # Multiple-comparison correction applied across this task's family of
    # tests (OTel metrics + judge criteria): "holm", "benjamini-hochberg", or
    # "none" (disabled via --no-mc-correction). `mc_tests` is the family size
    # actually corrected (0 when correction is disabled or nothing was testable).
    mc_correction: str = "none"
    mc_tests: int = 0


# Each entry is (label, RunMetrics attribute, decimal precision). Precision drives
# how the aggregated value, its ±stddev, and the paired CI render. Cost needs more
# decimals than the rest because typical per-run costs are small fractions of a
# dollar (e.g. $0.04) that would collapse to "0.0" at 1-decimal precision.
_METRIC_DEFS = [
    ("Duration (s)", "duration", 1),
    ("Turn count", "turn_count", 1),
    ("Total spans", "total_spans", 1),
    ("Tool calls", "tool_count", 1),
    ("Input tokens", "total_input_tokens", 1),
    ("Output tokens", "total_output_tokens", 1),
    ("Cache tokens", "total_cache_tokens", 1),
    ("Tool duration (s)", "tool_duration", 1),
    ("Cost ($)", "cost", 4),
]


# --- Aggregation helpers ---


def _median(vals: list[float]) -> float:
    if not vals:
        return 0
    return float(median(vals))


def _mean_agg(vals: list[float]) -> float:
    if not vals:
        return 0
    return float(_mean(vals))


def _aggregate_values(
    vals_by_variant: dict[str, dict[str, float]], variants: list[str], method: str
) -> tuple[dict[str, float], str]:
    """Aggregate per-variant values and compute delta string.

    Values are keyed by epoch (variant -> epoch -> value) so that paired
    aggregation can match variants on a shared epoch key rather than relying on
    list position. This prevents deltas from silently shifting when an epoch is
    missing or failed for one variant.
    """
    agg_fn = _median if method != "mean" else _mean_agg

    if method == "paired" and len(variants) == 2:
        v0, v1 = variants
        m0, m1 = vals_by_variant.get(v0, {}), vals_by_variant.get(v1, {})
        ref0, ref1 = _median(list(m0.values())), _median(list(m1.values()))
        # Pair only on epochs present in both variants. Exclude the "?" sentinel
        # (used when OTel epoch tags are missing) so unknown epochs never pair.
        common = sorted((set(m0) & set(m1)) - {"?"}, key=_epoch_sort_key)
        if common:
            deltas = [m1[k] - m0[k] for k in common]
            d = _median(deltas)
            # Use the paired baseline (same epochs as the delta) as the denominator
            # so the percentage isn't skewed by unpaired epochs.
            paired_ref0 = _median([m0[k] for k in common])
            pct = f"{(d / paired_ref0) * 100:+.1f}%" if paired_ref0 > 0 else ""
        else:
            pct = ""
        return {v0: ref0, v1: ref1}, pct

    agg = {v: agg_fn(list(vals_by_variant.get(v, {}).values())) for v in variants}
    return agg, _calc_delta(agg, variants)


def _epoch_sort_key(epoch: str) -> tuple[int, object]:
    """Sort epochs numerically when possible, falling back to string order."""
    try:
        return (0, int(epoch))
    except (TypeError, ValueError):
        return (1, str(epoch))


def _pair_label(fixture: str, epoch: str) -> str:
    """Fixture-qualified pairing/display key for a run.

    Single-fixture / legacy runs (empty fixture) keep the bare epoch, so paired
    aggregation and report layout are byte-identical to the pre-fixture behavior.
    Multi-fixture runs get a ``{fixture}#{epoch}`` key so variants are paired
    within the same (fixture, epoch) cell and the paired delta pools across
    fixtures. The ``"?"`` sentinel (missing OTel epoch) is preserved verbatim so
    it never pairs.
    """
    if not fixture or epoch == "?":
        return epoch
    return f"{fixture}#{epoch}"


def _task_run_key(pair_label: str) -> str:
    """Recover the pass@k/pass^k "task-run" grouping key from a pair label.

    pass@k ("did it ever succeed?") and pass^k ("did it always succeed?") are
    each computed over one *set* of k repeated attempts at the same task
    instance. Single-fixture tasks have exactly one such instance per variant
    (this returns ``""`` for every epoch, so all epochs pool into one k-sized
    group). Multi-fixture tasks (see ``_pair_label``) get one task-run per
    fixture, so the input-coverage axis becomes the sample the rate averages
    over -- matching the "sum over task_run / n_task_runs" aggregation in
    issue #88.
    """
    if "#" in pair_label:
        return pair_label.rsplit("#", 1)[0]
    return ""


def _calc_delta(values: dict[str, float], variants: list[str]) -> str:
    if len(variants) != 2:
        return ""
    m0, m1 = values.get(variants[0], 0), values.get(variants[1], 0)
    return f"{((m1 - m0) / m0) * 100:+.1f}%" if m0 > 0 else ""


# --- Dispersion + significance helpers ---


def _stddev(vals: list[float]) -> float:
    """Sample standard deviation; 0 when fewer than two samples."""
    if len(vals) < 2:
        return 0.0
    return float(_stdev(vals))


def _min_max(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    return float(min(vals)), float(max(vals))


def _paired_deltas(m0: dict[str, float], m1: dict[str, float]) -> list[float]:
    """Per-epoch (v1 - v0) deltas over epochs present in both variants.

    Mirrors the pairing rule in `_aggregate_values`: the "?" sentinel epoch
    (missing OTel tags) never pairs.
    """
    common = sorted((set(m0) & set(m1)) - {"?"}, key=_epoch_sort_key)
    return [m1[k] - m0[k] for k in common]


def _bootstrap_stats(
    deltas: list[float],
    confidence: float = _CI_CONFIDENCE,
    iterations: int = _BOOTSTRAP_ITERATIONS,
    seed: int = _BOOTSTRAP_SEED,
) -> tuple[float, float, float] | None:
    """Bootstrap CI + a two-sided p-value proxy for the median of paired deltas.

    Returns ``(ci_low, ci_high, p_value)``, or None when there are fewer than
    two deltas (both would be meaningless). Uses a fixed seed so identical
    inputs yield identical output, keeping report output reproducible.

    The p-value is derived from the same resampled distribution as the CI: the
    smaller of the two tail proportions crossing zero, doubled for a two-sided
    test (the standard bootstrap-percentile p-value, e.g. Efron & Tibshirani
    1993). It has no independent meaning beyond ranking/thresholding deltas for
    the multiple-comparison correction (`_holm_bonferroni` /
    `_benjamini_hochberg`) — the CI-excludes-zero check alone has no p-value to
    adjust by family-wise error rate.
    """
    n = len(deltas)
    if n < 2:
        return None
    rng = random.Random(seed)
    medians: list[float] = []
    for _ in range(iterations):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        medians.append(float(median(sample)))
    medians.sort()
    lo_idx = int((1 - confidence) / 2 * iterations)
    hi_idx = min(iterations - 1, int((1 + confidence) / 2 * iterations))
    frac_le = sum(1 for m in medians if m <= 0) / iterations
    frac_ge = sum(1 for m in medians if m >= 0) / iterations
    p_value = min(1.0, 2 * min(frac_le, frac_ge))
    return medians[lo_idx], medians[hi_idx], p_value


def _bootstrap_ci(
    deltas: list[float],
    confidence: float = _CI_CONFIDENCE,
    iterations: int = _BOOTSTRAP_ITERATIONS,
    seed: int = _BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Bootstrap CI for the median of paired deltas (see `_bootstrap_stats`)."""
    stats = _bootstrap_stats(deltas, confidence, iterations, seed)
    return None if stats is None else (stats[0], stats[1])


def _ci_significant(ci: tuple[float, float] | None) -> bool | None:
    """A paired delta is statistically supported when its CI excludes 0.

    This is the *raw*, uncorrected significance check for a single test. The
    report's `*` marker additionally requires the delta to survive the
    per-task multiple-comparison correction (`_apply_mc_correction`) — see
    `SummaryRow.significant`.
    """
    if ci is None:
        return None
    lo, hi = ci
    return lo > 0 or hi < 0


# --- Multiple-comparison correction ---
#
# Each task independently bootstrap-tests ~9 OTel metrics plus one hypothesis
# per judge criterion at alpha=0.05. Left uncorrected, the per-family
# false-positive rate compounds fast — with m=11 independent tests,
# P(>=1 false positive) = 1 - 0.95**11 ~= 43% — so a `*` marker doesn't mean
# what it looks like it means (see GitHub issue #71). These corrections adjust
# which deltas keep their `*` after all of a task's tests are considered
# together; the CI itself is left untouched (still a plain 95% interval, shown
# for descriptive purposes regardless of correction).

_DEFAULT_ALPHA = 0.05


def _holm_bonferroni(p_values: list[float], alpha: float = _DEFAULT_ALPHA) -> list[bool]:
    """Holm-Bonferroni step-down correction (controls the family-wise error rate).

    Returns a reject/fail-to-reject decision per p-value, aligned to the input
    order. Sort ascending, then walk the sorted order comparing each p-value to
    an increasingly lenient threshold (alpha / remaining-tests); the first
    failure stops all further rejections, since Holm's guarantee only holds for
    that contiguous prefix.
    """
    m = len(p_values)
    reject = [False] * m
    order = sorted(range(m), key=lambda i: p_values[i])
    for rank, idx in enumerate(order):
        if p_values[idx] <= alpha / (m - rank):
            reject[idx] = True
        else:
            break
    return reject


def _benjamini_hochberg(p_values: list[float], alpha: float = _DEFAULT_ALPHA) -> list[bool]:
    """Benjamini-Hochberg FDR correction (controls the false-discovery rate).

    Less conservative than Holm — more power to detect real effects, at the
    cost of tolerating a small expected fraction of false positives among the
    rejected hypotheses, rather than bounding the probability of *any* false
    positive. Finds the largest rank k with p(k) <= (k/m) * alpha and rejects
    every hypothesis at or below that rank.
    """
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    largest_k = -1
    for rank, idx in enumerate(order):
        if p_values[idx] <= (rank + 1) / m * alpha:
            largest_k = rank
    reject = [False] * m
    for rank in range(largest_k + 1):
        reject[order[rank]] = True
    return reject


# Public correction methods accepted by `build_report(mc_correction=...)` / the
# `analyze` CLI. "none" (from --no-mc-correction) skips correction entirely.
_MC_METHODS = {
    "holm": _holm_bonferroni,
    "benjamini-hochberg": _benjamini_hochberg,
    "bh": _benjamini_hochberg,
}


def _apply_mc_correction(rows: list[SummaryRow], method: str) -> int:
    """Recompute each row's `significant` marker across the family after a
    multiple-comparison correction. Mutates `rows` in place.

    Returns the number of tests actually corrected (0 when correction is
    disabled, or there is nothing testable in this family).

    Correction can only take a `*` away, never grant one a raw CI-excludes-zero
    check didn't already support: rows with `significant is False` (or `None`,
    for insufficient paired samples) are left as-is.
    """
    testable = [r for r in rows if r.p_value is not None and r.paired_n >= MIN_RELIABLE_N]
    if method == "none" or not testable:
        return 0
    fn = _MC_METHODS.get(method)
    if fn is None:
        raise ValueError(f"Unknown mc_correction method: {method!r}")
    p_values = [r.p_value for r in testable if r.p_value is not None]
    decisions = fn(p_values)
    for row, reject in zip(testable, decisions, strict=True):
        if row.significant is True and not reject:
            row.significant = False
    return len(testable)


# --- Cross-run baseline comparison (issue #65) ---
#
# Within-run `analyze` pairs variants on a shared epoch key (`_paired_deltas`).
# A saved baseline is a snapshot from a *different* run, so there is no shared
# epoch to pair on (different run, possibly a different epoch count). These
# comparisons instead resample each sample independently ("unpaired bootstrap")
# to build a CI for the (current - baseline) difference.


def _unpaired_bootstrap_stats(
    baseline_vals: list[float],
    current_vals: list[float],
    confidence: float = _CI_CONFIDENCE,
    iterations: int = _BOOTSTRAP_ITERATIONS,
    seed: int = _BOOTSTRAP_SEED,
) -> tuple[float, float, float] | None:
    """Bootstrap CI + two-sided p-value proxy for the unpaired median difference
    (current - baseline).

    Returns ``(ci_low, ci_high, p_value)``, or None when either sample has
    fewer than two values. Mirrors `_bootstrap_stats`'s percentile-CI /
    doubled-tail-proportion p-value, but resamples the baseline and current
    samples independently (with replacement) rather than resampling shared
    paired deltas -- the two runs share no epoch to pair on.
    """
    nb, nc = len(baseline_vals), len(current_vals)
    if nb < 2 or nc < 2:
        return None
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(iterations):
        b_sample = [baseline_vals[rng.randrange(nb)] for _ in range(nb)]
        c_sample = [current_vals[rng.randrange(nc)] for _ in range(nc)]
        diffs.append(float(median(c_sample) - median(b_sample)))
    diffs.sort()
    lo_idx = int((1 - confidence) / 2 * iterations)
    hi_idx = min(iterations - 1, int((1 + confidence) / 2 * iterations))
    frac_le = sum(1 for d in diffs if d <= 0) / iterations
    frac_ge = sum(1 for d in diffs if d >= 0) / iterations
    p_value = min(1.0, 2 * min(frac_le, frac_ge))
    return diffs[lo_idx], diffs[hi_idx], p_value


@dataclass
class BaselineRow:
    """One metric's baseline-vs-current comparison for a (task, variant)."""

    metric: str
    baseline_value: float
    current_value: float
    baseline_n: int
    current_n: int
    delta: str = ""
    ci_low: float | None = None
    ci_high: float | None = None
    p_value: float | None = None
    # True/False once a CI is available and the family-wise correction has been
    # applied; None when either sample is too small (see MIN_RELIABLE_N).
    significant: bool | None = None
    # True when `significant` and the current run is *worse* than baseline.
    # Every OTel metric tracked here (duration, tokens, cost, tool/turn counts)
    # is "lower is better", so worse == current median higher than baseline's.
    regression: bool = False
    # True when `significant` and the current run is *better* than baseline.
    improved: bool = False
    precision: int = 1


@dataclass
class BaselineComparison:
    """Baseline-vs-current comparison for one (task, variant) pair."""

    task: str
    variant: str
    baseline_name: str
    baseline_run_id: str
    rows: list[BaselineRow] = field(default_factory=list)
    has_regression: bool = False
    mc_correction: str = "none"
    mc_tests: int = 0


def _apply_baseline_mc_correction(rows: list[BaselineRow], method: str) -> int:
    """`_apply_mc_correction`, adapted for `BaselineRow` (gates on the smaller
    of the two unpaired sample sizes rather than a shared paired_n). Mutates
    `rows` in place; also resets `regression`/`improved` when correction takes
    a `*` away, since both depend on `significant`.
    """
    testable = [
        r
        for r in rows
        if r.p_value is not None and min(r.baseline_n, r.current_n) >= MIN_RELIABLE_N
    ]
    if method == "none" or not testable:
        return 0
    fn = _MC_METHODS.get(method)
    if fn is None:
        raise ValueError(f"Unknown mc_correction method: {method!r}")
    p_values = [r.p_value for r in testable if r.p_value is not None]
    decisions = fn(p_values)
    for row, reject in zip(testable, decisions, strict=True):
        if row.significant is True and not reject:
            row.significant = False
            row.regression = False
            row.improved = False
    return len(testable)


def build_baseline_comparisons(
    metrics: list[RunMetrics],
    baseline_data: dict[str, Any],
    variant_order: list[str] | None = None,
    mc_correction: str = "holm",
) -> tuple[list[BaselineComparison], list[str]]:
    """Compare `metrics` (the current run's extracted RunMetrics) against a
    saved baseline snapshot (see `eval.services.baseline_service.save_baseline`),
    per (task, variant) pair.

    Returns ``(comparisons, missing)`` where `missing` lists task/variant
    combinations present in the current run but absent from the baseline
    (e.g. a new task, or a variant renamed since the baseline was captured) --
    surfaced as a warning rather than silently dropped.
    """
    baseline_name = str(baseline_data.get("name", ""))
    baseline_run_id = str(baseline_data.get("run_id", ""))
    baseline_tasks: dict[str, Any] = baseline_data.get("tasks", {}) or {}

    by_task_variant: dict[tuple[str, str], list[RunMetrics]] = defaultdict(list)
    for r in metrics:
        by_task_variant[(r.scenario, r.variant)].append(r)

    tasks = sorted({task for (task, _v) in by_task_variant})
    comparisons: list[BaselineComparison] = []
    missing: list[str] = []

    for task in tasks:
        variants_here = sorted({v for (t, v) in by_task_variant if t == task})
        if variant_order:
            variants_here = [v for v in variant_order if v in variants_here]
        baseline_task = baseline_tasks.get(task)
        if baseline_task is None:
            missing.append(task)
            continue
        baseline_variants: dict[str, Any] = baseline_task.get("variants", {}) or {}

        for variant in variants_here:
            baseline_variant = baseline_variants.get(variant)
            if baseline_variant is None:
                missing.append(f"{task}/{variant}")
                continue
            baseline_runs: list[dict[str, Any]] = baseline_variant.get("runs", []) or []
            current_runs = by_task_variant[(task, variant)]

            rows: list[BaselineRow] = []
            for label, key, precision in _METRIC_DEFS:
                baseline_vals = [float(r[key]) for r in baseline_runs if key in r]
                current_vals = [float(getattr(r, key)) for r in current_runs]
                row = BaselineRow(
                    metric=label,
                    baseline_value=_median(baseline_vals),
                    current_value=_median(current_vals),
                    baseline_n=len(baseline_vals),
                    current_n=len(current_vals),
                    precision=precision,
                )
                if baseline_vals and row.baseline_value > 0:
                    pct = (row.current_value - row.baseline_value) / row.baseline_value * 100
                    row.delta = f"{pct:+.1f}%"
                stats = _unpaired_bootstrap_stats(baseline_vals, current_vals)
                if stats is not None:
                    row.ci_low, row.ci_high, row.p_value = stats
                    min_n = min(row.baseline_n, row.current_n)
                    row.significant = (
                        _ci_significant((row.ci_low, row.ci_high))
                        if min_n >= MIN_RELIABLE_N
                        else None
                    )
                    if row.significant:
                        row.regression = row.ci_low > 0
                        row.improved = row.ci_high < 0
                rows.append(row)

            mc_tests = _apply_baseline_mc_correction(rows, mc_correction)
            comparisons.append(
                BaselineComparison(
                    task=task,
                    variant=variant,
                    baseline_name=baseline_name,
                    baseline_run_id=baseline_run_id,
                    rows=rows,
                    has_regression=any(r.regression for r in rows),
                    mc_correction=mc_correction if mc_tests else "none",
                    mc_tests=mc_tests,
                )
            )

    return comparisons, missing


def format_baseline_table(
    comparisons: list[BaselineComparison], missing: list[str] | None = None
) -> str:
    """Human-readable text report for a set of baseline comparisons (used for
    both the `table` and `markdown` output formats -- appended after the
    within-run report)."""
    if not comparisons and not missing:
        return ""
    lines: list[str] = ["", "=== Baseline comparison ==="]
    for c in comparisons:
        lines.append(
            f"\n{c.task} [{c.variant}] vs baseline '{c.baseline_name}' ({c.baseline_run_id}):"
        )
        header = f"  {'Metric':<18} {'Baseline':>12} {'Current':>12} {'Delta':>10}"
        lines.append(header)
        for row in c.rows:
            marker = ""
            if row.regression:
                marker = "  REGRESSION"
            elif row.improved:
                marker = "  * improved"
            elif row.significant is False:
                marker = "  ns"
            bval = f"{row.baseline_value:.{row.precision}f}"
            cval = f"{row.current_value:.{row.precision}f}"
            lines.append(f"  {row.metric:<18} {bval:>12} {cval:>12} {row.delta:>10}{marker}")
        if c.has_regression:
            lines.append("  \u26a0\ufe0f  Regression detected vs baseline.")
    if missing:
        lines.append("\nNo baseline data for: " + ", ".join(missing))
    return "\n".join(lines)


def baseline_comparisons_json(
    comparisons: list[BaselineComparison], missing: list[str] | None = None
) -> dict[str, Any]:
    """JSON-serializable payload for a set of baseline comparisons, merged into
    `analyze -o json` output under the `"baseline"` key."""
    return {
        "comparisons": [
            {
                "task": c.task,
                "variant": c.variant,
                "baseline_name": c.baseline_name,
                "baseline_run_id": c.baseline_run_id,
                "has_regression": c.has_regression,
                "mc_correction": c.mc_correction,
                "mc_tests": c.mc_tests,
                "metrics": [
                    {
                        "metric": r.metric,
                        "baseline_value": r.baseline_value,
                        "current_value": r.current_value,
                        "baseline_n": r.baseline_n,
                        "current_n": r.current_n,
                        "delta": r.delta,
                        "ci_low": r.ci_low,
                        "ci_high": r.ci_high,
                        "p_value": r.p_value,
                        "significant": r.significant,
                        "regression": r.regression,
                        "improved": r.improved,
                    }
                    for r in c.rows
                ],
            }
            for c in comparisons
        ],
        "missing": missing or [],
    }


def _build_summary_row(
    metric: str,
    vals_by_variant: dict[str, dict[str, float]],
    variants: list[str],
    aggregate: str,
    precision: int = 1,
) -> SummaryRow:
    """Aggregate one metric and attach n, dispersion, and (paired) CI."""
    agg, delta = _aggregate_values(vals_by_variant, variants, aggregate)
    row = SummaryRow(metric=metric, values=agg, delta=delta, precision=precision)
    for v in variants:
        vals = list(vals_by_variant.get(v, {}).values())
        row.n[v] = len(vals)
        row.stddev[v] = _stddev(vals)
        row.vmin[v], row.vmax[v] = _min_max(vals)

    if aggregate == "paired" and len(variants) == 2:
        v0, v1 = variants
        deltas = _paired_deltas(vals_by_variant.get(v0, {}), vals_by_variant.get(v1, {}))
        row.paired_n = len(deltas)
        stats = _bootstrap_stats(deltas)
        ci = None
        if stats is not None:
            row.ci_low, row.ci_high, row.p_value = stats
            ci = (row.ci_low, row.ci_high)
        # Only claim statistical support with enough paired samples. At tiny n a
        # bootstrap of the median can produce a degenerate CI that excludes 0,
        # which would re-create the "looks decisive at n=3" failure mode. This is
        # the *raw*, uncorrected decision -- `_apply_mc_correction` may still
        # downgrade True to False once the whole task's family is considered.
        row.significant = _ci_significant(ci) if row.paired_n >= MIN_RELIABLE_N else None
    return row


# --- pass@k / pass^k reliability ---
#
# pass@k = P(at least one of k attempts at a task instance succeeded) -- the
# capability ceiling. pass^k = P(every one of k attempts succeeded) -- the
# consistency floor. Both are computed per evaluator (reusing whichever
# evaluator produced a per-epoch score) from that evaluator's persisted
# ``passed`` bit, grouped into task-run buckets (see `_task_run_key`) and
# averaged across task-runs, per Anthropic's agent-eval methodology
# (https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).


def _task_run_groups(
    epoch_passed: dict[tuple[str, str], dict[str, bool]], variant: str, evaluator: str
) -> dict[str, list[bool]]:
    """This variant's per-epoch pass/fail values for one evaluator, by task-run."""
    groups: dict[str, list[bool]] = defaultdict(list)
    for (v, epoch_str), passed_by_name in epoch_passed.items():
        if v != variant or evaluator not in passed_by_name:
            continue
        groups[_task_run_key(epoch_str)].append(passed_by_name[evaluator])
    return groups


def _build_pass_k_row(
    metric: str,
    rates_by_variant: dict[str, dict[str, float]],
    variants: list[str],
    aggregate: str,
) -> SummaryRow:
    """Build a pass@k/pass^k row: values already expressed as a 0-100 rate.

    Unlike `_build_summary_row`, the delta is an absolute percentage-point
    difference (the metric is already a rate), not a relative-to-baseline
    percentage -- e.g. baseline=67%, experimental=100% -> delta="+33%", not
    "+49%". The bootstrap CI reuses the same paired-task-run-delta machinery
    as other metrics, just over {0, 100} values instead of continuous ones.
    """
    row = SummaryRow(metric=metric, values={}, precision=0)
    for v in variants:
        rates = list(rates_by_variant.get(v, {}).values())
        row.n[v] = len(rates)
        row.values[v] = _mean_agg(rates)
        row.stddev[v] = _stddev(rates)
        row.vmin[v], row.vmax[v] = _min_max(rates)

    if len(variants) == 2:
        v0, v1 = variants
        row.delta = f"{(row.values.get(v1, 0.0) - row.values.get(v0, 0.0)):+.0f}%"

    if aggregate == "paired" and len(variants) == 2:
        v0, v1 = variants
        deltas = _paired_deltas(rates_by_variant.get(v0, {}), rates_by_variant.get(v1, {}))
        row.paired_n = len(deltas)
        stats = _bootstrap_stats(deltas)
        ci = None
        if stats is not None:
            row.ci_low, row.ci_high, row.p_value = stats
            ci = (row.ci_low, row.ci_high)
        row.significant = _ci_significant(ci) if row.paired_n >= MIN_RELIABLE_N else None
    return row


def _build_pass_k_rows(
    epoch_passed: dict[tuple[str, str], dict[str, bool]],
    variants: list[str],
    evaluator_names: list[str],
    aggregate: str,
) -> tuple[list[SummaryRow], int | None]:
    """Build pass@k/pass^k rows for every evaluator that produced scores.

    Returns (rows, min_k) where min_k is the smallest task-run attempt count
    seen across all evaluators/variants (None when there's no data at all) --
    used to decide whether to raise the "insufficient data" warning.
    """
    rows: list[SummaryRow] = []
    min_k: int | None = None

    for name in evaluator_names:
        at_k_by_variant: dict[str, dict[str, float]] = {}
        all_k_by_variant: dict[str, dict[str, float]] = {}
        k = 0
        for v in variants:
            groups = _task_run_groups(epoch_passed, v, name)
            at_k_by_variant[v] = {run: (100.0 if any(g) else 0.0) for run, g in groups.items()}
            all_k_by_variant[v] = {run: (100.0 if all(g) else 0.0) for run, g in groups.items()}
            for g in groups.values():
                k = max(k, len(g))
                min_k = len(g) if min_k is None else min(min_k, len(g))

        if k == 0:
            continue  # no data at all for this evaluator
        rows.append(_build_pass_k_row(f"pass@{k} ({name})", at_k_by_variant, variants, aggregate))
        rows.append(_build_pass_k_row(f"pass^{k} ({name})", all_k_by_variant, variants, aggregate))

    return rows, min_k


# --- Report building ---


def build_report(
    results: list[RunMetrics],
    results_dir: Path | None = None,
    variant_order: list[str] | None = None,
    aggregate: str = "paired",
    manifest_runs: list[dict[str, Any]] | None = None,
    trace_test_ids: set[str] | None = None,
    mc_correction: str = "holm",
) -> list[Report]:
    """Build per-task A/B comparison reports.

    ``manifest_runs`` (the persisted full set of attempted runs) and
    ``trace_test_ids`` (test ids that produced an ingested trace) drive the
    reliability table and the trace-missing rate. Both are optional so reports
    for older runs without a manifest still render — reliability simply degrades
    to per-variant trace counts.

    ``mc_correction`` controls the multiple-comparison correction applied to
    the `*` significance marker across each task's family of tests (OTel
    metrics + judge criteria + pass@k/pass^k rows): ``"holm"`` (default,
    Holm-Bonferroni), ``"benjamini-hochberg"``/``"bh"`` (FDR, less
    conservative), or ``"none"`` to disable (the `analyze` CLI's
    ``--no-mc-correction`` flag).
    """
    if not results and not manifest_runs:
        return []

    by_task: dict[str, list[RunMetrics]] = defaultdict(list)
    for r in results:
        by_task[r.scenario].append(r)

    # Seed tasks/variants from the manifest too, so a variant (or whole task)
    # whose every run failed/timed out still appears — with a 0% success rate —
    # instead of silently vanishing (the survivorship bias this fixes).
    mlist = manifest_runs or []
    manifest_tasks: set[str] = {str(mr.get("task")) for mr in mlist}
    manifest_vars_by_task: dict[str, set[str]] = defaultdict(set)
    for mr in mlist:
        manifest_vars_by_task[str(mr.get("task"))].add(str(mr.get("variant")))

    reports: list[Report] = []
    for task_name in sorted(set(by_task.keys()) | manifest_tasks):
        # Qualify each run's epoch with its fixture so paired aggregation pools
        # across fixtures (variant × fixture × epoch). Single-fixture runs keep
        # the bare epoch, preserving the legacy report layout exactly.
        task_runs = [replace(r, epoch=_pair_label(r.fixture, r.epoch)) for r in by_task[task_name]]
        task_runs.sort(key=lambda r: (r.variant, _epoch_sort_key(r.epoch)))

        by_variant: dict[str, list[RunMetrics]] = defaultdict(list)
        for r in task_runs:
            by_variant[r.variant].append(r)

        manifest_vars = manifest_vars_by_task.get(task_name, set())
        if variant_order:
            variants = [v for v in variant_order if v in by_variant or v in manifest_vars]
        else:
            variants = sorted(set(by_variant.keys()) | manifest_vars)

        # OTel metrics summary (with n, dispersion, and paired CI)
        summary = []
        for label, key, precision in _METRIC_DEFS:
            vals_by_v = {
                v: {r.epoch: float(getattr(r, key)) for r in by_variant[v]} for v in variants
            }
            summary.append(_build_summary_row(label, vals_by_v, variants, aggregate, precision))

        # Tool patterns
        tool_patterns: dict[str, dict[str, int]] = {}
        for v in variants:
            counts: dict[str, int] = defaultdict(int)
            for r in by_variant[v]:
                for t in r.tool_names:
                    counts[t] += 1
            tool_patterns[v] = dict(counts)

        # Judge scores (both aggregated + per-epoch)
        epoch_judges, epoch_reasons, judge_names = {}, {}, []
        epoch_stddevs: dict[tuple[str, str], dict[str, float]] = {}
        epoch_passed: dict[tuple[str, str], dict[str, bool]] = {}
        judge_rows: list[SummaryRow] = []
        judge_runtime: dict[str, Any] = {}
        pass_k_rows: list[SummaryRow] = []
        min_k: int | None = None
        if results_dir:
            raw, reasons, names, stddevs, passed = _load_judge_raw(results_dir, variants, task_name)
            epoch_judges = raw
            epoch_reasons = reasons
            epoch_stddevs = stddevs
            epoch_passed = passed
            judge_names = names
            judge_runtime = _load_judge_runtime(results_dir, variants, task_name)
            # Aggregate judge scores
            for name in names:
                vals_by_v = {}
                for v in variants:
                    vals_by_v[v] = {}
                    for (rv, ep), scores in raw.items():
                        if rv == v and name in scores:
                            vals_by_v[v][ep] = float(scores[name])
                judge_rows.append(_build_summary_row(name, vals_by_v, variants, aggregate))
            pass_k_rows, min_k = _build_pass_k_rows(epoch_passed, variants, names, aggregate)

        # Multiple-comparison correction: the `*` marker on any of this task's
        # summary/judge/pass_k rows must reflect the whole family of tests, not
        # each row's raw (uncorrected) CI-excludes-zero check in isolation.
        mc_tests = _apply_mc_correction([*summary, *judge_rows, *pass_k_rows], mc_correction)

        # Sample sizes: per-variant trace count and shared paired-epoch count.
        variant_n = {v: len(by_variant[v]) for v in variants}
        paired_n = 0
        if aggregate == "paired" and len(variants) == 2:
            e0 = {r.epoch for r in by_variant[variants[0]]} - {"?"}
            e1 = {r.epoch for r in by_variant[variants[1]]} - {"?"}
            paired_n = len(e0 & e1)

        reliability = _build_reliability(
            task_name,
            variants,
            by_variant,
            manifest_runs,
            trace_test_ids,
            epoch_judges if judge_names else None,
        )
        warnings = _build_warnings(variants, variant_n, paired_n, aggregate)
        if min_k is not None and min_k < MIN_RELIABLE_K:
            warnings.append(
                {
                    "type": "insufficient_k",
                    "message": (
                        f"Insufficient data for pass@k/pass^k (k={min_k} < {MIN_RELIABLE_K}). "
                        "Run with more epochs for a meaningful capability-ceiling/consistency read."
                    ),
                    "k": min_k,
                    "min_reliable_k": MIN_RELIABLE_K,
                }
            )

        reports.append(
            Report(
                task=task_name,
                runs=task_runs,
                variants=variants,
                summary=summary,
                tool_patterns=tool_patterns,
                judge_scores=judge_rows,
                epoch_judges=epoch_judges,
                epoch_reasons=epoch_reasons,
                epoch_stddevs=epoch_stddevs,
                judge_names=judge_names,
                pass_k=pass_k_rows,
                judge_runtime=judge_runtime,
                aggregate=aggregate,
                variant_n=variant_n,
                paired_n=paired_n,
                reliability=reliability,
                warnings=warnings,
                mc_correction=mc_correction if mc_tests else "none",
                mc_tests=mc_tests,
            )
        )

    return reports


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (stdlib-only, no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _approx_power(n: int, effect_size: float, alpha: float = 0.05) -> float:
    """Approximate statistical power of a paired two-sided test at sample size n.

    This is a dependency-free stand-in for ``scipy.stats.ttest_rel`` power
    analysis: the noncentrality parameter for a paired t-test is
    ``delta = effect_size * sqrt(n)``, and its normal approximation gives
    ``power ~= Phi(delta - z_alpha/2) + Phi(-delta - z_alpha/2)``. Because the
    t-distribution has heavier tails than the normal at small n, this tends to
    *overestimate* true power a bit for tiny samples -- fine for a "should I
    trust this delta" warning, but not a substitute for a real power analysis
    (e.g. statsmodels' ``TTestPower``) when precision matters.
    """
    if n < 2:
        return 0.0
    delta = effect_size * math.sqrt(n)
    z = _Z_ALPHA_TWO_SIDED
    power = _norm_cdf(delta - z) + _norm_cdf(-delta - z)
    return max(0.0, min(1.0, power))


def _power_table(n: int) -> dict[str, float]:
    """Power at each tracked Cohen's d, keyed by string (JSON-friendly)."""
    return {str(d): _approx_power(n, d) for d in _EFFECT_SIZES}


def _low_power_banner(paired_n: int, power: dict[str, float]) -> str:
    """Multi-line banner explaining why N is too low to trust conclusions."""
    medium = power[str(_MEDIUM_EFFECT)]
    miss_pct = (1 - medium) * 100
    return "\n".join(
        [
            f"\u26a0\ufe0f  LOW STATISTICAL POWER: N={paired_n} paired epochs. Minimum "
            f"recommended: N={MIN_RELIABLE_N} for exploratory, N={CI_GATE_RECOMMENDED_N}+ "
            "for CI gates.",
            f"    Current power at d={_MEDIUM_EFFECT} (medium effect): {medium * 100:.0f}% "
            f"\u2014 {miss_pct:.0f}% chance of missing a real difference.",
            f"    Run with --epochs {CI_GATE_RECOMMENDED_N} for reliable conclusions.",
        ]
    )


def _build_warnings(
    variants: list[str], variant_n: dict[str, int], paired_n: int, aggregate: str
) -> list[dict[str, Any]]:
    """Flag insufficient-data conditions so small deltas aren't over-read."""
    warnings: list[dict[str, Any]] = []
    low_variants = [v for v in variants if variant_n.get(v, 0) < MIN_RELIABLE_N]
    if low_variants:
        detail = ", ".join(f"{v}={variant_n.get(v, 0)}" for v in low_variants)
        warnings.append(
            {
                "type": "small_sample_size",
                "message": (
                    f"Small sample size (n<{MIN_RELIABLE_N}): {detail}. "
                    "Treat deltas as observed, not statistically supported."
                ),
                "variant_n": {v: variant_n.get(v, 0) for v in low_variants},
                "min_reliable_n": MIN_RELIABLE_N,
            }
        )
    if aggregate == "paired" and len(variants) == 2:
        if paired_n == 0:
            warnings.append(
                {
                    "type": "no_paired_epochs",
                    "message": "No shared epochs between variants; paired delta unavailable.",
                }
            )
        elif paired_n < MIN_RELIABLE_N:
            power = _power_table(paired_n)
            warnings.append(
                {
                    "type": "low_power",
                    "message": _low_power_banner(paired_n, power),
                    "paired_n": paired_n,
                    "min_reliable_n": MIN_RELIABLE_N,
                    "ci_gate_recommended_n": CI_GATE_RECOMMENDED_N,
                    "power": power,
                }
            )
    return warnings


def _build_reliability(
    task: str,
    variants: list[str],
    by_variant: dict[str, list[RunMetrics]],
    manifest_runs: list[dict[str, Any]] | None,
    trace_test_ids: set[str] | None,
    epoch_judges: dict[tuple[str, str], dict[str, int]] | None,
) -> list[ReliabilityRow]:
    """Compute per-variant success/failure rates as first-class metrics.

    Without a manifest the full set of attempted runs is unknown, so only the
    surviving trace count is reported (success/failure rates need the manifest
    to avoid survivorship bias).
    """

    def pct(num: int, den: int) -> str:
        return f"{(num / den) * 100:.1f}%" if den else "—"

    if manifest_runs is None:
        rows = [ReliabilityRow("Runs (traces)", {v: str(len(by_variant[v])) for v in variants})]
        rows.append(
            ReliabilityRow(
                "Success/failure rates",
                {v: "n/a (no manifest)" for v in variants},
            )
        )
        return rows

    tids = trace_test_ids or set()
    counts: dict[str, dict[str, int]] = {
        v: {"total": 0, "success": 0, "timeout": 0, "failed": 0, "missing": 0} for v in variants
    }
    for r in manifest_runs:
        if r.get("task") != task:
            continue
        v = str(r.get("variant"))
        if v not in counts:
            continue
        c = counts[v]
        c["total"] += 1
        status = r.get("status", RunStatus.SUCCESS.value)
        has_trace = r.get("test_id") in tids
        if status == RunStatus.TIMEOUT.value:
            c["timeout"] += 1
        elif status in (RunStatus.FAILED.value, RunStatus.SETUP_FAILED.value):
            c["failed"] += 1
        elif not has_trace:
            c["missing"] += 1
        else:
            c["success"] += 1

    rows = [
        ReliabilityRow("Runs (attempted)", {v: str(counts[v]["total"]) for v in variants}),
        ReliabilityRow(
            "Success rate", {v: pct(counts[v]["success"], counts[v]["total"]) for v in variants}
        ),
        ReliabilityRow(
            "Timeout rate", {v: pct(counts[v]["timeout"], counts[v]["total"]) for v in variants}
        ),
        ReliabilityRow(
            "Failed rate", {v: pct(counts[v]["failed"], counts[v]["total"]) for v in variants}
        ),
        ReliabilityRow(
            "Missing-trace rate",
            {v: pct(counts[v]["missing"], counts[v]["total"]) for v in variants},
        ),
    ]
    if epoch_judges is not None:
        rows.append(
            ReliabilityRow(
                "Judge-score coverage",
                {v: _judge_coverage(v, epoch_judges, counts[v]["success"]) for v in variants},
            )
        )
    return rows


def _judge_coverage(
    variant: str, epoch_judges: dict[tuple[str, str], dict[str, int]], success_n: int
) -> str:
    """Share of successful runs that yielded a usable judge score.

    Judge scores are written to ``*.scores.json`` during ``analyze`` (not into
    the run manifest), so coverage is computed from the loaded scores: a run with
    only null judge scores (timeout / parse error) has an empty score dict and so
    counts against coverage instead of silently vanishing.
    """
    if success_n == 0:
        return "—"
    scored = sum(1 for (rv, _ep), scores in epoch_judges.items() if rv == variant and scores)
    # Cap at the success denominator: a judge can in principle run off a log-file
    # fallback for a trace-less run, which shouldn't push coverage past 100%.
    scored = min(scored, success_n)
    return f"{(scored / success_n) * 100:.1f}%"


# --- Format functions ---


def _fmt_value(row: SummaryRow, v: str) -> str:
    """Aggregate value with ±stddev when a spread is meaningful."""
    if row.n.get(v, 0) == 0:
        return "\u2014"  # no traces for this variant
    val = row.values.get(v, 0.0)
    sd = row.stddev.get(v, 0.0)
    p = row.precision
    if row.n.get(v, 0) >= 2 and sd > 0:
        return f"{val:.{p}f} \u00b1{sd:.{p}f}"
    return f"{val:.{p}f}"


def _fmt_delta(row: SummaryRow) -> str:
    """Delta (percentage) annotated with the absolute-unit paired CI + marker."""
    if not row.delta:
        return ""
    out = row.delta
    if row.ci_low is not None and row.ci_high is not None:
        # CI is in absolute metric units, not percentage points — labelled to
        # avoid being misread against the % delta it sits beside.
        p = row.precision
        out += f" [CI {row.ci_low:+.{p}f},{row.ci_high:+.{p}f} abs]"
        if row.significant is True:
            out += "*"
        elif row.significant is False:
            out += " ns"
        else:
            out += " low-n"
    return out


def _fmt_pass_k_value(row: SummaryRow, v: str) -> str:
    """pass@k/pass^k value: already a 0-100 rate, rendered as a percentage."""
    if row.n.get(v, 0) == 0:
        return "\u2014"
    val = row.values.get(v, 0.0)
    sd = row.stddev.get(v, 0.0)
    if row.n.get(v, 0) >= 2 and sd > 0:
        return f"{val:.0f}% \u00b1{sd:.0f}%"
    return f"{val:.0f}%"


def _fmt_pass_k_delta(row: SummaryRow) -> str:
    """pass@k/pass^k delta: an absolute percentage-point difference (the metric
    is already a rate), not a relative-to-baseline percentage like other
    metrics -- e.g. baseline=67%, experimental=100% -> "+33%"."""
    if not row.delta:
        return ""
    out = row.delta
    if row.ci_low is not None and row.ci_high is not None:
        out += f" [CI {row.ci_low:+.0f}%,{row.ci_high:+.0f}% abs]"
        if row.significant is True:
            out += "*"
        elif row.significant is False:
            out += " ns"
        else:
            out += " low-n"
    return out


def _significance_legend(reports: list[Report]) -> bool:
    return any(
        row.ci_low is not None
        for report in reports
        for row in (*report.summary, *report.judge_scores, *report.pass_k)
    )


_MC_METHOD_LABELS = {
    "holm": "Holm-Bonferroni",
    "benjamini-hochberg": "Benjamini-Hochberg (FDR)",
    "bh": "Benjamini-Hochberg (FDR)",
}


def _mc_correction_line(report: Report) -> str | None:
    """One-line disclosure of the multiple-comparison correction applied (or
    not) to this task's `*` markers -- the number of tests and the method, so
    a reader can judge how conservative the significance claims are."""
    if report.mc_correction == "none" or not report.mc_tests:
        return None
    label = _MC_METHOD_LABELS.get(report.mc_correction, report.mc_correction)
    return f"Multiple-comparison correction: {label} across {report.mc_tests} test(s)."


def _legend_marker_meaning(reports: list[Report]) -> tuple[str, str, str]:
    """Plain-text (format-agnostic) description of what `*`/`ns` mean, plus a
    trailing note -- swapped based on whether any report actually applied a
    multiple-comparison correction (it's a single run-wide setting, so either
    all reports agree or none did any correction at all)."""
    if not any(r.mc_correction != "none" for r in reports):
        return (
            "CI excludes 0 (statistically supported)",
            "not supported (observed only)",
            "No multiple-comparison correction applied.",
        )
    method = next(r.mc_correction for r in reports if r.mc_correction != "none")
    label = _MC_METHOD_LABELS.get(method, method)
    return (
        f"CI excludes 0 and remains significant after {label} multiple-comparison "
        "correction (see the per-task line above for the test count)",
        "not significant after correction, or CI included 0",
        "",
    )


def _stdout_supports_color(stream: Any = None) -> bool:
    """Whether ANSI color should be emitted for ``stream`` (defaults to stdout).

    Honors the ``NO_COLOR`` convention, ``TERM=dumb``, and non-TTY (piped /
    redirected) output so escape codes never leak into files, pipes, or CI
    logs. This keeps the colored path opt-in to interactive terminals only.

    Per the no-color.org spec, ``NO_COLOR`` disables color only when *present
    and non-empty* — so ``NO_COLOR=""`` intentionally does not disable color
    (the falsy ``.get()`` check below is deliberate, not a bug).
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if stream is None:
        stream = sys.stdout
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _colorize_delta(
    text: str, row: SummaryRow, *, lower_is_better: bool, enabled: bool
) -> str:
    """Wrap an already-justified Delta cell in ANSI color when ``enabled``.

    Significant improvements render green + bold, significant regressions
    red + bold, and non-significant deltas dim. ``low-n`` / uncomputed deltas
    (``significant is None``) are left uncolored. Direction is metric-aware:
    for lower-is-better metrics (duration, cost, tokens…) a decrease is an
    improvement; for higher-is-better metrics (judge scores, pass@k) an
    increase is.

    Direction is read from the paired-delta CI (the same signal the issue's
    spec names: "CI excludes zero, direction positive/negative"), so a
    tiny-but-significant delta that rounds to ``+0.0%`` in the printed string
    is still colored by its true sign.
    """
    if not enabled or not text.strip():
        return text
    if row.significant is True:
        # A significant delta has a CI that excludes 0, so both bounds share a
        # sign. Derive the direction from whichever bound proves it; if neither
        # does (should be impossible for significant rows), leave it uncolored
        # rather than guess a color.
        if row.ci_high is not None and row.ci_high < 0:
            decreased = True
        elif row.ci_low is not None and row.ci_low > 0:
            decreased = False
        else:
            return text
        improved = decreased if lower_is_better else not decreased
        return click.style(text, fg="green" if improved else "red", bold=True)
    if row.significant is False:
        return click.style(text, dim=True)
    return text


def format_table(reports: list[Report], *, color: bool | None = None) -> str:
    # `color=None` auto-detects an interactive terminal; tests and callers can
    # force it on/off. Auto-detection resolves to False under pytest / pipes,
    # so the default (uncolored) output stays byte-stable for golden tests.
    color_enabled = _stdout_supports_color() if color is None else color
    sections: list[str] = []
    for report in reports:
        lines: list[str] = []
        lines.append(f"\n{'=' * 80}")
        lines.append(f"TASK: {report.task}")
        lines.append("=" * 80)

        # Sample size + data-sufficiency caveats
        n_desc = ", ".join(f"{v}={report.variant_n.get(v, 0)}" for v in report.variants)
        sample_line = f"Samples: {n_desc}"
        if report.aggregate == "paired" and len(report.variants) == 2:
            sample_line += f"; paired epochs={report.paired_n}"
        lines.append("\n" + sample_line)
        if mc_line := _mc_correction_line(report):
            lines.append(mc_line)
        for w in report.warnings:
            if w["type"] == "low_power":
                lines.append("")
                lines.extend(w["message"].split("\n"))
            else:
                lines.append(f"  ! {w['message']}")

        # Reliability (success/failure rates as first-class metrics)
        if report.reliability:
            rhdr = "".join(f"{v:>18}" for v in report.variants)
            lines.append("\nReliability")
            lines.append(f"{'Metric':<24} {rhdr}")
            lines.append("-" * (24 + 18 * len(report.variants)))
            for rrow in report.reliability:
                cols = "".join(f"{rrow.values.get(v, '—'):>18}" for v in report.variants)
                lines.append(f"{rrow.metric:<24} {cols}")

        # Per-run header — column order matches _METRIC_DEFS
        jnames = report.judge_names
        jhdr = "".join(f" {n[:8]:>8}" for n in jnames)
        lines.append(
            f"\n{'Variant':<18} {'Epoch':>5} {'Dur(s)':>7} {'Turns':>5} {'Spans':>5} "
            f"{'Tools':>5} {'In Tok':>8} {'Out Tok':>8} {'Cache':>8} {'TDur(s)':>7} "
            f"{'Cost($)':>9}{jhdr}"
        )
        lines.append("-" * (90 + 9 * len(jnames)))
        for r in report.runs:
            jvals = ""
            for n in jnames:
                s = report.epoch_judges.get((r.variant, r.epoch), {}).get(n)
                jvals += f" {s:>8}" if s is not None else f" {'—':>8}"
            lines.append(
                f"{r.variant:<18} {r.epoch:>5} {r.duration:>7.1f} {r.turn_count:>5} "
                f"{r.total_spans:>5} {r.tool_count:>5} "
                f"{r.total_input_tokens:>8} {r.total_output_tokens:>8} "
                f"{r.total_cache_tokens:>8} {r.tool_duration:>7.1f} {r.cost:>9.4f}{jvals}"
            )

        # Summary
        hdr = "".join(f"{v:>22}" for v in report.variants)
        lines.append(f"\nMetrics ({report.aggregate})")
        lines.append(f"{'Metric':<24} {hdr} {'Delta':>26}")
        lines.append("-" * (24 + 22 * len(report.variants) + 26))
        for row in report.summary:
            cols = "".join(f"{_fmt_value(row, v):>22}" for v in report.variants)
            delta = _colorize_delta(
                f"{_fmt_delta(row):>26}", row, lower_is_better=True, enabled=color_enabled
            )
            lines.append(f"{row.metric:<24} {cols} {delta}")
        if report.judge_scores:
            lines.append(f"\n{'Judge':<24} {hdr} {'Delta':>26}")
            lines.append("-" * (24 + 22 * len(report.variants) + 26))
            for row in report.judge_scores:
                cols = "".join(f"{_fmt_value(row, v):>22}" for v in report.variants)
                delta = _colorize_delta(
                    f"{_fmt_delta(row):>26}", row, lower_is_better=False, enabled=color_enabled
                )
                lines.append(f"{row.metric:<24} {cols} {delta}")
        if report.pass_k:
            lines.append(f"\n{'Pass@k / Pass^k':<24} {hdr} {'Delta':>26}")
            lines.append("-" * (24 + 22 * len(report.variants) + 26))
            for row in report.pass_k:
                cols = "".join(f"{_fmt_pass_k_value(row, v):>22}" for v in report.variants)
                delta = _colorize_delta(
                    f"{_fmt_pass_k_delta(row):>26}", row, lower_is_better=False, enabled=color_enabled
                )
                lines.append(f"{row.metric:<24} {cols} {delta}")

        rt = report.judge_runtime
        if rt:
            lines.append("\nJudge runtime")
            outcomes = ", ".join(f"{k}={v}" for k, v in rt.get("outcomes", {}).items())
            lines.append(f"  Outcomes ({rt.get('total', 0)}): {outcomes}")
            if rt.get("versions"):
                lines.append(f"  Host Copilot: {', '.join(rt['versions'])}")
            if rt.get("truncated"):
                lines.append(f"  Truncated context: {rt['truncated']} judge run(s)")
            if rt.get("version_mismatch"):
                lines.append("  WARNING: host Copilot version mismatch detected")

        sections.append("\n".join(lines))
    body = "\n".join(sections)
    if _significance_legend(reports):
        star_meaning, ns_meaning, trailing_note = _legend_marker_meaning(reports)
        body += (
            "\n\nLegend: value \u00b1stddev; Delta is a percentage, [CI low,high abs] is a "
            "bootstrap interval of the paired delta in absolute metric units; "
            f"* = {star_meaning}, ns = {ns_meaning}, low-n = fewer than {MIN_RELIABLE_N} "
            f"paired samples (significance not assessed).{' ' + trailing_note if trailing_note else ''}"
        )
    return body


def _summary_row_json(r: SummaryRow, key_name: str) -> dict[str, Any]:
    return {
        key_name: r.metric,
        "values": r.values,
        "delta": r.delta,
        "n": r.n,
        "stddev": r.stddev,
        "min": r.vmin,
        "max": r.vmax,
        "paired_n": r.paired_n,
        "ci_low": r.ci_low,
        "ci_high": r.ci_high,
        "p_value": r.p_value,
        "significant": r.significant,
    }


def format_json(reports: list[Report]) -> str:
    data = {
        "tasks": [
            {
                "task": report.task,
                "aggregate": report.aggregate,
                "variants": report.variants,
                "variant_n": report.variant_n,
                "paired_n": report.paired_n,
                "warnings": report.warnings,
                "mc_correction": report.mc_correction,
                "mc_tests": report.mc_tests,
                "reliability": [
                    {"metric": rr.metric, "values": rr.values} for rr in report.reliability
                ],
                "runs": [
                    {
                        "variant": r.variant,
                        "epoch": r.epoch,
                        "duration": r.duration,
                        "turn_count": r.turn_count,
                        "total_spans": r.total_spans,
                        "tool_count": r.tool_count,
                        "input_tokens": r.total_input_tokens,
                        "output_tokens": r.total_output_tokens,
                        "cache_tokens": r.total_cache_tokens,
                        "tool_duration": r.tool_duration,
                        "tool_names": r.tool_names,
                        "model": r.model,
                        "cost": r.cost,
                        "judges": report.epoch_judges.get((r.variant, r.epoch), {}),
                        "judge_stddevs": report.epoch_stddevs.get((r.variant, r.epoch), {}),
                    }
                    for r in report.runs
                ],
                "summary": [_summary_row_json(r, "metric") for r in report.summary],
                "tool_patterns": report.tool_patterns,
                "judge_scores": [_summary_row_json(r, "judge") for r in report.judge_scores],
                "pass_k": [_summary_row_json(r, "metric") for r in report.pass_k],
                "judge_runtime": report.judge_runtime,
            }
            for report in reports
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_markdown(reports: list[Report]) -> str:
    sections: list[str] = []
    for report in reports:
        lines: list[str] = []
        lines.append(f"## {report.task}\n")

        # Sample size + data-sufficiency caveats
        n_desc = ", ".join(f"{v}={report.variant_n.get(v, 0)}" for v in report.variants)
        sample_line = f"**Samples:** {n_desc}"
        if report.aggregate == "paired" and len(report.variants) == 2:
            sample_line += f"; paired epochs={report.paired_n}"
        lines.append(sample_line)
        if mc_line := _mc_correction_line(report):
            lines.append(f"**{mc_line}**")
        for w in report.warnings:
            if w["type"] == "low_power":
                lines.append("")
                lines.extend(f"> {ln}" for ln in w["message"].split("\n"))
            else:
                lines.append(f"\n> ⚠️ {w['message']}")

        # Reliability (success/failure rates as first-class metrics)
        if report.reliability:
            lines.append("\n### Reliability\n")
            lines.append("| Metric |" + "".join(f" {v} |" for v in report.variants))
            lines.append("|--------|" + "".join("--------:|" for _ in report.variants))
            for rrow in report.reliability:
                cols = "".join(f" {rrow.values.get(v, '—')} |" for v in report.variants)
                lines.append(f"| {rrow.metric} |{cols}")

        # Summary
        lines.append(f"\n### Metrics ({report.aggregate})\n")
        lines.append("| Metric |" + "".join(f" {v} |" for v in report.variants) + " Delta |")
        lines.append("|--------|" + "".join("--------:|" for _ in report.variants) + "------:|")
        for row in report.summary:
            cols = "".join(f" {_fmt_value(row, v)} |" for v in report.variants)
            lines.append(f"| {row.metric} |{cols} {_fmt_delta(row)} |")

        # Tool usage
        lines.append("\n### Tool Usage\n")
        for v in report.variants:
            tools = report.tool_patterns.get(v, {})
            top = sorted(tools.items(), key=lambda x: -x[1])[:10]
            lines.append(f"**{v}**: " + ", ".join(f"`{t}`({n})" for t, n in top))

        # Judge summary
        if report.judge_scores:
            lines.append(f"\n### Judge Scores ({report.aggregate})\n")
            lines.append("| Judge |" + "".join(f" {v} |" for v in report.variants) + " Delta |")
            lines.append("|-------|" + "".join("--------:|" for _ in report.variants) + "------:|")
            for row in report.judge_scores:
                cols = "".join(f" {_fmt_value(row, v)} |" for v in report.variants)
                lines.append(f"| {row.metric} |{cols} {_fmt_delta(row)} |")

        # pass@k / pass^k reliability
        if report.pass_k:
            lines.append("\n### Pass@k / Pass^k Reliability\n")
            lines.append("| Metric |" + "".join(f" {v} |" for v in report.variants) + " Delta |")
            lines.append("|--------|" + "".join("--------:|" for _ in report.variants) + "------:|")
            for row in report.pass_k:
                cols = "".join(f" {_fmt_pass_k_value(row, v)} |" for v in report.variants)
                lines.append(f"| {row.metric} |{cols} {_fmt_pass_k_delta(row)} |")

        # Judge runtime (reproducibility / observability)
        rt = report.judge_runtime
        if rt:
            lines.append("\n### Judge Runtime\n")
            outcomes = ", ".join(f"`{k}`={v}" for k, v in rt.get("outcomes", {}).items())
            lines.append(f"- Outcomes ({rt.get('total', 0)}): {outcomes}")
            if rt.get("versions"):
                lines.append(f"- Host Copilot: {', '.join(f'`{v}`' for v in rt['versions'])}")
            if rt.get("truncated"):
                lines.append(f"- ⚠️ Truncated context: {rt['truncated']} judge run(s)")
            if rt.get("version_mismatch"):
                lines.append("- ⚠️ Host Copilot version mismatch detected")

        # Per-run details — column order matches _METRIC_DEFS
        jnames = report.judge_names
        lines.append("\n### Per-Run Details\n")
        jhdr = "".join(f" {n} |" for n in jnames)
        jsep = "".join("------:|" for _ in jnames)
        lines.append(
            f"| Variant | Epoch | Dur(s) | Turns | Spans | Tools | In Tok | Out Tok | Cache | TDur(s) | Cost($) |{jhdr}"
        )
        lines.append(
            f"|---------|------:|-------:|------:|------:|------:|-------:|--------:|------:|--------:|--------:|{jsep}"
        )
        for r in report.runs:
            jvals = ""
            for n in jnames:
                s = report.epoch_judges.get((r.variant, r.epoch), {}).get(n)
                sd = report.epoch_stddevs.get((r.variant, r.epoch), {}).get(n)
                if s is None:
                    jvals += " — |"
                elif sd:
                    jvals += f" {s} (±{sd:.2f}) |"
                else:
                    jvals += f" {s} |"
            lines.append(
                f"| {r.variant} | {r.epoch} | {r.duration:.1f} | {r.turn_count} | "
                f"{r.total_spans} | {r.tool_count} | {r.total_input_tokens} | "
                f"{r.total_output_tokens} | {r.total_cache_tokens} | {r.tool_duration:.1f} | "
                f"{r.cost:.4f} |{jvals}"
            )

        # Judge reasons
        if report.epoch_reasons:
            lines.append("\n### Judge Reasons\n")
            for r in report.runs:
                reasons = report.epoch_reasons.get((r.variant, r.epoch), {})
                if reasons:
                    lines.append(f"**{r.variant} epoch {r.epoch}**:")
                    for n in report.judge_names:
                        reason = reasons.get(n, "")
                        score = report.epoch_judges.get((r.variant, r.epoch), {}).get(n)
                        if reason:
                            lines.append(f"- {n} ({score}): {reason}")
                    lines.append("")

        sections.append("\n".join(lines))
    body = "\n\n---\n\n".join(sections)
    if _significance_legend(reports):
        star_meaning, ns_meaning, trailing_note = _legend_marker_meaning(reports)
        body += (
            "\n\n---\n\n_Legend: value ±stddev; Delta is a percentage, `[CI low,high abs]` "
            "is a bootstrap interval of the paired delta in **absolute metric units**; "
            f"`*` = {star_meaning}, `ns` = {ns_meaning}, `low-n` = fewer than "
            f"{MIN_RELIABLE_N} paired samples (significance not assessed)."
            f"{' ' + trailing_note if trailing_note else ''}_"
        )
    return body


# --- Compact markdown (PR comment) ---


def _fmt_delta_compact(row: SummaryRow, *, higher_is_better: bool) -> str:
    """Delta cell for the compact table: bold + ✅/❌ only when the delta is
    CI-significant (survives multiple-comparison correction), otherwise a bare
    percentage. The full `[CI low,high abs]`/`ns`/`low-n` detail from
    `_fmt_delta` is left out on purpose -- that's for the full `analyze`
    report, not a PR comment skimmed in a few seconds."""
    if not row.delta:
        return ""
    if row.significant is not True:
        return row.delta
    try:
        pct = float(row.delta.rstrip("%"))
    except ValueError:
        return row.delta
    improved = (pct > 0) if higher_is_better else (pct < 0)
    return f"**{row.delta}** {'✅' if improved else '❌'}"


def _significant_metrics(report: Report) -> list[str]:
    return [
        row.metric
        for row in (*report.summary, *report.judge_scores, *report.pass_k)
        if row.significant is True
    ]


def _ci_summary_line(report: Report) -> str | None:
    """One-line, plain-language summary of which metrics (if any) have a
    bootstrap CI that excludes zero -- the headline takeaway for a reviewer
    who only reads one line of the PR comment."""
    if report.aggregate != "paired" or len(report.variants) != 2:
        return None
    n = report.paired_n
    epoch_word = "epoch" if n == 1 else "epochs"
    sig = _significant_metrics(report)
    if sig:
        return f"> 95% CI excludes zero for {', '.join(sig)}. N={n} paired {epoch_word}."
    return f"> No metric's 95% CI excludes zero. N={n} paired {epoch_word}."


def _warning_banners_compact(report: Report) -> list[str]:
    """Condensed (first-line-only) warning banners for the compact format."""
    return [f"> ⚠️ {w['message'].split(chr(10), 1)[0]}" for w in report.warnings]


def _truncate_for_pr_comment(text: str, limit: int = PR_COMMENT_CHAR_LIMIT) -> str:
    """Truncate to fit GitHub's comment size limit, appending a visible notice.

    Cuts on a line boundary so the result never ends mid-table-row, and never
    exceeds `limit` even after the notice is appended.
    """
    if len(text) <= limit:
        return text
    notice = "\n\n> ⚠️ **Report truncated** to fit GitHub's 65,536-character PR comment limit."
    budget = limit - len(notice)
    truncated = text[:budget]
    # Cut at the last full line so we don't emit a broken markdown table row.
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        truncated = truncated[:last_newline]
    return truncated + notice


def _compact_row(metric: str, cols: str, delta: str, *, two_variant: bool) -> str:
    if two_variant:
        return f"| {metric} |{cols} {delta} |"
    return f"| {metric} |{cols}"


def format_markdown_compact(reports: list[Report]) -> str:
    """Condensed markdown for CI PR comments (`analyze -o markdown --compact`).

    Unlike `format_markdown`, this drops per-run tables, tool-usage lists, and
    judge reasons -- just the headline metric/judge/pass@k tables, a one-line
    CI summary, and any data-sufficiency warnings -- so it comfortably fits
    GitHub's 65KB comment limit (enforced as a last-resort truncation guard)
    and reads well pasted straight into `gh pr comment --body`.
    """
    sections: list[str] = []
    for report in reports:
        lines: list[str] = [f"## 📊 copilot-eval: {report.task}\n"]

        variants = report.variants
        two_variant = report.aggregate == "paired" and len(variants) == 2

        header = "| Metric |" + "".join(f" {v} |" for v in variants)
        sep = "|--------|" + "".join("--------:|" for _ in variants)
        if two_variant:
            header += " Δ |"
            sep += "------:|"
        lines.append(header)
        lines.append(sep)

        for row in report.summary:
            cols = "".join(f" {_fmt_value(row, v)} |" for v in variants)
            delta = _fmt_delta_compact(row, higher_is_better=False) if two_variant else ""
            lines.append(_compact_row(row.metric, cols, delta, two_variant=two_variant))
        for row in report.judge_scores:
            cols = "".join(f" {_fmt_value(row, v)} |" for v in variants)
            delta = _fmt_delta_compact(row, higher_is_better=True) if two_variant else ""
            lines.append(_compact_row(row.metric, cols, delta, two_variant=two_variant))
        for row in report.pass_k:
            cols = "".join(f" {_fmt_pass_k_value(row, v)} |" for v in variants)
            delta = _fmt_delta_compact(row, higher_is_better=True) if two_variant else ""
            lines.append(_compact_row(row.metric, cols, delta, two_variant=two_variant))

        if ci_line := _ci_summary_line(report):
            lines.append("")
            lines.append(ci_line)
        lines.extend(_warning_banners_compact(report))
        if mc_line := _mc_correction_line(report):
            lines.append(f"> {mc_line}")

        sections.append("\n".join(lines))

    body = "\n\n---\n\n".join(sections)
    return _truncate_for_pr_comment(body)


# --- CI-native output formats (JUnit XML, GitHub Actions step summary, HTML) ---

# Groups of (kind, rows, higher_is_better) mirroring the direction rules used
# by the compact markdown formatter (`_fmt_delta_compact` call sites): OTel
# metrics (duration/cost/tokens/...) regress when they go *up*, judge scores
# and pass@k/pass^k rates regress when they go *down*.
_RowGroup = tuple[str, list[SummaryRow], bool]


def _row_groups(report: Report) -> list[_RowGroup]:
    return [
        ("metric", report.summary, False),
        ("judge", report.judge_scores, True),
        ("pass_k", report.pass_k, True),
    ]


def _delta_pct(row: SummaryRow) -> float | None:
    """Parse `row.delta` (e.g. `"+12.5%"`) back to a float, or None when the
    delta is empty/unparseable (no common paired epochs)."""
    if not row.delta:
        return None
    try:
        return float(row.delta.rstrip("%"))
    except ValueError:
        return None


def _is_improvement(row: SummaryRow, *, higher_is_better: bool) -> bool | None:
    """Whether the delta moved in the favorable direction; None when there's
    no parseable delta (e.g. no paired epochs)."""
    pct = _delta_pct(row)
    if pct is None or pct == 0:
        return None
    return (pct > 0) if higher_is_better else (pct < 0)


def _is_regression(row: SummaryRow, *, higher_is_better: bool) -> bool:
    """A CI-gate-worthy regression: the paired-delta CI excludes zero (and
    survives multiple-comparison correction -- `row.significant` already
    reflects that), *and* the change moved in the unfavorable direction."""
    if row.significant is not True:
        return False
    improved = _is_improvement(row, higher_is_better=higher_is_better)
    return improved is False


# --- JUnit XML ---


def _junit_testcase(row: SummaryRow, *, task: str, higher_is_better: bool) -> ET.Element:
    testcase = ET.Element("testcase", {"classname": task, "name": row.metric})
    detail = [f"values: {row.values}"]
    if row.ci_low is not None and row.ci_high is not None:
        p = row.precision
        detail.append(
            f"delta: {row.delta} (95% CI [{row.ci_low:+.{p}f},{row.ci_high:+.{p}f}] abs, "
            f"paired_n={row.paired_n})"
        )
    elif row.delta:
        detail.append(f"delta: {row.delta}")
    else:
        detail.append("delta: n/a (no common paired epochs)")
    if _is_regression(row, higher_is_better=higher_is_better):
        failure = ET.SubElement(
            testcase,
            "failure",
            {
                "message": f"Statistically significant regression in {row.metric} ({row.delta})",
                "type": "RegressionError",
            },
        )
        failure.text = "\n".join(detail)
    else:
        system_out = ET.SubElement(testcase, "system-out")
        system_out.text = "\n".join(detail)
    return testcase


def format_junit(reports: list[Report]) -> str:
    """JUnit XML for native CI test-report integration (GitHub Actions,
    Azure Pipelines, Jenkins, GitLab CI, ...) -- ``analyze -o junit``.

    One ``<testsuite>`` per task, one ``<testcase>`` per metric/judge-score/
    pass@k comparison. A comparison whose bootstrap CI excludes zero (and
    survives multiple-comparison correction) *and* moved in the unfavorable
    direction (metrics regress upward, judge/pass@k scores regress downward)
    renders as a ``<failure>`` so CI systems surface it as a failed test.
    """
    testsuites = ET.Element("testsuites")
    for report in reports:
        groups = _row_groups(report)
        all_rows = [row for _, rows, _ in groups for row in rows]
        failures = sum(
            1
            for _, rows, hib in groups
            for row in rows
            if _is_regression(row, higher_is_better=hib)
        )
        testsuite = ET.SubElement(
            testsuites,
            "testsuite",
            {
                "name": report.task,
                "tests": str(len(all_rows)),
                "failures": str(failures),
                "errors": "0",
                "skipped": "0",
            },
        )
        for _, rows, hib in groups:
            for row in rows:
                testsuite.append(_junit_testcase(row, task=report.task, higher_is_better=hib))
    total_tests = sum(int(ts.get("tests", "0")) for ts in testsuites)
    total_failures = sum(int(ts.get("failures", "0")) for ts in testsuites)
    testsuites.set("name", "copilot-eval")
    testsuites.set("tests", str(total_tests))
    testsuites.set("failures", str(total_failures))
    ET.indent(testsuites, space="  ")
    body = ET.tostring(testsuites, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


# --- GitHub Actions step summary ---


def format_gha_summary(reports: list[Report]) -> str:
    """Markdown for a GitHub Actions step summary (``analyze -o gha-summary``).

    Reuses the compact PR-comment format: a step summary is read in the same
    few-seconds-skim context as a PR comment, so the same condensed table +
    CI-summary-line + warnings shape applies.
    """
    return format_markdown_compact(reports)


def write_gha_summary(content: str, *, env: Mapping[str, str] | None = None) -> bool:
    """Append `content` to `$GITHUB_STEP_SUMMARY` when set.

    Returns True when the write happened, False when `GITHUB_STEP_SUMMARY`
    isn't set (e.g. running locally, or on a non-GitHub-Actions CI system) --
    callers should print `content` to stdout instead in that case.
    """
    env = os.environ if env is None else env
    path = env.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")
    return True


# --- Self-contained HTML report ---

_HTML_CSS = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
  margin: 2rem; max-width: 1100px; }
h1 { font-size: 1.4rem; }
h2 { font-size: 1.15rem; margin-top: 2.5rem; border-bottom: 1px solid #8884; padding-bottom: .3rem; }
table { border-collapse: collapse; width: 100%; margin: .75rem 0 1.5rem; font-size: .9rem; }
th, td { border: 1px solid #8884; padding: .4rem .6rem; text-align: right; }
th:first-child, td:first-child { text-align: left; }
th { background: #8881; }
td.sig-good { background: #2ecc7133; font-weight: 600; }
td.sig-bad { background: #e74c3c33; font-weight: 600; }
.bar { position: relative; height: .55rem; background: #8882; border-radius: 2px; margin-top: .3rem; }
.bar span { position: absolute; inset: 0; background: #3498db; border-radius: 2px; }
.warning { background: #f39c1233; border-left: 3px solid #f39c12; padding: .5rem .75rem; margin: .5rem 0; }
.meta { color: #888; font-size: .85rem; }
.legend { color: #888; font-size: .8rem; margin-top: .5rem; }
"""


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _html_bar(value: float, max_value: float) -> str:
    """CSS-only horizontal bar (a sparkline substitute) sized relative to the
    largest value across variants for this row -- an at-a-glance sense of
    relative magnitude with no JS/chart library."""
    pct = 0.0 if max_value <= 0 else max(0.0, min(100.0, (value / max_value) * 100))
    return f'<div class="bar"><span style="width:{pct:.1f}%"></span></div>'


def _html_sig_class(row: SummaryRow, *, higher_is_better: bool) -> str:
    if row.significant is not True:
        return ""
    improved = _is_improvement(row, higher_is_better=higher_is_better)
    if improved is None:
        return ""
    return ' class="sig-good"' if improved else ' class="sig-bad"'


def _html_rows_table(
    title: str,
    rows: list[SummaryRow],
    variants: list[str],
    *,
    higher_is_better: bool,
    pass_k: bool = False,
) -> str:
    if not rows:
        return ""
    fmt_value = _fmt_pass_k_value if pass_k else _fmt_value
    fmt_delta = _fmt_pass_k_delta if pass_k else _fmt_delta
    out = [f"<h3>{_html_escape(title)}</h3>", "<table>", "<tr><th>Metric</th>"]
    out.extend(f"<th>{_html_escape(v)}</th>" for v in variants)
    out.append("<th>Delta</th></tr>")
    for row in rows:
        max_v = max((row.values.get(v, 0.0) for v in variants), default=0.0)
        cells = "".join(
            f"<td>{_html_escape(fmt_value(row, v))}{_html_bar(row.values.get(v, 0.0), max_v)}</td>"
            for v in variants
        )
        sig_class = _html_sig_class(row, higher_is_better=higher_is_better)
        out.append(
            f"<tr><td>{_html_escape(row.metric)}</td>{cells}"
            f"<td{sig_class}>{_html_escape(fmt_delta(row))}</td></tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def format_html(reports: list[Report]) -> str:
    """Self-contained single-file HTML report (``analyze -o html``): all CSS
    inlined, no external resources, tables color-coded for statistically
    significant deltas, and CSS-only bars standing in for a score-distribution
    chart.
    """
    body: list[str] = []
    for report in reports:
        body.append(f"<h2>{_html_escape(report.task)}</h2>")
        n_desc = ", ".join(f"{v}={report.variant_n.get(v, 0)}" for v in report.variants)
        meta = f"Samples: {n_desc}"
        if report.aggregate == "paired" and len(report.variants) == 2:
            meta += f"; paired epochs={report.paired_n}"
        body.append(f'<p class="meta">{_html_escape(meta)}</p>')
        if mc_line := _mc_correction_line(report):
            body.append(f'<p class="meta">{_html_escape(mc_line)}</p>')
        for w in report.warnings:
            first_line = w["message"].split("\n", 1)[0]
            body.append(f'<div class="warning">⚠️ {_html_escape(first_line)}</div>')

        if report.reliability:
            body.append("<h3>Reliability</h3><table><tr><th>Metric</th>")
            body.extend(f"<th>{_html_escape(v)}</th>" for v in report.variants)
            body.append("</tr>")
            for rrow in report.reliability:
                cells = "".join(
                    f"<td>{_html_escape(rrow.values.get(v, '—'))}</td>" for v in report.variants
                )
                body.append(f"<tr><td>{_html_escape(rrow.metric)}</td>{cells}</tr>")
            body.append("</table>")

        body.append(
            _html_rows_table(
                f"Metrics ({report.aggregate})",
                report.summary,
                report.variants,
                higher_is_better=False,
            )
        )
        if report.judge_scores:
            body.append(
                _html_rows_table(
                    f"Judge Scores ({report.aggregate})",
                    report.judge_scores,
                    report.variants,
                    higher_is_better=True,
                )
            )
        if report.pass_k:
            body.append(
                _html_rows_table(
                    "Pass@k / Pass^k Reliability",
                    report.pass_k,
                    report.variants,
                    higher_is_better=True,
                    pass_k=True,
                )
            )

    legend = ""
    if _significance_legend(reports):
        star_meaning, ns_meaning, trailing_note = _legend_marker_meaning(reports)
        legend = (
            '<p class="legend">Green = statistically significant improvement, '
            "red = statistically significant regression "
            f"({star_meaning} / {ns_meaning}). {trailing_note}</p>"
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>copilot-eval report</title>"
        f"<style>{_HTML_CSS}</style></head><body>"
        "<h1>copilot-eval A/B report</h1>"
        f"{''.join(body)}"
        f"{legend}"
        "</body></html>\n"
    )


# --- Judge score loading ---


def _load_judge_raw(
    results_dir: Path, variants: list[str], task: str
) -> tuple[
    dict[tuple[str, str], dict[str, int]],
    dict[tuple[str, str], dict[str, str]],
    list[str],
    dict[tuple[str, str], dict[str, float]],
    dict[tuple[str, str], dict[str, bool]],
]:
    """Load per-epoch judge scores, reasons, spread, and pass/fail.

    Returns (epoch_data, epoch_reasons, evaluator_names, epoch_stddevs, epoch_passed).
    ``epoch_passed`` is each evaluator's persisted ``EvalScore.passed`` bit (see
    ``eval.protocols.score_to_dict``) — the binary signal pass@k/pass^k are built
    from. Older/hand-written ``*.scores.json`` fixtures that predate the
    ``passed`` key fall back to the score's truthiness, which matches
    production for the deterministic evaluator types (contains/regex/script/
    metric all derive ``score`` from ``passed`` 1:1); for judge evaluators
    production always persists ``passed=True`` for a successful score, so this
    fallback is only ever exercised by fixtures, not real data.
    """
    epoch_data: dict[tuple[str, str], dict[str, int]] = {}
    epoch_reasons: dict[tuple[str, str], dict[str, str]] = {}
    epoch_stddevs: dict[tuple[str, str], dict[str, float]] = {}
    epoch_passed: dict[tuple[str, str], dict[str, bool]] = {}
    all_names: set[str] = set()

    if not results_dir or not results_dir.exists():
        return {}, {}, [], {}, {}

    for pattern in ["*.scores.json", "*.judges.json"]:
        for jf in results_dir.glob(pattern):
            stem = jf.stem.replace(".scores", "").replace(".judges", "")
            if not stem.startswith(f"{task}_"):
                continue
            parsed = parse_slug(stem, variants)
            if not parsed:
                continue
            variant, fixture, epoch = parsed
            # Fixture-qualified key so scores pair with the same (fixture, epoch)
            # cell as the OTel metrics; single-fixture keeps the bare epoch.
            epoch_str = _pair_label(fixture, epoch)
            try:
                scores = {}
                reasons = {}
                stddevs = {}
                passed = {}
                for s in json.loads(jf.read_text()):
                    if s.get("score") is not None:
                        scores[s["name"]] = int(s["score"])
                        reasons[s["name"]] = str(s.get("reason", ""))
                        if s.get("score_stddev") is not None:
                            stddevs[s["name"]] = float(s["score_stddev"])
                        passed[s["name"]] = bool(s.get("passed", s.get("score")))
                        all_names.add(s["name"])
                epoch_data[(variant, epoch_str)] = scores
                epoch_reasons[(variant, epoch_str)] = reasons
                epoch_stddevs[(variant, epoch_str)] = stddevs
                epoch_passed[(variant, epoch_str)] = passed
            except (json.JSONDecodeError, KeyError):
                continue

    return epoch_data, epoch_reasons, sorted(all_names), epoch_stddevs, epoch_passed


def _load_judge_runtime(results_dir: Path, variants: list[str], task: str) -> dict[str, Any]:
    """Aggregate judge-runtime metadata across a task's scores files.

    Returns outcome counts (ok/parse_error/error/timeout/...), the set of host
    Copilot versions used, how many judges saw truncated context, and whether any
    host version mismatched the configured expectation.
    """
    outcomes: dict[str, int] = {}
    versions: set[str] = set()
    truncated = 0
    mismatch = False
    total = 0
    seen: set[Path] = set()

    if not results_dir or not results_dir.exists():
        return {}

    for jf in results_dir.glob("*.scores.json"):
        stem = jf.stem.replace(".scores", "")
        if not stem.startswith(f"{task}_"):
            continue
        name_variant = stem.rsplit("_epoch", 1)[0]
        # Match the longest variant name (mirrors _load_judge_raw) so a shorter
        # variant doesn't claim a file belonging to a longer-named one.
        matches = [v for v in variants if name_variant.endswith(f"_{v}")]
        if not matches:
            continue
        if jf in seen:
            continue
        seen.add(jf)
        try:
            scores = json.loads(jf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for s in scores:
            if s.get("type") != "judge":
                continue
            total += 1
            meta = s.get("meta") or {}
            outcome = meta.get("outcome") or ("ok" if s.get("score") is not None else "unknown")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if v := meta.get("judge_version"):
                versions.add(str(v))
            if meta.get("judge_version_mismatch"):
                mismatch = True
            if meta.get("truncation"):
                truncated += 1

    if not total:
        return {}
    return {
        "total": total,
        "outcomes": dict(sorted(outcomes.items())),
        "versions": sorted(versions),
        "truncated": truncated,
        "version_mismatch": mismatch,
    }
