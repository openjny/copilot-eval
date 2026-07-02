"""Runner implementations."""
from __future__ import annotations

from typing import TYPE_CHECKING

from eval.runners.docker_cli_runner import DockerCLIRunner

if TYPE_CHECKING:
    from eval.protocols import AgentRunner


RUNNER_TYPES = {"cli": DockerCLIRunner}


def create_runner(runner_type: str, **kwargs) -> AgentRunner:
    """Create a runner instance by type."""
    try:
        runner_cls = RUNNER_TYPES[runner_type]
    except KeyError as exc:
        supported = ", ".join(sorted(RUNNER_TYPES))
        raise ValueError(f"Unknown runner type: {runner_type}. Supported types: {supported}") from exc
    return runner_cls(**kwargs)


__all__ = ["DockerCLIRunner", "RUNNER_TYPES", "create_runner"]
