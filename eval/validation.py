"""Pre-flight validation: config sanity checks and runtime readiness checks.

Two audiences share the same `CheckResult` shape:

- **Static checks** (`check_config_schema`, `check_json_schema`, `check_fixtures`,
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

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    check should suggest a concrete fix. `blocking` distinguishes hard errors
    (fail `validate`/`run` pre-flight) from warnings (surfaced but never
    abort) — used for checks whose runtime counterpart tolerates the same
    condition (e.g. a missing fixture dir, or an undefined `{var}` in a
    prompt, are both silently accepted at run time).
    """

    name: str
    passed: bool
    message: str
    remediation: str | None = None
    blocking: bool = True

    def format(self, strict: bool = False) -> str:
        promoted = strict and not self.passed and not self.blocking
        if self.passed:
            icon = "✓"
        elif self.blocking or promoted:
            icon = "✗"
        else:
            icon = "⚠"
        line = f"  {icon} {self.name}: {self.message}"
        if promoted:
            line += " [promoted to failure by --strict]"
        if not self.passed and self.remediation:
            line += f"\n      → {self.remediation}"
        return line


def _ok(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, passed=True, message=message)


def _fail(name: str, message: str, remediation: str) -> CheckResult:
    return CheckResult(name=name, passed=False, message=message, remediation=remediation)


def _warn(name: str, message: str, remediation: str) -> CheckResult:
    """A non-blocking check result: reported, but never fails `validate`/`run`.

    Use for conditions the runtime itself tolerates (e.g. a missing fixture
    directory or an undefined prompt `{var}` — both silently pass through
    rather than erroring), so `validate`/pre-flight never abort a run that
    would otherwise succeed.

    `validate --strict` (and CI, by default) promotes these to a non-zero exit
    without changing this default, non-blocking behavior (issue #128).
    """
    return CheckResult(
        name=name, passed=False, message=message, remediation=remediation, blocking=False
    )


def any_failed(results: list[CheckResult]) -> bool:
    """True when at least one *blocking* check failed. Warnings don't count."""
    return any(not r.passed and r.blocking for r in results)


def any_warnings(results: list[CheckResult]) -> bool:
    """True when at least one *non-blocking* (warning) check did not pass.

    Under `validate --strict` these are promoted to a non-zero CI gate; in the
    default/interactive mode they are surfaced but never affect the exit code.
    """
    return any(not r.passed and not r.blocking for r in results)


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


def _item_subschema(schema: dict[str, Any], key: str, required: list[str]) -> dict[str, Any] | None:
    """Extract the per-item sub-schema for a `tasks`/`variants` split file.

    The generated schema (`eval/schema.py`) inlines the task/variant item shape
    under `properties.<key>` — as `items` for `variants`, and inside an `anyOf`
    array branch for `tasks`. Split files hold a *single* task/variant body, so
    we validate each document against that item schema directly, with `required`
    relaxed (a split file's `name` is optional — the loader falls back to the
    file stem). All `$ref`s in the generated schema are already inlined, so the
    extracted sub-schema is self-contained.
    """
    node = schema.get("properties", {}).get(key, {})
    item: dict[str, Any] | None = None
    if isinstance(node.get("items"), dict):
        item = node["items"]
    else:
        for branch in node.get("anyOf", []):
            if branch.get("type") == "array" and isinstance(branch.get("items"), dict):
                item = branch["items"]
                break
    if item is None:
        return None
    return {**item, "required": required}


def check_json_schema(config_dir: Path | None) -> CheckResult:
    """Validate the config against `schemas/eval-config.schema.json`.

    This is a *structural* check on the files as written (before the dataclass
    coercion `check_config_schema` performs), so it catches typo'd keys and
    wrong value types (e.g. `timeout_secods: 300`, `judge_batch: "tru"`) even
    when they'd otherwise be silently accepted as unknown/default fields.

    Coverage spans **both** config layouts: the inline/top-level
    `eval-config.yaml` *and* the split-file layout (`tasks/*.yaml` /
    `variants/*.yaml`), which the project's conventions call the primary layout.
    Each split document is validated against the relevant item sub-schema (with
    `name` optional, since the loader falls back to the file stem).

    Non-blocking only for genuinely unavailable prerequisites: a missing
    `jsonschema` dependency, an unreadable schema file, or no `eval-config.yaml`
    at all — these degrade to a warning rather than failing `validate`. A config
    that merely *splits* its tasks/variants is fully validated, not skipped.
    """
    try:
        import jsonschema
    except ImportError:
        return _warn(
            "json_schema",
            "Skipped: the 'jsonschema' package is not installed.",
            "Install it (e.g. `uv sync`) to enable JSON Schema validation.",
        )

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "eval-config.schema.json"
    try:
        schema = json.loads(schema_path.read_text())
    except OSError as exc:
        return _warn("json_schema", f"Skipped: could not read schema file: {exc}", str(schema_path))

    directory = config_dir if config_dir is not None else schema_path.parent.parent
    config_path = directory / "eval-config.yaml"
    if not config_path.exists():
        return _warn(
            "json_schema",
            f"Skipped: no eval-config.yaml at {config_path}.",
            "Create an eval-config.yaml (or pass --config-dir pointing to one) "
            "to enable JSON Schema validation.",
        )

    validator = jsonschema.Draft202012Validator(schema)
    # (source label, schema error) tuples, so each problem points at its file.
    problems: list[tuple[str, Any]] = []
    covered: list[str] = []

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError:
        # check_config_schema already reports YAML syntax errors as blocking.
        return _warn("json_schema", "Skipped: eval-config.yaml has a YAML syntax error.", "")
    covered.append("eval-config.yaml")
    for e in validator.iter_errors(raw):
        problems.append(("eval-config.yaml", e))

    # Split-file layouts: validate each tasks/*.yaml and variants/*.yaml against
    # the relevant item sub-schema. This mirrors eval.config._load_tasks /
    # _load_variants, which read these directories as the primary layout.
    for key in ("tasks", "variants"):
        split_dir = directory / key
        if not split_dir.is_dir():
            continue
        yaml_files = sorted(split_dir.glob("*.yaml"))
        if not yaml_files:
            continue
        subschema = _item_subschema(schema, key, required=["prompt"] if key == "tasks" else [])
        if subschema is None:
            continue
        sub_validator = jsonschema.Draft202012Validator(subschema)
        covered.append(f"{key}/*.yaml")
        for yaml_file in yaml_files:
            rel = f"{key}/{yaml_file.name}"
            try:
                doc = yaml.safe_load(yaml_file.read_text()) or {}
            except yaml.YAMLError:
                return _warn("json_schema", f"Skipped: {rel} has a YAML syntax error.", "")
            for e in sub_validator.iter_errors(doc):
                problems.append((rel, e))

    if not problems:
        return _ok("json_schema", f"{' + '.join(covered)} match schemas/eval-config.schema.json")

    problems.sort(key=lambda pe: (pe[0], list(pe[1].path)))
    lines = []
    for source, e in problems[:10]:
        where = ".".join(str(p) for p in e.path) or "<root>"
        lines.append(f"{source} → {where}: {e.message}")
    summary = "; ".join(lines)
    if len(problems) > 10:
        summary += f" (+{len(problems) - 10} more)"
    return _fail(
        "json_schema",
        f"Config does not match the JSON Schema: {summary}",
        "Fix the field(s) above, or see schemas/eval-config.schema.json / "
        "docs/configuration.md for the expected shape.",
    )


def check_fixtures(config: Config, tasks: list[Task] | None = None) -> list[CheckResult]:
    """Verify every fixture referenced by `tasks` (default: all tasks) exists on disk.

    Non-blocking: `eval.runner.run_one` only copies a fixture directory when it
    exists (`if fixture_dir.is_dir(): ...`) and silently runs without one
    otherwise, so a missing fixture never fails a run today. Flagging it here
    as a hard error would abort runs that currently succeed (e.g. a task that
    relies solely on a `before_run` hook, with no fixture directory at all).
    Reported as a warning so typos are still surfaced without blocking `run`.

    A missing directory is only *warned* about when the task **explicitly**
    declares a fixture (`fixture:` / `fixtures:`). When the fixture name is
    merely the implicit fallback to the task name (a fixed-answer task that
    writes its own output and consumes no input), a missing directory is the
    expected, benign case and is reported as a passing check — so canonical
    fixed-answer examples validate cleanly instead of shipping alarming
    warnings (issue #129).
    """
    tasks = config.tasks if tasks is None else tasks
    fixtures_dir = config.config_dir / "fixtures"
    # Fixtures a task explicitly opts into (vs. the implicit task-name fallback).
    explicit: set[str] = set()
    for task in tasks:
        if task.fixtures:
            explicit.update(task.fixtures)
        elif task.fixture:
            explicit.add(task.fixture)
    results: list[CheckResult] = []
    seen: set[str] = set()
    for task in tasks:
        for fixture in task.fixture_names():
            if fixture in seen:
                continue
            seen.add(fixture)
            # Remote fixtures (issue #122) have no local directory — they are
            # fetched + verified from their URL at run time — so a missing dir
            # is expected and not worth flagging.
            rf = task.remote_fixtures.get(fixture)
            if rf is not None:
                results.append(_ok(f"fixture:{fixture}", f"Remote fixture (fetched from {rf.url})"))
                continue
            path = fixtures_dir / fixture
            if path.is_dir():
                results.append(_ok(f"fixture:{fixture}", f"Found at {path}"))
            elif fixture in explicit:
                results.append(
                    _warn(
                        f"fixture:{fixture}",
                        f"Declared fixture '{fixture}' has no directory yet — the run "
                        "will start without a fixture (non-blocking)",
                        f"Create {path}/ with the fixture's files, or remove the "
                        "'fixture:' declaration if this task needs no fixture.",
                    )
                )
            else:
                # No fixture declared: the name is just the task-name fallback,
                # and the task provides its own input / writes fixed output.
                results.append(
                    _ok(
                        f"fixture:{fixture}",
                        "No fixture declared; task runs without one",
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


def _display_path(resolved: Path, config_dir: Path) -> str:
    """Render a resolved file path readably for `validate` output.

    Some config values (notably `build.dockerfile`, which `init` writes relative
    to the repo/project dir so the Docker build context resolves) round-trip into
    long `../../../..`-style cwd-relative walks. Prefer a config-dir-relative path
    when the file lives under the config dir, otherwise fall back to an absolute
    path — whichever is shorter — so paths stay short and verifiable (issue #130).
    """
    resolved = resolved.resolve()
    abs_str = str(resolved)
    try:
        rel_str = str(resolved.relative_to(config_dir.resolve()))
    except ValueError:
        return abs_str
    return rel_str if len(rel_str) <= len(abs_str) else abs_str


def check_script_references(config: Config) -> list[CheckResult]:
    """Verify variant build/run files and task hook/health_check/evaluator
    script paths referenced by the config exist on disk.

    Found paths are echoed in a readable form (config-dir-relative or absolute,
    whichever is shorter) rather than the raw config value, which can be a deep
    `../../..` walk for out-of-tree config dirs (issue #130).
    """
    results: list[CheckResult] = []
    config_dir = config.config_dir

    for variant in config.variants:
        if variant.dockerfile:
            path = (config.project_dir / variant.dockerfile).resolve()
            if path.is_file():
                results.append(
                    _ok(
                        f"variant:{variant.name}:dockerfile",
                        f"Found {_display_path(path, config_dir)}",
                    )
                )
            else:
                results.append(
                    _fail(
                        f"variant:{variant.name}:dockerfile",
                        f"Dockerfile '{variant.dockerfile}' not found for variant '{variant.name}'",
                        f"Expected at: {_display_path(path, config_dir)}",
                    )
                )
        if variant.run_script:
            path = (config.project_dir / variant.run_script).resolve()
            if path.is_file():
                results.append(
                    _ok(
                        f"variant:{variant.name}:run_script",
                        f"Found {_display_path(path, config_dir)}",
                    )
                )
            else:
                results.append(
                    _fail(
                        f"variant:{variant.name}:run_script",
                        f"Run script '{variant.run_script}' not found for variant '{variant.name}'",
                        f"Expected at: {_display_path(path, config_dir)}",
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
                results.append(_ok(check_name, f"Found {_display_path(path, config_dir)}"))
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
                results.append(_ok(check_name, f"Found {_display_path(path, config_dir)}"))
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
    output_instruction) resolves for every variant it could run under.

    Non-blocking: `Config.resolve_prompt()` interpolates with plain
    `str.replace` and leaves any unresolved `{token}` in the prompt text
    as-is rather than erroring (e.g. a prompt asking the model to "emit JSON
    like {status}" is valid and runs fine today). Reported as a warning so
    genuine typos in var names are still surfaced without blocking `run`.
    """
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
                    _warn(
                        check_name,
                        f"Task '{task.name}' references undefined var(s) {missing} for "
                        f"variant '{variant.name}' (left as literal text at run time)",
                        f"Define {', '.join(missing)} in vars:, task.vars, or variant.vars — "
                        "or ignore if this is intentional literal text (e.g. JSON braces).",
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
    token = (
        os.environ.get("GITHUB_TOKEN", "").strip()
        or os.environ.get("COPILOT_GITHUB_TOKEN", "").strip()
    )
    if token:
        return _ok("github_token", "GITHUB_TOKEN/COPILOT_GITHUB_TOKEN is set")
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
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
