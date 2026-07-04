"""Protocol interfaces for eval runner components."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from eval.config import Config, Task, Variant
from eval.config import Evaluator as EvaluatorConfig
from eval.trace import RunMetrics, Trace


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
    # Concrete fixture directory this run mounts (defaults to the task's first
    # effective fixture when empty). `fixture_label` is the reporting label
    # ("" for single-fixture tasks; the fixture name for multi-fixture tasks).
    fixture: str = ""
    fixture_label: str = ""


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


@dataclass
class EvalScore:
    name: str
    type: str
    score: int | None
    reason: str = ""
    passed: bool = True
    # Self-consistency metadata (judge evaluators only). ``samples`` holds the
    # successful per-call scores; ``n_samples`` is the number of calls requested
    # (== sum of ``outcomes``), which may exceed len(samples) when some fail.
    samples: list[int] = field(default_factory=list)
    score_stddev: float | None = None
    n_samples: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    judge_model: str | None = None
    judge_version: str | None = None
    # Free-form judge runtime/context metadata (judge runtime, truncation, etc.).
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return score_to_dict(self)


def score_to_dict(s: EvalScore) -> dict[str, Any]:
    """Serialize an EvalScore to the *.scores.json schema.

    Base keys mirror the legacy shape; judge self-consistency metadata
    (samples, stddev, outcomes, model/version) is added only for judge scores
    so non-judge serialization stays byte-identical.
    """
    d: dict[str, Any] = {
        "name": s.name,
        "type": s.type,
        "score": s.score,
        "reason": s.reason,
        "passed": s.passed,
    }
    if s.meta:
        d["meta"] = s.meta
    if s.type == "judge" and s.n_samples:
        d.update(
            {
                "samples": s.samples,
                "score_stddev": s.score_stddev,
                "n_samples": s.n_samples,
                "outcomes": s.outcomes,
                "judge_model": s.judge_model,
                "judge_version": s.judge_version,
            }
        )
    return d


@dataclass
class EvalContext:
    """Unified input to :meth:`Evaluator.evaluate`.

    Different evaluator types consume different subsets of this data: script/
    contains/regex evaluators run inline during ``run_one`` and need
    ``task``/``variant``/``log_file``; judge evaluators run during ``analyze``
    against a captured transcript (``conversation``/``output_files_text``);
    metric evaluators run during ``analyze`` against parsed OTel ``metrics``.
    Fields that don't apply to a given evaluator type are left at their
    default.
    """

    evaluator: EvaluatorConfig
    config: Config
    task: Task | None = None
    variant: Variant | None = None
    log_file: Path | None = None
    work_dir: Path | None = None
    token: str | None = None
    conversation: str | None = None
    output_files_text: str | None = None
    extra_meta: dict[str, Any] | None = None
    metrics: RunMetrics | None = None


@runtime_checkable
class Evaluator(Protocol):
    """Strategy interface for scoring a run against one evaluator config entry.

    Mirrors the :class:`AgentRunner`/:class:`TraceCollector` pattern: the
    ``EVALUATOR_REGISTRY`` (see ``eval.evaluators``) maps an evaluator config's
    ``type`` string (judge/script/contains/regex/metric) to a class
    implementing this protocol, so evaluator dispatch is a registry lookup
    instead of an if/elif chain, and third-party evaluator types can be
    registered without modifying ``eval.runner`` (see issue #66).

    Note: ``evaluate`` returns ``EvalScore | None`` rather than a bare
    ``EvalScore`` — a ``None`` result means "this evaluator isn't applicable
    right now" (e.g. a script evaluator with no ``script`` configured, or a
    judge/metric evaluator whose context isn't available inline during
    ``run_one``), mirroring the existing ``_eval_*`` helpers this protocol
    replaces.

    ``name``/``evaluator_type`` are declared as read-only properties (rather
    than plain attributes) so implementations may back them with either a
    class attribute or a ``@property`` without tripping structural-typing
    mismatches when a concrete class is stored as ``type[Evaluator]`` (as in
    ``EVALUATOR_REGISTRY``).
    """

    @property
    def name(self) -> str: ...

    @property
    def evaluator_type(self) -> str: ...

    def evaluate(self, context: EvalContext) -> EvalScore | None: ...

    @classmethod
    def from_config(cls, config: EvaluatorConfig) -> Evaluator: ...
