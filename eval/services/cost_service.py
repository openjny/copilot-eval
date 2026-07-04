"""Cost governance (issue #70): pre-flight cost estimation and budget caps.

Copilot doesn't expose a pricing API, so this module estimates cost with a
simple, deliberately conservative model:

    cost = cells × avg_tokens_per_cell × price_per_token
         + judge_calls × avg_judge_tokens × judge_price_per_token

``avg_tokens_per_cell`` comes from :func:`load_historical_costs` when past
runs exist (average of persisted OTel traces under ``results/*/.traces/``),
falling back to hardcoded defaults otherwise. Because per-prompt token counts
vary, treat the result as an order-of-magnitude estimate, not a bill --
consistent with the project's non-goal of *not* becoming a FinOps/billing
platform (see docs/vision.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.collectors.file_collector import TRACE_FILE, parse_file_traces
from eval.config import Config, RunnerConfig, Task, Variant
from eval.trace import extract_metrics

# --- Pricing constants ------------------------------------------------------
#
# Hardcoded, deliberately-rounded-up USD prices per 1K tokens. These are *not*
# pulled from a live pricing API (Copilot doesn't expose one) and exist only
# to give a conservative, order-of-magnitude pre-flight estimate and budget
# gate. Update these if actual observed costs diverge significantly.
DEFAULT_PRICE_PER_1K_INPUT_TOKENS = 0.01
DEFAULT_PRICE_PER_1K_OUTPUT_TOKENS = 0.03
DEFAULT_PRICE_PER_1K_JUDGE_INPUT_TOKENS = 0.01
DEFAULT_PRICE_PER_1K_JUDGE_OUTPUT_TOKENS = 0.03

# Fallback average tokens/cell when no historical run data exists under
# `results/`. Deliberately on the high side -- overestimating is safer than
# underestimating for a budget gate.
DEFAULT_AVG_INPUT_TOKENS_PER_CELL = 8_000
DEFAULT_AVG_OUTPUT_TOKENS_PER_CELL = 2_000

# Fallback average tokens per judge call (judge prompts are typically shorter
# than agent runs: rubric + truncated conversation/output evidence).
DEFAULT_AVG_JUDGE_INPUT_TOKENS = 3_000
DEFAULT_AVG_JUDGE_OUTPUT_TOKENS = 200

# How many of the most-recently-modified run directories to scan for
# historical token averages. Keeps the scan fast on long-lived projects with
# many past runs.
_MAX_HISTORICAL_RUNS = 5


@dataclass
class HistoricalCosts:
    """Average token usage derived from previously persisted trace files."""

    avg_input_tokens_per_cell: float
    avg_output_tokens_per_cell: float
    sample_size: int  # number of historical cells the average is based on


@dataclass
class CostEstimate:
    """Pre-flight cost estimate for a `run` invocation."""

    cells: int
    judge_calls: int
    est_input_tokens: int
    est_output_tokens: int
    est_judge_input_tokens: int
    est_judge_output_tokens: int
    cost_agent: float
    cost_judge: float
    cost_total: float
    based_on_history: bool
    history_sample_size: int

    def over_budget(self, budget_limit: float | None) -> bool:
        return budget_limit is not None and self.cost_total > budget_limit

    def to_dict(self) -> dict[str, Any]:
        return {
            "cells": self.cells,
            "judge_calls": self.judge_calls,
            "est_input_tokens": self.est_input_tokens,
            "est_output_tokens": self.est_output_tokens,
            "est_judge_input_tokens": self.est_judge_input_tokens,
            "est_judge_output_tokens": self.est_judge_output_tokens,
            "cost_agent": self.cost_agent,
            "cost_judge": self.cost_judge,
            "cost_total": self.cost_total,
            "based_on_history": self.based_on_history,
            "history_sample_size": self.history_sample_size,
        }


def load_historical_costs(
    results_dir: Path, max_runs: int = _MAX_HISTORICAL_RUNS
) -> HistoricalCosts | None:
    """Average input/output tokens per cell from the most recent past runs.

    Scans up to ``max_runs`` of the most-recently-modified directories under
    ``results_dir`` for persisted file-collector traces
    (``<run_dir>/.traces/*.jsonl``, see ``eval.collectors.file_collector`` and
    ``eval.runner._persist_trace_file``) -- the zero-dependency default trace
    path, so this works without Jaeger. Returns None when no historical trace
    data is found (first run, or all runs used the jaeger collector without
    also persisting file traces).
    """
    if not results_dir.exists():
        return None

    run_dirs = sorted(
        (d for d in results_dir.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )[:max_runs]

    total_input = 0
    total_output = 0
    count = 0
    for run_dir in run_dirs:
        traces_dir = run_dir / TRACE_FILE.parent
        if not traces_dir.is_dir():
            continue
        for trace_path in traces_dir.glob("*.jsonl"):
            for trace in parse_file_traces(trace_path):
                metrics = extract_metrics(trace)
                if metrics is None:
                    continue
                total_input += metrics.total_input_tokens
                total_output += metrics.total_output_tokens
                count += 1

    if count == 0:
        return None
    return HistoricalCosts(
        avg_input_tokens_per_cell=total_input / count,
        avg_output_tokens_per_cell=total_output / count,
        sample_size=count,
    )


def judge_calls_per_cell(task: Task, runner: RunnerConfig) -> int:
    """Number of judge LLM calls one cell of ``task`` makes.

    Mirrors the call-count logic in ``eval.judge_executor.JudgeExecutor``:
    one call per judge evaluator per sample, unless ``judge_batch`` is on and
    there is more than one judge evaluator, in which case all of them are
    scored in a single call per sample.
    """
    judge_evaluators = [e for e in task.evaluators if e.type == "judge"]
    if not judge_evaluators:
        return 0
    samples = max(1, runner.judge_samples)
    if runner.judge_batch and len(judge_evaluators) > 1:
        return samples
    return len(judge_evaluators) * samples


def estimate_run_cost(
    config: Config,
    tasks: list[Task],
    variants: list[Variant],
    epochs: int,
) -> CostEstimate:
    """Estimate the total cost of running ``tasks`` × ``variants`` × ``epochs``.

    Cells are counted per (task, fixture, variant, epoch) -- matching the
    matrix ``eval.services.orchestrator._execute_schedule`` actually
    schedules. Judge calls are counted per cell via
    :func:`judge_calls_per_cell`. Token/cell averages come from
    :func:`load_historical_costs` when available, else hardcoded defaults.
    """
    n_variants = len(variants) or 1
    cells = sum(len(t.fixture_names()) for t in tasks) * epochs * n_variants
    judge_calls = sum(
        judge_calls_per_cell(t, config.runner) * len(t.fixture_names()) * epochs * n_variants
        for t in tasks
    )

    history = load_historical_costs(config.results_dir)
    if history is not None:
        avg_input = history.avg_input_tokens_per_cell
        avg_output = history.avg_output_tokens_per_cell
        based_on_history = True
        history_sample_size = history.sample_size
    else:
        avg_input = DEFAULT_AVG_INPUT_TOKENS_PER_CELL
        avg_output = DEFAULT_AVG_OUTPUT_TOKENS_PER_CELL
        based_on_history = False
        history_sample_size = 0

    est_input_tokens = round(cells * avg_input)
    est_output_tokens = round(cells * avg_output)
    est_judge_input_tokens = round(judge_calls * DEFAULT_AVG_JUDGE_INPUT_TOKENS)
    est_judge_output_tokens = round(judge_calls * DEFAULT_AVG_JUDGE_OUTPUT_TOKENS)

    cost_agent = (
        est_input_tokens / 1000 * DEFAULT_PRICE_PER_1K_INPUT_TOKENS
        + est_output_tokens / 1000 * DEFAULT_PRICE_PER_1K_OUTPUT_TOKENS
    )
    cost_judge = (
        est_judge_input_tokens / 1000 * DEFAULT_PRICE_PER_1K_JUDGE_INPUT_TOKENS
        + est_judge_output_tokens / 1000 * DEFAULT_PRICE_PER_1K_JUDGE_OUTPUT_TOKENS
    )

    return CostEstimate(
        cells=cells,
        judge_calls=judge_calls,
        est_input_tokens=est_input_tokens,
        est_output_tokens=est_output_tokens,
        est_judge_input_tokens=est_judge_input_tokens,
        est_judge_output_tokens=est_judge_output_tokens,
        cost_agent=round(cost_agent, 4),
        cost_judge=round(cost_judge, 4),
        cost_total=round(cost_agent + cost_judge, 4),
        based_on_history=based_on_history,
        history_sample_size=history_sample_size,
    )


def format_cost_report(estimate: CostEstimate, budget_limit: float | None = None) -> str:
    """Render a human-readable cost breakdown for the pre-flight banner."""
    basis = (
        f"historical average ({estimate.history_sample_size} past cell(s))"
        if estimate.based_on_history
        else "default estimate (no historical run data found)"
    )
    lines = [
        "=" * 50,
        " Cost Estimate (pre-flight)",
        "=" * 50,
        f" Cells:        {estimate.cells}",
        f" Judge calls:  {estimate.judge_calls}",
        f" Agent tokens: {estimate.est_input_tokens:,} in / {estimate.est_output_tokens:,} out",
        f" Judge tokens: {estimate.est_judge_input_tokens:,} in / "
        f"{estimate.est_judge_output_tokens:,} out",
        f" Basis:        {basis}",
        f" Agent cost:   ${estimate.cost_agent:.4f}",
        f" Judge cost:   ${estimate.cost_judge:.4f}",
        f" Total cost:   ${estimate.cost_total:.4f}",
    ]
    if budget_limit is not None:
        status = "OVER BUDGET" if estimate.over_budget(budget_limit) else "within budget"
        lines.append(f" Budget limit: ${budget_limit:.4f} ({status})")
    lines.append("=" * 50)
    return "\n".join(lines)
