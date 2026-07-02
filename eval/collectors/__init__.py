"""Trace collector implementations."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eval.collectors.file_collector import FileCollector
from eval.collectors.jaeger_collector import JaegerCollector
from eval.protocols import TraceCollector

CollectorFactory = Callable[..., TraceCollector]

COLLECTOR_TYPES: dict[str, CollectorFactory] = {
    "file": FileCollector,
    "jaeger": JaegerCollector,
}


def create_collector(collector_type: str, **kwargs: Any) -> TraceCollector:
    """Create a trace collector by type."""
    collector_cls = COLLECTOR_TYPES.get(collector_type)
    if collector_cls is None:
        available = ", ".join(sorted(COLLECTOR_TYPES))
        raise ValueError(f"Unknown collector type '{collector_type}'. Available: {available}")
    return collector_cls(**kwargs)


__all__ = ["COLLECTOR_TYPES", "FileCollector", "JaegerCollector", "create_collector"]
