"""Runner strategy classes + registry.

Provides the concrete implementation(s) of ``eval.protocols.AgentRunner``, plus
a name -> class registry so runner selection (``runner.backend`` in
eval-config.yaml) is a lookup instead of a hardcoded assumption that Copilot
always runs via Docker.

Third-party runner backends can be added without touching ``eval.runner`` or
``eval.config`` (issue #66): register a class implementing
``eval.protocols.AgentRunner`` under the ``copilot_eval.runners`` entry-point
group, and it is loaded into ``RUNNER_REGISTRY`` by ``load_runner_plugins()``
— called once at CLI startup (``eval.cli.main``) — after which
``runner.backend: <name>`` selects it.
"""

from __future__ import annotations

from importlib import metadata as importlib_metadata
from logging import getLogger
from typing import Any

from eval.protocols import AgentRunner
from eval.runners.docker_cli_runner import DockerCLIRunner

logger = getLogger(__name__)

# `runner.backend: <key>` in eval-config.yaml selects the corresponding class
# below. "docker" is the only built-in backend today, but it's registered like
# any other so Docker isolation is one implementation of AgentRunner, not a
# hardcoded assumption (see docs/vision.md — "environment-isolated", not
# "Docker-isolated").
RUNNER_REGISTRY: dict[str, type[AgentRunner]] = {
    "docker": DockerCLIRunner,
}

# Entry-point group third-party packages can use to register additional
# runner backends (enables #66), e.g. in their pyproject.toml:
#
#   [project.entry-points."copilot_eval.runners"]
#   my_backend = "my_package.runners:MyRunner"
#
# where `my_package.runners.MyRunner` implements `eval.protocols.AgentRunner`.
ENTRY_POINT_GROUP = "copilot_eval.runners"

_plugins_loaded = False


def load_runner_plugins() -> None:
    """Discover and register third-party runner backends via entry points.

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
            runner_cls = ep.load()
        except Exception as exc:  # noqa: BLE001 - one bad plugin shouldn't break the rest
            logger.warning("Failed to load runner plugin '%s': %s", ep.name, exc)
            continue
        RUNNER_REGISTRY[ep.name] = runner_cls


def get_runner_class(backend: str) -> type[AgentRunner] | None:
    """Look up a registered runner class by its config `runner.backend` string."""
    return RUNNER_REGISTRY.get(backend)


def create_runner(runner_type: str, **kwargs: Any) -> AgentRunner:
    """Create a runner instance by backend name (convenience factory)."""
    runner_cls = RUNNER_REGISTRY.get(runner_type)
    if runner_cls is None:
        supported = ", ".join(sorted(RUNNER_REGISTRY))
        raise ValueError(f"Unknown runner type: {runner_type}. Supported types: {supported}")
    return runner_cls(**kwargs)


__all__ = [
    "RUNNER_REGISTRY",
    "ENTRY_POINT_GROUP",
    "DockerCLIRunner",
    "create_runner",
    "get_runner_class",
    "load_runner_plugins",
]
