"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

import yaml

from eval.config import load_config
from eval.trace import RunMetrics, Span, Trace


def make_metrics(
    scenario: str, variant: str, epoch: str, duration: float = 1.0, **kwargs
) -> RunMetrics:
    """Build a RunMetrics with sensible defaults for report tests."""
    defaults = dict(
        scenario=scenario,
        variant=variant,
        epoch=epoch,
        test_id="t" + epoch,
        total_spans=1,
        duration=duration,
        turn_count=1,
        tool_count=0,
        tool_names=[],
        tool_duration=0.0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_tokens=0,
        model="m",
        cost="0",
    )
    defaults.update(kwargs)
    return RunMetrics(**defaults)


def write_config(config_dir: Path, config: dict) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "eval-config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def load_inline(config_dir: Path, config: dict):
    write_config(config_dir, config)
    return load_config(config_dir)


def make_trace(trace_id: str, spans: list[Span], resource_tags: dict[str, str]) -> Trace:
    return Trace(trace_id=trace_id, spans=spans, resource_tags=resource_tags)
