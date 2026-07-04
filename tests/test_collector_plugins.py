"""Tests for the TraceCollector protocol, COLLECTOR_REGISTRY, and plugin
discovery (mirrors tests/test_evaluators.py's coverage of the same mechanism,
issue #66).
"""

from __future__ import annotations

import pytest

from eval.collectors import COLLECTOR_REGISTRY, FileCollector, JaegerCollector, create_collector
from eval.protocols import RunContext, Trace, TraceCollector

# --- COLLECTOR_REGISTRY ---


def test_registry_has_all_built_in_collectors():
    assert set(COLLECTOR_REGISTRY) == {"file", "jaeger"}
    assert COLLECTOR_REGISTRY["file"] is FileCollector
    assert COLLECTOR_REGISTRY["jaeger"] is JaegerCollector


def test_create_collector_unknown_type():
    with pytest.raises(ValueError, match="Unknown collector type"):
        create_collector("missing")


# --- Third-party collector: registry dispatch end-to-end ---


class _FakeCollector:
    """Minimal third-party TraceCollector implementation used only by this test."""

    def exporter_env(self, run_context: RunContext) -> dict[str, str]:
        return {}

    def collect(self, run_context: RunContext) -> list[Trace]:
        return []


@pytest.fixture
def custom_collector_type(monkeypatch):
    """Register a fake third-party collector type for the duration of a test."""
    monkeypatch.setitem(COLLECTOR_REGISTRY, "fake", _FakeCollector)
    yield "fake"


def test_custom_collector_type_dispatches_via_create_collector(custom_collector_type):
    collector = create_collector(custom_collector_type)
    assert isinstance(collector, _FakeCollector)
    assert isinstance(collector, TraceCollector)


def test_custom_collector_type_passes_config_validation(tmp_path, custom_collector_type):
    """A type registered in COLLECTOR_REGISTRY (not just file/jaeger) must
    validate in eval-config.yaml's `runner.collector` without any eval.config
    change."""
    from tests.conftest import load_inline

    cfg = load_inline(tmp_path, {"runner": {"collector": custom_collector_type}})
    assert cfg.runner.collector == custom_collector_type


# --- Entry-point plugin discovery ---


class _FakeEntryPoint:
    def __init__(self, name: str, cls: type) -> None:
        self.name = name
        self._cls = cls

    def load(self) -> type:
        return self._cls


def test_load_collector_plugins_registers_entry_points(monkeypatch):
    """load_collector_plugins() discovers a fake entry point and adds it to
    COLLECTOR_REGISTRY, exercising the mechanism issue #66 depends on."""
    import eval.collectors as collectors_mod

    monkeypatch.setattr(collectors_mod, "_plugins_loaded", False)
    monkeypatch.delitem(COLLECTOR_REGISTRY, "plugin_collector", raising=False)

    fake_ep = _FakeEntryPoint("plugin_collector", _FakeCollector)

    def fake_entry_points(*, group: str):
        assert group == collectors_mod.ENTRY_POINT_GROUP
        return [fake_ep]

    monkeypatch.setattr(collectors_mod.importlib_metadata, "entry_points", fake_entry_points)

    collectors_mod.load_collector_plugins()

    assert COLLECTOR_REGISTRY["plugin_collector"] is _FakeCollector
    del COLLECTOR_REGISTRY["plugin_collector"]


def test_load_collector_plugins_is_idempotent(monkeypatch):
    """A second call is a no-op (doesn't re-scan entry points)."""
    import eval.collectors as collectors_mod

    monkeypatch.setattr(collectors_mod, "_plugins_loaded", True)
    calls = []

    def fake_entry_points(*, group: str):
        calls.append(group)
        return []

    monkeypatch.setattr(collectors_mod.importlib_metadata, "entry_points", fake_entry_points)

    collectors_mod.load_collector_plugins()

    assert calls == []


def test_load_collector_plugins_skips_broken_plugin(monkeypatch):
    """A plugin whose entry point fails to load is logged and skipped, not
    fatal to CLI startup."""
    import eval.collectors as collectors_mod

    monkeypatch.setattr(collectors_mod, "_plugins_loaded", False)

    class _BrokenEntryPoint:
        name = "broken"

        def load(self) -> type:
            raise ImportError("boom")

    def fake_entry_points(*, group: str):
        return [_BrokenEntryPoint()]

    monkeypatch.setattr(collectors_mod.importlib_metadata, "entry_points", fake_entry_points)

    collectors_mod.load_collector_plugins()  # must not raise

    assert "broken" not in COLLECTOR_REGISTRY
