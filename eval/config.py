"""Configuration loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when an eval configuration is invalid."""


# Single source of truth for the pinned Copilot CLI version. Referenced by the
# config defaults below and injected into the Docker build via the
# COPILOT_VERSION build-arg (see cli._build_images). Update this in one place to
# bump the version everywhere.
DEFAULT_COPILOT_VERSION = "1.0.18"

EVALUATOR_TYPES = ("judge", "script", "contains", "regex", "metric", "python")
METRIC_OPS = ("<", "<=", ">", ">=", "==", "!=")
PARALLEL_MODES = ("off", "per_task", "full")
VARIANT_ORDER_MODES = ("fixed", "counterbalance", "random")
OUTPUT_FORMATS = ("text", "json")
JUDGE_AGGREGATE_MODES = ("median", "mean", "majority")
DEFAULT_OUTPUT_INSTRUCTION = "Save all output files under /workspace/output/."
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# `docker run --cpus`: a positive (optionally fractional) number of CPUs.
_CPUS_RE = re.compile(r"^\d+(\.\d+)?$")
# `docker run --memory`: a positive integer optionally suffixed with a byte
# unit (b|k|m|g, case-insensitive). e.g. "512m", "2g", "1073741824".
_MEMORY_RE = re.compile(r"^\d+(\.\d+)?[bkmgBKMG]?$")


@dataclass
class ResourceLimits:
    """Docker container resource limits (issue #72), used to reduce metric
    noise from containers competing for host CPU/memory/process resources.
    All fields are optional; `None` means "no limit" (current behavior)."""

    cpus: str | None = None  # --cpus, e.g. "2.0"
    memory: str | None = None  # --memory, e.g. "4g"
    pids_limit: int | None = None  # --pids-limit, e.g. 100


@dataclass
class RunnerConfig:
    epochs: int = 1
    timeout_seconds: int = 300
    model: str | None = None
    judge_model: str | None = "gpt-4.1"
    # Self-consistency: sample each judge this many times and aggregate the
    # successful scores. 1 keeps the legacy single-shot behavior.
    judge_samples: int = 1
    judge_aggregate: str = "median"  # median | mean | majority
    # Opt-in: score all of a task's judges in a single LLM call (keyed by
    # evaluator name), then split the response back into per-evaluator scores.
    # Cuts judge calls from n_judges × judge_samples to judge_samples at the
    # cost of judge independence (halo effect, shared failure blast radius,
    # correlated per-criterion noise). Default False keeps judges independent.
    judge_batch: bool = False
    reasoning_effort: str | None = None
    max_turns: int | None = None
    parallel: str = "off"  # off | per_task | full
    max_workers: int = 8
    variant_order: str = "fixed"  # fixed | counterbalance | random
    seed: int | None = None  # RNG seed for variant_order=random (reproducibility)
    judge_timeout_seconds: int = 60
    # Expected host `copilot --version` for judge runs. When set, analyze warns
    # if the host Copilot CLI used for judging differs from this value.
    judge_copilot_version: str | None = None
    # Context budgets (in characters) for the judge prompt. Conversation and
    # output-file text are truncated to these limits; truncation is recorded in
    # judge score metadata and surfaced in the report.
    judge_max_conversation_chars: int = 8000
    judge_max_output_chars: int = 8000
    output_format: str = "text"
    capture_content: bool = True
    # Instruction appended to every prompt so generated files reach the judges.
    # Empty string disables it; supports {var} interpolation like prompts.
    output_instruction: str = DEFAULT_OUTPUT_INSTRUCTION
    container_image_base: str = "copilot-eval"
    copilot_version: str = DEFAULT_COPILOT_VERSION
    otel_endpoint: str = "http://host.docker.internal:4318"
    jaeger_url: str = "http://localhost:16686"
    collector: str = "file"  # file | jaeger
    # Agent execution backend. "docker" (DockerCLIRunner) is built in; other
    # backends can be registered via the `copilot_eval.runners` entry-point
    # group (see eval.runners.load_runner_plugins, issue #66).
    backend: str = "docker"
    # analyze: how many traces to request from a remote collector, and how long to wait
    # for ingestion to catch up with the expected set of runs.
    trace_fetch_limit: int = 2000
    trace_fetch_retries: int = 5
    trace_fetch_retry_delay: float = 2.0
    # run_one: retry a run when it fails with a transient error (Docker daemon
    # hiccup, container timeout) instead of permanently recording it as
    # setup_failed/timeout. 0 keeps the legacy no-retry behavior. Deterministic
    # failures (AuthError, HookError, FixtureError) are never retried. Delay
    # backs off exponentially (delay * 2**attempt, capped at 60s) between
    # attempts. See issue #69.
    retries: int = 0
    retry_delay: float = 5.0
    # Container resource limits (issue #72). Unset fields mean "no limit",
    # matching the behavior before this option existed.
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    # Cost governance (issue #70): maximum estimated USD cost for a `run`
    # invocation (see eval.services.cost_service). None (default) means
    # unlimited -- no pre-flight budget gate is applied. When set, `run`
    # aborts before doing any Docker/agent work if the pre-flight estimate
    # exceeds this value. CLI `--budget-limit` overrides this per-invocation.
    budget_limit: float | None = None


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
    """Evaluation criterion. type: judge | script | contains | regex | metric."""

    name: str
    type: str = "judge"
    prompt: str | None = None  # type=judge (composed from criterion+rubric when given)
    script: str | None = None  # type=script
    value: str | None = None  # type=contains/regex
    # type=judge structured form: a scoring axis (criterion) plus score→anchor
    # descriptions (rubric). When provided, the framework composes `prompt` from
    # them; the strict-JSON output contract is appended by the runner.
    criterion: str | None = None
    rubric: dict[int, str] | None = None
    metric: str | None = None  # type=metric: RunMetrics field to assert on
    op: str | None = None  # type=metric: comparison operator
    threshold: float | None = None  # type=metric: numeric threshold


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
    # Multiple fixtures expand the eval matrix along an input-coverage axis:
    # the task runs once per fixture (variant × fixture × epoch). Empty means
    # "single fixture" and falls back to `fixture` / the task name.
    fixtures: list[str] = field(default_factory=list)
    timeout_seconds: int | None = None
    health_check: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=Hooks)
    evaluators: list[Evaluator] = field(default_factory=list)

    def fixture_names(self) -> list[str]:
        """Effective list of fixture directory names this task runs against.

        Falls back to the singular `fixture`, then to the task name, so a task
        with no fixture declared behaves exactly as before (one run per
        variant × epoch reading `fixtures/<task-name>/`).
        """
        if self.fixtures:
            return list(self.fixtures)
        if self.fixture:
            return [self.fixture]
        return [self.name]

    @property
    def is_multi_fixture(self) -> bool:
        """True when the task spans more than one fixture (input-coverage axis)."""
        return len(self.fixture_names()) > 1

    def fixture_label(self, fixture: str) -> str:
        """Reporting label for a run's fixture: empty for single-fixture tasks
        (keeps legacy file names / report layout), the fixture name otherwise."""
        return fixture if self.is_multi_fixture else ""


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
        vars_ = self.resolve_vars(task, variant)

        def interpolate(text: str) -> str:
            for key, value in vars_.items():
                text = text.replace("{" + key + "}", str(value))
            return text

        result = interpolate(task.prompt)
        instruction = interpolate(self.runner.output_instruction)
        if instruction:
            result += "\n\n" + instruction
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

    return Config(
        vars=vars_dict,
        runner=runner,
        tasks=tasks,
        variants=variants,
        project_dir=project_dir,
        config_dir=config_dir,
    )


def _build_runner(runner_raw: dict[str, Any]) -> RunnerConfig:
    if not isinstance(runner_raw, dict):
        raise ConfigError(f"'runner' must be a mapping, got {type(runner_raw).__name__}.")

    parallel = runner_raw.get("parallel", "off")
    if parallel not in PARALLEL_MODES:
        raise ConfigError(
            f"runner.parallel has invalid value '{parallel}'. Must be one of: {', '.join(PARALLEL_MODES)}."
        )
    variant_order = runner_raw.get("variant_order", "fixed")
    if variant_order not in VARIANT_ORDER_MODES:
        raise ConfigError(
            f"runner.variant_order has invalid value '{variant_order}'. "
            f"Must be one of: {', '.join(VARIANT_ORDER_MODES)}."
        )
    output_format = runner_raw.get("output_format", "text")
    if output_format not in OUTPUT_FORMATS:
        raise ConfigError(
            f"runner.output_format has invalid value '{output_format}'. "
            f"Must be one of: {', '.join(OUTPUT_FORMATS)}."
        )
    judge_aggregate = runner_raw.get("judge_aggregate", "median")
    if judge_aggregate not in JUDGE_AGGREGATE_MODES:
        raise ConfigError(
            f"runner.judge_aggregate has invalid value '{judge_aggregate}'. "
            f"Must be one of: {', '.join(JUDGE_AGGREGATE_MODES)}."
        )
    collector = runner_raw.get("collector", "file")
    from eval.collectors import COLLECTOR_REGISTRY

    collector_types = tuple(sorted(COLLECTOR_REGISTRY))
    if collector not in collector_types:
        raise ConfigError(
            f"runner.collector has invalid value '{collector}'. Must be one of: {', '.join(collector_types)}."
        )

    backend = runner_raw.get("backend", "docker")
    from eval.runners import RUNNER_REGISTRY

    runner_backends = tuple(sorted(RUNNER_REGISTRY))
    if backend not in runner_backends:
        raise ConfigError(
            f"runner.backend has invalid value '{backend}'. Must be one of: {', '.join(runner_backends)}."
        )

    output_instruction = runner_raw.get("output_instruction")
    if output_instruction is None:
        output_instruction = DEFAULT_OUTPUT_INSTRUCTION
    elif not isinstance(output_instruction, str):
        raise ConfigError(
            f"runner.output_instruction must be a string, got {output_instruction!r}."
        )

    epochs = _require_int(runner_raw, "epochs", 1, minimum=1)
    timeout_seconds = _require_int(runner_raw, "timeout_seconds", 300, minimum=1)
    max_workers = _require_int(runner_raw, "max_workers", 8, minimum=1)
    judge_timeout_seconds = _require_int(runner_raw, "judge_timeout_seconds", 60, minimum=1)
    judge_samples = _require_int(runner_raw, "judge_samples", 1, minimum=1)
    judge_batch = _require_bool(runner_raw, "judge_batch", False)
    judge_max_conversation_chars = _require_int(
        runner_raw, "judge_max_conversation_chars", 8000, minimum=1
    )
    judge_max_output_chars = _require_int(runner_raw, "judge_max_output_chars", 8000, minimum=1)
    max_turns = runner_raw.get("max_turns")
    if max_turns is not None:
        max_turns = _coerce_int("runner.max_turns", max_turns, minimum=1)

    seed = runner_raw.get("seed")
    if seed is not None:
        seed = _coerce_int("runner.seed", seed)

    trace_fetch_limit = _require_int(runner_raw, "trace_fetch_limit", 2000, minimum=1)
    trace_fetch_retries = _require_int(runner_raw, "trace_fetch_retries", 5, minimum=0)
    trace_fetch_retry_delay = _require_number(runner_raw, "trace_fetch_retry_delay", 2.0, minimum=0)
    retries = _require_int(runner_raw, "retries", 0, minimum=0)
    retry_delay = _require_number(runner_raw, "retry_delay", 5.0, minimum=0)
    resources = _build_resources(runner_raw.get("resources"))
    budget_limit = _optional_number(runner_raw, "budget_limit", minimum=0)

    return RunnerConfig(
        epochs=epochs,
        timeout_seconds=timeout_seconds,
        model=runner_raw.get("model"),
        judge_model=runner_raw.get("judge_model", "gpt-4.1"),
        judge_samples=judge_samples,
        judge_aggregate=judge_aggregate,
        judge_batch=judge_batch,
        reasoning_effort=runner_raw.get("reasoning_effort"),
        max_turns=max_turns,
        parallel=parallel,
        max_workers=max_workers,
        variant_order=variant_order,
        seed=seed,
        judge_timeout_seconds=judge_timeout_seconds,
        judge_copilot_version=runner_raw.get("judge_copilot_version"),
        judge_max_conversation_chars=judge_max_conversation_chars,
        judge_max_output_chars=judge_max_output_chars,
        output_format=output_format,
        capture_content=runner_raw.get("capture_content", True),
        output_instruction=output_instruction,
        container_image_base=runner_raw.get("container_image_base", "copilot-eval"),
        copilot_version=runner_raw.get("copilot_version", DEFAULT_COPILOT_VERSION),
        otel_endpoint=runner_raw.get("otel_endpoint", "http://host.docker.internal:4318"),
        jaeger_url=runner_raw.get("jaeger_url", "http://localhost:16686"),
        collector=collector,
        backend=backend,
        trace_fetch_limit=trace_fetch_limit,
        trace_fetch_retries=trace_fetch_retries,
        trace_fetch_retry_delay=trace_fetch_retry_delay,
        retries=retries,
        retry_delay=retry_delay,
        resources=resources,
        budget_limit=budget_limit,
    )


def _build_resources(resources_raw: Any) -> ResourceLimits:
    if resources_raw is None:
        return ResourceLimits()
    if not isinstance(resources_raw, dict):
        raise ConfigError(
            f"runner.resources must be a mapping, got {type(resources_raw).__name__}."
        )

    cpus = resources_raw.get("cpus")
    if cpus is not None:
        if not isinstance(cpus, str) or not _CPUS_RE.match(cpus):
            raise ConfigError(
                f"runner.resources.cpus must be a positive number as a string "
                f"(e.g. '2.0'), got {cpus!r}."
            )
        if float(cpus) <= 0:
            raise ConfigError(f"runner.resources.cpus must be > 0, got {cpus!r}.")

    memory = resources_raw.get("memory")
    if memory is not None:
        if not isinstance(memory, str) or not _MEMORY_RE.match(memory):
            raise ConfigError(
                f"runner.resources.memory must be a Docker memory value "
                f"(e.g. '512m', '2g', '1073741824'), got {memory!r}."
            )

    pids_limit = resources_raw.get("pids_limit")
    if pids_limit is not None:
        pids_limit = _coerce_int("runner.resources.pids_limit", pids_limit, minimum=1)

    return ResourceLimits(cpus=cpus, memory=memory, pids_limit=pids_limit)


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


def _require_number(
    raw: dict[str, Any], key: str, default: float, minimum: float | None = None
) -> float:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"runner.{key} must be a number, got {value!r}.")
    if minimum is not None and value < minimum:
        raise ConfigError(f"runner.{key} must be >= {minimum}, got {value}.")
    return float(value)


def _optional_number(raw: dict[str, Any], key: str, minimum: float | None = None) -> float | None:
    """Like :func:`_require_number` but the field is optional (default None)."""
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"runner.{key} must be a number, got {value!r}.")
    if minimum is not None and value < minimum:
        raise ConfigError(f"runner.{key} must be >= {minimum}, got {value}.")
    return float(value)


def _require_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"runner.{key} must be a boolean, got {value!r}.")
    return value


def _check_duplicate_names(items: list[Any], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item.name in seen:
            raise ConfigError(f"Duplicate {label} name '{item.name}'.")
        seen.add(item.name)


# --- Internal parsers ---


def _known_evaluator_types() -> tuple[str, ...]:
    """Return the currently registered evaluator type strings.

    Built-in types always come from ``EVALUATOR_TYPES``; additional types
    registered via entry points (see ``eval.evaluators.load_evaluator_plugins``,
    issue #66) are picked up too, so a plugin-defined ``type:`` validates
    without any change here. This is a local import — ``eval.evaluators``
    transitively imports ``eval.runner``, which imports this module, so
    importing it at module scope would create a circular import.
    """
    try:
        from eval.evaluators import EVALUATOR_REGISTRY
    except ImportError:  # pragma: no cover - defensive, should not happen normally
        return EVALUATOR_TYPES
    return tuple(dict.fromkeys((*EVALUATOR_TYPES, *EVALUATOR_REGISTRY.keys())))


def _parse_evaluators(raw_list: list[Any] | None, context: str = "") -> list[Evaluator]:
    if not raw_list:
        return []
    where = f" in {context}" if context else ""
    evaluators: list[Evaluator] = []
    seen: set[str] = set()
    for i, e in enumerate(raw_list):
        if not isinstance(e, dict):
            raise ConfigError(
                f"Evaluator #{i + 1}{where} must be a mapping, got {type(e).__name__}."
            )
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
        known_types = _known_evaluator_types()
        if etype not in known_types:
            raise ConfigError(
                f"Evaluator '{name}'{where} has invalid type '{etype}'. "
                f"Must be one of: {', '.join(known_types)}."
            )
        prompt, script, value = e.get("prompt"), e.get("script"), e.get("value")
        criterion, rubric = None, None
        if etype == "judge":
            criterion, rubric = _parse_rubric(e, name, where)
            if rubric is not None:
                if prompt:
                    raise ConfigError(
                        f"Evaluator '{name}'{where} (type=judge) cannot set both 'prompt' and "
                        f"'rubric'; use one or the other."
                    )
                assert criterion is not None  # guaranteed by _parse_rubric when rubric is set
                prompt = _build_rubric_prompt(criterion, rubric)
            elif not prompt:
                raise ConfigError(
                    f"Evaluator '{name}'{where} (type=judge) requires a 'prompt' or a 'rubric'."
                )
        if etype == "script" and not script:
            raise ConfigError(f"Evaluator '{name}'{where} (type=script) requires a 'script'.")
        if etype == "python":
            if not script:
                raise ConfigError(
                    f"Evaluator '{name}'{where} (type=python) requires a 'script' in "
                    f"'module:func' format."
                )
            script_str = str(script)
            module_part, _, func_part = script_str.rpartition(":")
            if not module_part or not func_part:
                raise ConfigError(
                    f"Evaluator '{name}'{where} (type=python) 'script' must be in "
                    f"'module:func' format with both parts non-empty, got '{script}'."
                )
        if etype in ("contains", "regex") and not value:
            raise ConfigError(f"Evaluator '{name}'{where} (type={etype}) requires a 'value'.")
        if etype == "regex" and value is not None:
            try:
                re.compile(str(value))
            except re.error as exc:
                raise ConfigError(
                    f"Evaluator '{name}'{where} has an invalid regex 'value': {exc}."
                ) from exc

        metric, op, threshold = None, None, None
        if etype == "metric":
            metric, op, threshold = _parse_metric_fields(e, name, where)
            value = None  # metric stores its numeric threshold, not a string value

        evaluators.append(
            Evaluator(
                name=name,
                type=etype,
                prompt=prompt,
                script=script,
                value=value,
                criterion=criterion,
                rubric=rubric,
                metric=metric,
                op=op,
                threshold=threshold,
            )
        )
    return evaluators


def _parse_rubric(
    e: dict[str, Any], name: str, where: str
) -> tuple[str | None, dict[int, str] | None]:
    """Validate and normalize a judge's structured `criterion`/`rubric` fields.

    Returns ``(criterion, rubric)`` with integer-keyed anchors, or ``(criterion, None)``
    when no rubric is present. Raises ``ConfigError`` on malformed input.
    """
    raw_rubric = e.get("rubric")
    criterion = e.get("criterion")
    if raw_rubric is None:
        if criterion:
            raise ConfigError(
                f"Evaluator '{name}'{where} (type=judge) sets 'criterion' without a 'rubric'."
            )
        return None, None

    if not isinstance(raw_rubric, dict) or not raw_rubric:
        raise ConfigError(
            f"Evaluator '{name}'{where} 'rubric' must be a non-empty mapping of score to description."
        )
    if not criterion or not str(criterion).strip():
        raise ConfigError(
            f"Evaluator '{name}'{where} (type=judge) with a 'rubric' requires a non-empty 'criterion'."
        )

    rubric: dict[int, str] = {}
    for k, v in raw_rubric.items():
        score = _coerce_rubric_score(k)
        if score is None:
            raise ConfigError(
                f"Evaluator '{name}'{where} 'rubric' has a non-integer score key {k!r}."
            )
        if not isinstance(v, str) or not v.strip():
            raise ConfigError(
                f"Evaluator '{name}'{where} 'rubric' anchor for score {score} must be a non-empty string."
            )
        rubric[score] = v.strip()
    return str(criterion).strip(), rubric


def _coerce_rubric_score(key: object) -> int | None:
    """Coerce a rubric key to an int. YAML keys may be ints or numeric strings.

    Returns ``None`` when the key is not a valid integer score.
    """
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key
    if isinstance(key, str):
        try:
            return int(key.strip())
        except ValueError:
            return None
    return None


def _build_rubric_prompt(criterion: str, rubric: dict[int, str]) -> str:
    """Compose a judge prompt from a criterion and score→anchor descriptions.

    Anchors are listed high-to-low. The strict-JSON output contract is appended
    by the runner, so it is intentionally omitted here.
    """
    scores = sorted(rubric, reverse=True)
    lines = [criterion, "", f"Score from {scores[-1]} to {scores[0]} using these anchors:"]
    lines += [f"- {s}: {rubric[s]}" for s in scores]
    return "\n".join(lines)


def _parse_metric_fields(e: dict[str, Any], name: str, where: str) -> tuple[str, str, float]:
    """Validate and extract the metric/op/value fields of a type=metric evaluator."""
    from eval.trace import METRIC_FIELDS

    metric = e.get("metric")
    if not metric:
        raise ConfigError(f"Evaluator '{name}'{where} (type=metric) requires a 'metric'.")
    metric = str(metric)
    if metric not in METRIC_FIELDS:
        raise ConfigError(
            f"Evaluator '{name}'{where} has invalid metric '{metric}'. "
            f"Must be one of: {', '.join(METRIC_FIELDS)}."
        )

    op = e.get("op")
    if not op:
        raise ConfigError(f"Evaluator '{name}'{where} (type=metric) requires an 'op'.")
    op = str(op)
    if op not in METRIC_OPS:
        raise ConfigError(
            f"Evaluator '{name}'{where} has invalid op '{op}'. "
            f"Must be one of: {', '.join(METRIC_OPS)}."
        )

    raw_value = e.get("value")
    if raw_value is None:
        raise ConfigError(f"Evaluator '{name}'{where} (type=metric) requires a numeric 'value'.")
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ConfigError(
            f"Evaluator '{name}'{where} (type=metric) requires a numeric 'value', got {raw_value!r}."
        )
    return metric, op, float(raw_value)


def _parse_hooks(raw: dict[str, Any] | None) -> Hooks:
    if not raw:
        return Hooks()
    on_failure = str(raw.get("on_failure", "fail")).lower()
    if on_failure not in ("fail", "warn"):
        raise ConfigError(f"Invalid hooks.on_failure '{on_failure}'. Use 'fail' or 'warn'.")
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
            evaluators_raw = [
                {"name": j["name"], "type": "judge", "prompt": j["prompt"]} for j in judges
            ]
        except (KeyError, TypeError) as exc:
            raise ConfigError(
                f"Task '{name}' has a malformed 'judges' entry (missing {exc})."
            ) from exc
        if p.get("verify"):
            evaluators_raw.append({"name": "verify", "type": "script", "script": p["verify"]})

    # Hooks: try hooks → reset_script (backward compat)
    hooks_raw = p.get("hooks")
    if not hooks_raw and p.get("reset_script"):
        hooks_raw = {"before_run": p["reset_script"]}

    fixtures = _parse_fixtures(p.get("fixtures"), name)

    return Task(
        name=name,
        prompt=prompt,
        enabled=p.get("enabled", True),
        fixture=p.get("fixture"),
        fixtures=fixtures,
        timeout_seconds=p.get("timeout_seconds"),
        health_check=p.get("health_check"),
        vars={str(k): str(v) for k, v in (p.get("vars") or {}).items()},
        hooks=_parse_hooks(hooks_raw),
        evaluators=_parse_evaluators(evaluators_raw, context=f"task '{name}'"),
    )


def _parse_fixtures(raw: Any, task_name: str) -> list[str]:
    """Normalize the optional `fixtures` list into a list of fixture names.

    Accepts a list of non-empty, unique strings. Returns [] when unset so the
    task falls back to singular `fixture` / the task name.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(
            f"Task '{task_name}' has an invalid 'fixtures': expected a list, "
            f"got {type(raw).__name__}."
        )
    fixtures: list[str] = []
    seen: set[str] = set()
    for i, f in enumerate(raw):
        if not isinstance(f, str) or not f.strip():
            raise ConfigError(
                f"Task '{task_name}' fixtures[{i}] must be a non-empty string, got {f!r}."
            )
        f = f.strip()
        if not _NAME_RE.match(f):
            raise ConfigError(
                f"Task '{task_name}' fixture name '{f}' is invalid. Use letters, digits, "
                f"'.', '_' or '-' and start with a letter or digit."
            )
        if f in seen:
            raise ConfigError(f"Task '{task_name}' has a duplicate fixture '{f}'.")
        seen.add(f)
        fixtures.append(f)
    return fixtures


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
