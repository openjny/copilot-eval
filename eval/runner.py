"""Execute a single eval run in a Docker container."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from eval.config import Config, Evaluator, Task, Variant


@dataclass
class EvalScore:
    name: str
    type: str
    score: int | None
    reason: str = ""
    passed: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name, "type": self.type, "score": self.score,
            "reason": self.reason, "passed": self.passed,
        }
        if self.meta:
            d["meta"] = self.meta
        return d


@dataclass
class RunResult:
    task: str
    variant: str
    epoch: int
    test_id: str
    run_id: str
    log_file: Path
    exit_code: int
    status: str = "completed"  # completed | setup_failed | timeout | failed
    scores: list[EvalScore] = field(default_factory=list)
    order_index: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None

    @property
    def passed(self) -> bool:
        return self.status == "completed" and (all(s.passed for s in self.scores) if self.scores else True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "variant": self.variant,
            "epoch": self.epoch,
            "test_id": self.test_id,
            "run_id": self.run_id,
            "exit_code": self.exit_code,
            "status": self.status,
            "passed": self.passed,
            "order_index": self.order_index,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "scores": [s.to_dict() for s in self.scores],
        }


def status_from_exit_code(exit_code: int) -> str:
    """Map a process exit code to a run status.

    124 is GNU `timeout`'s signal that the command was killed for exceeding
    its time budget; any other non-zero code indicates a failed run.
    """
    if exit_code == 0:
        return "completed"
    if exit_code == 124:
        return "timeout"
    return "failed"


def get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError("GITHUB_TOKEN not set and gh CLI not authenticated") from exc


def run_one(
    task: Task, variant: Variant, epoch: int,
    config: Config, run_id: str, run_dir: Path, github_token: str,
    order_index: int | None = None,
) -> RunResult:
    test_id = str(uuid.uuid4())
    log_file = run_dir / f"{task.name}_{variant.name}_epoch{epoch}.log"
    print(f"--- [{task.name}] epoch={epoch} variant={variant.name} test_id={test_id[:8]}")

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

    # Tracked across the run so the outer `finally` can always clean up and
    # redact secrets, even on early returns (e.g. setup_failed) or exceptions.
    env_file_arg: Path | None = None
    work_dir: Path | None = None
    scores: list[EvalScore] = []
    proc: subprocess.CompletedProcess[bytes] | None = None
    try:
        before_rc = _run_hook(task.hooks.before_run, config, task, variant, log_file, "before_run")
        if before_rc != 0:
            if task.hooks.on_failure == "fail":
                print(f"    ✗ before_run hook failed (exit {before_rc}) — skipping run")
                _append_log(log_file, f"before_run hook failed with exit code {before_rc}")
                return RunResult(
                    task=task.name, variant=variant.name, epoch=epoch,
                    test_id=test_id, run_id=run_id, log_file=log_file,
                    exit_code=-1, status="setup_failed",
                )
            print(f"    WARNING: before_run hook failed (exit {before_rc}) — continuing (on_failure=warn)")
            _append_log(log_file, f"before_run hook failed with exit code {before_rc}; continuing because hooks.on_failure=warn")

        # Health check: verify environment is ready before running Copilot
        if task.health_check:
            if not _run_health_check(task.health_check, config, task, variant, log_file):
                print("    ✗ Health check failed — skipping run")
                return RunResult(
                    task=task.name, variant=variant.name, epoch=epoch,
                    test_id=test_id, run_id=run_id, log_file=log_file,
                    exit_code=-1, status="setup_failed", **_timing(),
                )

        prompt = config.resolve_prompt(task, variant)
        image = config.image_name(variant)
        otel_attrs = ",".join([
            f"eval.test_id={test_id}", f"eval.scenario={task.name}",
            f"eval.variant={variant.name}", f"eval.epoch={epoch}", f"eval.run_id={run_id}",
        ])
        # Build a sanitized env file (quotes stripped) so the container sees the
        # same values as hooks/evaluators. Values are passed via --env-file rather
        # than -e KEY=value so they never appear in argv (`ps` leakage).
        env_file_arg = _write_sanitized_env_file(config)
        cmd = [
            "docker", "run", "--rm", "--add-host=host.docker.internal:host-gateway",
            "--env-file", str(env_file_arg),
            "-e", "GITHUB_TOKEN",
            "-e", "COPILOT_OTEL_ENABLED=true",
            "-e", f"COPILOT_OTEL_CAPTURE_CONTENT={'true' if config.runner.capture_content else 'false'}",
            "-e", f"OTEL_EXPORTER_OTLP_ENDPOINT={config.runner.otel_endpoint}",
            "-e", f"OTEL_RESOURCE_ATTRIBUTES={otel_attrs}",
            "-e", "OTEL_SERVICE_NAME=github-copilot",
        ]
        copilot_home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")).resolve()
        if copilot_home.is_dir():
            cmd.extend(["-v", f"{copilot_home}:/copilot-home-src:ro"])

        # Writable workspace: copy fixture to tmpdir so Copilot can read AND write
        work_dir = Path(tempfile.mkdtemp(prefix="eval-work-"))
        fixture_dir = (config.config_dir / "fixtures" / (task.fixture or task.name)).resolve()
        if fixture_dir.is_dir():
            shutil.copytree(fixture_dir, work_dir, dirs_exist_ok=True)
        # Create output dir for Copilot to write artifacts (used by judge evaluator)
        (work_dir / "output").mkdir(exist_ok=True)
        cmd.extend(["-v", f"{work_dir}:/workspace"])

        if variant.run_script:
            rsp = (config.project_dir / variant.run_script).resolve()
            if rsp.exists():
                cmd.extend(["-v", f"{rsp}:/tmp/eval-setup.sh:ro", "-e", "EVAL_SETUP_SCRIPT=/tmp/eval-setup.sh"])

        copilot_args = ["copilot", "-p", prompt, "--yolo"]
        model = variant.model or config.runner.model
        if model:
            copilot_args.extend(["--model", model])
        if config.runner.reasoning_effort:
            copilot_args.extend(["--effort", config.runner.reasoning_effort])
        if config.runner.max_turns:
            copilot_args.extend(["--max-autopilot-continues", str(config.runner.max_turns)])
        if config.runner.output_format == "json":
            copilot_args.extend(["--output-format", "json"])
        timeout = task.timeout_seconds or config.runner.timeout_seconds
        cmd.extend([image, "timeout", f"{timeout}s", *copilot_args])

        print("    Running copilot in container...")
        # Pass GITHUB_TOKEN through the process environment rather than embedding the
        # value in argv, so it does not leak via `ps` / process-args listings.
        # (Residual exposure via `docker inspect` requires Docker socket access.)
        run_env = {**os.environ, "GITHUB_TOKEN": github_token}
        with open(log_file, "a") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=run_env)
        _print_summary(log_file)

        after_rc = _run_hook(task.hooks.after_run, config, task, variant, log_file, "after_run")
        if after_rc != 0:
            print(f"    WARNING: after_run hook failed (exit {after_rc}) — surfacing in results")
            _append_log(log_file, f"after_run hook failed with exit code {after_rc}")
            scores.append(EvalScore(
                name="after_run_hook", type="hook", score=None,
                reason=f"after_run hook failed with exit code {after_rc}", passed=False,
            ))

        # Persist output files to results dir before tmpdir cleanup
        _persist_output_files(work_dir, run_dir, task.name, variant.name, epoch)

        scores.extend(_run_evaluators(task, variant, config, log_file, github_token, work_dir))
        # Persist the full score set (hook + evaluator scores) so later analysis
        # sees hook failures too, not just evaluator-produced scores.
        _write_scores_file(log_file, scores)
        _print_scores(scores)
    except Exception as exc:  # noqa: BLE001 - isolate per-run failures from the batch
        # A failure before the container ran is a setup problem; a failure during
        # post-processing (persist/evaluators/after_run) happened after a real run,
        # so preserve the container's exit status instead of mislabeling it.
        if proc is None:
            print(f"    ✗ Run errored during setup: {exc}")
            _append_log(log_file, f"run_one raised during setup: {exc!r}")
            return RunResult(
                task=task.name, variant=variant.name, epoch=epoch,
                test_id=test_id, run_id=run_id, log_file=log_file,
                exit_code=-1, status="setup_failed", scores=scores,
            )
        print(f"    ✗ Run errored during post-processing: {exc}")
        _append_log(log_file, f"run_one raised during post-processing: {exc!r}")
        scores.append(EvalScore(
            name="post_processing", type="infra", score=None,
            reason=f"post-run exception: {exc!r}", passed=False,
        ))
        return RunResult(
            task=task.name, variant=variant.name, epoch=epoch,
            test_id=test_id, run_id=run_id, log_file=log_file,
            exit_code=proc.returncode, status=status_from_exit_code(proc.returncode),
            scores=scores,
        )
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        if env_file_arg is not None:
            env_file_arg.unlink(missing_ok=True)
        # Redact secret values from the persisted log. Done in `finally` (after
        # evaluators, which read the raw log for contains/regex) so it also
        # covers early returns and exceptions, and never skews evaluator results.
        _mask_log_file(log_file, collect_secrets(config, github_token))

    return RunResult(
        task=task.name, variant=variant.name, epoch=epoch,
        test_id=test_id, run_id=run_id, log_file=log_file,
        exit_code=proc.returncode, status=status_from_exit_code(proc.returncode),
        scores=scores, **_timing(),
    )


def _run_hook(script: str | None, config: Config, task: Task, variant: Variant, log_file: Path, label: str) -> int:
    """Run a before/after hook script. Returns the script's exit code (0 when no
    script is configured or the script is missing, so a missing hook never fails)."""
    if not script:
        return 0
    resolved = (config.config_dir / script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / script).resolve()
    if not resolved.exists():
        print(f"    WARNING: {label} script not found: {script}")
        return 0
    print(f"    Running {label}...")
    merged_vars = config.resolve_vars(task, variant)
    env = {**os.environ, **_load_env_file(config.env_file), **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()}}
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


def _run_health_check(script: str, config: Config, task: Task, variant: Variant, log_file: Path) -> bool:
    """Run health check script. Returns True if environment is ready."""
    resolved = (config.config_dir / script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / script).resolve()
    if not resolved.exists():
        print(f"    WARNING: health_check script not found: {script}")
        return True  # skip check if script missing
    print("    Running health_check...")
    merged_vars = config.resolve_vars(task, variant)
    env = {**os.environ, **_load_env_file(config.env_file), **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()}}
    with open(log_file, "a") as lf:
        proc = subprocess.run(["bash", str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)
    return proc.returncode == 0


def _persist_output_files(work_dir: Path, run_dir: Path, task: str, variant: str, epoch: int) -> None:
    """Copy output files from tmpdir to results dir for later analysis."""
    output_dir = work_dir / "output"
    if not output_dir.is_dir():
        return
    files = [f for f in output_dir.rglob("*") if f.is_file()]
    if not files:
        return
    dest = run_dir / "outputs" / f"{task}_{variant}_epoch{epoch}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        rel = f.relative_to(output_dir)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)


def _run_evaluators(task: Task, variant: Variant, config: Config, log_file: Path, token: str, work_dir: Path | None = None) -> list[EvalScore]:
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


def run_judge(ev: Evaluator, conversation: str, config: Config, token: str,
              output_files_text: str | None = None,
              extra_meta: dict[str, Any] | None = None) -> EvalScore:
    """Run a single judge evaluator against captured conversation + output files.

    Shared by the `analyze` command. Builds the judge prompt, invokes Copilot
    (with OTel disabled so judge calls don't contaminate eval traces), and parses
    the JSON verdict. Records judge-runtime metadata (host Copilot version,
    process returncode/stderr, and any caller-supplied truncation flags) on the
    returned score so the analyze report can surface reproducibility issues.
    """
    secrets = collect_secrets(config, token)
    conversation = mask_secrets(conversation, secrets) or ""
    output_files_text = mask_secrets(output_files_text, secrets)
    sections = [f"--- COPILOT OUTPUT ---\n{conversation}\n--- END OUTPUT ---"]
    if output_files_text:
        sections.append(f"--- OUTPUT FILES ---\n{output_files_text}\n--- END FILES ---")
    prompt = (
        f"You are an eval judge. Score the following Copilot output.\n\n"
        f"{ev.prompt}\n\n"
        f"{chr(10).join(sections)}\n\n"
        f'Output ONLY valid JSON: {{"score": N, "reason": "..."}}'
    )
    cmd = ["copilot", "-p", prompt, "-s"]
    if config.runner.judge_model:
        cmd.extend(["--model", config.runner.judge_model])
    # Disable OTel to avoid contaminating eval traces with judge calls
    judge_env = {**os.environ, "GITHUB_TOKEN": token, "COPILOT_OTEL_ENABLED": "false"}

    version = host_copilot_version()
    meta: dict[str, Any] = {**(extra_meta or {})}
    if version:
        meta["judge_version"] = version
    expected = config.runner.judge_copilot_version
    # Record a mismatch when the host version differs from the configured
    # expectation — including the case where the host version is unavailable,
    # which is exactly when reproducibility is least observable.
    if expected and version != expected:
        meta["judge_version_mismatch"] = {"expected": expected, "actual": version}

    def _score(score: int | None, reason: str, outcome: str) -> EvalScore:
        meta["outcome"] = outcome
        return EvalScore(name=ev.name, type="judge", score=score,
                         reason=mask_secrets(reason, secrets) or reason, meta=meta)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=config.runner.judge_timeout_seconds, env=judge_env)
    except subprocess.TimeoutExpired:
        return _score(None, f"timeout after {config.runner.judge_timeout_seconds}s", "timeout")
    except FileNotFoundError:
        return _score(None, "copilot CLI not found on host", "not_found")

    meta["returncode"] = proc.returncode
    stderr = mask_secrets((proc.stderr or "").strip(), secrets) or ""
    if stderr:
        meta["stderr"] = stderr[:_STDERR_SNIPPET_CHARS]

    data = _parse_json(proc.stdout, require_keys=("score",))
    if data is not None:
        try:
            score = int(data.get("score", 0))
        except (TypeError, ValueError):
            return _score(None, f"invalid_score: {data.get('score')!r}", "invalid_score")
        # A parseable verdict from a process that exited non-zero is suspicious:
        # keep the score but flag the anomaly so it isn't counted as a clean run.
        outcome = "ok" if proc.returncode == 0 else "ok_nonzero"
        return _score(score, str(data.get("reason", "")), outcome)
    if proc.returncode != 0:
        detail = f" — {stderr[:200]}" if stderr else ""
        return _score(None, f"error: rc={proc.returncode}{detail}", "error")
    return _score(None, "parse_error", "parse_error")


def _eval_script(ev: Evaluator, config: Config, task: Task, variant: Variant, log_file: Path) -> EvalScore | None:
    if not ev.script:
        return None
    resolved = (config.config_dir / ev.script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / ev.script).resolve()
    if not resolved.exists():
        return None
    print(f"    Evaluating: {ev.name} (script)...")
    merged_vars = config.resolve_vars(task, variant)
    env = {**os.environ, **_load_env_file(config.env_file), **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()}}
    with open(log_file, "a") as lf:
        proc = subprocess.run([str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)
    passed = proc.returncode == 0
    return EvalScore(name=ev.name, type="script", score=1 if passed else 0, reason="PASS" if passed else "FAIL", passed=passed)


def _eval_contains(ev: Evaluator, log_file: Path) -> EvalScore | None:
    if not ev.value:
        return None
    output = _read_log(log_file)
    found = ev.value in (output or "")
    return EvalScore(name=ev.name, type="contains", score=1 if found else 0, reason=f"{'found' if found else 'not found'}", passed=found)


def _eval_regex(ev: Evaluator, log_file: Path) -> EvalScore | None:
    if not ev.value:
        return None
    output = _read_log(log_file)
    match = bool(re.search(ev.value, output or ""))
    return EvalScore(name=ev.name, type="regex", score=1 if match else 0, reason=f"{'matched' if match else 'no match'}", passed=match)


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
                parts.append(f"=== {rel} ===\n{content[:remaining]}")
            omitted = [str(g.relative_to(directory)) for g in files[i + (1 if remaining > 0 else 0):]]
            if omitted:
                parts.append(f"[omitted {len(omitted)} file(s): {', '.join(omitted)}]")
            parts.append("... (truncated)")
            break
        parts.append(f"=== {rel} ===\n{content}")
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
        return text[:max_chars] + "\n... (truncated)" if max_chars and len(text) > max_chars else text
    except OSError:
        return None


def _print_summary(log_file: Path) -> None:
    try:
        for line in log_file.read_text().splitlines():
            if line.startswith("Total ") or line.startswith("Breakdown"):
                print(f"    {line}")
    except OSError:
        pass


def _print_scores(scores: list[EvalScore]) -> None:
    for s in scores:
        icon = "✓" if s.passed else "✗"
        score_str = str(s.score) if s.score is not None else "?"
        print(f"    {icon} {s.name} ({s.type}): {score_str} — {s.reason[:50]}")


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
        candidates.append(stripped[start:end + 1])
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
    """Remove a single pair of matching surrounding quotes from a value.

    Mirrors standard dotenv semantics: ``KEY="value"`` and ``KEY='value'``
    yield ``value`` (without the quotes). Values without matching surrounding
    quotes are returned unchanged.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _load_env_file(env_file: Path) -> dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and empty lines.

    Surrounding matching quotes are stripped from values so hooks and
    evaluator scripts receive the same value the container sees (the sanitized
    env file passed to ``docker --env-file`` is built from this same parse).
    """
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = _strip_quotes(value.strip())
    return env


# Values shorter than this are not treated as secrets to mask, to avoid
# redacting trivial non-sensitive values like "1", "true", or short flags.
_MIN_SECRET_LEN = 6
_SECRET_PLACEHOLDER = "***REDACTED***"


def collect_secrets(config: Config, token: str | None = None) -> list[str]:
    """Collect secret values to redact from logs and judge input.

    Combines the values of the project's ``.env`` file with ``GITHUB_TOKEN``
    (and any explicitly provided ``token``). Short values are filtered out to
    avoid masking trivial, non-sensitive values.
    """
    candidates = list(_load_env_file(config.env_file).values())
    candidates.append(os.environ.get("GITHUB_TOKEN", ""))
    if token:
        candidates.append(token)
    secrets: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        value = (value or "").strip()
        if len(value) >= _MIN_SECRET_LEN and value not in seen:
            seen.add(value)
            secrets.append(value)
    # Mask longer values first so overlapping substrings are handled correctly.
    secrets.sort(key=len, reverse=True)
    return secrets


def mask_secrets(text: str | None, secrets: list[str]) -> str | None:
    """Replace any occurrence of a secret value in ``text`` with a placeholder."""
    if not text or not secrets:
        return text
    for secret in secrets:
        if secret:
            text = text.replace(secret, _SECRET_PLACEHOLDER)
    return text


def _write_sanitized_env_file(config: Config) -> Path:
    """Write a quote-stripped copy of the project's .env for ``docker --env-file``.

    Returns a temp file path (mode 0600) so the container receives the same
    normalized values as hooks/evaluators, without exposing them via argv. The
    caller is responsible for deleting the returned file. If no .env exists, an
    empty temp file is returned so ``--env-file`` still gets a valid path.
    """
    parsed = _load_env_file(config.env_file)
    fd, name = tempfile.mkstemp(prefix="eval-env-", suffix=".env")
    path = Path(name)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w") as f:
        for key, value in parsed.items():
            f.write(f"{key}={value}\n")
    return path


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
