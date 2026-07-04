"""Metric evaluator scoring: deterministic (LLM-free) `type: metric` gates
computed from parsed OTel telemetry, merged into each run's `*.scores.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from eval.config import Config
from eval.evaluators import MetricEvaluator
from eval.naming import run_slug
from eval.protocols import EvalContext, EvalScore, score_to_dict
from eval.trace import Trace, extract_metrics


def _run_metric_evaluators(config: Config, traces: list[Trace], results_dir: Path) -> list[str]:
    """Score type=metric evaluators from parsed telemetry.

    Deterministic and LLM-free: for each trace, thresholds the requested
    ``RunMetrics`` fields and merges the 1/0 pass/fail scores into the run's
    ``*.scores.json`` file (alongside judge/contains/regex scores). Recomputed on
    every ``analyze`` since it's cheap and telemetry-driven.

    Returns a list of human-readable labels for every metric gate that did **not**
    pass — including gates whose value was unavailable (``score is None``), which
    count as failures — so ``analyze`` can exit non-zero for CI gating.
    """
    tasks_by_name = {t.name: t for t in config.tasks}
    seen: set[Path] = set()
    failed_gates: list[str] = []

    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        fixture = trace.resource_tags.get("eval.fixture", "")
        task = tasks_by_name.get(scenario)
        if not task:
            continue

        metric_evaluators = [ev for ev in task.evaluators if ev.type == "metric"]
        if not metric_evaluators:
            continue

        scores_file = results_dir / f"{run_slug(scenario, variant, epoch, fixture)}.scores.json"
        if scores_file in seen:
            continue  # one trace per scores file is enough
        seen.add(scores_file)

        fx = f"/{fixture}" if fixture else ""

        run_metrics = extract_metrics(trace)
        if run_metrics is None:
            # Fail CLOSED: a metric-gated task whose trace can't yield metrics must
            # not silently pass. Emit an unavailable (score=None, passed=False)
            # score for each metric evaluator so the gate below counts as failed.
            new_scores = [
                EvalScore(
                    name=ev.name,
                    type="metric",
                    score=None,
                    reason="metrics unavailable in trace",
                    passed=False,
                )
                for ev in metric_evaluators
            ]
        else:
            new_scores = []
            for ev in metric_evaluators:
                score = MetricEvaluator.from_config(ev).evaluate(
                    EvalContext(evaluator=ev, config=config, metrics=run_metrics)
                )
                # MetricEvaluator only returns None when metrics is None, which
                # is excluded by the branch above (run_metrics is not None here).
                assert score is not None
                new_scores.append(score)

        _merge_scores_file(scores_file, new_scores, replace_type="metric")
        for s in new_scores:
            status = "PASS" if s.passed else ("n/a" if s.score is None else "FAIL")
            click.echo(
                f"    [{scenario}{fx}/{variant}/e{epoch}] {s.name} (metric): {status} — {s.reason}",
                err=True,
            )
            # A gate fails when it does not pass — this deliberately includes the
            # unavailable-value case (score is None), which must NOT silently pass.
            if not s.passed:
                reason = s.reason.split(" → ", 1)[0]  # drop the trailing "→ FAIL"
                failed_gates.append(f"{s.name} [{scenario}{fx}/{variant}/e{epoch}]: {reason}")

    return failed_gates


def _merge_scores_file(scores_file: Path, new_scores: list[Any], replace_type: str) -> None:
    """Merge freshly computed scores into a run's scores file.

    Keeps existing scores except those of ``replace_type`` whose name is being
    recomputed, so re-running ``analyze`` refreshes metric scores idempotently
    without clobbering judge/contains/regex scores.
    """
    existing: list[dict[str, Any]] = []
    if scores_file.exists():
        try:
            existing = json.loads(scores_file.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []

    new_dicts = [score_to_dict(s) for s in new_scores]
    new_names = {d["name"] for d in new_dicts}
    kept = [
        s for s in existing if not (s.get("type") == replace_type and s.get("name") in new_names)
    ]
    all_scores = kept + new_dicts
    scores_file.parent.mkdir(parents=True, exist_ok=True)
    scores_file.write_text(json.dumps(all_scores, indent=2, ensure_ascii=False))
