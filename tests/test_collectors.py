"""Tests for trace collectors."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.collectors import FileCollector, JaegerCollector, create_collector
from eval.collectors import jaeger_collector as jaeger_mod
from eval.collectors.file_collector import parse_file_traces
from eval.trace import Span, Trace

FIXTURE = Path(__file__).parent / "fixtures" / "file-exporter-sample.jsonl"


def _run_context(run_dir: Path, run_id: str = "spike-run") -> SimpleNamespace:
    return SimpleNamespace(run_dir=run_dir, run_id=run_id)


def _write_trace_fixture(run_dir: Path) -> Path:
    trace_dir = run_dir / ".traces"
    trace_dir.mkdir()
    trace_file = trace_dir / "traces.jsonl"
    trace_file.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return trace_file


def _fixture_payload(
    run_id: str = "spike-run",
    test_id: str = "spike-001",
    trace_id: str = "c5b55d939c5df4939aa20c7090a13cc9",
) -> str:
    return (
        FIXTURE.read_text(encoding="utf-8")
        .replace("spike-run", run_id)
        .replace("spike-001", test_id)
        .replace("c5b55d939c5df4939aa20c7090a13cc9", trace_id)
    )


def test_file_collector_parses_fixture(tmp_path: Path):
    trace_file = _write_trace_fixture(tmp_path)

    traces = FileCollector().collect(_run_context(tmp_path))

    assert traces == parse_file_traces(trace_file)
    assert len(traces) == 1
    trace = traces[0]
    assert trace.trace_id == "c5b55d939c5df4939aa20c7090a13cc9"
    assert {span.name for span in trace.spans} == {"chat claude-opus-4.8", "invoke_agent"}


def test_file_collector_collects_multiple_trace_files(tmp_path: Path):
    trace_dir = tmp_path / ".traces"
    trace_dir.mkdir()
    (trace_dir / "task_a_epoch1.jsonl").write_text(_fixture_payload(), encoding="utf-8")
    (trace_dir / "task_b_epoch1.jsonl").write_text(
        _fixture_payload(
            run_id="run-2", test_id="test-2", trace_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
        encoding="utf-8",
    )

    traces = FileCollector().collect(_run_context(tmp_path, "run-2"))

    assert len(traces) == 1
    assert traces[0].trace_id == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert traces[0].resource_tags["eval.run_id"] == "run-2"


def test_file_collector_normalizes_time():
    trace = parse_file_traces(FIXTURE)[0]
    root = trace.root

    assert root is not None
    assert root.start_time == 1_782_968_835_020_524
    assert root.duration_s == pytest.approx(3.016233)


def test_file_collector_filters_metrics():
    trace = parse_file_traces(FIXTURE)[0]

    assert len(trace.spans) == 2
    assert all(not span.name.startswith("gen_ai.client.") for span in trace.spans)


def test_file_collector_resource_tags():
    trace = parse_file_traces(FIXTURE)[0]

    assert trace.resource_tags["service.name"] == "github-copilot"
    assert trace.resource_tags["service.version"] == "1.0.69-0"
    assert trace.resource_tags["eval.run_id"] == "spike-run"
    assert trace.resource_tags["eval.test_id"] == "spike-001"


def test_file_collector_exporter_env():
    assert FileCollector().exporter_env(_run_context(Path("."))) == {
        "COPILOT_OTEL_EXPORTER_TYPE": "file",
        "COPILOT_OTEL_FILE_EXPORTER_PATH": "/workspace/.traces/traces.jsonl",
    }


def test_jaeger_collector_exporter_env():
    assert JaegerCollector("http://localhost:16686").exporter_env(_run_context(Path("."))) == {
        "COPILOT_OTEL_EXPORTER_TYPE": "otlp-http",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://host.docker.internal:4318",
    }


def test_jaeger_collector_delegates_to_fetch_traces(monkeypatch):
    matching = Trace("t1", [Span("invoke_agent", 1.0, "s1", None)], {"eval.run_id": "run-1"})
    other = Trace("t2", [], {"eval.run_id": "run-2"})
    calls = []

    def fake_fetch_traces(jaeger_url: str, run_id: str | None = None):
        calls.append((jaeger_url, run_id))
        return [matching, other]

    monkeypatch.setattr(jaeger_mod, "fetch_traces", fake_fetch_traces)

    traces = JaegerCollector("http://jaeger:16686").collect(_run_context(Path("."), "run-1"))

    assert traces == [matching]
    assert calls == [("http://jaeger:16686", "run-1")]


def test_create_collector_unknown_type():
    with pytest.raises(ValueError, match="Unknown collector type"):
        create_collector("missing")
