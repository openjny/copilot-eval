"""JudgeExecutor: single source of truth for LLM-as-judge invocation.

Prompt construction, Copilot CLI invocation, JSON response parsing, error
handling, and self-consistency sampling used to be duplicated between the
single-judge (``run_judge``) and batched (``run_judges_batch``) code paths in
``eval.runner``. :class:`JudgeExecutor` extracts that shared logic so both
paths (now thin wrappers, see ``eval.runner``) and
:class:`eval.evaluators.judge.JudgeEvaluator` produce byte-identical score
shapes for the same inputs (issue #80).
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Any

from eval.config import Config
from eval.config import Evaluator as EvaluatorConfig
from eval.env_utils import collect_secrets, mask_secrets
from eval.protocols import EvalScore

# Per-call judge result: (score, masked_reason, outcome, sample_meta). outcome
# is one of ok | ok_nonzero | parse_error | invalid_score | timeout |
# not_found | error.
SampleResult = tuple["int | None", str, str, dict[str, Any]]

# Truncate captured judge stderr to this many characters before attaching it
# to score metadata, so *.scores.json stays readable.
_STDERR_SNIPPET_CHARS = 500

_host_copilot_version_cache: str | None = None


def host_copilot_version() -> str | None:
    """Return the host `copilot --version` string, cached for the process.

    Returns None if the Copilot CLI is missing or the call fails. Used to record
    and verify which (unpinned) host Copilot performed the judge scoring.
    """
    global _host_copilot_version_cache
    if _host_copilot_version_cache is not None:
        return _host_copilot_version_cache or None
    try:
        proc = subprocess.run(["copilot", "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        _host_copilot_version_cache = ""
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    # Reduce noisy multi-line banners to the first non-empty line.
    version = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
    _host_copilot_version_cache = version
    return version or None


def _parse_json(text: str, require_keys: tuple[str, ...] | None = None) -> dict[str, Any] | None:
    """Extract a JSON object from possibly noisy LLM output.

    Handles single-line JSON, whole-text JSON, markdown code fences, and
    multiline JSON objects embedded in surrounding prose. When ``require_keys``
    is given, only a parsed object containing all of those keys is accepted, so
    stray JSON fragments don't masquerade as a valid result.
    """
    if not text:
        return None
    stripped = text.strip()

    candidates: list[str] = []
    # Markdown code fence (```json ... ``` or ``` ... ```)
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1).strip())
    # Whole text
    candidates.append(stripped)
    # First brace .. last brace (multiline object embedded in prose)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    # Single-line JSON objects
    for line in stripped.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if require_keys and not all(k in data for k in require_keys):
            continue
        return data
    return None


def _aggregate_scores(samples: list[int], method: str) -> int:
    """Aggregate successful judge sample scores into a single integer score.

    Uses half-up rounding (not Python's banker's rounding) so an even-length
    median/mean of e.g. 6.5 rounds to 7 rather than 6.
    """

    def _round_half_up(x: float) -> int:
        return int(math.floor(x + 0.5))

    if method == "mean":
        return _round_half_up(mean(samples))
    if method == "majority":
        # Most common value; ties broken by the lower score for determinism.
        counts = Counter(samples)
        top = max(counts.values())
        return min(v for v, c in counts.items() if c == top)
    return _round_half_up(median(samples))  # default: median


@dataclass
class JudgeContext:
    """Evidence + ambient info needed to score a run against judge evaluators.

    Shared by :meth:`JudgeExecutor.execute_single` and
    :meth:`JudgeExecutor.execute_batch`. ``conversation``/``output_files_text``
    carry the captured transcript/output-file evidence; ``token`` authenticates
    the judge Copilot call; ``extra_meta`` is caller-supplied context (e.g.
    truncation flags) merged into the returned score's metadata.
    """

    conversation: str
    output_files_text: str | None = None
    token: str | None = None
    extra_meta: dict[str, Any] | None = None


class JudgeExecutor:
    """Encapsulates a Copilot-as-judge invocation end to end.

    Owns prompt construction (with rubric, conversation, and output context),
    Copilot CLI invocation (timeout, model override), response parsing (JSON
    extraction, score normalization), error handling, runtime metadata
    collection (returncode/stderr, host Copilot version, version mismatch),
    and the self-consistency sampling loop (``runner.judge_samples`` calls,
    aggregated via ``runner.judge_aggregate``).

    :meth:`execute_single` scores one evaluator per Copilot call;
    :meth:`execute_batch` scores every evaluator in one call (opt-in via
    ``runner.judge_batch``), splitting the response back into per-evaluator
    scores that are byte-compatible with ``execute_single``.
    """

    def __init__(self, config: Config, copilot_cmd: list[str] | None = None) -> None:
        self.config = config
        self.copilot_cmd = copilot_cmd or ["copilot"]

    # -- prompt construction ------------------------------------------------

    def _sections(self, conversation: str, output_files_text: str | None) -> str:
        """Build the evidence block (conversation + optional output files)
        shared by single and batched judge prompts."""
        sections = [f"--- COPILOT OUTPUT ---\n{conversation}\n--- END OUTPUT ---"]
        if output_files_text:
            sections.append(f"--- OUTPUT FILES ---\n{output_files_text}\n--- END FILES ---")
        return chr(10).join(sections)

    def _build_single_prompt(
        self, evaluator: EvaluatorConfig, conversation: str, output_files_text: str | None
    ) -> str:
        return (
            f"You are an eval judge. Score the following Copilot output.\n\n"
            f"{evaluator.prompt}\n\n"
            f"{self._sections(conversation, output_files_text)}\n\n"
            f'Output ONLY valid JSON: {{"score": N, "reason": "..."}}'
        )

    def _build_batch_prompt(
        self,
        evaluators: list[EvaluatorConfig],
        conversation: str,
        output_files_text: str | None,
    ) -> str:
        criteria = "\n\n".join(f"### {ev.name}\n{ev.prompt}" for ev in evaluators)
        example = ", ".join(f'"{ev.name}": {{"score": N, "reason": "..."}}' for ev in evaluators)
        return (
            f"You are an eval judge. Score the following Copilot output against "
            f"MULTIPLE independent criteria. Judge each criterion strictly on its own "
            f"merits; do not let one criterion's score influence another.\n\n"
            f"Criteria:\n{criteria}\n\n"
            f"{self._sections(conversation, output_files_text)}\n\n"
            f"Output ONLY valid JSON mapping each criterion name to its verdict: "
            f"{{{example}}}"
        )

    # -- Copilot CLI invocation ----------------------------------------------

    def _cmd(self, prompt: str) -> list[str]:
        cmd = [*self.copilot_cmd, "-p", prompt, "-s"]
        if self.config.runner.judge_model:
            cmd.extend(["--model", self.config.runner.judge_model])
        return cmd

    def _judge_env(self, token: str | None) -> dict[str, str]:
        # Disable OTel to avoid contaminating eval traces with judge calls.
        return {**os.environ, "GITHUB_TOKEN": token or "", "COPILOT_OTEL_ENABLED": "false"}

    def _invoke_single(self, prompt: str, token: str | None, secrets: list[str]) -> SampleResult:
        """Invoke the judge Copilot once for a single evaluator."""
        sample_meta: dict[str, Any] = {}
        try:
            proc = subprocess.run(
                self._cmd(prompt),
                capture_output=True,
                text=True,
                timeout=self.config.runner.judge_timeout_seconds,
                env=self._judge_env(token),
            )
        except subprocess.TimeoutExpired:
            timeout_s = self.config.runner.judge_timeout_seconds
            return None, f"timeout after {timeout_s}s", "timeout", sample_meta
        except FileNotFoundError:
            return None, "copilot CLI not found on host", "not_found", sample_meta
        except OSError as exc:
            return None, f"error: {exc}", "error", sample_meta
        sample_meta["returncode"] = proc.returncode
        stderr = mask_secrets((proc.stderr or "").strip(), secrets) or ""
        if stderr:
            sample_meta["stderr"] = stderr[:_STDERR_SNIPPET_CHARS]
        data = _parse_json(proc.stdout, require_keys=("score",))
        if data is not None:
            try:
                score = int(data.get("score", 0))
            except (TypeError, ValueError):
                return None, f"invalid_score: {data.get('score')!r}", "invalid_score", sample_meta
            # A parseable verdict from a process that exited non-zero is
            # suspicious: keep the score but flag the anomaly so it isn't
            # counted as a clean run.
            outcome = "ok" if proc.returncode == 0 else "ok_nonzero"
            return score, str(data.get("reason", "")), outcome, sample_meta
        if proc.returncode != 0:
            detail = f" — {stderr[:200]}" if stderr else ""
            return None, f"error: rc={proc.returncode}{detail}", "error", sample_meta
        return None, "parse_error", "parse_error", sample_meta

    def _invoke_batch(
        self, prompt: str, names: list[str], token: str | None, secrets: list[str]
    ) -> tuple[dict[str, tuple[int | None, str, str]], dict[str, Any]]:
        """Invoke the judge Copilot once for *all* criteria in ``names``.

        Returns ``(results, sample_meta)`` where ``results`` maps each evaluator
        name to ``(score, reason, outcome)``. A process-level failure
        (timeout/error) or an unparseable top-level response is applied to
        *every* criterion (the batching failure blast radius). When the
        top-level object parses, a missing key or bad score fails only that
        individual criterion.
        """
        sample_meta: dict[str, Any] = {}

        def _all(
            outcome: str, reason: str
        ) -> tuple[dict[str, tuple[int | None, str, str]], dict[str, Any]]:
            return {name: (None, reason, outcome) for name in names}, sample_meta

        try:
            proc = subprocess.run(
                self._cmd(prompt),
                capture_output=True,
                text=True,
                timeout=self.config.runner.judge_timeout_seconds,
                env=self._judge_env(token),
            )
        except subprocess.TimeoutExpired:
            timeout_s = self.config.runner.judge_timeout_seconds
            return _all("timeout", f"timeout after {timeout_s}s")
        except FileNotFoundError:
            return _all("not_found", "copilot CLI not found on host")
        except OSError as exc:
            return _all("error", f"error: {exc}")

        sample_meta["returncode"] = proc.returncode
        stderr = mask_secrets((proc.stderr or "").strip(), secrets) or ""
        if stderr:
            sample_meta["stderr"] = stderr[:_STDERR_SNIPPET_CHARS]

        data = _parse_json(proc.stdout)
        if data is None:
            if proc.returncode != 0:
                detail = f" — {stderr[:200]}" if stderr else ""
                return _all("error", f"error: rc={proc.returncode}{detail}")
            return _all("parse_error", "parse_error")

        # A parseable verdict from a non-zero exit is suspicious: keep scores
        # but flag the anomaly so it isn't counted as a clean run.
        ok_outcome = "ok" if proc.returncode == 0 else "ok_nonzero"
        results: dict[str, tuple[int | None, str, str]] = {}
        for name in names:
            entry = data.get(name)
            if not isinstance(entry, dict) or "score" not in entry:
                results[name] = (None, "parse_error", "parse_error")
                continue
            try:
                score = int(entry.get("score", 0))
            except (TypeError, ValueError):
                results[name] = (None, f"invalid_score: {entry.get('score')!r}", "invalid_score")
                continue
            results[name] = (score, str(entry.get("reason", "")), ok_outcome)
        return results, sample_meta

    # -- metadata + aggregation ----------------------------------------------

    def _base_meta(self, extra_meta: dict[str, Any] | None, version: str | None) -> dict[str, Any]:
        """Seed judge meta with caller extras, host version, and any version
        mismatch against the configured expectation."""
        base_meta: dict[str, Any] = {**(extra_meta or {})}
        if version:
            base_meta["judge_version"] = version
        expected = self.config.runner.judge_copilot_version
        # Record a mismatch when the host version differs from the configured
        # expectation -- including when the host version is unavailable, which
        # is exactly when reproducibility is least observable.
        if expected and version != expected:
            base_meta["judge_version_mismatch"] = {"expected": expected, "actual": version}
        return base_meta

    def _finalize(
        self,
        evaluator: EvaluatorConfig,
        per_sample: list[SampleResult],
        samples: list[int],
        outcomes: dict[str, int],
        n: int,
        base_meta: dict[str, Any],
        version: str | None,
    ) -> EvalScore:
        """Aggregate an evaluator's per-sample results into one EvalScore.

        Shared by :meth:`execute_single` and :meth:`execute_batch` so both
        produce identical score shapes.
        """
        if not samples:
            # No usable score across all samples; surface the dominant failure
            # mode along with its representative reason and runtime meta.
            dominant = max(outcomes, key=lambda o: outcomes[o])
            idx = next(i for i, t in enumerate(per_sample) if t[2] == dominant)
            meta = {**base_meta, **per_sample[idx][3], "outcome": dominant}
            reason = per_sample[idx][1] if n == 1 else dominant
            return EvalScore(
                name=evaluator.name,
                type="judge",
                score=None,
                reason=reason,
                passed=False,
                samples=samples,
                score_stddev=None,
                n_samples=n,
                outcomes=outcomes,
                judge_model=self.config.runner.judge_model,
                judge_version=version,
                meta=meta,
            )

        agg = _aggregate_scores(samples, self.config.runner.judge_aggregate)
        stddev = float(pstdev(samples)) if len(samples) > 1 else 0.0
        # Representative: successful call whose score is closest to the aggregate.
        succ = [(s, t) for t in per_sample if (s := t[0]) is not None]
        _, rep = min(succ, key=lambda it: abs(it[0] - agg))
        reason = rep[1]
        if n > 1:
            agg_method = self.config.runner.judge_aggregate
            reason = f"[{agg_method} of {len(samples)}/{n}, σ={stddev:.2f}] {reason}"
        meta = {**base_meta, **rep[3], "outcome": rep[2]}
        return EvalScore(
            name=evaluator.name,
            type="judge",
            score=agg,
            reason=reason,
            samples=samples,
            score_stddev=round(stddev, 4),
            n_samples=n,
            outcomes=outcomes,
            judge_model=self.config.runner.judge_model,
            judge_version=version,
            meta=meta,
        )

    # -- public API -----------------------------------------------------------

    def execute_single(self, evaluator: EvaluatorConfig, context: JudgeContext) -> EvalScore:
        """Score a run against one judge evaluator.

        Builds the judge prompt and samples the judge ``runner.judge_samples``
        times (self-consistency), disabling OTel so judge calls don't
        contaminate eval traces. Successful samples are aggregated via
        ``runner.judge_aggregate`` (median/mean/majority); the per-sample
        spread (stddev) and outcome counts are recorded for reliability
        reporting. Judge runtime metadata (host Copilot version,
        returncode/stderr, version mismatch, and caller-supplied truncation
        flags) is recorded on the returned score.
        """
        secrets = collect_secrets(self.config, context.token)
        conversation = mask_secrets(context.conversation, secrets) or ""
        output_files_text = mask_secrets(context.output_files_text, secrets)
        prompt = self._build_single_prompt(evaluator, conversation, output_files_text)

        version = host_copilot_version()
        base_meta = self._base_meta(context.extra_meta, version)

        n = max(1, self.config.runner.judge_samples)
        per_sample: list[SampleResult] = []
        samples: list[int] = []
        outcomes: dict[str, int] = {}
        for _ in range(n):
            score, reason, outcome, smeta = self._invoke_single(prompt, context.token, secrets)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            per_sample.append((score, mask_secrets(reason, secrets) or reason, outcome, smeta))
            if score is not None:
                samples.append(score)

        return self._finalize(evaluator, per_sample, samples, outcomes, n, base_meta, version)

    def execute_batch(
        self, evaluators: list[EvaluatorConfig], context: JudgeContext
    ) -> list[EvalScore]:
        """Score *all* of ``evaluators`` in a single LLM call per sample.

        Opt-in optimization (``runner.judge_batch``). Instead of one Copilot
        call per evaluator, one call scores every criterion at once, returning
        a JSON object keyed by evaluator name. The response is split back into
        per-evaluator scores that are byte-compatible with
        :meth:`execute_single`. Calls drop from ``n_judges × judge_samples`` to
        ``judge_samples``.

        This trades judge independence for cost: criteria can cross-contaminate
        (halo effect), a single parse failure fails every criterion, and
        per-criterion noise within a sample becomes correlated. Keep it off
        (default) when accuracy matters.

        A single evaluator is delegated to :meth:`execute_single` since there
        is nothing to batch.
        """
        if len(evaluators) == 1:
            return [self.execute_single(evaluators[0], context)]

        secrets = collect_secrets(self.config, context.token)
        conversation = mask_secrets(context.conversation, secrets) or ""
        output_files_text = mask_secrets(context.output_files_text, secrets)
        prompt = self._build_batch_prompt(evaluators, conversation, output_files_text)

        names = [ev.name for ev in evaluators]
        version = host_copilot_version()
        base_meta = self._base_meta(context.extra_meta, version)

        n = max(1, self.config.runner.judge_samples)
        per_sample: dict[str, list[SampleResult]] = {name: [] for name in names}
        samples: dict[str, list[int]] = {name: [] for name in names}
        outcomes: dict[str, dict[str, int]] = {name: {} for name in names}
        for _ in range(n):
            results, smeta = self._invoke_batch(prompt, names, context.token, secrets)
            for name in names:
                score, reason, outcome = results[name]
                outcomes[name][outcome] = outcomes[name].get(outcome, 0) + 1
                per_sample[name].append(
                    (score, mask_secrets(reason, secrets) or reason, outcome, smeta)
                )
                if score is not None:
                    samples[name].append(score)

        return [
            self._finalize(
                ev,
                per_sample[ev.name],
                samples[ev.name],
                outcomes[ev.name],
                n,
                base_meta,
                version,
            )
            for ev in evaluators
        ]
