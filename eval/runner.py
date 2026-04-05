"""Execute a single eval run in a Docker container."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from eval.config import Config, Judge, Pattern, Variant


@dataclass
class JudgeScore:
    name: str
    score: int
    reason: str


@dataclass
class RunResult:
    pattern: str
    variant: str
    epoch: int
    test_id: str
    run_id: str
    log_file: Path
    result: str  # "PASS" | "FAIL" | "SKIP"
    exit_code: int
    judge_scores: list[JudgeScore] = field(default_factory=list)


def get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError("GITHUB_TOKEN not set and gh CLI not authenticated")


def reset_environment(pattern: Pattern, config: Config, log_file: Path) -> None:
    """Run the pattern's reset script before a write-type eval."""
    reset_path = pattern.reset_script
    if not reset_path:
        return
    reset_file = config.config_dir / reset_path
    if not reset_file.exists():
        print(f"    WARNING: {reset_file} not found, skipping reset")
        return

    print("    Running reset script...")
    env = {**os.environ, **{f"EVAL_{k.upper()}": v for k, v in config.vars.items()}}
    with open(log_file, "a") as lf:
        subprocess.run(
            ["bash", str(reset_file)],
            stdout=lf, stderr=subprocess.STDOUT,
            env=env,
        )


def run_one(
    pattern: Pattern,
    variant: Variant,
    epoch: int,
    config: Config,
    run_id: str,
    run_dir: Path,
    github_token: str,
) -> RunResult:
    test_id = str(uuid.uuid4())
    log_file = run_dir / f"{pattern.name}_{variant.name}_epoch{epoch}.log"

    print(f"--- [{pattern.name}] epoch={epoch} variant={variant.name} test_id={test_id[:8]}")

    # Write pattern: reset environment before each run
    if pattern.type == "write":
        reset_environment(pattern, config, log_file)

    # Build docker command
    prompt = config.resolve_prompt(pattern)
    image = config.image_name(variant)

    otel_attrs = ",".join([
        f"eval.test_id={test_id}",
        f"eval.scenario={pattern.name}",
        f"eval.variant={variant.name}",
        f"eval.epoch={epoch}",
        f"eval.run_id={run_id}",
    ])

    cmd = [
        "docker", "run", "--rm",
        "--add-host=host.docker.internal:host-gateway",
        "--env-file", str(config.env_file),
        "-e", f"GITHUB_TOKEN={github_token}",
        "-e", "COPILOT_OTEL_ENABLED=true",
        "-e", f"OTEL_EXPORTER_OTLP_ENDPOINT={config.runner.otel_endpoint}",
        "-e", f"OTEL_RESOURCE_ATTRIBUTES={otel_attrs}",
        "-e", "OTEL_SERVICE_NAME=github-copilot",
    ]

    # Mount Copilot home for auth
    copilot_home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")).resolve()
    if copilot_home.is_dir():
        cmd.extend(["-v", f"{copilot_home}:/copilot-home-src:ro"])

    # Mount fixture if exists
    fixture_name = pattern.fixture or pattern.name
    fixture_dir = (config.config_dir / "fixtures" / fixture_name).resolve()
    if fixture_dir.is_dir():
        cmd.extend(["-v", f"{fixture_dir}:/workspace:ro"])

    # Mount run/setup script if variant has one
    if variant.run_script:
        run_script_path = (config.project_dir / variant.run_script).resolve()
        if run_script_path.exists():
            cmd.extend(["-v", f"{run_script_path}:/tmp/eval-setup.sh:ro"])
            cmd.extend(["-e", "EVAL_SETUP_SCRIPT=/tmp/eval-setup.sh"])

    copilot_args = ["copilot", "-p", prompt, "--yolo"]
    # Model: variant override > runner default
    model = variant.model or config.runner.model
    if model:
        copilot_args.extend(["--model", model])
    if config.runner.reasoning_effort:
        copilot_args.extend(["--effort", config.runner.reasoning_effort])
    if config.runner.max_turns:
        copilot_args.extend(["--max-autopilot-continues", str(config.runner.max_turns)])
    if config.runner.output_format == "json":
        copilot_args.extend(["--output-format", "json"])

    # Timeout: pattern override > runner default
    timeout = pattern.timeout_seconds or config.runner.timeout_seconds
    cmd.extend([image, "timeout", f"{timeout}s", *copilot_args])

    # Execute
    print("    Running copilot in container...")
    with open(log_file, "a") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)

    # Print Copilot output summary
    _print_copilot_summary(log_file)

    # Verification
    result = _verify(pattern, config, log_file)
    print(f"    {'✓' if result == 'PASS' else '✗' if result == 'FAIL' else '−'} {result}")

    # LLM-as-Judge
    judge_scores = _run_judges(pattern, log_file, github_token)

    return RunResult(
        pattern=pattern.name,
        variant=variant.name,
        epoch=epoch,
        test_id=test_id,
        run_id=run_id,
        log_file=log_file,
        result=result,
        exit_code=proc.returncode,
        judge_scores=judge_scores,
    )


def _print_copilot_summary(log_file: Path) -> None:
    try:
        text = log_file.read_text()
        for line in text.splitlines():
            if line.startswith("Total ") or line.startswith("Breakdown"):
                print(f"    {line}")
    except OSError:
        pass


def _verify(pattern: Pattern, config: Config, log_file: Path) -> str:
    verify_path = pattern.verify
    if not verify_path:
        verify_path = f"scripts/verify-{pattern.name}.sh"
    verify_script = config.config_dir / verify_path
    if not verify_script.exists():
        return "SKIP"

    print("    Running verification...")
    env = {**os.environ, **{f"EVAL_{k.upper()}": v for k, v in config.vars.items()}}
    with open(log_file, "a") as lf:
        proc = subprocess.run(
            [str(verify_script)],
            stdout=lf, stderr=subprocess.STDOUT,
            env=env,
        )
    return "PASS" if proc.returncode == 0 else "FAIL"


def _run_judges(pattern: Pattern, log_file: Path, github_token: str) -> list[JudgeScore]:
    """Run LLM-as-Judge scoring using Copilot CLI on the host."""
    judges = pattern.metrics.judges
    if not judges:
        return []

    try:
        output = log_file.read_text()
    except OSError:
        return []

    scores: list[JudgeScore] = []
    for judge in judges:
        print(f"    Judging: {judge.name}...")
        score = _run_one_judge(judge, output, github_token)
        if score:
            scores.append(score)
            print(f"    → {judge.name}: {score.score}/5 ({score.reason[:50]})")

    # Save judge results alongside log
    if scores:
        judge_file = log_file.with_suffix(".judges.json")
        judge_file.write_text(json.dumps([
            {"name": s.name, "score": s.score, "reason": s.reason} for s in scores
        ], indent=2, ensure_ascii=False))

    return scores


def _run_one_judge(judge: Judge, eval_output: str, github_token: str) -> JudgeScore | None:
    """Call Copilot CLI to judge a single aspect."""
    # Truncate output to avoid token limits
    max_chars = 8000
    if len(eval_output) > max_chars:
        eval_output = eval_output[:max_chars] + "\n... (truncated)"

    prompt = f"""You are an eval judge. Score the following Copilot output.

{judge.prompt}

--- COPILOT OUTPUT ---
{eval_output}
--- END OUTPUT ---

Output ONLY valid JSON: {{"score": N, "reason": "..."}}"""

    env = {**os.environ, "GITHUB_TOKEN": github_token}
    proc = subprocess.run(
        ["copilot", "-p", prompt, "-s"],
        capture_output=True, text=True, timeout=60, env=env,
    )

    if proc.returncode != 0:
        return None

    # Parse JSON from output
    text = proc.stdout.strip()
    # Find JSON in output (may have markdown wrapping)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                return JudgeScore(
                    name=judge.name,
                    score=int(data.get("score", 0)),
                    reason=str(data.get("reason", "")),
                )
            except (json.JSONDecodeError, ValueError):
                continue

    # Try parsing entire output as JSON
    try:
        data = json.loads(text)
        return JudgeScore(
            name=judge.name,
            score=int(data.get("score", 0)),
            reason=str(data.get("reason", "")),
        )
    except (json.JSONDecodeError, ValueError):
        return None
