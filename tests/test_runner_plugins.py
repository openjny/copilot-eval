"""Tests for the AgentRunner protocol, RUNNER_REGISTRY, and plugin discovery
(mirrors tests/test_evaluators.py's coverage of the same mechanism, issue #66).
"""

from __future__ import annotations

import pytest

from eval.protocols import AgentRunner, RunArtifacts, RunContext
from eval.runners import RUNNER_REGISTRY, DockerCLIRunner, create_runner

# --- RUNNER_REGISTRY ---


def test_registry_has_docker_backend():
    assert RUNNER_REGISTRY["docker"] is DockerCLIRunner


def test_create_runner_unknown():
    with pytest.raises(ValueError, match="Unknown runner type"):
        create_runner("unknown", github_token="tok")


# --- Third-party runner backend: registry dispatch end-to-end ---


class _FakeRunner:
    """Minimal third-party AgentRunner implementation used only by this test."""

    def __init__(self, github_token: str) -> None:
        self.github_token = github_token

    @property
    def supported_collectors(self) -> tuple[str, ...]:
        return ("file",)

    def build(self, variant, config) -> None:
        del variant, config

    def health_check(self) -> None:
        pass

    def run(self, run_context: RunContext) -> RunArtifacts:
        raise NotImplementedError


@pytest.fixture
def custom_runner_backend(monkeypatch):
    """Register a fake third-party runner backend for the duration of a test."""
    monkeypatch.setitem(RUNNER_REGISTRY, "fake", _FakeRunner)
    yield "fake"


def test_custom_runner_backend_dispatches_via_create_runner(custom_runner_backend):
    runner = create_runner(custom_runner_backend, github_token="tok")
    assert isinstance(runner, _FakeRunner)
    assert isinstance(runner, AgentRunner)


def test_custom_runner_backend_passes_config_validation(tmp_path, custom_runner_backend):
    """A backend registered in RUNNER_REGISTRY (not just 'docker') must validate
    in eval-config.yaml's `runner.backend` without any eval.config change."""
    from tests.conftest import load_inline

    cfg = load_inline(tmp_path, {"runner": {"backend": custom_runner_backend}})
    assert cfg.runner.backend == custom_runner_backend


# --- Entry-point plugin discovery ---


class _FakeEntryPoint:
    def __init__(self, name: str, cls: type) -> None:
        self.name = name
        self._cls = cls

    def load(self) -> type:
        return self._cls


def test_load_runner_plugins_registers_entry_points(monkeypatch):
    """load_runner_plugins() discovers a fake entry point and adds it to
    RUNNER_REGISTRY, exercising the mechanism issue #66 depends on."""
    import eval.runners as runners_mod

    monkeypatch.setattr(runners_mod, "_plugins_loaded", False)
    monkeypatch.delitem(RUNNER_REGISTRY, "plugin_backend", raising=False)

    fake_ep = _FakeEntryPoint("plugin_backend", _FakeRunner)

    def fake_entry_points(*, group: str):
        assert group == runners_mod.ENTRY_POINT_GROUP
        return [fake_ep]

    monkeypatch.setattr(runners_mod.importlib_metadata, "entry_points", fake_entry_points)

    runners_mod.load_runner_plugins()

    assert RUNNER_REGISTRY["plugin_backend"] is _FakeRunner
    del RUNNER_REGISTRY["plugin_backend"]


def test_load_runner_plugins_is_idempotent(monkeypatch):
    """A second call is a no-op (doesn't re-scan entry points)."""
    import eval.runners as runners_mod

    monkeypatch.setattr(runners_mod, "_plugins_loaded", True)
    calls = []

    def fake_entry_points(*, group: str):
        calls.append(group)
        return []

    monkeypatch.setattr(runners_mod.importlib_metadata, "entry_points", fake_entry_points)

    runners_mod.load_runner_plugins()

    assert calls == []


def test_load_runner_plugins_skips_broken_plugin(monkeypatch):
    """A plugin whose entry point fails to load is logged and skipped, not
    fatal to CLI startup."""
    import eval.runners as runners_mod

    monkeypatch.setattr(runners_mod, "_plugins_loaded", False)

    class _BrokenEntryPoint:
        name = "broken"

        def load(self) -> type:
            raise ImportError("boom")

    def fake_entry_points(*, group: str):
        return [_BrokenEntryPoint()]

    monkeypatch.setattr(runners_mod.importlib_metadata, "entry_points", fake_entry_points)

    runners_mod.load_runner_plugins()  # must not raise

    assert "broken" not in RUNNER_REGISTRY
