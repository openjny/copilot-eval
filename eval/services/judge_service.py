"""Judge evaluation service: runs `type: judge` evaluators against traces from
a previous run, plus reporting on judge reliability/reproducibility.

Judge invocation itself (prompt construction, Copilot CLI calls, JSON parsing,
self-consistency sampling) lives in :mod:`eval.judge_executor` (issue #80);
this module is the orchestration layer that decides *which* judges need to run
for a batch of traces, dispatches them in parallel, and persists/report their
scores.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import click

from eval.config import Config
from eval.evaluators import JudgeEvaluator
from eval.naming import run_slug
from eval.progress import NullProgress, ProgressReporter
from eval.protocols import EvalContext
from eval.runner import get_github_token, read_files_from_dir, run_judges_batch, score_to_dict
from eval.trace import Trace, extract_conversation


def _is_truncated(text: str | None) -> bool:
    """Whether judge context text was cut to its char budget by the readers."""
    return bool(text and text.rstrip().endswith("(truncated)"))


def _run_judges(
    config: Config,
    traces: list[Trace],
    results_dir: Path,
    force: bool = False,
    reporter: ProgressReporter | None = None,
) -> None:
    """Run judge evaluators using OTel traces + output files.

    Skips judge evaluators that already have a recorded score (judge presence,
    not file existence — non-judge scores share the same file). Pass force=True
    to re-run every judge regardless of cached scores. ``reporter`` (default: a
    silent :class:`NullProgress`) receives per-call progress events.
    """
    reporter = reporter or NullProgress()
    github_token = get_github_token()
    tasks_by_name = {t.name: t for t in config.tasks}

    # Phase 1: build per-trace context and collect the judge work items. Each
    # trace owns a dedicated scores file; contexts are keyed by that file so
    # duplicate traces for the same scenario/variant/epoch coalesce into one
    # writer and don't clobber each other. In batch mode (runner.judge_batch)
    # a context's judges share one work item (one LLM call); otherwise each
    # judge is its own item so failures stay isolated.
    contexts: dict[str, dict[str, Any]] = {}
    work: list[tuple[str, list[Any]]] = []  # (context key, evaluators)

    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        fixture = trace.resource_tags.get("eval.fixture", "")
        task = tasks_by_name.get(scenario)
        if not task:
            continue

        judge_evaluators = [ev for ev in task.evaluators if ev.type == "judge" and ev.prompt]
        if not judge_evaluators:
            continue

        slug = run_slug(scenario, variant, epoch, fixture)
        scores_file = results_dir / f"{slug}.scores.json"
        key = str(scores_file)
        if key in contexts:
            continue  # already collected work for this scores file

        # Load existing scores and decide which judges still need scoring.
        existing_scores: list[dict[str, Any]] = []
        if scores_file.exists():
            try:
                existing_scores = json.loads(scores_file.read_text())
            except (json.JSONDecodeError, OSError):
                existing_scores = []
        existing_judge_names = {s.get("name") for s in existing_scores if s.get("type") == "judge"}
        pending = (
            judge_evaluators
            if force
            else [ev for ev in judge_evaluators if ev.name not in existing_judge_names]
        )
        if not pending:
            continue  # all judge scores already present

        # Extract conversation from OTel trace
        conv_limit = config.runner.judge_max_conversation_chars
        out_limit = config.runner.judge_max_output_chars
        conversation = extract_conversation(trace, max_chars=conv_limit)

        # Fall back to log file if OTel content not available
        if not conversation:
            log_file = results_dir / f"{slug}.log"
            if log_file.exists():
                text = log_file.read_text()
                conversation = (
                    text[:conv_limit] + "\n... (truncated)" if len(text) > conv_limit else text
                )

        # Read output files from persisted outputs. The judge can score on output
        # files alone (e.g. file-writing tasks), so only skip when neither the
        # conversation nor any output file is available.
        output_dir = results_dir / "outputs" / slug
        output_files_text = read_files_from_dir(output_dir, max_chars=out_limit)

        truncation: dict[str, Any] = {}
        if _is_truncated(conversation):
            truncation["conversation"] = conv_limit
        if _is_truncated(output_files_text):
            truncation["output_files"] = out_limit

        if not conversation and not output_files_text:
            continue

        contexts[key] = {
            "scenario": scenario,
            "variant": variant,
            "epoch": epoch,
            "fixture": fixture,
            "scores_file": scores_file,
            "existing_scores": existing_scores,
            "conversation": conversation,
            "output_files_text": output_files_text,
            "truncation": truncation,
            "pending_names": {ev.name for ev in pending},
            "order": {ev.name: i for i, ev in enumerate(pending)},
            "remaining": len(pending),
            "scores": [],
        }
        # Batch mode groups all of a context's judges into one call; otherwise
        # each judge is its own work item so a failure only affects that judge.
        if config.runner.judge_batch:
            work.append((key, list(pending)))
        else:
            for ev in pending:
                work.append((key, [ev]))

    if not work:
        return

    def _write_ctx(ctx: dict[str, Any]) -> None:
        """Merge a trace's collected judge scores with kept scores and persist."""
        rerun_names = ctx["pending_names"]
        kept = [
            s
            for s in ctx["existing_scores"]
            if s.get("type") != "judge" or s.get("name") not in rerun_names
        ]
        scores = sorted(ctx["scores"], key=lambda s: ctx["order"].get(s.get("name"), 0))
        all_scores = kept + scores
        if all_scores:
            ctx["scores_file"].write_text(json.dumps(all_scores, indent=2, ensure_ascii=False))

    # Phase 2: run judge evaluators in parallel (each work item invokes Copilot).
    # Results are collected per trace context on the main thread; a context's
    # scores file is written as soon as all of its judges complete, so an
    # interrupt or crash only loses traces still in flight.
    def _judge(key: str, evs: list[Any]) -> tuple[str, list[dict[str, Any]]]:
        ctx = contexts[key]
        label = ", ".join(ev.name for ev in evs)
        fx = f"/{ctx['fixture']}" if ctx.get("fixture") else ""
        cell_name = f"{ctx['scenario']}{fx}/{ctx['variant']}/e{ctx['epoch']}: {label}"
        reporter.cell_started(cell_name)
        reporter.notice(f"    [{cell_name}] Evaluating (judge)...")
        extra_meta = {"truncation": ctx["truncation"]} if ctx["truncation"] else None
        if config.runner.judge_batch and len(evs) > 1:
            scored = run_judges_batch(
                evs,
                ctx["conversation"],
                config,
                github_token,
                ctx["output_files_text"],
                extra_meta=extra_meta,
            )
        else:
            score = JudgeEvaluator.from_config(evs[0]).evaluate(
                EvalContext(
                    evaluator=evs[0],
                    config=config,
                    token=github_token,
                    conversation=ctx["conversation"],
                    output_files_text=ctx["output_files_text"],
                    extra_meta=extra_meta,
                )
            )
            # JudgeEvaluator only returns None when both conversation and
            # output_files_text are empty; the collection phase above already
            # guarantees at least one is present for every context here.
            assert score is not None
            scored = [score]
        for s in scored:
            if s.score is not None:
                reporter.notice(f"    ✓ {s.name}: {s.score} — {s.reason[:60]}")
            else:
                reporter.notice(f"    ! {s.name}: {s.reason}")
        return key, [score_to_dict(s) for s in scored]

    reporter.start(len(work), label="judge scoring", workers=config.runner.max_workers)
    try:
        with ThreadPoolExecutor(max_workers=config.runner.max_workers) as pool:
            futures = {
                pool.submit(_judge, key, evs): (key, evs, time.monotonic()) for key, evs in work
            }
            for future in as_completed(futures):
                key, evs, started = futures[future]
                fx = f"/{contexts[key]['fixture']}" if contexts[key].get("fixture") else ""
                cell_name = (
                    f"{contexts[key]['scenario']}{fx}/{contexts[key]['variant']}"
                    f"/e{contexts[key]['epoch']}: {', '.join(ev.name for ev in evs)}"
                )
                duration = time.monotonic() - started
                try:
                    key, scores = future.result()
                    reporter.cell_completed(cell_name, duration=duration, status="scored")
                except Exception as exc:  # never let one judge abort the whole batch
                    reporter.notice(f"    ! {', '.join(ev.name for ev in evs)}: error — {exc}")
                    reporter.cell_failed(cell_name, duration=duration, reason=str(exc))
                    n = max(1, config.runner.judge_samples)
                    scores = [
                        {
                            "name": ev.name,
                            "type": "judge",
                            "score": None,
                            "reason": f"error: {exc}",
                            "passed": False,
                            "samples": [],
                            "score_stddev": None,
                            "n_samples": n,
                            "outcomes": {"ok": 0, "parse_error": 0, "timeout": 0, "error": n},
                            "judge_model": config.runner.judge_model,
                            "judge_version": None,
                        }
                        for ev in evs
                    ]
                ctx = contexts[key]
                ctx["scores"].extend(scores)
                ctx["remaining"] -= len(scores)
                if ctx["remaining"] <= 0:
                    _write_ctx(ctx)
    finally:
        reporter.finish()


def _warn_unscored_judges(config: Config, traces: list[Trace], results_dir: Path) -> None:
    """Surface judge reproducibility issues: unusable scores, outcome-rate
    breakdown, host Copilot version mismatches, and truncated context."""
    tasks_by_name = {t.name: t for t in config.tasks}
    problems: list[str] = []
    outcomes: dict[str, int] = {}
    judge_total = 0
    mismatches: set[str] = set()
    versions: set[str] = set()
    truncated: list[str] = []
    seen_files: set[Path] = set()
    for trace in traces:
        scenario = trace.resource_tags.get("eval.scenario", "")
        variant = trace.resource_tags.get("eval.variant", "")
        epoch = trace.resource_tags.get("eval.epoch", "")
        fixture = trace.resource_tags.get("eval.fixture", "")
        task = tasks_by_name.get(scenario)
        if not task or not any(ev.type == "judge" for ev in task.evaluators):
            continue
        scores_file = results_dir / f"{run_slug(scenario, variant, epoch, fixture)}.scores.json"
        if not scores_file.exists() or scores_file in seen_files:
            continue
        seen_files.add(scores_file)
        try:
            scores = json.loads(scores_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        label = (
            f"{scenario}/{fixture}/{variant}/e{epoch}"
            if fixture
            else f"{scenario}/{variant}/e{epoch}"
        )
        for s in scores:
            if s.get("type") != "judge":
                continue
            judge_total += 1
            meta = s.get("meta") or {}
            outcome = meta.get("outcome") or ("ok" if s.get("score") is not None else "unknown")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if v := meta.get("judge_version"):
                versions.add(str(v))
            if mm := meta.get("judge_version_mismatch"):
                mismatches.add(f"expected {mm.get('expected')} got {mm.get('actual')}")
            if meta.get("truncation"):
                truncated.append(f"{label}:{s.get('name')}")
            if s.get("score") is None:
                reason = s.get("reason", "no score")
                problems.append(f"{label}:{s.get('name')} ({reason})")

    if judge_total:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
        click.echo(f"  Judge outcomes ({judge_total} total): {breakdown}", err=True)
    if versions:
        click.echo(f"  Judge host Copilot version(s): {', '.join(sorted(versions))}", err=True)
    if mismatches:
        click.echo(
            f"  WARNING: judge Copilot version mismatch — {'; '.join(sorted(mismatches))}", err=True
        )
    if truncated:
        click.echo(
            f"  WARNING: {len(truncated)} judge(s) saw truncated context "
            f"(raise runner.judge_max_conversation_chars / judge_max_output_chars): "
            f"{', '.join(truncated)}",
            err=True,
        )
    if problems:
        click.echo(
            f"  WARNING: {len(problems)} judge score(s) unavailable: {', '.join(problems)}",
            err=True,
        )


def _report_judge_reliability(results_dir: Path) -> None:
    """Summarize judge self-consistency + parse/error/timeout rates for a run.

    Reads every persisted ``*.scores.json`` in the run directory, aggregates
    judge sample outcomes (ok/parse_error/timeout/error) and per-evaluator score
    spread (stddev), and prints a compact reliability summary so noisy or
    failure-prone judges are visible alongside the metrics.
    """
    outcomes: dict[str, int] = {"ok": 0, "parse_error": 0, "timeout": 0, "error": 0}
    judge_evals = 0  # number of judge score records
    sampled_evals = 0  # records that ran >1 sample
    stddevs: list[float] = []
    no_score = 0

    for jf in sorted(results_dir.glob("*.scores.json")):
        try:
            scores = json.loads(jf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for s in scores:
            if s.get("type") != "judge":
                continue
            judge_evals += 1
            for k, v in (s.get("outcomes") or {}).items():
                outcomes[k] = outcomes.get(k, 0) + int(v)
            n = s.get("n_samples") or 0
            if n and n > 1:
                sampled_evals += 1
            sd = s.get("score_stddev")
            if sd is not None and s.get("score") is not None:
                stddevs.append(float(sd))
            if s.get("score") is None:
                no_score += 1

    if judge_evals == 0:
        return

    total_samples = sum(outcomes.values())
    click.echo("Judge reliability:", err=True)
    click.echo(
        f"  {judge_evals} judge evaluation(s), {total_samples} sample(s)"
        f"{f', {sampled_evals} multi-sampled' if sampled_evals else ''}.",
        err=True,
    )
    if total_samples:

        def rate(k: str) -> str:
            c = outcomes.get(k, 0)
            return f"{c} ({c / total_samples * 100:.0f}%)"

        click.echo(
            f"  Sample outcomes: ok {rate('ok')}, parse_error {rate('parse_error')}, "
            f"timeout {rate('timeout')}, error {rate('error')}.",
            err=True,
        )
    if stddevs:
        mean_sd = sum(stddevs) / len(stddevs)
        max_sd = max(stddevs)
        click.echo(f"  Score spread (σ): mean {mean_sd:.2f}, max {max_sd:.2f}.", err=True)
    if no_score:
        click.echo(f"  WARNING: {no_score} judge evaluation(s) produced no usable score.", err=True)
