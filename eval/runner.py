"""Execute a single eval run in a Docker container."""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from eval.config import Config, Evaluator, Pattern, Variant


@dataclass
class EvalScore:
    name: str
    type: str
    score: int | None
    reason: str = ""
    passed: bool = True


@dataclass
class RunResult:
    pattern: str
    variant: str
    epoch: int
    test_id: str
    run_id: str
    log_file: Path
    exit_code: int
    scores: list[EvalScore] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.scores) if self.scores else True


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
    pattern: Pattern, variant: Variant, epoch: int,
    config: Config, run_id: str, run_dir: Path, github_token: str,
) -> RunResult:
    test_id = str(uuid.uuid4())
    log_file = run_dir / f"{pattern.name}_{variant.name}_epoch{epoch}.log"
    print(f"--- [{pattern.name}] epoch={epoch} variant={variant.name} test_id={test_id[:8]}")

    _run_hook(pattern.hooks.before_run, config, log_file, "before_run")

    prompt = config.resolve_prompt(pattern)
    image = config.image_name(variant)
    otel_attrs = ",".join([
        f"eval.test_id={test_id}", f"eval.scenario={pattern.name}",
        f"eval.variant={variant.name}", f"eval.epoch={epoch}", f"eval.run_id={run_id}",
    ])
    cmd = [
        "docker", "run", "--rm", "--add-host=host.docker.internal:host-gateway",
        "--env-file", str(config.env_file),
        "-e", f"GITHUB_TOKEN={github_token}",
        "-e", "COPILOT_OTEL_ENABLED=true",
        "-e", f"OTEL_EXPORTER_OTLP_ENDPOINT={config.runner.otel_endpoint}",
        "-e", f"OTEL_RESOURCE_ATTRIBUTES={otel_attrs}",
        "-e", "OTEL_SERVICE_NAME=github-copilot",
    ]
    copilot_home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")).resolve()
    if copilot_home.is_dir():
        cmd.extend(["-v", f"{copilot_home}:/copilot-home-src:ro"])
    fixture_dir = (config.config_dir / "fixtures" / (pattern.fixture or pattern.name)).resolve()
    if fixture_dir.is_dir():
        cmd.extend(["-v", f"{fixture_dir}:/workspace:ro"])
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
    timeout = pattern.timeout_seconds or config.runner.timeout_seconds
    cmd.extend([image, "timeout", f"{timeout}s", *copilot_args])

    print("    Running copilot in container...")
    with open(log_file, "a") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
    _print_summary(log_file)

    _run_hook(pattern.hooks.after_run, config, log_file, "after_run")

    scores = _run_evaluators(pattern, config, log_file, github_token)
    _print_scores(scores)

    return RunResult(
        pattern=pattern.name, variant=variant.name, epoch=epoch,
        test_id=test_id, run_id=run_id, log_file=log_file,
        exit_code=proc.returncode, scores=scores,
    )


def _run_hook(script: str | None, config: Config, log_file: Path, label: str) -> None:
    if not script:
        return
    resolved = (config.config_dir / script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / script).resolve()
    if not resolved.exists():
        print(f"    WARNING: {label} script not found: {script}")
        return
    print(f"    Running {label}...")
    env = {**os.environ, **{f"EVAL_{k.upper()}": v for k, v in config.vars.items()}}
    with open(log_file, "a") as lf:
        subprocess.run(["bash", str(resolved)], stdout=lf, stderr=subprocess.STDOUT, env=env)


def _run_evaluators(pattern: Pattern, config: Config, log_file: Path, token: str) -> list[EvalScore]:
    scores: list[EvalScore] = []
    for ev in pattern.evaluators:
        s = None
        if ev.type == "judge":
            s = _eval_judge(ev, log_file, token)
        elif ev.type == "script":
            s = _eval_script(ev, config, log_file)
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


def _eval_judge(ev: Evaluator, log_file: Path, token: str) -> EvalScore | None:
    if not ev.prompt:
        return None
    output = _read_log(log_file, max_chars=8000)
    if not output:
        return None
    prompt = f"You are an eval judge. Score the following Copilot output.\n\n{ev.prompt}\n\n--- COPILOT OUTPUT ---\n{output}\n--- END OUTPUT ---\n\nOutput ONLY valid JSON: {{\"score\": N, \"reason\": \"...\"}}"
    print(f"    Evaluating: {ev.name} (judge)...")
    try:
        proc = subprocess.run(
            ["copilot", "-p", prompt, "-s"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "GITHUB_TOKEN": token},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return EvalScore(name=ev.name, type="judge", score=None, reason="timeout")
    data = _parse_json(proc.stdout)
    if data:
        return EvalScore(name=ev.name, type="judge", score=int(data.get("score", 0)), reason=str(data.get("reason", "")))
    return EvalScore(name=ev.name, type="judge", score=None, reason="parse_error")


def _eval_script(ev: Evaluator, config: Config, log_file: Path) -> EvalScore | None:
    if not ev.script:
        return None
    resolved = (config.config_dir / ev.script).resolve()
    if not resolved.exists():
        resolved = (config.project_dir / ev.script).resolve()
    if not resolved.exists():
        return None
    print(f"    Evaluating: {ev.name} (script)...")
    env = {**os.environ, **{f"EVAL_{k.upper()}": v for k, v in config.vars.items()}}
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


def _parse_json(text: str) -> dict | None:
    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
