"""Protocol interfaces for eval runner components."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from eval.config import Config, Task, Variant
from eval.trace import Trace


class RunStatus(str, Enum):  # noqa: UP042 - keep Python 3.10 compatibility without StrEnum.
    SUCCESS = "completed"
    SETUP_FAILED = "setup_failed"
    TIMEOUT = "timeout"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


def status_from_exit_code(exit_code: int) -> RunStatus:
    """Map a process exit code to a normalized run status."""
    if exit_code == 0:
        return RunStatus.SUCCESS
    if exit_code == 124:
        return RunStatus.TIMEOUT
    return RunStatus.FAILED


@dataclass
class RunContext:
    run_id: str
    test_id: str
    epoch: int
    run_dir: Path
    task: Task
    variant: Variant
    config: Config
    extra_env: dict[str, str] = field(default_factory=dict)
    work_dir: Path | None = None


@dataclass
class RunArtifacts:
    exit_code: int
    log_file: Path
    trace_file: Path | None
    output_dir: Path | None
    duration_seconds: float
    status: RunStatus
    started_at: str | None
    finished_at: str | None


@runtime_checkable
class AgentRunner(Protocol):
    def build(self, variant: Variant, config: Config) -> None: ...
    def run(self, run_context: RunContext) -> RunArtifacts: ...
    def health_check(self) -> None: ...

    @property
    def supported_collectors(self) -> tuple[str, ...]: ...


@runtime_checkable
class TraceCollector(Protocol):
    def collect(self, run_context: RunContext) -> list[Trace]: ...
    def exporter_env(self, run_context: RunContext) -> dict[str, str]: ...
