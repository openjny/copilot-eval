"""Execute a single eval run in a Docker container."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

from eval.config import Config, Evaluator, Task, Variant


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
            "scores": [score_to_dict(s) for s in self.scores],
        }


def score_to_dict(s: EvalScore) -> dict[str, Any]:
    """Serialize an EvalScore to the *.scores.json schema.

    Base keys mirror the legacy shape; judge self-consistency metadata
    (samples, stddev, outcomes, model/version) is added only for judge scores
    so non-judge serialization stays byte-identical.
    """
    d: dict[str, Any] = {
        "name": s.name, "type": s.type, "score": s.score,
        "reason": s.reason, "passed": s.passed,
    }
    if s.type == "judge":
        d.update({
            "samples": s.samples,
            "score_stddev": s.score_stddev,
            "n_samples": s.n_samples,
            "outcomes": s.outcomes,
            "judge_model": s.judge_model,
            "judge_version": s.judge_version,
        })
    return d


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
) -> RunResult:
    test_id = str(uuid.uuid4())
    log_file = run_dir / f"{task.name}_{variant.name}_epoch{epoch}.log"
    print(f"--- [{task.name}] epoch={epoch} variant={variant.name} test_id={test_id[:8]}")

    # Tracked across the run so the outer `finally` can always clean up and
    # redact secrets, even on early returns (e.g. setup_failed) or exceptions.
    env_file_arg: Path | None = None
    work_dir: Path | None = None
    scores: list[EvalScore] = []
    proc: subprocess.CompletedProcess[bytes] | None = None
    try:
        _run_hook(task.hooks.before_run, config, task, variant, log_file, "before_run")

        # Health check: verify environment is ready before running Copilot
        if task.health_check:
            if not _run_health_check(task.health_check, config, task, variant, log_file):
                print("    ✗ Health check failed — skipping run")
                return RunResult(
                    task=task.name, variant=variant.name, epoch=epoch,
                    test_id=test_id, run_id=run_id, log_file=log_file,
                    exit_code=-1, status="setup_failed",
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

        _run_hook(task.hooks.after_run, config, task, variant, log_file, "after_run")

        # Persist output files to results dir before tmpdir cleanup
        _persist_output_files(work_dir, run_dir, task.name, variant.name, epoch)

        scores = _run_evaluators(task, variant, config, log_file, github_token, work_dir)
        _print_scores(scores)
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
        scores=scores,
    )


def _run_hook(script: str | None, config: Config, task: Task, variant: Variant, log_file: Path, label: str) -> None:
    if not script:
        return
    resolved = (config.config_dir / script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / script).resolve()
    if not resolved.exists():
        print(f"    WARNING: {label} script not found: {script}")
        return
    print(f"    Running {label}...")
    merged_vars = config.resolve_vars(task, variant)
    env = {**os.environ, **_load_env_file(config.env_file), **{f"EVAL_{k.upper()}": v for k, v in merged_vars.items()}}
    with open(log_file, "a") as lf:
        subprocess.run(["bash", str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)


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
        sf = log_file.with_suffix(".scores.json")
        sf.write_text(json.dumps(
            [score_to_dict(s) for s in scores],
            indent=2, ensure_ascii=False,
        ))
    return scores


_JUDGE_CLI_VERSION: str | None = None


def judge_cli_version() -> str:
    """Return the Copilot CLI version string, cached for the process.

    Recorded as judge metadata so scores can be tied to a specific CLI build.
    Returns "unknown" if the version can't be determined.
    """
    global _JUDGE_CLI_VERSION
    if _JUDGE_CLI_VERSION is not None:
        return _JUDGE_CLI_VERSION
    version = "unknown"
    try:
        proc = subprocess.run(["copilot", "--version"], capture_output=True, text=True, timeout=15)
        out = (proc.stdout or proc.stderr or "").strip()
        if out:
            version = out.splitlines()[0].strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        version = "unknown"
    _JUDGE_CLI_VERSION = version
    return version


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


def _run_judge_once(prompt: str, config: Config, token: str) -> tuple[int | None, str, str]:
    """Invoke the judge Copilot once. Returns (score, reason, outcome).

    outcome is one of: ok | parse_error | timeout | error.
    """
    cmd = ["copilot", "-p", prompt, "-s"]
    if config.runner.judge_model:
        cmd.extend(["--model", config.runner.judge_model])
    # Disable OTel to avoid contaminating eval traces with judge calls
    judge_env = {**os.environ, "GITHUB_TOKEN": token, "COPILOT_OTEL_ENABLED": "false"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=config.runner.judge_timeout_seconds, env=judge_env)
    except subprocess.TimeoutExpired:
        return None, "timeout", "timeout"
    except (FileNotFoundError, OSError) as exc:
        return None, f"error: {exc}", "error"
    data = _parse_json(proc.stdout, require_keys=("score",))
    if data:
        try:
            return int(data.get("score", 0)), str(data.get("reason", "")), "ok"
        except (TypeError, ValueError):
            return None, "parse_error", "parse_error"
    return None, "parse_error", "parse_error"


def run_judge(ev: Evaluator, conversation: str, config: Config, token: str,
              output_files_text: str | None = None) -> EvalScore:
    """Run a judge evaluator against captured conversation + output files.

    Shared by the `analyze` command. Builds the judge prompt and samples the
    judge ``runner.judge_samples`` times (self-consistency), disabling OTel so
    judge calls don't contaminate eval traces. Successful samples are aggregated
    via ``runner.judge_aggregate`` (median/mean/majority) and the per-sample
    spread (stddev) plus outcome counts (ok/parse_error/timeout/error) are
    recorded for reliability reporting.
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

    n = max(1, config.runner.judge_samples)
    samples: list[int] = []
    reasons: list[str] = []
    outcomes: dict[str, int] = {"ok": 0, "parse_error": 0, "timeout": 0, "error": 0}
    for _ in range(n):
        score, reason, outcome = _run_judge_once(prompt, config, token)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome == "ok" and score is not None:
            samples.append(score)
            reasons.append(reason)

    judge_version = judge_cli_version()

    if not samples:
        # No usable score across all samples; surface the dominant failure mode.
        failure = max(("parse_error", "timeout", "error"), key=lambda o: outcomes.get(o, 0))
        return EvalScore(
            name=ev.name, type="judge", score=None, reason=failure, passed=False,
            samples=samples, score_stddev=None, n_samples=n, outcomes=outcomes,
            judge_model=config.runner.judge_model, judge_version=judge_version,
        )

    agg = _aggregate_scores(samples, config.runner.judge_aggregate)
    stddev = float(pstdev(samples)) if len(samples) > 1 else 0.0
    # Pick the reason from the sample whose score is closest to the aggregate.
    reason = min(zip(samples, reasons, strict=True), key=lambda sr: abs(sr[0] - agg))[1]
    if n > 1:
        reason = f"[{config.runner.judge_aggregate} of {len(samples)}/{n}, σ={stddev:.2f}] {reason}"
    return EvalScore(
        name=ev.name, type="judge", score=agg, reason=reason,
        samples=samples, score_stddev=round(stddev, 4), n_samples=n, outcomes=outcomes,
        judge_model=config.runner.judge_model, judge_version=judge_version,
    )


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
    """Read all files under a directory, concatenated with per-file headers."""
    if not directory or not directory.is_dir():
        return None
    parts: list[str] = []
    total = 0
    for f in sorted(directory.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(directory)
        try:
            content = f.read_text(errors="replace")
        except OSError:
            continue
        if total + len(content) > max_chars:
            remaining = max_chars - total
            if remaining > 0:
                parts.append(f"=== {rel} ===\n{content[:remaining]}\n... (truncated)")
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
