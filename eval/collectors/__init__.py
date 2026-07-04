"""Trace collector implementations + registry.

Provides the concrete implementations of ``eval.protocols.TraceCollector``
(file/jaeger), plus a name -> class registry so collector selection
(``runner.collector`` in eval-config.yaml) is a lookup instead of a hardcoded
dict.

Third-party collectors can be added without touching ``eval.runner`` or
``eval.config`` (issue #66): register a class implementing
``eval.protocols.TraceCollector`` under the ``copilot_eval.collectors``
entry-point group, and it is loaded into ``COLLECTOR_REGISTRY`` by
``load_collector_plugins()`` — called once at CLI startup (``eval.cli.main``)
— after which ``runner.collector: <name>`` selects it.
"""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from logging import getLogger
from typing import Any

from eval.collectors.file_collector import FileCollector
from eval.collectors.jaeger_collector import JaegerCollector
from eval.protocols import TraceCollector

logger = getLogger(__name__)

# `runner.collector: <key>` in eval-config.yaml selects the corresponding
# class below.
COLLECTOR_REGISTRY: dict[str, type[TraceCollector]] = {
    "file": FileCollector,
    "jaeger": JaegerCollector,
}

# Entry-point group third-party packages can use to register additional
# collectors (enables #66), e.g. in their pyproject.toml:
#
#   [project.entry-points."copilot_eval.collectors"]
#   my_collector = "my_package.collectors:MyCollector"
#
# where `my_package.collectors.MyCollector` implements
# `eval.protocols.TraceCollector`.
ENTRY_POINT_GROUP = "copilot_eval.collectors"

_plugins_loaded = False


def load_collector_plugins() -> None:
    """Discover and register third-party collectors via entry points.

    Idempotent — safe to call more than once (e.g. once per CLI invocation).
    A plugin that fails to load is logged and skipped rather than aborting the
    process, since one broken plugin package shouldn't take down the CLI.
    """
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    try:
        entry_points = importlib_metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - defensive against odd metadata backends
        logger.debug("No '%s' entry points available", ENTRY_POINT_GROUP)
        return
    for ep in entry_points:
        try:
            collector_cls = ep.load()
        except Exception as exc:  # noqa: BLE001 - one bad plugin shouldn't break the rest
            logger.warning("Failed to load collector plugin '%s': %s", ep.name, exc)
            continue
        COLLECTOR_REGISTRY[ep.name] = collector_cls


def get_collector_class(collector_type: str) -> type[TraceCollector] | None:
    """Look up a registered collector class by its config `runner.collector` string."""
    return COLLECTOR_REGISTRY.get(collector_type)


def create_collector(collector_type: str, **kwargs: Any) -> TraceCollector:
    """Create a trace collector by type (convenience factory)."""
    collector_cls = COLLECTOR_REGISTRY.get(collector_type)
    if collector_cls is None:
        available = ", ".join(sorted(COLLECTOR_REGISTRY))
        raise ValueError(f"Unknown collector type '{collector_type}'. Available: {available}")
    return collector_cls(**kwargs)


__all__ = [
    "COLLECTOR_REGISTRY",
    "ENTRY_POINT_GROUP",
    "FileCollector",
    "JaegerCollector",
    "create_collector",
    "get_collector_class",
    "load_collector_plugins",
]
