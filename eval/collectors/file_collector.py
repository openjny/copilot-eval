"""Collect OTel spans from the Copilot file exporter JSONL output."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.protocols import RunContext
from eval.trace import Span, Trace

TRACE_FILE = Path(".traces") / "traces.jsonl"
CONTAINER_TRACE_FILE = "/workspace/.traces/traces.jsonl"


class FileCollector:
    """Trace collector for Copilot's JSONL file exporter."""

    def exporter_env(self, run_context: RunContext) -> dict[str, str]:
        return {
            "COPILOT_OTEL_EXPORTER_TYPE": "file",
            "COPILOT_OTEL_FILE_EXPORTER_PATH": CONTAINER_TRACE_FILE,
        }

    def collect(self, run_context: RunContext) -> list[Trace]:
        traces: list[Trace] = []
        traces_dir = run_context.run_dir / TRACE_FILE.parent
        if traces_dir.is_dir():
            trace_paths = sorted(traces_dir.glob("*.jsonl"))
        else:
            trace_paths = [
                trace_path
                for trace_path in sorted(run_context.run_dir.rglob("*.jsonl"))
                if TRACE_FILE.parent.name in trace_path.parts
            ]
        for trace_path in trace_paths:
            traces.extend(parse_file_traces(trace_path))
        if run_context.run_id:
            return [
                trace for trace in traces
                if trace.resource_tags.get("eval.run_id") == run_context.run_id
            ]
        return traces


def parse_file_traces(path: Path) -> list[Trace]:
    """Parse Copilot file-exporter JSONL into grouped traces."""
    if not path.exists():
        return []

    spans_by_trace: dict[str, list[Span]] = {}
    resources_by_trace: dict[str, dict[str, Any]] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "span":
            continue

        trace_id = record.get("traceId")
        span_id = record.get("spanId")
        if not isinstance(trace_id, str) or not isinstance(span_id, str):
            continue

        spans_by_trace.setdefault(trace_id, []).append(_parse_span(record, span_id))
        resource_tags = _parse_resource_tags(record)
        if resource_tags:
            resources_by_trace.setdefault(trace_id, {}).update(resource_tags)

    return [
        Trace(trace_id=trace_id, spans=spans, resource_tags=resources_by_trace.get(trace_id, {}))
        for trace_id, spans in spans_by_trace.items()
    ]


def _parse_span(record: dict[str, Any], span_id: str) -> Span:
    return Span(
        name=str(record.get("name", "")),
        duration_s=_duration_seconds(record.get("startTime"), record.get("endTime")),
        span_id=span_id,
        parent_id=_optional_str(record.get("parentSpanId")),
        start_time=_timestamp_microseconds(record.get("startTime")),
        tags=_parse_attributes(record),
    )


def _parse_attributes(record: dict[str, Any]) -> dict[str, Any]:
    attributes = record.get("attributes", {})
    return dict(attributes) if isinstance(attributes, dict) else {}


def _parse_resource_tags(record: dict[str, Any]) -> dict[str, Any]:
    resource = record.get("resource", {})
    if not isinstance(resource, dict):
        return {}
    attributes = resource.get("attributes", {})
    return dict(attributes) if isinstance(attributes, dict) else {}


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp_parts(value: Any) -> tuple[int, int]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        return 0, 0
    sec, ns = value
    try:
        return int(sec), int(ns)
    except (TypeError, ValueError):
        return 0, 0


def _timestamp_microseconds(value: Any) -> int:
    sec, ns = _timestamp_parts(value)
    return sec * 1_000_000 + ns // 1_000


def _duration_seconds(start: Any, end: Any) -> float:
    start_sec, start_ns = _timestamp_parts(start)
    end_sec, end_ns = _timestamp_parts(end)
    return (end_sec - start_sec) + ((end_ns - start_ns) / 1_000_000_000)
