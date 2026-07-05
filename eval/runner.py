"""Execute a single eval run in a Docker container."""

from __future__ import annotations

import json
import operator
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from logging import getLogger
from pathlib import Path
from typing import Any

from eval.collectors import create_collector
from eval.collectors.file_collector import TRACE_FILE
from eval.config import Config, ConfigError, Evaluator, Task, Variant
from eval.env_utils import (
    collect_secrets,
    mask_secrets,
    strip_quotes,
    write_sanitized_env_file,
)
from eval.env_utils import (
    load_env_file as _load_env_file,
)
from eval.exceptions import AuthError, DockerError, EvalError, FixtureError, HookError
from eval.judge_executor import JudgeContext, JudgeExecutor
from eval.judge_executor import _aggregate_scores as _aggregate_scores
from eval.judge_executor import _parse_json as _parse_json
from eval.judge_executor import host_copilot_version as host_copilot_version
from eval.naming import run_slug
from eval.protocols import (
    AgentRunner,
    EvalContext,
    RunArtifacts,
    RunContext,
    RunStatus,
)
from eval.protocols import (
    EvalScore as EvalScore,
)
from eval.protocols import (
    score_to_dict as score_to_dict,
)
from eval.protocols import (
    status_from_exit_code as _status_from_exit_code,
)
from eval.services.fixtures_service import resolve_fixture_dir
from eval.trace import RunMetrics, metric_value

logger = getLogger(__name__)

status_from_exit_code = _status_from_exit_code
# EvalScore/score_to_dict now live in eval.protocols (alongside the Evaluator
# protocol + EvalContext); re-exported here for backward compatibility since
# most callers import them from eval.runner.


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
    # Number of transient-failure retries performed before this result was
    # produced (0 = succeeded/failed on the first attempt). See issue #69.
    retry_count: int = 0

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
            "retry_count": self.retry_count,
            "scores": [score_to_dict(s) for s in self.scores],
        }


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
        raise AuthError("GITHUB_TOKEN not set and gh CLI not authenticated") from exc


def _build_default_runner(config: Config, github_token: str) -> AgentRunner:
    """Instantiate the configured runner backend (`runner.backend`, default
    `docker`) via the plugin registry instead of hardcoding DockerCLIRunner.

    `eval.config._build_runner` already validates `runner.backend` against
    `RUNNER_REGISTRY` at config-load time, so this only needs to guard against
    the (unlikely) case of a backend being removed from the registry between
    config load and run — e.g. a plugin unloaded mid-process. Local import:
    `eval.runners.docker_cli_runner` imports from `eval.config`, so importing
    `eval.runners` at module scope here would create a cycle.
    """
    from eval.runners import RUNNER_REGISTRY

    runner_cls = RUNNER_REGISTRY.get(config.runner.backend)
    if runner_cls is None:
        supported = ", ".join(sorted(RUNNER_REGISTRY))
        raise ConfigError(
            f"Unknown runner.backend '{config.runner.backend}'. Available: {supported}."
        )
    return runner_cls(github_token)  # type: ignore[call-arg]  # AgentRunner Protocol has no __init__ signature


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
    runner: AgentRunner | None = None,
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

    # `runner` is injectable (see AgentRunner protocol) so callers -- notably
    # unit tests -- can exercise run_one's setup/hook/evaluator orchestration
    # without a live Docker daemon; production callers rely on the default,
    # which is selected via `runner.backend` (see eval.runners, issue #66)
    # instead of being hardcoded to DockerCLIRunner.
    runner = runner or _build_default_runner(config, github_token)

    # Fail fast on invalid runner/collector combinations before doing any setup
    # work, so a misconfiguration surfaces as a clear ConfigError instead of a
    # confusing runtime failure later in the run.
    if config.runner.collector not in runner.supported_collectors:
        supported = ", ".join(runner.supported_collectors)
        raise ConfigError(
            f"Runner '{type(runner).__name__}' does not support collector "
            f"'{config.runner.collector}'. Supported collectors: {supported}."
        )

    # Retry budget for transient failures (DockerError, container timeout) --
    # see issue #69. `attempt` is the 0-based index of the current try; it is
    # also recorded as `retry_count` on the final RunResult so `analyze` can
    # tell a flaky-but-eventually-passing run from a clean first try.
    max_retries = config.runner.retries
    retry_delay = config.runner.retry_delay
    attempt = 0
    while True:
        # Tracked across each attempt so the inner `finally` can always clean up
        # and redact secrets, even on early returns (e.g. setup_failed) or
        # exceptions.
        work_dir: Path | None = None
        scores: list[EvalScore] = []
        artifacts: RunArtifacts | None = None
        container_run_completed = False
        try:
            before_rc = _run_hook(
                task.hooks.before_run, config, task, variant, log_file, "before_run"
            )
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
                        retry_count=attempt,
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
                        retry_count=attempt,
                    )

            # Writable workspace: copy fixture to tmpdir so Copilot can read AND write.
            # Remote fixtures (issue #122) resolve to their content-addressed
            # extracted cache dir (already materialized before the run); local
            # fixtures resolve to fixtures/<name>. resolve_fixture_dir returns
            # None when a local fixture directory is simply absent.
            work_dir = Path(tempfile.mkdtemp(prefix="eval-work-"))
            fixture_dir = resolve_fixture_dir(config, fixture_dir_name, task.remote_fixtures)
            if fixture_dir is not None and fixture_dir.is_dir():
                try:
                    shutil.copytree(fixture_dir, work_dir, dirs_exist_ok=True)
                except OSError as exc:
                    raise FixtureError(f"failed to copy fixture '{fixture_dir}': {exc}") from exc
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

            if getattr(runner, "is_synthetic", False):
                logger.info("Replaying pre-recorded run (offline replay runner)...")
            else:
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
        except DockerError as exc:
            # A DockerError can currently only originate from the `runner.run()`
            # call above (container execution), i.e. before the container produced
            # any artifacts -- so it's always a setup-stage failure.
            logger.error("Docker error: %s", exc)
            _append_log(log_file, f"run_one raised a DockerError: {exc}")
            if attempt < max_retries:
                delay = min(retry_delay * (2**attempt), 60.0)
                logger.warning(
                    "Retrying after transient DockerError (attempt %s/%s): %s "
                    "— waiting %.1fs before retry",
                    attempt + 1,
                    max_retries,
                    exc,
                    delay,
                )
                _append_log(
                    log_file,
                    f"retrying (attempt {attempt + 1}/{max_retries}) after DockerError: "
                    f"{exc}; waiting {delay:.1f}s",
                )
                time.sleep(delay)
                attempt += 1
                continue
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
                retry_count=attempt,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("Run timed out: %s", exc)
            _append_log(log_file, f"run_one timed out: {exc}")
            if attempt < max_retries:
                delay = min(retry_delay * (2**attempt), 60.0)
                logger.warning(
                    "Retrying after timeout (attempt %s/%s): %s — waiting %.1fs before retry",
                    attempt + 1,
                    max_retries,
                    exc,
                    delay,
                )
                _append_log(
                    log_file,
                    f"retrying (attempt {attempt + 1}/{max_retries}) after timeout: "
                    f"{exc}; waiting {delay:.1f}s",
                )
                time.sleep(delay)
                attempt += 1
                continue
            return RunResult(
                task=task.name,
                variant=variant.name,
                epoch=epoch,
                test_id=test_id,
                run_id=run_id,
                log_file=log_file,
                exit_code=124,
                status=RunStatus.TIMEOUT,
                scores=scores,
                **_timing(),
                fixture=fixture_label,
                retry_count=attempt,
            )
        except EvalError as exc:
            # A failure before the container ran is a setup problem; a failure during
            # post-processing (persist/evaluators/after_run) happened after a real run,
            # so preserve the container's exit status instead of mislabeling it.
            if not container_run_completed or artifacts is None:
                logger.error("Eval error during setup: %s", exc)
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
                    retry_count=attempt,
                )
            logger.error("Eval error during post-processing: %s", exc)
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
                retry_count=attempt,
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
            retry_count=attempt,
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
        try:
            proc = subprocess.run(
                ["bash", str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env
            )
        except OSError as exc:
            raise HookError(f"{label} script '{resolved}' failed to execute: {exc}") from exc
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
        try:
            proc = subprocess.run(
                ["bash", str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env
            )
        except OSError as exc:
            raise HookError(f"health_check script '{resolved}' failed to execute: {exc}") from exc
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


# Evaluator types scored later, during `analyze`, instead of inline here:
# judge needs the captured trace/log text assembled from OTel data, and metric
# needs telemetry parsed from the exported trace — neither is available yet at
# this point in `run_one` (see eval.cli._run_judges / _run_metric_evaluators).
# Every *other* registered type — including script/contains/regex and any
# third-party type registered via entry points (see issue #66) — is assumed to
# be inline-capable (scoreable from just the log file right after the run), so
# it's picked up here automatically without this function needing changes.
_DEFERRED_EVALUATOR_TYPES = ("judge", "metric")


def _run_evaluators(
    task: Task,
    variant: Variant,
    config: Config,
    log_file: Path,
    token: str,
    work_dir: Path | None = None,
) -> list[EvalScore]:
    """Run inline evaluators (script, contains, regex, ...) via the Evaluator registry.

    Dispatch is a registry lookup (`eval.evaluators.EVALUATOR_REGISTRY`)
    keyed by `ev.type`, rather than an if/elif chain, so new evaluator types
    — including third-party ones registered via entry points — don't require
    touching this function (see issue #78/#66).
    """
    # Local import: eval.evaluators' judge/metric classes call back into this
    # module's run_judge/eval_metric helpers, so importing it at module scope
    # here would create an eval.runner <-> eval.evaluators import cycle.
    from eval.evaluators import EVALUATOR_REGISTRY

    scores: list[EvalScore] = []
    for ev in task.evaluators:
        if ev.type in _DEFERRED_EVALUATOR_TYPES:
            continue  # judge/metric evaluators run in `analyze`
        evaluator_cls = EVALUATOR_REGISTRY.get(ev.type)
        if evaluator_cls is None:
            continue
        context = EvalContext(
            evaluator=ev,
            config=config,
            task=task,
            variant=variant,
            log_file=log_file,
            work_dir=work_dir,
            token=token,
        )
        s = evaluator_cls.from_config(ev).evaluate(context)
        if s:
            scores.append(s)
    if scores:
        _write_scores_file(log_file, scores)
    return scores


def _write_scores_file(log_file: Path, scores: list[EvalScore]) -> None:
    """Persist scores next to the run log as `<log>.scores.json`."""
    if not scores:
        return
    sf = log_file.with_suffix(".scores.json")
    sf.write_text(json.dumps([s.to_dict() for s in scores], indent=2, ensure_ascii=False))


def run_judge(
    ev: Evaluator,
    conversation: str,
    config: Config,
    token: str | None,
    output_files_text: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> EvalScore:
    """Run a judge evaluator against captured conversation + output files.

    Thin wrapper around :class:`eval.judge_executor.JudgeExecutor` (see #80),
    which owns prompt construction, the Copilot CLI call, response parsing,
    and self-consistency sampling. Shared by the `analyze` command.
    """
    executor = JudgeExecutor(config)
    context = JudgeContext(
        conversation=conversation,
        output_files_text=output_files_text,
        token=token,
        extra_meta=extra_meta,
    )
    return executor.execute_single(ev, context)


def run_judges_batch(
    evaluators: list[Evaluator],
    conversation: str,
    config: Config,
    token: str | None,
    output_files_text: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> list[EvalScore]:
    """Score *all* of a task's judge evaluators in a single LLM call per sample.

    Thin wrapper around :class:`eval.judge_executor.JudgeExecutor` (see #80).
    Opt-in optimization (``runner.judge_batch``): instead of one Copilot call
    per evaluator, one call scores every criterion at once. Trades judge
    independence (halo effect, shared failure blast radius, correlated noise)
    for cost -- keep it off (default) when accuracy matters. A single
    evaluator is delegated to :func:`run_judge` since there is nothing to
    batch.
    """
    executor = JudgeExecutor(config)
    context = JudgeContext(
        conversation=conversation,
        output_files_text=output_files_text,
        token=token,
        extra_meta=extra_meta,
    )
    return executor.execute_batch(evaluators, context)


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
    can't be derived from the trace, mirroring how judges surface an unusable
    score rather than silently passing. This includes any telemetry-tag-backed
    gate whose tag was absent on a partial trace: ``cost`` (absent or the ``"?"``
    sentinel) and the #121 integer-tag metrics — ``turn_count`` and the token
    aggregates (``total_input_tokens`` / ``total_output_tokens`` /
    ``total_cache_tokens`` / ``total_tokens``). Each is rendered as its coerced
    ``0`` for reporting, but its availability flag is False, so the accessor
    yields ``None`` and the gate fails CLOSED instead of passing a ``<=``/``<``
    threshold on missing telemetry.
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


def _strip_quotes(value: str) -> str:
    """Backward-compatible alias for :func:`eval.env_utils.strip_quotes`."""
    return strip_quotes(value)


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
