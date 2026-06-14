"""Configuration loading and validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when an eval configuration is invalid."""


EVALUATOR_TYPES = ("judge", "script", "contains", "regex")
PARALLEL_MODES = ("off", "per_task", "full")
OUTPUT_FORMATS = ("text", "json")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class RunnerConfig:
    epochs: int = 1
    timeout_seconds: int = 300
    model: str | None = None
    judge_model: str | None = "gpt-4.1"
    reasoning_effort: str | None = None
    max_turns: int | None = None
    parallel: str = "off"  # off | per_task | full
    max_workers: int = 8
    judge_timeout_seconds: int = 60
    output_format: str = "text"
    capture_content: bool = True
    container_image_base: str = "copilot-eval"
    copilot_version: str = "1.0.18"
    otel_endpoint: str = "http://host.docker.internal:4318"
    jaeger_url: str = "http://localhost:16686"
    # analyze: how many traces to request from Jaeger, and how long to wait
    # for ingestion to catch up with the expected set of runs.
    trace_fetch_limit: int = 2000
    trace_fetch_retries: int = 5
    trace_fetch_retry_delay: float = 2.0


@dataclass
class Variant:
    name: str
    description: str = ""
    dockerfile: str | None = None
    run_script: str | None = None
    model: str | None = None
    vars: dict[str, str] = field(default_factory=dict)

    @property
    def image_tag(self) -> str:
        return self.name


@dataclass
class Evaluator:
    """Evaluation criterion. type: judge | script | contains | regex."""
    name: str
    type: str = "judge"
    prompt: str | None = None     # type=judge
    script: str | None = None     # type=script
    value: str | None = None      # type=contains/regex


@dataclass
class Hooks:
    before_run: str | None = None
    after_run: str | None = None
    # Failure policy for before_run: "fail" aborts the run (setup_failed),
    # "warn" logs and continues. after_run failures are always warned and
    # surfaced in the run's scores regardless of this setting.
    on_failure: str = "fail"


@dataclass
class Task:
    name: str
    prompt: str
    enabled: bool = True
    fixture: str | None = None
    timeout_seconds: int | None = None
    health_check: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=Hooks)
    evaluators: list[Evaluator] = field(default_factory=list)


@dataclass
class Config:
    vars: dict[str, str]
    runner: RunnerConfig
    tasks: list[Task]
    variants: list[Variant]
    project_dir: Path
    config_dir: Path

    @property
    def env_file(self) -> Path:
        return self.project_dir / ".env"

    @property
    def results_dir(self) -> Path:
        return self.project_dir / "results"

    def get_task(self, name: str) -> Task | None:
        return next((t for t in self.tasks if t.name == name), None)

    def get_variant(self, name: str) -> Variant | None:
        return next((v for v in self.variants if v.name == name), None)

    def enabled_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.enabled]

    def image_name(self, variant: Variant) -> str:
        return f"{self.runner.container_image_base}:{variant.image_tag}"

    def resolve_vars(self, task: Task, variant: Variant) -> dict[str, str]:
        """Merge global vars → task vars → variant vars."""
        return {**self.vars, **task.vars, **variant.vars}

    def resolve_prompt(self, task: Task, variant: Variant) -> str:
        result = task.prompt
        for key, value in self.resolve_vars(task, variant).items():
            result = result.replace("{" + key + "}", str(value))
        result += "\n\nSave all output files under /workspace/output/."
        return result


def load_config(config_dir: Path | None = None) -> Config:
    project_dir = Path(__file__).resolve().parent.parent
    if config_dir is None:
        config_dir = project_dir

    config_path = config_dir / "eval-config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    vars_dict = {str(k): str(v) for k, v in (raw.get("vars") or {}).items()}

    runner_raw = raw.get("runner") or {}
    runner = _build_runner(runner_raw)

    tasks = _load_tasks(config_dir, raw)
    variants = _load_variants(config_dir, raw)

    _check_duplicate_names(tasks, "task")
    _check_duplicate_names(variants, "variant")

    return Config(vars=vars_dict, runner=runner, tasks=tasks, variants=variants,
                  project_dir=project_dir, config_dir=config_dir)


def _build_runner(runner_raw: dict[str, Any]) -> RunnerConfig:
    if not isinstance(runner_raw, dict):
        raise ConfigError(f"'runner' must be a mapping, got {type(runner_raw).__name__}.")

    parallel = runner_raw.get("parallel", "off")
    if parallel not in PARALLEL_MODES:
        raise ConfigError(
            f"runner.parallel has invalid value '{parallel}'. Must be one of: {', '.join(PARALLEL_MODES)}."
        )
    output_format = runner_raw.get("output_format", "text")
    if output_format not in OUTPUT_FORMATS:
        raise ConfigError(
            f"runner.output_format has invalid value '{output_format}'. "
            f"Must be one of: {', '.join(OUTPUT_FORMATS)}."
        )

    epochs = _require_int(runner_raw, "epochs", 1, minimum=1)
    timeout_seconds = _require_int(runner_raw, "timeout_seconds", 300, minimum=1)
    max_workers = _require_int(runner_raw, "max_workers", 8, minimum=1)
    judge_timeout_seconds = _require_int(runner_raw, "judge_timeout_seconds", 60, minimum=1)
    max_turns = runner_raw.get("max_turns")
    if max_turns is not None:
        max_turns = _coerce_int("runner.max_turns", max_turns, minimum=1)

    trace_fetch_limit = _require_int(runner_raw, "trace_fetch_limit", 2000, minimum=1)
    trace_fetch_retries = _require_int(runner_raw, "trace_fetch_retries", 5, minimum=0)
    trace_fetch_retry_delay = _require_number(runner_raw, "trace_fetch_retry_delay", 2.0, minimum=0)

    return RunnerConfig(
        epochs=epochs,
        timeout_seconds=timeout_seconds,
        model=runner_raw.get("model"),
        judge_model=runner_raw.get("judge_model", "gpt-4.1"),
        reasoning_effort=runner_raw.get("reasoning_effort"),
        max_turns=max_turns,
        parallel=parallel,
        max_workers=max_workers,
        judge_timeout_seconds=judge_timeout_seconds,
        output_format=output_format,
        capture_content=runner_raw.get("capture_content", True),
        container_image_base=runner_raw.get("container_image_base", "copilot-eval"),
        copilot_version=runner_raw.get("copilot_version", "1.0.18"),
        otel_endpoint=runner_raw.get("otel_endpoint", "http://host.docker.internal:4318"),
        jaeger_url=runner_raw.get("jaeger_url", "http://localhost:16686"),
        trace_fetch_limit=trace_fetch_limit,
        trace_fetch_retries=trace_fetch_retries,
        trace_fetch_retry_delay=trace_fetch_retry_delay,
    )


def _coerce_int(key: str, value: object, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer, got {value!r}.")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}.")
    return value


def _require_int(raw: dict[str, Any], key: str, default: int, minimum: int | None = None) -> int:
    if key not in raw or raw[key] is None:
        return default
    return _coerce_int(f"runner.{key}", raw[key], minimum=minimum)


def _require_number(raw: dict[str, Any], key: str, default: float, minimum: float | None = None) -> float:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"runner.{key} must be a number, got {value!r}.")
    if minimum is not None and value < minimum:
        raise ConfigError(f"runner.{key} must be >= {minimum}, got {value}.")
    return float(value)


def _check_duplicate_names(items: list[Any], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item.name in seen:
            raise ConfigError(f"Duplicate {label} name '{item.name}'.")
        seen.add(item.name)


# --- Internal parsers ---

def _parse_evaluators(raw_list: list[Any] | None, context: str = "") -> list[Evaluator]:
    if not raw_list:
        return []
    where = f" in {context}" if context else ""
    evaluators: list[Evaluator] = []
    seen: set[str] = set()
    for i, e in enumerate(raw_list):
        if not isinstance(e, dict):
            raise ConfigError(f"Evaluator #{i + 1}{where} must be a mapping, got {type(e).__name__}.")
        name = e.get("name")
        if not name or not str(name).strip():
            raise ConfigError(f"Evaluator #{i + 1}{where} is missing a required 'name'.")
        name = str(name)
        if not _NAME_RE.match(name):
            raise ConfigError(
                f"Evaluator name '{name}'{where} is invalid. Use letters, digits, '.', '_' or '-' "
                f"and start with a letter or digit."
            )
        if name in seen:
            raise ConfigError(f"Duplicate evaluator name '{name}'{where}.")
        seen.add(name)

        etype = e.get("type", "judge")
        if etype not in EVALUATOR_TYPES:
            raise ConfigError(
                f"Evaluator '{name}'{where} has invalid type '{etype}'. "
                f"Must be one of: {', '.join(EVALUATOR_TYPES)}."
            )
        prompt, script, value = e.get("prompt"), e.get("script"), e.get("value")
        if etype == "judge" and not prompt:
            raise ConfigError(f"Evaluator '{name}'{where} (type=judge) requires a 'prompt'.")
        if etype == "script" and not script:
            raise ConfigError(f"Evaluator '{name}'{where} (type=script) requires a 'script'.")
        if etype in ("contains", "regex") and not value:
            raise ConfigError(f"Evaluator '{name}'{where} (type={etype}) requires a 'value'.")
        if etype == "regex" and value is not None:
            try:
                re.compile(str(value))
            except re.error as exc:
                raise ConfigError(f"Evaluator '{name}'{where} has an invalid regex 'value': {exc}.") from exc

        evaluators.append(Evaluator(name=name, type=etype, prompt=prompt, script=script, value=value))
    return evaluators


def _parse_hooks(raw: dict[str, Any] | None) -> Hooks:
    if not raw:
        return Hooks()
    on_failure = str(raw.get("on_failure", "fail")).lower()
    if on_failure not in ("fail", "warn"):
        raise ConfigError(
            f"Invalid hooks.on_failure '{on_failure}'. Use 'fail' or 'warn'."
        )
    return Hooks(
        before_run=raw.get("before_run"),
        after_run=raw.get("after_run"),
        on_failure=on_failure,
    )


def _parse_task(p: dict[str, Any], fallback_name: str = "") -> Task:
    if not isinstance(p, dict):
        raise ConfigError(f"Task definition must be a mapping, got {type(p).__name__}.")
    name = str(p.get("name", fallback_name) or "")
    if not name.strip():
        raise ConfigError("Task is missing a required 'name'.")
    if not _NAME_RE.match(name):
        raise ConfigError(
            f"Task name '{name}' is invalid. Use letters, digits, '.', '_' or '-' "
            f"and start with a letter or digit."
        )

    prompt = p.get("prompt")
    if not prompt or not str(prompt).strip():
        raise ConfigError(f"Task '{name}' is missing a required 'prompt'.")

    # Evaluators: try evaluators → judges → metrics.judges + verify (backward compat)
    evaluators_raw = p.get("evaluators")
    if not evaluators_raw:
        judges = p.get("judges") or (p.get("metrics") or {}).get("judges") or []
        try:
            evaluators_raw = [{"name": j["name"], "type": "judge", "prompt": j["prompt"]} for j in judges]
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"Task '{name}' has a malformed 'judges' entry (missing {exc}).") from exc
        if p.get("verify"):
            evaluators_raw.append({"name": "verify", "type": "script", "script": p["verify"]})

    # Hooks: try hooks → reset_script (backward compat)
    hooks_raw = p.get("hooks")
    if not hooks_raw and p.get("reset_script"):
        hooks_raw = {"before_run": p["reset_script"]}

    return Task(
        name=name,
        prompt=prompt,
        enabled=p.get("enabled", True),
        fixture=p.get("fixture"),
        timeout_seconds=p.get("timeout_seconds"),
        health_check=p.get("health_check"),
        vars={str(k): str(v) for k, v in (p.get("vars") or {}).items()},
        hooks=_parse_hooks(hooks_raw),
        evaluators=_parse_evaluators(evaluators_raw, context=f"task '{name}'"),
    )


def _parse_variant(v: dict[str, Any], fallback_name: str = "") -> Variant:
    if not isinstance(v, dict):
        raise ConfigError(f"Variant definition must be a mapping, got {type(v).__name__}.")
    name = str(v.get("name", fallback_name) or "")
    if not name.strip():
        raise ConfigError("Variant is missing a required 'name'.")
    if not _NAME_RE.match(name):
        raise ConfigError(
            f"Variant name '{name}' is invalid. Use letters, digits, '.', '_' or '-' "
            f"and start with a letter or digit."
        )
    build = v.get("build") or {}
    run = v.get("run") or {}
    return Variant(
        name=name,
        description=v.get("description", ""),
        dockerfile=build.get("dockerfile"),
        run_script=run.get("script"),
        model=v.get("model"),
        vars={str(k): str(val) for k, val in (v.get("vars") or {}).items()},
    )


def _load_tasks(config_dir: Path, raw_config: dict[str, Any]) -> list[Task]:
    tasks: list[Task] = []

    # Primary: tasks/*.yaml files
    tasks_dir = config_dir / "tasks"
    if tasks_dir.is_dir():
        for yaml_file in sorted(tasks_dir.glob("*.yaml")):
            with open(yaml_file) as f:
                p = yaml.safe_load(f)
            if p:
                tasks.append(_parse_task(p, fallback_name=yaml_file.stem))

    # Fallback: inline in eval-config.yaml
    if not tasks:
        inline = raw_config.get("tasks") or []
        if isinstance(inline, list):
            for p in inline:
                tasks.append(_parse_task(p))
        elif isinstance(inline, dict):
            for name, p in inline.items():
                tasks.append(_parse_task({**p, "name": name}, fallback_name=name))

    return tasks


def _load_variants(config_dir: Path, raw_config: dict[str, Any]) -> list[Variant]:
    variants: list[Variant] = []

    # Primary: variants/*.yaml files
    variants_dir = config_dir / "variants"
    if variants_dir.is_dir():
        for yaml_file in sorted(variants_dir.glob("*.yaml")):
            with open(yaml_file) as f:
                v = yaml.safe_load(f)
            if v:
                variants.append(_parse_variant(v, fallback_name=yaml_file.stem))

    # Fallback: inline in eval-config.yaml
    if not variants:
        inline = raw_config.get("variants") or []
        if isinstance(inline, list):
            for v in inline:
                variants.append(_parse_variant(v))

    # Default
    if not variants:
        variants = [Variant(name="baseline")]

    return variants
