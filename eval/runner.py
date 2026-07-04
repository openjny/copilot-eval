"""Execute a single eval run in a Docker container."""

from __future__ import annotations

import json
import math
import operator
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from logging import getLogger
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

from eval.collectors import create_collector
from eval.collectors.file_collector import TRACE_FILE
from eval.config import Config, ConfigError, Evaluator, Task, Variant
from eval.env_utils import (
    _SECRET_PLACEHOLDER,
    collect_secrets,
    strip_quotes,
    write_sanitized_env_file,
)
from eval.env_utils import (
    load_env_file as _load_env_file,
)
from eval.naming import run_slug
from eval.protocols import (
    RunArtifacts,
    RunContext,
    RunStatus,
)
from eval.protocols import (
    status_from_exit_code as _status_from_exit_code,
)
from eval.runners.docker_cli_runner import DockerCLIRunner
from eval.trace import RunMetrics, metric_value

logger = getLogger(__name__)

status_from_exit_code = _status_from_exit_code


@dataclass
class EvalScore:
    name: str
    type: str
    score: int | None
    reason: str = ""
    passed: bool = True
    # Self-consistency metadata (judge evaluators only). ``samples`` holds the
    # successful per-call scores; ``n_samples`` is the number of calls requested
    # (== sum of ``outcomes``), which may exceed len(samples) when some fail.
    samples: list[int] = field(default_factory=list)
    score_stddev: float | None = None
    n_samples: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    judge_model: str | None = None
    judge_version: str | None = None
    # Free-form judge runtime/context metadata (judge runtime, truncation, etc.).
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return score_to_dict(self)


@dataclass
class RunResult:
    task: str
    variant: str
    epoch: int
    test_id: str
    run_id: str
    log_file: Path
    exit_code: int
    status: RunStatus = RunStatus.SUCCESS
    scores: list[EvalScore] = field(default_factory=list)
    order_index: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    # Reporting fixture label ("" for single-fixture tasks; the fixture name for
    # multi-fixture tasks). Recorded in the manifest so `analyze` can group runs
    # along the input-coverage axis.
    fixture: str = ""

    @property
    def passed(self) -> bool:
        return self.status == RunStatus.SUCCESS and (
            all(s.passed for s in self.scores) if self.scores else True
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "variant": self.variant,
            "epoch": self.epoch,
            "fixture": self.fixture,
            "test_id": self.test_id,
            "run_id": self.run_id,
            "exit_code": self.exit_code,
            "status": str(self.status),
            "passed": self.passed,
            "order_index": self.order_index,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "scores": [score_to_dict(s) for s in self.scores],
        }


def score_to_dict(s: EvalScore) -> dict[str, Any]:
    """Serialize an EvalScore to the *.scores.json schema.

    Base keys mirror the legacy shape; judge self-consistency metadata
    (samples, stddev, outcomes, model/version) is added only for judge scores
    so non-judge serialization stays byte-identical.
    """
    d: dict[str, Any] = {
        "name": s.name,
        "type": s.type,
        "score": s.score,
        "reason": s.reason,
        "passed": s.passed,
    }
    if s.meta:
        d["meta"] = s.meta
    if s.type == "judge" and s.n_samples:
        d.update(
            {
                "samples": s.samples,
                "score_stddev": s.score_stddev,
                "n_samples": s.n_samples,
                "outcomes": s.outcomes,
                "judge_model": s.judge_model,
                "judge_version": s.judge_version,
            }
        )
    return d


def get_github_token() -> str:
    # COPILOT_GITHUB_TOKEN (the name used in .env.example) is accepted as a
    # fallback so it stays in sync with validation.check_github_token().
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("COPILOT_GITHUB_TOKEN", "")
    if token:
        return token
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "GITHUB_TOKEN not set and gh CLI not authenticated"
        ) from exc


def run_one(
    task: Task,
    variant: Variant,
    epoch: int,
    config: Config,
    run_id: str,
    run_dir: Path,
    github_token: str,
    order_index: int | None = None,
    fixture: str | None = None,
) -> RunResult:
    # Concrete fixture directory to mount; the reporting label is empty for
    # single-fixture tasks so legacy file names / report layout are preserved.
    fixture_dir_name = fixture if fixture is not None else task.fixture_names()[0]
    fixture_label = task.fixture_label(fixture_dir_name)
    test_id = str(uuid.uuid4())
    log_file = run_dir / (run_slug(task.name, variant.name, epoch, fixture_label) + ".log")
    suffix = f" fixture={fixture_label}" if fixture_label else ""
    logger.info(
        "[%s] epoch=%s variant=%s%s test_id=%s",
        task.name,
        epoch,
        variant.name,
        suffix,
        test_id[:8],
    )

    # Capture wall-clock schedule so post-hoc analysis can detect order/concurrency
    # confounders. monotonic clock is used for duration to avoid clock-skew issues;
    # microsecond timestamps preserve sub-second ordering under concurrency.
    started_at = datetime.now().isoformat(timespec="microseconds")
    started_monotonic = time.monotonic()

    def _timing() -> dict[str, Any]:
        return {
            "order_index": order_index,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="microseconds"),
            "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        }

    # Fail fast on invalid runner/collector combinations before doing any setup
    # work, so a misconfiguration surfaces as a clear ConfigError instead of a
    # confusing runtime failure later in the run.
    runner = DockerCLIRunner(github_token, run_command=subprocess.run)
    if config.runner.collector not in runner.supported_collectors:
        supported = ", ".join(runner.supported_collectors)
        raise ConfigError(
            f"Runner '{type(runner).__name__}' does not support collector "
            f"'{config.runner.collector}'. Supported collectors: {supported}."
        )

    # Tracked across the run so the outer `finally` can always clean up and
    # redact secrets, even on early returns (e.g. setup_failed) or exceptions.
    work_dir: Path | None = None
    scores: list[EvalScore] = []
    artifacts: RunArtifacts | None = None
    container_run_completed = False
    try:
        before_rc = _run_hook(task.hooks.before_run, config, task, variant, log_file, "before_run")
        if before_rc != 0:
            if task.hooks.on_failure == "fail":
                logger.error("before_run hook failed (exit %s) — skipping run", before_rc)
                _append_log(log_file, f"before_run hook failed with exit code {before_rc}")
                return RunResult(
                    task=task.name,
                    variant=variant.name,
                    epoch=epoch,
                    test_id=test_id,
                    run_id=run_id,
                    log_file=log_file,
                    exit_code=-1,
                    status=RunStatus.SETUP_FAILED,
                    **_timing(),
                    fixture=fixture_label,
                )
            logger.warning(
                "before_run hook failed (exit %s) — continuing (on_failure=warn)",
                before_rc,
            )
            _append_log(
                log_file,
                f"before_run hook failed with exit code {before_rc}; continuing because hooks.on_failure=warn",
            )

        # Health check: verify environment is ready before running Copilot
        if task.health_check:
            if not _run_health_check(task.health_check, config, task, variant, log_file):
                logger.error("Health check failed — skipping run")
                return RunResult(
                    task=task.name,
                    variant=variant.name,
                    epoch=epoch,
                    test_id=test_id,
                    run_id=run_id,
                    log_file=log_file,
                    exit_code=-1,
                    status=RunStatus.SETUP_FAILED,
                    fixture=fixture_label,
                    **_timing(),
                )

        # Writable workspace: copy fixture to tmpdir so Copilot can read AND write
        work_dir = Path(tempfile.mkdtemp(prefix="eval-work-"))
        fixture_dir = (config.config_dir / "fixtures" / fixture_dir_name).resolve()
        if fixture_dir.is_dir():
            shutil.copytree(fixture_dir, work_dir, dirs_exist_ok=True)
        # Create directories DockerCLIRunner expects and Copilot writes into.
        (work_dir / "output").mkdir(exist_ok=True)
        (work_dir / TRACE_FILE.parent).mkdir(exist_ok=True)
        collector_kwargs = (
            {
                "jaeger_url": config.runner.jaeger_url,
                "otel_endpoint": config.runner.otel_endpoint,
            }
            if config.runner.collector == "jaeger"
            else {}
        )
        collector = create_collector(config.runner.collector, **collector_kwargs)
        collector_context = RunContext(
            run_id=run_id,
            test_id=test_id,
            epoch=epoch,
            run_dir=run_dir,
            task=task,
            variant=variant,
            config=config,
            work_dir=work_dir,
            fixture=fixture_dir_name,
            fixture_label=fixture_label,
        )
        run_context = RunContext(
            run_id=run_id,
            test_id=test_id,
            epoch=epoch,
            run_dir=run_dir,
            task=task,
            variant=variant,
            config=config,
            extra_env=collector.exporter_env(collector_context),
            work_dir=work_dir,
            fixture=fixture_dir_name,
            fixture_label=fixture_label,
        )

        logger.info("Running copilot in container...")
        artifacts = runner.run(run_context)
        container_run_completed = True
        _print_summary(log_file)

        after_rc = _run_hook(task.hooks.after_run, config, task, variant, log_file, "after_run")
        if after_rc != 0:
            logger.warning("after_run hook failed (exit %s) — surfacing in results", after_rc)
            _append_log(log_file, f"after_run hook failed with exit code {after_rc}")
            scores.append(
                EvalScore(
                    name="after_run_hook",
                    type="hook",
                    score=None,
                    reason=f"after_run hook failed with exit code {after_rc}",
                    passed=False,
                )
            )

        # Persist output files to results dir before tmpdir cleanup
        _persist_output_files(work_dir, run_dir, task.name, variant.name, epoch, fixture_label)
        _persist_trace_file(work_dir, run_dir, task.name, variant.name, epoch, fixture_label)
        if (
            config.runner.collector == "file"
            and artifacts.exit_code == 0
            and not (work_dir / TRACE_FILE).exists()
        ):
            logger.warning(
                "file collector enabled but no trace file was written; "
                "ensure your Copilot CLI supports COPILOT_OTEL_FILE_EXPORTER_PATH."
            )

        scores.extend(_run_evaluators(task, variant, config, log_file, github_token, work_dir))
        # Persist the full score set (hook + evaluator scores) so later analysis
        # sees hook failures too, not just evaluator-produced scores.
        _write_scores_file(log_file, scores)
        _print_scores(scores)
    except Exception as exc:  # noqa: BLE001 - isolate per-run failures from the batch
        # A failure before the container ran is a setup problem; a failure during
        # post-processing (persist/evaluators/after_run) happened after a real run,
        # so preserve the container's exit status instead of mislabeling it.
        if not container_run_completed or artifacts is None:
            logger.error("Run errored during setup: %s", exc)
            _append_log(log_file, f"run_one raised during setup: {exc!r}")
            return RunResult(
                task=task.name,
                variant=variant.name,
                epoch=epoch,
                test_id=test_id,
                run_id=run_id,
                log_file=log_file,
                exit_code=-1,
                status=RunStatus.SETUP_FAILED,
                scores=scores,
                **_timing(),
                fixture=fixture_label,
            )
        logger.error("Run errored during post-processing: %s", exc)
        _append_log(log_file, f"run_one raised during post-processing: {exc!r}")
        scores.append(
            EvalScore(
                name="post_processing",
                type="infra",
                score=None,
                reason=f"post-run exception: {exc!r}",
                passed=False,
            )
        )
        return RunResult(
            task=task.name,
            variant=variant.name,
            epoch=epoch,
            test_id=test_id,
            run_id=run_id,
            log_file=log_file,
            exit_code=artifacts.exit_code,
            status=artifacts.status,
            scores=scores,
            **_timing(),
            fixture=fixture_label,
        )
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        # Redact secret values from the persisted log. Done in `finally` (after
        # evaluators, which read the raw log for contains/regex) so it also
        # covers early returns and exceptions, and never skews evaluator results.
        _mask_log_file(log_file, collect_secrets(config, github_token))

    return RunResult(
        task=task.name,
        variant=variant.name,
        epoch=epoch,
        test_id=test_id,
        run_id=run_id,
        log_file=log_file,
        exit_code=artifacts.exit_code,
        status=artifacts.status,
        scores=scores,
        fixture=fixture_label,
        **_timing(),
    )


def _run_hook(
    script: str | None, config: Config, task: Task, variant: Variant, log_file: Path, label: str
) -> int:
    """Run a before/after hook script. Returns the script's exit code (0 when no
    script is configured or the script is missing, so a missing hook never fails)."""
    if not script:
        return 0
    resolved = (config.config_dir / script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / script).resolve()
    if not resolved.exists():
        logger.warning("%s script not found: %s", label, script)
        return 0
    logger.info("Running %s...", label)
    merged_vars = config.resolve_vars(task, variant)
    env = {
        **os.environ,
        **_load_env_file(config.env_file),
        **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()},
    }
    with open(log_file, "a") as lf:
        proc = subprocess.run(["bash", str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)
    return proc.returncode


def _append_log(log_file: Path, message: str) -> None:
    """Append a diagnostic line to the run log, ignoring I/O errors."""
    try:
        with open(log_file, "a") as lf:
            lf.write(f"\n[eval] {message}\n")
    except OSError:
        pass


def _run_health_check(
    script: str, config: Config, task: Task, variant: Variant, log_file: Path
) -> bool:
    """Run health check script. Returns True if environment is ready."""
    resolved = (config.config_dir / script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / script).resolve()
    if not resolved.exists():
        logger.warning("health_check script not found: %s", script)
        return True  # skip check if script missing
    logger.info("Running health_check...")
    merged_vars = config.resolve_vars(task, variant)
    env = {
        **os.environ,
        **_load_env_file(config.env_file),
        **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()},
    }
    with open(log_file, "a") as lf:
        proc = subprocess.run(["bash", str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)
    return proc.returncode == 0


def _persist_output_files(
    work_dir: Path, run_dir: Path, task: str, variant: str, epoch: int, fixture: str = ""
) -> None:
    """Copy output files from tmpdir to results dir for later analysis."""
    output_dir = work_dir / "output"
    if not output_dir.is_dir():
        return
    files = [f for f in output_dir.rglob("*") if f.is_file()]
    if not files:
        return
    dest = run_dir / "outputs" / run_slug(task, variant, epoch, fixture)
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        rel = f.relative_to(output_dir)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)


def _persist_trace_file(
    work_dir: Path, run_dir: Path, task: str, variant: str, epoch: int, fixture: str = ""
) -> None:
    """Copy trace file from work tmpdir to results dir for later analysis."""
    trace_src = work_dir / TRACE_FILE
    if not trace_src.exists():
        return
    trace_dest = run_dir / TRACE_FILE.parent / (run_slug(task, variant, epoch, fixture) + ".jsonl")
    trace_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(trace_src, trace_dest)


def _run_evaluators(
    task: Task,
    variant: Variant,
    config: Config,
    log_file: Path,
    token: str,
    work_dir: Path | None = None,
) -> list[EvalScore]:
    """Run non-judge evaluators (script, contains, regex). Judge runs in analyze."""
    scores: list[EvalScore] = []
    for ev in task.evaluators:
        s = None
        if ev.type == "judge":
            continue  # Judge evaluators run in `analyze` command
        elif ev.type == "script":
            s = _eval_script(ev, config, task, variant, log_file)
        elif ev.type == "contains":
            s = _eval_contains(ev, log_file)
        elif ev.type == "regex":
            s = _eval_regex(ev, log_file)
        if s:
            scores.append(s)
    if scores:
        _write_scores_file(log_file, scores)
    return scores


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


def _write_scores_file(log_file: Path, scores: list[EvalScore]) -> None:
    """Persist scores next to the run log as `<log>.scores.json`."""
    if not scores:
        return
    sf = log_file.with_suffix(".scores.json")
    sf.write_text(json.dumps([s.to_dict() for s in scores], indent=2, ensure_ascii=False))


def _run_judge_once(
    prompt: str, config: Config, token: str | None, secrets: list[str]
) -> tuple[int | None, str, str, dict[str, Any]]:
    """Invoke the judge Copilot once.

    Returns ``(score, reason, outcome, sample_meta)`` where ``outcome`` is one of
    ok | ok_nonzero | parse_error | invalid_score | timeout | not_found | error.
    ``sample_meta`` carries per-call runtime details (returncode, masked stderr).
    """
    cmd = ["copilot", "-p", prompt, "-s"]
    if config.runner.judge_model:
        cmd.extend(["--model", config.runner.judge_model])
    # Disable OTel to avoid contaminating eval traces with judge calls
    judge_env = {**os.environ, "GITHUB_TOKEN": token or "", "COPILOT_OTEL_ENABLED": "false"}
    sample_meta: dict[str, Any] = {}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.runner.judge_timeout_seconds,
            env=judge_env,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout after {config.runner.judge_timeout_seconds}s", "timeout", sample_meta
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
        # A parseable verdict from a process that exited non-zero is suspicious:
        # keep the score but flag the anomaly so it isn't counted as a clean run.
        outcome = "ok" if proc.returncode == 0 else "ok_nonzero"
        return score, str(data.get("reason", "")), outcome, sample_meta
    if proc.returncode != 0:
        detail = f" — {stderr[:200]}" if stderr else ""
        return None, f"error: rc={proc.returncode}{detail}", "error", sample_meta
    return None, "parse_error", "parse_error", sample_meta


# Per-call judge result: (score, masked_reason, outcome, sample_meta)
_SampleResult = tuple["int | None", str, str, dict[str, Any]]


def _judge_sections(conversation: str, output_files_text: str | None) -> str:
    """Build the evidence block (conversation + optional output files) shared by
    single and batched judge prompts."""
    sections = [f"--- COPILOT OUTPUT ---\n{conversation}\n--- END OUTPUT ---"]
    if output_files_text:
        sections.append(f"--- OUTPUT FILES ---\n{output_files_text}\n--- END FILES ---")
    return chr(10).join(sections)


def _judge_base_meta(
    config: Config, extra_meta: dict[str, Any] | None, version: str | None
) -> dict[str, Any]:
    """Seed judge meta with caller extras, host version, and any version
    mismatch against the configured expectation."""
    base_meta: dict[str, Any] = {**(extra_meta or {})}
    if version:
        base_meta["judge_version"] = version
    expected = config.runner.judge_copilot_version
    # Record a mismatch when the host version differs from the configured
    # expectation -- including when the host version is unavailable, which is
    # exactly when reproducibility is least observable.
    if expected and version != expected:
        base_meta["judge_version_mismatch"] = {"expected": expected, "actual": version}
    return base_meta


def _finalize_judge_score(
    ev: Evaluator,
    per_sample: list[_SampleResult],
    samples: list[int],
    outcomes: dict[str, int],
    n: int,
    base_meta: dict[str, Any],
    version: str | None,
    config: Config,
) -> EvalScore:
    """Aggregate a judge's per-sample results into one EvalScore.

    Shared by the single-judge (:func:`run_judge`) and batched
    (:func:`run_judges_batch`) paths so both produce identical score shapes.
    """
    if not samples:
        # No usable score across all samples; surface the dominant failure mode
        # along with its representative reason and runtime meta.
        dominant = max(outcomes, key=lambda o: outcomes[o])
        idx = next(i for i, t in enumerate(per_sample) if t[2] == dominant)
        meta = {**base_meta, **per_sample[idx][3], "outcome": dominant}
        reason = per_sample[idx][1] if n == 1 else dominant
        return EvalScore(
            name=ev.name,
            type="judge",
            score=None,
            reason=reason,
            passed=False,
            samples=samples,
            score_stddev=None,
            n_samples=n,
            outcomes=outcomes,
            judge_model=config.runner.judge_model,
            judge_version=version,
            meta=meta,
        )

    agg = _aggregate_scores(samples, config.runner.judge_aggregate)
    stddev = float(pstdev(samples)) if len(samples) > 1 else 0.0
    # Representative: successful call whose score is closest to the aggregate.
    succ = [(s, t) for t in per_sample if (s := t[0]) is not None]
    _, rep = min(succ, key=lambda it: abs(it[0] - agg))
    reason = rep[1]
    if n > 1:
        reason = f"[{config.runner.judge_aggregate} of {len(samples)}/{n}, σ={stddev:.2f}] {reason}"
    meta = {**base_meta, **rep[3], "outcome": rep[2]}
    return EvalScore(
        name=ev.name,
        type="judge",
        score=agg,
        reason=reason,
        samples=samples,
        score_stddev=round(stddev, 4),
        n_samples=n,
        outcomes=outcomes,
        judge_model=config.runner.judge_model,
        judge_version=version,
        meta=meta,
    )


def run_judge(
    ev: Evaluator,
    conversation: str,
    config: Config,
    token: str | None,
    output_files_text: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> EvalScore:
    """Run a judge evaluator against captured conversation + output files.

    Shared by the `analyze` command. Builds the judge prompt and samples the
    judge ``runner.judge_samples`` times (self-consistency), disabling OTel so
    judge calls don't contaminate eval traces. Successful samples are aggregated
    via ``runner.judge_aggregate`` (median/mean/majority); the per-sample spread
    (stddev) and outcome counts are recorded for reliability reporting. Judge
    runtime metadata (host Copilot version, returncode/stderr, version mismatch,
    and caller-supplied truncation flags) is recorded on the returned score so
    the analyze report can surface reproducibility issues.
    """
    secrets = collect_secrets(config, token)
    conversation = mask_secrets(conversation, secrets) or ""
    output_files_text = mask_secrets(output_files_text, secrets)
    prompt = (
        f"You are an eval judge. Score the following Copilot output.\n\n"
        f"{ev.prompt}\n\n"
        f"{_judge_sections(conversation, output_files_text)}\n\n"
        f'Output ONLY valid JSON: {{"score": N, "reason": "..."}}'
    )

    version = host_copilot_version()
    base_meta = _judge_base_meta(config, extra_meta, version)

    n = max(1, config.runner.judge_samples)
    per_sample: list[_SampleResult] = []
    samples: list[int] = []
    outcomes: dict[str, int] = {}
    for _ in range(n):
        score, reason, outcome, smeta = _run_judge_once(prompt, config, token, secrets)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        per_sample.append((score, mask_secrets(reason, secrets) or reason, outcome, smeta))
        if score is not None:
            samples.append(score)

    return _finalize_judge_score(ev, per_sample, samples, outcomes, n, base_meta, version, config)


def _run_judges_batch_once(
    prompt: str, names: list[str], config: Config, token: str | None, secrets: list[str]
) -> tuple[dict[str, tuple[int | None, str, str]], dict[str, Any]]:
    """Invoke the judge Copilot once for *all* criteria in ``names``.

    Returns ``(results, sample_meta)`` where ``results`` maps each evaluator name
    to ``(score, reason, outcome)``. A process-level failure (timeout/error) or an
    unparseable top-level response is applied to *every* criterion (the batching
    failure blast radius). When the top-level object parses, a missing key or bad
    score fails only that individual criterion.
    """
    cmd = ["copilot", "-p", prompt, "-s"]
    if config.runner.judge_model:
        cmd.extend(["--model", config.runner.judge_model])
    judge_env = {**os.environ, "GITHUB_TOKEN": token or "", "COPILOT_OTEL_ENABLED": "false"}
    sample_meta: dict[str, Any] = {}

    def _all(
        outcome: str, reason: str
    ) -> tuple[dict[str, tuple[int | None, str, str]], dict[str, Any]]:
        return {name: (None, reason, outcome) for name in names}, sample_meta

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.runner.judge_timeout_seconds,
            env=judge_env,
        )
    except subprocess.TimeoutExpired:
        return _all("timeout", f"timeout after {config.runner.judge_timeout_seconds}s")
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

    # A parseable verdict from a non-zero exit is suspicious: keep scores but flag
    # the anomaly so it isn't counted as a clean run.
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


def run_judges_batch(
    evaluators: list[Evaluator],
    conversation: str,
    config: Config,
    token: str | None,
    output_files_text: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> list[EvalScore]:
    """Score *all* of a task's judge evaluators in a single LLM call per sample.

    Opt-in optimization (``runner.judge_batch``). Instead of one Copilot call per
    evaluator, one call scores every criterion at once, returning a JSON object
    keyed by evaluator name. The response is split back into per-evaluator scores
    that are byte-compatible with :func:`run_judge`, so the report layer is
    untouched. Calls drop from ``n_judges × judge_samples`` to ``judge_samples``.

    This trades judge independence for cost: criteria can cross-contaminate (halo
    effect), a single parse failure fails every criterion, and per-criterion noise
    within a sample becomes correlated. Keep it off (default) when accuracy matters.

    A single evaluator is delegated to :func:`run_judge` since there is nothing to
    batch.
    """
    if len(evaluators) == 1:
        return [
            run_judge(evaluators[0], conversation, config, token, output_files_text, extra_meta)
        ]

    secrets = collect_secrets(config, token)
    conversation = mask_secrets(conversation, secrets) or ""
    output_files_text = mask_secrets(output_files_text, secrets)
    criteria = "\n\n".join(f"### {ev.name}\n{ev.prompt}" for ev in evaluators)
    example = ", ".join(f'"{ev.name}": {{"score": N, "reason": "..."}}' for ev in evaluators)
    prompt = (
        f"You are an eval judge. Score the following Copilot output against "
        f"MULTIPLE independent criteria. Judge each criterion strictly on its own "
        f"merits; do not let one criterion's score influence another.\n\n"
        f"Criteria:\n{criteria}\n\n"
        f"{_judge_sections(conversation, output_files_text)}\n\n"
        f"Output ONLY valid JSON mapping each criterion name to its verdict: "
        f"{{{example}}}"
    )

    names = [ev.name for ev in evaluators]
    version = host_copilot_version()
    base_meta = _judge_base_meta(config, extra_meta, version)

    n = max(1, config.runner.judge_samples)
    per_sample: dict[str, list[_SampleResult]] = {name: [] for name in names}
    samples: dict[str, list[int]] = {name: [] for name in names}
    outcomes: dict[str, dict[str, int]] = {name: {} for name in names}
    for _ in range(n):
        results, smeta = _run_judges_batch_once(prompt, names, config, token, secrets)
        for name in names:
            score, reason, outcome = results[name]
            outcomes[name][outcome] = outcomes[name].get(outcome, 0) + 1
            per_sample[name].append(
                (score, mask_secrets(reason, secrets) or reason, outcome, smeta)
            )
            if score is not None:
                samples[name].append(score)

    return [
        _finalize_judge_score(
            ev,
            per_sample[ev.name],
            samples[ev.name],
            outcomes[ev.name],
            n,
            base_meta,
            version,
            config,
        )
        for ev in evaluators
    ]


def _eval_script(
    ev: Evaluator, config: Config, task: Task, variant: Variant, log_file: Path
) -> EvalScore | None:
    if not ev.script:
        return None
    resolved = (config.config_dir / ev.script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / ev.script).resolve()
    if not resolved.exists():
        return None
    logger.info("Evaluating: %s (script)...", ev.name)
    merged_vars = config.resolve_vars(task, variant)
    env = {
        **os.environ,
        **_load_env_file(config.env_file),
        **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()},
    }
    with open(log_file, "a") as lf:
        proc = subprocess.run([str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)
    passed = proc.returncode == 0
    return EvalScore(
        name=ev.name,
        type="script",
        score=1 if passed else 0,
        reason="PASS" if passed else "FAIL",
        passed=passed,
    )


def _eval_contains(ev: Evaluator, log_file: Path) -> EvalScore | None:
    if not ev.value:
        return None
    output = _read_log(log_file)
    found = ev.value in (output or "")
    return EvalScore(
        name=ev.name,
        type="contains",
        score=1 if found else 0,
        reason=f"{'found' if found else 'not found'}",
        passed=found,
    )


def _eval_regex(ev: Evaluator, log_file: Path) -> EvalScore | None:
    if not ev.value:
        return None
    output = _read_log(log_file)
    match = bool(re.search(ev.value, output or ""))
    return EvalScore(
        name=ev.name,
        type="regex",
        score=1 if match else 0,
        reason=f"{'matched' if match else 'no match'}",
        passed=match,
    )


_METRIC_OP_FUNCS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


def eval_metric(ev: Evaluator, metrics: RunMetrics) -> EvalScore:
    """Score a type=metric evaluator by thresholding a RunMetrics field.

    Deterministic pass/fail (1/0), evaluated from parsed telemetry at ``analyze``
    time. Returns ``score=None`` (and ``passed=False``) when the metric value
    can't be derived from the trace (e.g. a non-numeric field), mirroring how
    judges surface an unusable score rather than silently passing. Note that an
    absent ``github.copilot.cost`` tag currently parses to ``0.0`` (a real float),
    so cost does not reach this None path from real telemetry today.
    """
    metric_name = ev.metric or ""
    value = metric_value(metrics, metric_name)
    if value is None:
        return EvalScore(
            name=ev.name,
            type="metric",
            score=None,
            reason=f"metric '{metric_name}' unavailable in trace",
            passed=False,
        )
    op = ev.op or ""
    threshold = ev.threshold if ev.threshold is not None else 0.0
    op_func = _METRIC_OP_FUNCS[op]
    passed = op_func(value, threshold)
    reason = f"{metric_name}={value:g} {op} {threshold:g} → {'PASS' if passed else 'FAIL'}"
    return EvalScore(
        name=ev.name, type="metric", score=1 if passed else 0, reason=reason, passed=passed
    )


def read_files_from_dir(directory: Path | None, max_chars: int = 8000) -> str | None:
    """Read all files under a directory, concatenated with per-file headers.

    When the combined content exceeds ``max_chars`` the output is truncated, but
    truncation is never silent: a trailing ``... (truncated)`` marker is always
    emitted and any fully omitted files are listed by name so the judge prompt
    (and the report's truncation metadata) reflects that evidence was dropped.
    """
    if not directory or not directory.is_dir():
        return None
    files = [f for f in sorted(directory.rglob("*")) if f.is_file()]
    parts: list[str] = []
    total = 0
    for i, f in enumerate(files):
        rel = f.relative_to(directory)
        try:
            content = f.read_text(errors="replace")
        except OSError:
            continue
        if total + len(content) > max_chars:
            remaining = max_chars - total
            if remaining > 0:
                parts.append(f"=== {rel.as_posix()} ===\n{content[:remaining]}")
            omitted = [
                g.relative_to(directory).as_posix()
                for g in files[i + (1 if remaining > 0 else 0) :]
            ]
            if omitted:
                parts.append(f"[omitted {len(omitted)} file(s): {', '.join(omitted)}]")
            parts.append("... (truncated)")
            break
        parts.append(f"=== {rel.as_posix()} ===\n{content}")
        total += len(content)
    return "\n\n".join(parts) if parts else None


def _read_output_files(work_dir: Path | None, max_chars: int = 8000) -> str | None:
    """Read all files from work_dir/output/ and return as a concatenated string."""
    if not work_dir:
        return None
    return read_files_from_dir(work_dir / "output", max_chars)


def _read_log(log_file: Path, max_chars: int = 0) -> str | None:
    try:
        text = log_file.read_text()
        return (
            text[:max_chars] + "\n... (truncated)" if max_chars and len(text) > max_chars else text
        )
    except OSError:
        return None


def _print_summary(log_file: Path) -> None:
    try:
        for line in log_file.read_text().splitlines():
            if line.startswith("Total ") or line.startswith("Breakdown"):
                logger.info("%s", line)
    except OSError:
        pass


def _print_scores(scores: list[EvalScore]) -> None:
    for s in scores:
        icon = "✓" if s.passed else "✗"
        score_str = str(s.score) if s.score is not None else "?"
        logger.info("%s %s (%s): %s — %s", icon, s.name, s.type, score_str, s.reason[:50])


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


def _strip_quotes(value: str) -> str:
    """Backward-compatible alias for :func:`eval.env_utils.strip_quotes`."""
    return strip_quotes(value)


def mask_secrets(text: str | None, secrets: list[str]) -> str | None:
    """Replace any occurrence of a secret value in ``text`` with a placeholder."""
    if not text or not secrets:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, _SECRET_PLACEHOLDER)
    return text


def _write_sanitized_env_file(config: Config) -> Path:
    """Backward-compatible alias for :func:`eval.env_utils.write_sanitized_env_file`."""
    return write_sanitized_env_file(config)


def _mask_log_file(log_file: Path, secrets: list[str]) -> None:
    """Rewrite ``log_file`` in place with secret values redacted."""
    if not secrets:
        return
    try:
        text = log_file.read_text()
    except OSError:
        return
    masked = mask_secrets(text, secrets)
    if masked is not None and masked != text:
        try:
            log_file.write_text(masked)
        except OSError:
            pass
