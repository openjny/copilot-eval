"""Execute a single eval run in a Docker container."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from eval.config import Config, Evaluator, Task, Variant


@dataclass
class EvalScore:
    name: str
    type: str
    score: int | None
    reason: str = ""
    passed: bool = True


@dataclass
class RunResult:
    task: str
    variant: str
    epoch: int
    test_id: str
    run_id: str
    log_file: Path
    exit_code: int
    status: str = "completed"  # completed | setup_failed | timeout
    scores: list[EvalScore] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "completed" and (all(s.passed for s in self.scores) if self.scores else True)


def get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError("GITHUB_TOKEN not set and gh CLI not authenticated")


def run_one(
    task: Task, variant: Variant, epoch: int,
    config: Config, run_id: str, run_dir: Path, github_token: str,
) -> RunResult:
    test_id = str(uuid.uuid4())
    log_file = run_dir / f"{task.name}_{variant.name}_epoch{epoch}.log"
    print(f"--- [{task.name}] epoch={epoch} variant={variant.name} test_id={test_id[:8]}")

    _run_hook(task.hooks.before_run, config, task, variant, log_file, "before_run")

    # Health check: verify environment is ready before running Copilot
    if task.health_check:
        if not _run_health_check(task.health_check, config, task, variant, log_file):
            print(f"    ✗ Health check failed — skipping run")
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
    cmd = [
        "docker", "run", "--rm", "--add-host=host.docker.internal:host-gateway",
        "--env-file", str(config.env_file),
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
    try:
        with open(log_file, "a") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=run_env)
        _print_summary(log_file)

        _run_hook(task.hooks.after_run, config, task, variant, log_file, "after_run")

        # Persist output files to results dir before tmpdir cleanup
        _persist_output_files(work_dir, run_dir, task.name, variant.name, epoch)

        scores = _run_evaluators(task, variant, config, log_file, github_token, work_dir)
        _print_scores(scores)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return RunResult(
        task=task.name, variant=variant.name, epoch=epoch,
        test_id=test_id, run_id=run_id, log_file=log_file,
        exit_code=proc.returncode, scores=scores,
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
    print(f"    Running health_check...")
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
            [{"name": s.name, "type": s.type, "score": s.score, "reason": s.reason, "passed": s.passed} for s in scores],
            indent=2, ensure_ascii=False,
        ))
    return scores


def run_judge(ev: Evaluator, conversation: str, config: Config, token: str,
              output_files_text: str | None = None) -> EvalScore:
    """Run a single judge evaluator against captured conversation + output files.

    Shared by the `analyze` command. Builds the judge prompt, invokes Copilot
    (with OTel disabled so judge calls don't contaminate eval traces), and parses
    the JSON verdict.
    """
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
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=judge_env)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return EvalScore(name=ev.name, type="judge", score=None, reason="timeout")
    data = _parse_json(proc.stdout, require_keys=("score",))
    if data:
        return EvalScore(name=ev.name, type="judge", score=int(data.get("score", 0)), reason=str(data.get("reason", "")))
    return EvalScore(name=ev.name, type="judge", score=None, reason="parse_error")


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


def _parse_json(text: str, require_keys: tuple[str, ...] | None = None) -> dict | None:
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


def _load_env_file(env_file: Path) -> dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and empty lines."""
    env: dict[str, str] = {}
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env
