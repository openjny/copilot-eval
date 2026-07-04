"""Pre-flight validation: config sanity checks and runtime readiness checks.

Two audiences share the same `CheckResult` shape:

- **Static checks** (`check_config_schema`, `check_fixtures`,
  `check_script_references`, `check_var_interpolation`) inspect the config on
  disk and are used by the `validate` CLI command.
- **Readiness checks** (`check_docker_daemon`, `check_github_token`,
  `check_disk_space`, `check_base_image`) probe the local environment and are
  used by `run` before any Docker work happens. `validate_readiness()`
  composes them into a single list.

All checks collect independently — a failure in one never short-circuits the
others — so callers can report every problem at once instead of a single
cryptic failure deep into a run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from eval.config import Config, ConfigError, Task, load_config

# Minimum free disk space required to run an eval (base image + variant images
# + per-run workspaces can easily consume several hundred MB).
MIN_DISK_SPACE_BYTES = 500 * 1024 * 1024

_VAR_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class CheckResult:
    """Outcome of a single validation/readiness check.

    `remediation` is only meaningful when `passed` is False; every failing
    check should suggest a concrete fix.
    """

    name: str
    passed: bool
    message: str
    remediation: str | None = None

    def format(self) -> str:
        icon = "✓" if self.passed else "✗"
        line = f"  {icon} {self.name}: {self.message}"
        if not self.passed and self.remediation:
            line += f"\n      → {self.remediation}"
        return line


def _ok(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, passed=True, message=message)


def _fail(name: str, message: str, remediation: str) -> CheckResult:
    return CheckResult(name=name, passed=False, message=message, remediation=remediation)


def any_failed(results: list[CheckResult]) -> bool:
    return any(not r.passed for r in results)


def format_results(results: list[CheckResult]) -> str:
    return "\n".join(r.format() for r in results)


# --- Static config checks (used by `validate`) ---


def check_config_schema(config_dir: Path | None) -> tuple[Config | None, CheckResult]:
    """Load the config, translating load errors into a CheckResult.

    Returns `(None, failing_result)` when the config can't be loaded at all —
    callers should skip the remaining (config-dependent) checks in that case.
    """
    try:
        config = load_config(config_dir)
    except FileNotFoundError as exc:
        return None, _fail(
            "config_schema",
            str(exc),
            "Create an eval-config.yaml in the config directory, or pass "
            "--config-dir pointing to one.",
        )
    except ConfigError as exc:
        return None, _fail(
            "config_schema",
            str(exc),
            "Fix the config error above. See docs/configuration.md for the schema reference.",
        )
    except yaml.YAMLError as exc:
        return None, _fail(
            "config_schema",
            f"YAML syntax error: {exc}",
            "Fix the YAML syntax (check indentation, quoting, and colons).",
        )
    return config, _ok("config_schema", "eval-config.yaml is valid")


def check_fixtures(config: Config, tasks: list[Task] | None = None) -> list[CheckResult]:
    """Verify every fixture referenced by `tasks` (default: all tasks) exists on disk."""
    tasks = config.tasks if tasks is None else tasks
    fixtures_dir = config.config_dir / "fixtures"
    results: list[CheckResult] = []
    seen: set[str] = set()
    for task in tasks:
        for fixture in task.fixture_names():
            if fixture in seen:
                continue
            seen.add(fixture)
            path = fixtures_dir / fixture
            if path.is_dir():
                results.append(_ok(f"fixture:{fixture}", f"Found at {path}"))
            else:
                results.append(
                    _fail(
                        f"fixture:{fixture}",
                        f"Fixture '{fixture}' not found",
                        f"Expected at: {path}",
                    )
                )
    return results


def _resolve_script(config: Config, script: str) -> Path:
    """Scripts (hooks/health_check/evaluator) resolve relative to config_dir
    first, falling back to project_dir — mirrors eval.runner's resolution."""
    candidate = config.config_dir / script
    if candidate.exists():
        return candidate
    return config.project_dir / script


def check_script_references(config: Config) -> list[CheckResult]:
    """Verify variant build/run files and task hook/health_check/evaluator
    script paths referenced by the config exist on disk."""
    results: list[CheckResult] = []

    for variant in config.variants:
        if variant.dockerfile:
            path = (config.project_dir / variant.dockerfile).resolve()
            if path.is_file():
                results.append(
                    _ok(f"variant:{variant.name}:dockerfile", f"Found {variant.dockerfile}")
                )
            else:
                results.append(
                    _fail(
                        f"variant:{variant.name}:dockerfile",
                        f"Dockerfile '{variant.dockerfile}' not found for variant '{variant.name}'",
                        f"Expected at: {path}",
                    )
                )
        if variant.run_script:
            path = (config.project_dir / variant.run_script).resolve()
            if path.is_file():
                results.append(
                    _ok(f"variant:{variant.name}:run_script", f"Found {variant.run_script}")
                )
            else:
                results.append(
                    _fail(
                        f"variant:{variant.name}:run_script",
                        f"Run script '{variant.run_script}' not found for variant "
                        f"'{variant.name}'",
                        f"Expected at: {path}",
                    )
                )

    for task in config.tasks:
        for label, script in (
            ("before_run", task.hooks.before_run),
            ("after_run", task.hooks.after_run),
            ("health_check", task.health_check),
        ):
            if not script:
                continue
            path = _resolve_script(config, script)
            check_name = f"task:{task.name}:{label}"
            if path.is_file():
                results.append(_ok(check_name, f"Found {script}"))
            else:
                results.append(
                    _fail(
                        check_name,
                        f"{label} script '{script}' not found for task '{task.name}'",
                        f"Expected at: {config.config_dir / script} or "
                        f"{config.project_dir / script}",
                    )
                )
        for ev in task.evaluators:
            if ev.type != "script" or not ev.script:
                continue
            path = _resolve_script(config, ev.script)
            check_name = f"task:{task.name}:evaluator:{ev.name}"
            if path.is_file():
                results.append(_ok(check_name, f"Found {ev.script}"))
            else:
                results.append(
                    _fail(
                        check_name,
                        f"Evaluator '{ev.name}' script '{ev.script}' not found for task "
                        f"'{task.name}'",
                        f"Expected at: {config.config_dir / ev.script} or "
                        f"{config.project_dir / ev.script}",
                    )
                )

    return results


def check_var_interpolation(config: Config) -> list[CheckResult]:
    """Verify every `{var}` placeholder in a task's prompt (and the global
    output_instruction) resolves for every variant it could run under."""
    results: list[CheckResult] = []
    for task in config.tasks:
        placeholders: set[str] = set(_VAR_PATTERN.findall(task.prompt))
        if config.runner.output_instruction:
            placeholders |= set(_VAR_PATTERN.findall(config.runner.output_instruction))
        if not placeholders:
            continue
        for variant in config.variants:
            resolved = config.resolve_vars(task, variant)
            missing = sorted(placeholders - resolved.keys())
            check_name = f"vars:{task.name}/{variant.name}"
            if missing:
                results.append(
                    _fail(
                        check_name,
                        f"Task '{task.name}' references undefined var(s) {missing} for "
                        f"variant '{variant.name}'",
                        f"Define {', '.join(missing)} in vars:, task.vars, or variant.vars.",
                    )
                )
            else:
                results.append(_ok(check_name, "All placeholders resolve"))
    return results


# --- Readiness checks (used by `run` pre-flight) ---


def check_docker_daemon() -> CheckResult:
    if shutil.which("docker") is None:
        return _fail(
            "docker_daemon",
            "docker CLI not found on PATH",
            "Install Docker: https://docs.docker.com/get-docker/",
        )
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        return _fail(
            "docker_daemon",
            "`docker info` timed out",
            "Check the Docker daemon's health and restart it if needed.",
        )
    if result.returncode != 0:
        return _fail(
            "docker_daemon",
            "Docker daemon not reachable",
            "Start Docker: `systemctl start docker` (Linux) or open Docker Desktop.",
        )
    return _ok("docker_daemon", "Docker daemon is reachable")


def check_github_token() -> CheckResult:
    token = os.environ.get("GITHUB_TOKEN", "").strip() or os.environ.get(
        "COPILOT_GITHUB_TOKEN", ""
    ).strip()
    if token:
        return _ok("github_token", "GITHUB_TOKEN/COPILOT_GITHUB_TOKEN is set")
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return _ok("github_token", "Authenticated via `gh auth token`")
    return _fail(
        "github_token",
        "GITHUB_TOKEN missing and gh CLI not authenticated",
        "Run: `gh auth login`, or set GITHUB_TOKEN / COPILOT_GITHUB_TOKEN in .env",
    )


def check_disk_space(path: Path, min_bytes: int = MIN_DISK_SPACE_BYTES) -> CheckResult:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return _fail(
            "disk_space",
            f"Could not determine disk usage for {path}: {exc}",
            "Check that the path exists and is accessible.",
        )
    free_mb = usage.free / (1024 * 1024)
    if usage.free < min_bytes:
        return _fail(
            "disk_space",
            f"Only {free_mb:.0f} MB free at {path} (need >= {min_bytes // (1024 * 1024)} MB)",
            "Free up disk space, e.g. `docker system prune` or remove old results/ runs.",
        )
    return _ok("disk_space", f"{free_mb:.0f} MB free at {path}")


def check_base_image(config: Config) -> CheckResult:
    base_image = f"{config.runner.container_image_base}:base"
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", base_image], capture_output=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return _fail(
            "base_image",
            f"Could not check for base image '{base_image}': {exc}",
            "Ensure Docker is installed and running.",
        )
    if result.returncode != 0:
        return _fail(
            "base_image",
            f"Base image '{base_image}' not found",
            "Build it: `uv run copilot-eval build` (or drop --no-build to auto-build).",
        )
    return _ok("base_image", f"Base image '{base_image}' present")


def validate_readiness(
    config: Config,
    tasks: list[Task] | None = None,
    check_build: bool = True,
) -> list[CheckResult]:
    """Run all pre-flight readiness checks for `run`, before any Docker work.

    Collects every result rather than raising on the first failure, so `run`
    can print a complete, actionable report in one shot.
    """
    results = [check_docker_daemon(), check_github_token()]
    results.extend(check_fixtures(config, tasks))
    results.append(check_disk_space(config.project_dir))
    if check_build:
        results.append(check_base_image(config))
    return results
