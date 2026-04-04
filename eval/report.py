"""A/B comparison report generation with multiple output formats."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from eval.trace import RunMetrics


@dataclass
class SummaryRow:
    metric: str
    values: dict[str, float]
    delta: str = ""


@dataclass
class Report:
    runs: list[RunMetrics]
    variants: list[str]
    summary: list[SummaryRow]
    tool_patterns: dict[str, dict[str, int]]
    judge_scores: list[SummaryRow] = field(default_factory=list)


def build_report(results: list[RunMetrics], results_dir: Path | None = None) -> Report | None:
    if not results:
        return None

    results.sort(key=lambda r: (r.variant, r.epoch))

    by_variant: dict[str, list[RunMetrics]] = defaultdict(list)
    for r in results:
        by_variant[r.variant].append(r)
    variants = sorted(by_variant.keys())

    metric_defs = [
        ("Duration (s)", "duration"),
        ("Turn count", "turn_count"),
        ("Total spans", "total_spans"),
        ("Tool calls", "tool_count"),
        ("Input tokens", "total_input_tokens"),
        ("Output tokens", "total_output_tokens"),
        ("Cache tokens", "total_cache_tokens"),
        ("Tool duration (s)", "tool_duration"),
    ]

    summary = []
    for label, key in metric_defs:
        means = {}
        for v in variants:
            vals = [float(getattr(r, key)) for r in by_variant[v]]
            means[v] = sum(vals) / len(vals) if vals else 0
        summary.append(SummaryRow(metric=label, values=means, delta=_calc_delta(means, variants)))

    tool_patterns: dict[str, dict[str, int]] = {}
    for v in variants:
        counts: dict[str, int] = defaultdict(int)
        for r in by_variant[v]:
            for t in r.tool_names:
                counts[t] += 1
        tool_patterns[v] = dict(counts)

    judge_rows = _load_judge_scores(results_dir, variants) if results_dir else []

    return Report(runs=results, variants=variants, summary=summary,
                  tool_patterns=tool_patterns, judge_scores=judge_rows)


def format_table(report: Report) -> str:
    lines: list[str] = []
    lines.append(
        f"{'Variant':<18} {'Epoch':>5} {'Spans':>5} {'Turns':>5} {'Dur(s)':>7} "
        f"{'Tools':>5} {'In Tok':>8} {'Out Tok':>8} {'Cache':>8} Tool Names"
    )
    lines.append("-" * 110)
    for r in report.runs:
        lines.append(
            f"{r.variant:<18} {r.epoch:>5} {r.total_spans:>5} {r.turn_count:>5} "
            f"{r.duration:>7.1f} {r.tool_count:>5} "
            f"{r.total_input_tokens:>8} {r.total_output_tokens:>8} {r.total_cache_tokens:>8} "
            f"{r.tool_names}"
        )
    hdr = "".join(f"{v:>18}" for v in report.variants)
    lines.append("\n" + "=" * 80)
    lines.append("SUMMARY (mean across epochs)")
    lines.append("=" * 80)
    lines.append(f"\n{'Metric':<30} {hdr} {'Delta':>12}")
    lines.append("-" * (30 + 18 * len(report.variants) + 12))
    for row in report.summary:
        cols = "".join(f"{row.values.get(v, 0):>18.1f}" for v in report.variants)
        lines.append(f"{row.metric:<30} {cols} {row.delta:>12}")
    lines.append("\nTool usage patterns:")
    for v in report.variants:
        lines.append(f"  {v}: {report.tool_patterns.get(v, {})}")
    if report.judge_scores:
        lines.append("\n" + "=" * 80)
        lines.append("JUDGE SCORES (LLM-as-Judge)")
        lines.append("=" * 80)
        lines.append(f"\n{'Judge':<30} {hdr} {'Delta':>12}")
        lines.append("-" * (30 + 18 * len(report.variants) + 12))
        for row in report.judge_scores:
            cols = "".join(f"{row.values.get(v, 0):>18.1f}" for v in report.variants)
            lines.append(f"{row.metric:<30} {cols} {row.delta:>12}")
    return "\n".join(lines)


def format_json(report: Report) -> str:
    data = {
        "variants": report.variants,
        "runs": [
            {
                "variant": r.variant, "epoch": r.epoch, "duration": r.duration,
                "turn_count": r.turn_count, "total_spans": r.total_spans,
                "tool_count": r.tool_count, "input_tokens": r.total_input_tokens,
                "output_tokens": r.total_output_tokens, "cache_tokens": r.total_cache_tokens,
                "tool_duration": r.tool_duration, "tool_names": r.tool_names, "model": r.model,
            }
            for r in report.runs
        ],
        "summary": [{"metric": r.metric, "values": r.values, "delta": r.delta} for r in report.summary],
        "tool_patterns": report.tool_patterns,
        "judge_scores": [{"judge": r.metric, "values": r.values, "delta": r.delta} for r in report.judge_scores],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_markdown(report: Report) -> str:
    lines: list[str] = []
    lines.append("## Summary\n")
    lines.append("| Metric |" + "".join(f" {v} |" for v in report.variants) + " Delta |")
    lines.append("|--------|" + "".join("--------:|" for _ in report.variants) + "------:|")
    for row in report.summary:
        cols = "".join(f" {row.values.get(v, 0):.1f} |" for v in report.variants)
        lines.append(f"| {row.metric} |{cols} {row.delta} |")
    lines.append("\n## Tool Usage\n")
    for v in report.variants:
        lines.append(f"- **{v}**: {report.tool_patterns.get(v, {})}")
    if report.judge_scores:
        lines.append("\n## Judge Scores\n")
        lines.append("| Judge |" + "".join(f" {v} |" for v in report.variants) + " Delta |")
        lines.append("|-------|" + "".join("--------:|" for _ in report.variants) + "------:|")
        for row in report.judge_scores:
            cols = "".join(f" {row.values.get(v, 0):.1f} |" for v in report.variants)
            lines.append(f"| {row.metric} |{cols} {row.delta} |")
    lines.append("\n## Per-Run Details\n")
    lines.append("| Variant | Epoch | Turns | Duration | Tools | In Tok | Out Tok |")
    lines.append("|---------|------:|------:|---------:|------:|-------:|--------:|")
    for r in report.runs:
        lines.append(f"| {r.variant} | {r.epoch} | {r.turn_count} | {r.duration:.1f}s | "
                     f"{r.tool_count} | {r.total_input_tokens} | {r.total_output_tokens} |")
    return "\n".join(lines)


def _calc_delta(means: dict[str, float], variants: list[str]) -> str:
    if len(variants) != 2:
        return ""
    m0, m1 = means.get(variants[0], 0), means.get(variants[1], 0)
    return f"{((m1 - m0) / m0) * 100:+.1f}%" if m0 > 0 else ""


def _load_judge_scores(results_dir: Path, variants: list[str]) -> list[SummaryRow]:
    judge_data: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    if not results_dir or not results_dir.exists():
        return []
    for jf in results_dir.glob("*.judges.json"):
        stem = jf.stem.replace(".judges", "")
        parts = stem.rsplit("_epoch", 1)
        if len(parts) < 2:
            continue
        name_variant = parts[0]
        variant = next((v for v in variants if name_variant.endswith(f"_{v}")), None)
        if not variant:
            continue
        try:
            for s in json.loads(jf.read_text()):
                judge_data[variant][s["name"]].append(s["score"])
        except (json.JSONDecodeError, KeyError):
            continue
    if not judge_data:
        return []
    all_names = set()
    for v_data in judge_data.values():
        all_names.update(v_data.keys())
    return [
        SummaryRow(
            metric=name,
            values={v: (sum(judge_data.get(v, {}).get(name, [])) / len(judge_data.get(v, {}).get(name, [1]))) for v in variants},
            delta=_calc_delta(
                {v: (sum(judge_data.get(v, {}).get(name, [])) / len(judge_data.get(v, {}).get(name, [1]))) for v in variants},
                variants,
            ),
        )
        for name in sorted(all_names)
    ]
