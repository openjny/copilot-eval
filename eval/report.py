"""A/B comparison report generation with multiple output formats."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median, mean as _mean

from eval.trace import RunMetrics


@dataclass
class SummaryRow:
    metric: str
    values: dict[str, float]
    delta: str = ""


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
    judge_names: list[str] = field(default_factory=list)
    aggregate: str = "paired"


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


# --- Report building ---

def build_report(results: list[RunMetrics], results_dir: Path | None = None,
                 variant_order: list[str] | None = None,
                 aggregate: str = "paired") -> list[Report]:
    """Build per-task A/B comparison reports."""
    if not results:
        return []

    by_task: dict[str, list[RunMetrics]] = defaultdict(list)
    for r in results:
        by_task[r.scenario].append(r)

    reports: list[Report] = []
    for task_name in sorted(by_task.keys()):
        task_runs = by_task[task_name]
        task_runs.sort(key=lambda r: (r.variant, _epoch_sort_key(r.epoch)))

        by_variant: dict[str, list[RunMetrics]] = defaultdict(list)
        for r in task_runs:
            by_variant[r.variant].append(r)

        if variant_order:
            variants = [v for v in variant_order if v in by_variant]
        else:
            variants = list(by_variant.keys())

        # OTel metrics summary
        summary = []
        for label, key in _METRIC_DEFS:
            vals_by_v = {v: {r.epoch: float(getattr(r, key)) for r in by_variant[v]} for v in variants}
            agg, delta = _aggregate_values(vals_by_v, variants, aggregate)
            summary.append(SummaryRow(metric=label, values=agg, delta=delta))

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
        judge_rows: list[SummaryRow] = []
        if results_dir:
            raw, reasons, names = _load_judge_raw(results_dir, variants, task_name)
            epoch_judges = raw
            epoch_reasons = reasons
            judge_names = names
            # Aggregate judge scores
            for name in names:
                vals_by_v = {}
                for v in variants:
                    vals_by_v[v] = {}
                    for (rv, ep), scores in raw.items():
                        if rv == v and name in scores:
                            vals_by_v[v][ep] = float(scores[name])
                agg, delta = _aggregate_values(vals_by_v, variants, aggregate)
                judge_rows.append(SummaryRow(metric=name, values=agg, delta=delta))

        reports.append(Report(
            task=task_name, runs=task_runs, variants=variants,
            summary=summary, tool_patterns=tool_patterns,
            judge_scores=judge_rows, epoch_judges=epoch_judges,
            epoch_reasons=epoch_reasons,
            judge_names=judge_names, aggregate=aggregate,
        ))

    return reports


# --- Format functions ---

def format_table(reports: list[Report]) -> str:
    sections: list[str] = []
    for report in reports:
        lines: list[str] = []
        lines.append(f"\n{'=' * 80}")
        lines.append(f"TASK: {report.task}")
        lines.append("=" * 80)

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
        hdr = "".join(f"{v:>18}" for v in report.variants)
        lines.append(f"\nMetrics ({report.aggregate})")
        lines.append(f"{'Metric':<30} {hdr} {'Delta':>12}")
        lines.append("-" * (30 + 18 * len(report.variants) + 12))
        for row in report.summary:
            cols = "".join(f"{row.values.get(v, 0):>18.1f}" for v in report.variants)
            lines.append(f"{row.metric:<30} {cols} {row.delta:>12}")
        if report.judge_scores:
            lines.append(f"\n{'Judge':<30} {hdr} {'Delta':>12}")
            lines.append("-" * (30 + 18 * len(report.variants) + 12))
            for row in report.judge_scores:
                cols = "".join(f"{row.values.get(v, 0):>18.1f}" for v in report.variants)
                lines.append(f"{row.metric:<30} {cols} {row.delta:>12}")

        sections.append("\n".join(lines))
    return "\n".join(sections)


def format_json(reports: list[Report]) -> str:
    data = {
        "tasks": [
            {
                "task": report.task,
                "aggregate": report.aggregate,
                "variants": report.variants,
                "runs": [
                    {
                        "variant": r.variant, "epoch": r.epoch, "duration": r.duration,
                        "turn_count": r.turn_count, "total_spans": r.total_spans,
                        "tool_count": r.tool_count, "input_tokens": r.total_input_tokens,
                        "output_tokens": r.total_output_tokens, "cache_tokens": r.total_cache_tokens,
                        "tool_duration": r.tool_duration, "tool_names": r.tool_names, "model": r.model,
                        "judges": report.epoch_judges.get((r.variant, r.epoch), {}),
                    }
                    for r in report.runs
                ],
                "summary": [{"metric": r.metric, "values": r.values, "delta": r.delta} for r in report.summary],
                "tool_patterns": report.tool_patterns,
                "judge_scores": [{"judge": r.metric, "values": r.values, "delta": r.delta} for r in report.judge_scores],
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

        # Summary
        lines.append(f"### Metrics ({report.aggregate})\n")
        lines.append("| Metric |" + "".join(f" {v} |" for v in report.variants) + " Delta |")
        lines.append("|--------|" + "".join("--------:|" for _ in report.variants) + "------:|")
        for row in report.summary:
            cols = "".join(f" {row.values.get(v, 0):.1f} |" for v in report.variants)
            lines.append(f"| {row.metric} |{cols} {row.delta} |")

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
                cols = "".join(f" {row.values.get(v, 0):.1f} |" for v in report.variants)
                lines.append(f"| {row.metric} |{cols} {row.delta} |")

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
                jvals += f" {s} |" if s is not None else " — |"
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
    return "\n\n---\n\n".join(sections)


# --- Judge score loading ---

def _load_judge_raw(results_dir: Path, variants: list[str], task: str
                    ) -> tuple[dict[tuple[str, str], dict[str, int]], dict[tuple[str, str], dict[str, str]], list[str]]:
    """Load per-epoch judge scores and reasons. Returns (epoch_data, epoch_reasons, evaluator_names)."""
    epoch_data: dict[tuple[str, str], dict[str, int]] = {}
    epoch_reasons: dict[tuple[str, str], dict[str, str]] = {}
    all_names: set[str] = set()

    if not results_dir or not results_dir.exists():
        return {}, {}, []

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
                for s in json.loads(jf.read_text()):
                    if s.get("score") is not None:
                        scores[s["name"]] = int(s["score"])
                        reasons[s["name"]] = str(s.get("reason", ""))
                        all_names.add(s["name"])
                epoch_data[(variant, epoch_str)] = scores
                epoch_reasons[(variant, epoch_str)] = reasons
            except (json.JSONDecodeError, KeyError):
                continue

    return epoch_data, epoch_reasons, sorted(all_names)
