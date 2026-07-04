"""Protocol contract tests (issue #66).

Every runner/collector/evaluator registered in the corresponding registry
(``RUNNER_REGISTRY`` / ``COLLECTOR_REGISTRY`` / ``EVALUATOR_REGISTRY``) —
whether built in or added via an entry-point plugin — must be a structurally
valid implementation of its ``Protocol`` (``eval.protocols.AgentRunner`` /
``TraceCollector`` / ``Evaluator``). These tests exist so a plugin author
gets a clear, actionable failure (missing method, wrong shape) instead of a
cryptic error surfacing much later at run time.

``@runtime_checkable`` Protocol ``isinstance`` checks only verify that the
named attributes/methods exist, not their signatures — so each test also
does a minimal behavioral smoke check (calling the method / reading the
property) to catch a same-named-but-wrong-shape attribute.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.collectors import COLLECTOR_REGISTRY
from eval.config import Config, RunnerConfig
from eval.config import Evaluator as EvaluatorConfig
from eval.evaluators import EVALUATOR_REGISTRY
from eval.protocols import AgentRunner, Evaluator, TraceCollector
from eval.runners import RUNNER_REGISTRY, DockerCLIRunner


def _build_runner_instance(cls: type[AgentRunner]) -> AgentRunner:
    """Construct a runner with the minimal args its constructor requires.

    All built-in runners take a single required ``github_token`` positional
    arg; a future runner needing something else should extend this.
    """
    try:
        return cls()  # type: ignore[call-arg]
    except TypeError:
        return cls("contract-test-token")  # type: ignore[call-arg]


def _build_collector_instance(cls: type[TraceCollector]) -> TraceCollector:
    """Construct a collector with no args; all built-ins support this
    (FileCollector takes none, JaegerCollector's args all have defaults)."""
    return cls()


@pytest.mark.parametrize("name,cls", sorted(RUNNER_REGISTRY.items()))
def test_runner_satisfies_agent_runner_protocol(name: str, cls: type) -> None:
    instance = _build_runner_instance(cls)
    assert isinstance(instance, AgentRunner), (
        f"Runner backend '{name}' ({cls.__name__}) does not satisfy the AgentRunner protocol"
    )
    # Behavioral smoke check: supported_collectors must actually be readable
    # and non-empty (a runner that supports zero collectors can never run).
    supported = instance.supported_collectors
    assert isinstance(supported, tuple)
    assert len(supported) > 0
    assert all(isinstance(c, str) for c in supported)
    assert callable(instance.build)
    assert callable(instance.run)
    assert callable(instance.health_check)


@pytest.mark.parametrize("name,cls", sorted(COLLECTOR_REGISTRY.items()))
def test_collector_satisfies_trace_collector_protocol(name: str, cls: type) -> None:
    instance = _build_collector_instance(cls)
    assert isinstance(instance, TraceCollector), (
        f"Collector '{name}' ({cls.__name__}) does not satisfy the TraceCollector protocol"
    )
    assert callable(instance.collect)
    assert callable(instance.exporter_env)


@pytest.mark.parametrize("name,cls", sorted(EVALUATOR_REGISTRY.items()))
def test_evaluator_satisfies_evaluator_protocol(name: str, cls: type) -> None:
    config = EvaluatorConfig(name="contract-test", type=name)
    instance = cls.from_config(config)
    assert isinstance(instance, Evaluator), (
        f"Evaluator type '{name}' ({cls.__name__}) does not satisfy the Evaluator protocol"
    )
    assert instance.name == "contract-test"
    assert instance.evaluator_type == name
    assert callable(instance.evaluate)


def test_docker_runner_supports_all_registered_collectors() -> None:
    """The built-in docker runner must declare support for every built-in
    collector, since it's the only backend today and both file/jaeger must
    keep working with it."""
    runner = DockerCLIRunner("tok")
    for collector_name in ("file", "jaeger"):
        assert collector_name in runner.supported_collectors


def test_default_runner_backend_is_registered() -> None:
    """RunnerConfig's default `backend` value must always resolve in
    RUNNER_REGISTRY -- a stale default would silently break every run."""
    default_backend = RunnerConfig().backend
    assert default_backend in RUNNER_REGISTRY


def test_default_collector_is_registered() -> None:
    """Same guarantee as above for the default trace collector."""
    default_collector = RunnerConfig().collector
    assert default_collector in COLLECTOR_REGISTRY


def test_config_rejects_unregistered_runner_backend(tmp_path) -> None:
    from tests.conftest import load_inline

    with pytest.raises(Exception, match="runner.backend"):
        load_inline(tmp_path, {"runner": {"backend": "totally-unregistered-backend"}})


def test_all_registry_values_are_classes() -> None:
    """RUNNER_REGISTRY/COLLECTOR_REGISTRY/EVALUATOR_REGISTRY must map to
    classes (`type[...]`), not instances -- callers (`create_runner`,
    `create_collector`, `EvaluatorCls.from_config`) rely on constructing/
    calling classmethods on the registered value."""
    for registry in (RUNNER_REGISTRY, COLLECTOR_REGISTRY, EVALUATOR_REGISTRY):
        for name, value in registry.items():
            assert isinstance(value, type), f"'{name}' registry value is not a class: {value!r}"


def test_config_dataclass_smoke() -> None:
    """Sanity check that Config/RunnerConfig import cleanly alongside the
    registries above (guards against an accidental circular import)."""
    cfg = Config(
        vars={},
        runner=RunnerConfig(),
        tasks=[],
        variants=[],
        project_dir=Path("."),
        config_dir=Path("."),
    )
    assert cfg.runner.backend == "docker"
    assert cfg.runner.collector == "file"
