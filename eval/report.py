"""A/B comparison report generation with multiple output formats."""
from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean as _mean
from statistics import median
from statistics import stdev as _stdev
from typing import Any

from eval.trace import RunMetrics

# Below this many (paired) samples, A/B deltas are treated as low-confidence: a
# % delta at n=3 is mostly noise. Used to gate "insufficient data" warnings.
MIN_RELIABLE_N = 5

# Bootstrap settings for the paired-delta confidence interval. The seed keeps
# report output deterministic across runs (same inputs -> same CI).
_BOOTSTRAP_ITERATIONS = 2000
_BOOTSTRAP_SEED = 12345
_CI_CONFIDENCE = 0.95


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
    # True/False when a CI is available (excludes/includes 0); None otherwise.
    significant: bool | None = None


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
    warnings: list[str] = field(default_factory=list)


_METRIC_DEFS = [
    ("Duration (s)", "duration"),
    ("Turn count", "turn_count"),
    ("Total spans", "total_spans"),
    ("Tool calls", "tool_count"),
    ("Input tokens", "total_input_tokens"),
    ("Output tokens", "total_output_tokens"),
    ("Cache tokens", "total_cache_tokens"),
    ("Tool duration (s)", "tool_duration"),
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


def _aggregate_values(vals_by_variant: dict[str, dict[str, float]], variants: list[str],
                      method: str) -> tuple[dict[str, float], str]:
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


def _bootstrap_ci(deltas: list[float],
                  confidence: float = _CI_CONFIDENCE,
                  iterations: int = _BOOTSTRAP_ITERATIONS,
                  seed: int = _BOOTSTRAP_SEED) -> tuple[float, float] | None:
    """Bootstrap CI for the median of paired deltas.

    Returns None when there are fewer than two deltas (a CI would be
    meaningless). Uses a fixed seed so identical inputs yield an identical
    interval, keeping report output reproducible.
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
    return medians[lo_idx], medians[hi_idx]


def _ci_significant(ci: tuple[float, float] | None) -> bool | None:
    """A paired delta is statistically supported when its CI excludes 0."""
    if ci is None:
        return None
    lo, hi = ci
    return lo > 0 or hi < 0


def _build_summary_row(metric: str, vals_by_variant: dict[str, dict[str, float]],
                       variants: list[str], aggregate: str) -> SummaryRow:
    """Aggregate one metric and attach n, dispersion, and (paired) CI."""
    agg, delta = _aggregate_values(vals_by_variant, variants, aggregate)
    row = SummaryRow(metric=metric, values=agg, delta=delta)
    for v in variants:
        vals = list(vals_by_variant.get(v, {}).values())
        row.n[v] = len(vals)
        row.stddev[v] = _stddev(vals)
        row.vmin[v], row.vmax[v] = _min_max(vals)

    if aggregate == "paired" and len(variants) == 2:
        v0, v1 = variants
        deltas = _paired_deltas(vals_by_variant.get(v0, {}), vals_by_variant.get(v1, {}))
        row.paired_n = len(deltas)
        ci = _bootstrap_ci(deltas)
        if ci is not None:
            row.ci_low, row.ci_high = ci
        # Only claim statistical support with enough paired samples. At tiny n a
        # bootstrap of the median can produce a degenerate CI that excludes 0,
        # which would re-create the "looks decisive at n=3" failure mode.
        row.significant = _ci_significant(ci) if row.paired_n >= MIN_RELIABLE_N else None
    return row


# --- Report building ---

def build_report(results: list[RunMetrics], results_dir: Path | None = None,
                 variant_order: list[str] | None = None,
                 aggregate: str = "paired",
                 manifest_runs: list[dict[str, Any]] | None = None,
                 trace_test_ids: set[str] | None = None) -> list[Report]:
    """Build per-task A/B comparison reports.

    ``manifest_runs`` (the persisted full set of attempted runs) and
    ``trace_test_ids`` (test ids that produced an ingested trace) drive the
    reliability table and the trace-missing rate. Both are optional so reports
    for older runs without a manifest still render — reliability simply degrades
    to per-variant trace counts.
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
        task_runs = by_task[task_name]
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
        for label, key in _METRIC_DEFS:
            vals_by_v = {v: {r.epoch: float(getattr(r, key)) for r in by_variant[v]} for v in variants}
            summary.append(_build_summary_row(label, vals_by_v, variants, aggregate))

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
        judge_rows: list[SummaryRow] = []
        judge_runtime: dict[str, Any] = {}
        if results_dir:
            raw, reasons, names, stddevs = _load_judge_raw(results_dir, variants, task_name)
            epoch_judges = raw
            epoch_reasons = reasons
            epoch_stddevs = stddevs
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

        # Sample sizes: per-variant trace count and shared paired-epoch count.
        variant_n = {v: len(by_variant[v]) for v in variants}
        paired_n = 0
        if aggregate == "paired" and len(variants) == 2:
            e0 = {r.epoch for r in by_variant[variants[0]]} - {"?"}
            e1 = {r.epoch for r in by_variant[variants[1]]} - {"?"}
            paired_n = len(e0 & e1)

        reliability = _build_reliability(
            task_name, variants, by_variant, manifest_runs, trace_test_ids,
            epoch_judges if judge_names else None,
        )
        warnings = _build_warnings(variants, variant_n, paired_n, aggregate)

        reports.append(Report(
            task=task_name, runs=task_runs, variants=variants,
            summary=summary, tool_patterns=tool_patterns,
            judge_scores=judge_rows, epoch_judges=epoch_judges,
            epoch_reasons=epoch_reasons, epoch_stddevs=epoch_stddevs,
            judge_names=judge_names, judge_runtime=judge_runtime, aggregate=aggregate,
            variant_n=variant_n, paired_n=paired_n,
            reliability=reliability, warnings=warnings,
        ))

    return reports


def _build_warnings(variants: list[str], variant_n: dict[str, int],
                    paired_n: int, aggregate: str) -> list[str]:
    """Flag insufficient-data conditions so small deltas aren't over-read."""
    warnings: list[str] = []
    low_variants = [v for v in variants if variant_n.get(v, 0) < MIN_RELIABLE_N]
    if low_variants:
        detail = ", ".join(f"{v}={variant_n.get(v, 0)}" for v in low_variants)
        warnings.append(
            f"Small sample size (n<{MIN_RELIABLE_N}): {detail}. "
            "Treat deltas as observed, not statistically supported."
        )
    if aggregate == "paired" and len(variants) == 2 and 0 < paired_n < MIN_RELIABLE_N:
        warnings.append(
            f"Only {paired_n} paired epoch(s) (<{MIN_RELIABLE_N}); paired deltas "
            "and confidence intervals are low-confidence."
        )
    if aggregate == "paired" and len(variants) == 2 and paired_n == 0:
        warnings.append("No shared epochs between variants; paired delta unavailable.")
    return warnings


def _build_reliability(task: str, variants: list[str],
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
        rows.append(ReliabilityRow(
            "Success/failure rates",
            {v: "n/a (no manifest)" for v in variants},
        ))
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
        status = r.get("status", "completed")
        has_trace = r.get("test_id") in tids
        if status == "timeout":
            c["timeout"] += 1
        elif status in ("failed", "setup_failed"):
            c["failed"] += 1
        elif not has_trace:
            c["missing"] += 1
        else:
            c["success"] += 1

    rows = [
        ReliabilityRow("Runs (attempted)", {v: str(counts[v]["total"]) for v in variants}),
        ReliabilityRow("Success rate", {v: pct(counts[v]["success"], counts[v]["total"]) for v in variants}),
        ReliabilityRow("Timeout rate", {v: pct(counts[v]["timeout"], counts[v]["total"]) for v in variants}),
        ReliabilityRow("Failed rate", {v: pct(counts[v]["failed"], counts[v]["total"]) for v in variants}),
        ReliabilityRow("Missing-trace rate", {v: pct(counts[v]["missing"], counts[v]["total"]) for v in variants}),
    ]
    if epoch_judges is not None:
        rows.append(ReliabilityRow(
            "Judge-score coverage",
            {v: _judge_coverage(v, epoch_judges, counts[v]["success"]) for v in variants},
        ))
    return rows


def _judge_coverage(variant: str, epoch_judges: dict[tuple[str, str], dict[str, int]],
                    success_n: int) -> str:
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
    if row.n.get(v, 0) >= 2 and sd > 0:
        return f"{val:.1f} \u00b1{sd:.1f}"
    return f"{val:.1f}"


def _fmt_delta(row: SummaryRow) -> str:
    """Delta (percentage) annotated with the absolute-unit paired CI + marker."""
    if not row.delta:
        return ""
    out = row.delta
    if row.ci_low is not None and row.ci_high is not None:
        # CI is in absolute metric units, not percentage points — labelled to
        # avoid being misread against the % delta it sits beside.
        out += f" [CI {row.ci_low:+.1f},{row.ci_high:+.1f} abs]"
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
        for row in (*report.summary, *report.judge_scores)
    )


def format_table(reports: list[Report]) -> str:
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
        for w in report.warnings:
            lines.append(f"  ! {w}")

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
            f"{'Tools':>5} {'In Tok':>8} {'Out Tok':>8} {'Cache':>8} {'TDur(s)':>7}{jhdr}"
        )
        lines.append("-" * (80 + 9 * len(jnames)))
        for r in report.runs:
            jvals = ""
            for n in jnames:
                s = report.epoch_judges.get((r.variant, r.epoch), {}).get(n)
                jvals += f" {s:>8}" if s is not None else f" {'—':>8}"
            lines.append(
                f"{r.variant:<18} {r.epoch:>5} {r.duration:>7.1f} {r.turn_count:>5} "
                f"{r.total_spans:>5} {r.tool_count:>5} "
                f"{r.total_input_tokens:>8} {r.total_output_tokens:>8} "
                f"{r.total_cache_tokens:>8} {r.tool_duration:>7.1f}{jvals}"
            )

        # Summary
        hdr = "".join(f"{v:>22}" for v in report.variants)
        lines.append(f"\nMetrics ({report.aggregate})")
        lines.append(f"{'Metric':<24} {hdr} {'Delta':>26}")
        lines.append("-" * (24 + 22 * len(report.variants) + 26))
        for row in report.summary:
            cols = "".join(f"{_fmt_value(row, v):>22}" for v in report.variants)
            lines.append(f"{row.metric:<24} {cols} {_fmt_delta(row):>26}")
        if report.judge_scores:
            lines.append(f"\n{'Judge':<24} {hdr} {'Delta':>26}")
            lines.append("-" * (24 + 22 * len(report.variants) + 26))
            for row in report.judge_scores:
                cols = "".join(f"{_fmt_value(row, v):>22}" for v in report.variants)
                lines.append(f"{row.metric:<24} {cols} {_fmt_delta(row):>26}")

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
        body += ("\n\nLegend: value \u00b1stddev; Delta is a percentage, [CI low,high abs] is a "
                 "bootstrap interval of the paired delta in absolute metric units; "
                 "* = CI excludes 0 (statistically supported), ns = not supported "
                 f"(observed only), low-n = fewer than {MIN_RELIABLE_N} paired samples "
                 "(significance not assessed). No multiple-comparison correction applied.")
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
                "reliability": [{"metric": rr.metric, "values": rr.values} for rr in report.reliability],
                "runs": [
                    {
                        "variant": r.variant, "epoch": r.epoch, "duration": r.duration,
                        "turn_count": r.turn_count, "total_spans": r.total_spans,
                        "tool_count": r.tool_count, "input_tokens": r.total_input_tokens,
                        "output_tokens": r.total_output_tokens, "cache_tokens": r.total_cache_tokens,
                        "tool_duration": r.tool_duration, "tool_names": r.tool_names, "model": r.model,
                        "judges": report.epoch_judges.get((r.variant, r.epoch), {}),
                        "judge_stddevs": report.epoch_stddevs.get((r.variant, r.epoch), {}),
                    }
                    for r in report.runs
                ],
                "summary": [_summary_row_json(r, "metric") for r in report.summary],
                "tool_patterns": report.tool_patterns,
                "judge_scores": [_summary_row_json(r, "judge") for r in report.judge_scores],
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
        for w in report.warnings:
            lines.append(f"\n> ⚠️ {w}")

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
        lines.append(f"| Variant | Epoch | Dur(s) | Turns | Spans | Tools | In Tok | Out Tok | Cache | TDur(s) |{jhdr}")
        lines.append(f"|---------|------:|-------:|------:|------:|------:|-------:|--------:|------:|--------:|{jsep}")
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
            lines.append(f"| {r.variant} | {r.epoch} | {r.duration:.1f} | {r.turn_count} | "
                         f"{r.total_spans} | {r.tool_count} | {r.total_input_tokens} | "
                         f"{r.total_output_tokens} | {r.total_cache_tokens} | {r.tool_duration:.1f} |{jvals}")

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
        body += ("\n\n---\n\n_Legend: value ±stddev; Delta is a percentage, `[CI low,high abs]` "
                 "is a bootstrap interval of the paired delta in **absolute metric units**; "
                 "`*` = CI excludes 0 (statistically supported), `ns` = observed only "
                 f"(not supported), `low-n` = fewer than {MIN_RELIABLE_N} paired samples "
                 "(significance not assessed). No multiple-comparison correction applied._")
    return body


# --- Judge score loading ---

def _load_judge_raw(results_dir: Path, variants: list[str], task: str
                    ) -> tuple[dict[tuple[str, str], dict[str, int]], dict[tuple[str, str], dict[str, str]],
                               list[str], dict[tuple[str, str], dict[str, float]]]:
    """Load per-epoch judge scores, reasons, and spread.

    Returns (epoch_data, epoch_reasons, evaluator_names, epoch_stddevs).
    """
    epoch_data: dict[tuple[str, str], dict[str, int]] = {}
    epoch_reasons: dict[tuple[str, str], dict[str, str]] = {}
    epoch_stddevs: dict[tuple[str, str], dict[str, float]] = {}
    all_names: set[str] = set()

    if not results_dir or not results_dir.exists():
        return {}, {}, [], {}

    for pattern in ["*.scores.json", "*.judges.json"]:
        for jf in results_dir.glob(pattern):
            stem = jf.stem.replace(".scores", "").replace(".judges", "")
            if not stem.startswith(f"{task}_"):
                continue
            parts = stem.rsplit("_epoch", 1)
            if len(parts) < 2:
                continue
            name_variant = parts[0]
            epoch_str = parts[1]
            # Match the longest variant name to avoid a shorter name (e.g. "v")
            # incorrectly claiming a file that belongs to "my_v".
            matches = [v for v in variants if name_variant.endswith(f"_{v}")]
            variant = max(matches, key=len) if matches else None
            if not variant:
                continue
            try:
                scores = {}
                reasons = {}
                stddevs = {}
                for s in json.loads(jf.read_text()):
                    if s.get("score") is not None:
                        scores[s["name"]] = int(s["score"])
                        reasons[s["name"]] = str(s.get("reason", ""))
                        if s.get("score_stddev") is not None:
                            stddevs[s["name"]] = float(s["score_stddev"])
                        all_names.add(s["name"])
                epoch_data[(variant, epoch_str)] = scores
                epoch_reasons[(variant, epoch_str)] = reasons
                epoch_stddevs[(variant, epoch_str)] = stddevs
            except (json.JSONDecodeError, KeyError):
                continue

    return epoch_data, epoch_reasons, sorted(all_names), epoch_stddevs


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
