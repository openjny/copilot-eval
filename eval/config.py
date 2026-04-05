"""Configuration loading and validation."""
from __future__ import annotations

import os
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
    output_format: str = "text"  # text or json (copilot --output-format)
    container_image_base: str = "copilot-eval"
    copilot_version: str = "1.0.18"
    otel_endpoint: str = "http://host.docker.internal:4318"


@dataclass
class Variant:
    name: str
    description: str = ""
    build_script: str | None = None
    run_script: str | None = None
    model: str | None = None  # Override runner model per variant

    @property
    def image_tag(self) -> str:
        return self.name


@dataclass
class Judge:
    name: str
    prompt: str


@dataclass
class Metrics:
    preset: list[str] = field(default_factory=lambda: [
        "duration", "turns", "tool_calls", "tool_duration",
        "input_tokens", "output_tokens", "tool_pattern",
    ])
    judges: list[Judge] = field(default_factory=list)


@dataclass
class Pattern:
    name: str
    type: str  # "read" or "write"
    prompt: str
    enabled: bool = True
    verify: str | None = None
    fixture: str | None = None
    reset_script: str | None = None
    timeout_seconds: int | None = None  # Override runner timeout per pattern
    metrics: Metrics = field(default_factory=Metrics)


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
    """Load config from config_dir (or project root).

    - config_dir: directory containing eval-config.yaml, patterns/, variants/
    - project_dir: repository root (where docker/, .env live). Auto-detected.
    """
    project_dir = Path(__file__).resolve().parent.parent
    if config_dir is None:
        config_dir = project_dir

    config_path = config_dir / "eval-config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    vars_dict = {str(k): str(v) for k, v in raw.get("vars", {}).items()}

    runner_raw = raw.get("runner", {})
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
    variants = _load_variants(config_dir)

    return Config(vars=vars_dict, runner=runner, patterns=patterns, variants=variants, project_dir=project_dir, config_dir=config_dir)


def _load_patterns(project_dir: Path, raw_config: dict) -> list[Pattern]:
    """Load patterns from patterns/*.yaml, falling back to inline config."""
    patterns: list[Pattern] = []
    patterns_dir = project_dir / "patterns"

    if patterns_dir.is_dir():
        for yaml_file in sorted(patterns_dir.glob("*.yaml")):
            with open(yaml_file) as f:
                p = yaml.safe_load(f)
            if p:
                metrics_raw = p.get("metrics", {})
                judges = [Judge(name=j["name"], prompt=j["prompt"]) for j in metrics_raw.get("judges", [])]
                metrics = Metrics(
                    preset=metrics_raw.get("preset", Metrics().preset),
                    judges=judges,
                )
                patterns.append(Pattern(
                    name=p.get("name", yaml_file.stem),
                    type=p.get("type", "read"),
                    prompt=p.get("prompt", ""),
                    enabled=p.get("enabled", True),
                    verify=p.get("verify"),
                    fixture=p.get("fixture"),
                    reset_script=p.get("reset_script"),
                    timeout_seconds=p.get("timeout_seconds"),
                    metrics=metrics,
                ))

    # Fallback: inline patterns in eval-config.yaml
    if not patterns:
        for name, p in raw_config.get("patterns", {}).items():
            patterns.append(Pattern(
                name=name,
                type=p.get("type", "read"),
                prompt=p.get("prompt", ""),
                enabled=p.get("enabled", False),
            ))

    return patterns


def _load_variants(project_dir: Path) -> list[Variant]:
    """Load variants from variants/*.yaml."""
    variants: list[Variant] = []
    variants_dir = project_dir / "variants"

    if variants_dir.is_dir():
        for yaml_file in sorted(variants_dir.glob("*.yaml")):
            with open(yaml_file) as f:
                v = yaml.safe_load(f)
            if v:
                build = v.get("build", {}) or {}
                run = v.get("run", {}) or {}
                variants.append(Variant(
                    name=v.get("name", yaml_file.stem),
                    description=v.get("description", ""),
                    build_script=build.get("script"),
                    run_script=run.get("script"),
                    model=v.get("model"),
                ))

    if not variants:
        variants = [Variant(name="baseline"), Variant(name="with-plugin")]

    return variants
