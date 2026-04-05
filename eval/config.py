"""Configuration loading and validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class RunnerConfig:
    epochs: int = 1
    timeout_seconds: int = 300
    model: str | None = None
    reasoning_effort: str | None = None
    max_turns: int | None = None
    parallel: bool = False
    output_format: str = "text"
    container_image_base: str = "copilot-eval"
    copilot_version: str = "1.0.18"
    otel_endpoint: str = "http://host.docker.internal:4318"


@dataclass
class Variant:
    name: str
    description: str = ""
    build_script: str | None = None
    run_script: str | None = None
    model: str | None = None

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


@dataclass
class Pattern:
    name: str
    prompt: str
    enabled: bool = True
    fixture: str | None = None
    timeout_seconds: int | None = None
    hooks: Hooks = field(default_factory=Hooks)
    evaluators: list[Evaluator] = field(default_factory=list)


@dataclass
class Config:
    vars: dict[str, str]
    runner: RunnerConfig
    patterns: list[Pattern]
    variants: list[Variant]
    project_dir: Path
    config_dir: Path

    @property
    def env_file(self) -> Path:
        return self.project_dir / ".env"

    @property
    def results_dir(self) -> Path:
        return self.project_dir / "results"

    def get_pattern(self, name: str) -> Pattern | None:
        return next((p for p in self.patterns if p.name == name), None)

    def get_variant(self, name: str) -> Variant | None:
        return next((v for v in self.variants if v.name == name), None)

    def enabled_patterns(self) -> list[Pattern]:
        return [p for p in self.patterns if p.enabled]

    def image_name(self, variant: Variant) -> str:
        return f"{self.runner.container_image_base}:{variant.image_tag}"

    def resolve_prompt(self, pattern: Pattern) -> str:
        result = pattern.prompt
        for key, value in self.vars.items():
            result = result.replace("{" + key + "}", str(value))
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
    runner = RunnerConfig(
        epochs=runner_raw.get("epochs", 1),
        timeout_seconds=runner_raw.get("timeout_seconds", 300),
        model=runner_raw.get("model"),
        reasoning_effort=runner_raw.get("reasoning_effort"),
        max_turns=runner_raw.get("max_turns"),
        parallel=runner_raw.get("parallel", False),
        output_format=runner_raw.get("output_format", "text"),
        container_image_base=runner_raw.get("container_image_base", "copilot-eval"),
        copilot_version=runner_raw.get("copilot_version", "1.0.18"),
        otel_endpoint=runner_raw.get("otel_endpoint", "http://host.docker.internal:4318"),
    )

    patterns = _load_patterns(config_dir, raw)
    variants = _load_variants(config_dir, raw)

    return Config(vars=vars_dict, runner=runner, patterns=patterns, variants=variants,
                  project_dir=project_dir, config_dir=config_dir)


# --- Internal parsers ---

def _parse_evaluators(raw_list: list | None) -> list[Evaluator]:
    if not raw_list:
        return []
    return [Evaluator(
        name=e["name"],
        type=e.get("type", "judge"),
        prompt=e.get("prompt"),
        script=e.get("script"),
        value=e.get("value"),
    ) for e in raw_list]


def _parse_hooks(raw: dict | None) -> Hooks:
    if not raw:
        return Hooks()
    return Hooks(before_run=raw.get("before_run"), after_run=raw.get("after_run"))


def _parse_pattern(p: dict, fallback_name: str = "") -> Pattern:
    # Evaluators: try evaluators → judges → metrics.judges + verify (backward compat)
    evaluators_raw = p.get("evaluators")
    if not evaluators_raw:
        judges = p.get("judges") or (p.get("metrics") or {}).get("judges") or []
        evaluators_raw = [{"name": j["name"], "type": "judge", "prompt": j["prompt"]} for j in judges]
        if p.get("verify"):
            evaluators_raw.append({"name": "verify", "type": "script", "script": p["verify"]})

    # Hooks: try hooks → reset_script (backward compat)
    hooks_raw = p.get("hooks")
    if not hooks_raw and p.get("reset_script"):
        hooks_raw = {"before_run": p["reset_script"]}

    return Pattern(
        name=p.get("name", fallback_name),
        prompt=p.get("prompt", ""),
        enabled=p.get("enabled", True),
        fixture=p.get("fixture"),
        timeout_seconds=p.get("timeout_seconds"),
        hooks=_parse_hooks(hooks_raw),
        evaluators=_parse_evaluators(evaluators_raw),
    )


def _parse_variant(v: dict, fallback_name: str = "") -> Variant:
    build = v.get("build") or {}
    run = v.get("run") or {}
    return Variant(
        name=v.get("name", fallback_name),
        description=v.get("description", ""),
        build_script=build.get("script"),
        run_script=run.get("script"),
        model=v.get("model"),
    )


def _load_patterns(config_dir: Path, raw_config: dict) -> list[Pattern]:
    patterns: list[Pattern] = []

    # Primary: patterns/*.yaml files
    patterns_dir = config_dir / "patterns"
    if patterns_dir.is_dir():
        for yaml_file in sorted(patterns_dir.glob("*.yaml")):
            with open(yaml_file) as f:
                p = yaml.safe_load(f)
            if p:
                patterns.append(_parse_pattern(p, fallback_name=yaml_file.stem))

    # Fallback: inline in eval-config.yaml
    if not patterns:
        inline = raw_config.get("patterns") or []
        if isinstance(inline, list):
            for p in inline:
                patterns.append(_parse_pattern(p))
        elif isinstance(inline, dict):
            for name, p in inline.items():
                patterns.append(_parse_pattern({**p, "name": name}, fallback_name=name))

    return patterns


def _load_variants(config_dir: Path, raw_config: dict) -> list[Variant]:
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
