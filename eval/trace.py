"""Fetch and parse OTel traces from Jaeger."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import requests


@dataclass
class Span:
    name: str
    duration_s: float
    span_id: str
    parent_id: str | None
    start_time: int = 0  # Jaeger startTime in microseconds
    tags: dict[str, str | int] = field(default_factory=dict)


@dataclass
class Trace:
    trace_id: str
    spans: list[Span]
    resource_tags: dict[str, str] = field(default_factory=dict)

    @property
    def root(self) -> Span | None:
        return next((s for s in self.spans if s.name == "invoke_agent"), None)

    @property
    def chats(self) -> list[Span]:
        return [s for s in self.spans if s.name.startswith("chat")]

    @property
    def tools(self) -> list[Span]:
        return [s for s in self.spans if s.name.startswith("execute_tool")]

    @property
    def permissions(self) -> list[Span]:
        return [s for s in self.spans if s.name == "permission"]


@dataclass
class RunMetrics:
    scenario: str
    variant: str
    epoch: str
    test_id: str
    total_spans: int
    duration: float
    turn_count: int
    tool_count: int
    tool_names: list[str]
    tool_duration: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_tokens: int
    model: str
    cost: float
    # Whether the `github.copilot.cost` tag was actually present and numeric in
    # the trace. #58 keeps `cost` a non-nullable float (rendered as 0.0 when the
    # tag is absent or the "?" sentinel appears on a partial trace); this flag
    # lets a `cost` metric gate fail CLOSED — treat cost as unavailable (→ None)
    # rather than silently passing a `cost < X` budget on missing telemetry.
    cost_available: bool = True
    # Reporting fixture label ("" for single-fixture tasks). Distinguishes runs
    # along the input-coverage axis when a task declares multiple fixtures.
    fixture: str = ""


def _parse_float(value: object) -> float | None:
    """Best-effort float conversion; returns None for missing/non-numeric values."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# Numeric RunMetrics fields a `metric` evaluator can assert on. Maps the public
# metric name (used in eval-config.yaml) to how its numeric value is derived from
# a RunMetrics instance. `duration_seconds` is an alias for `duration` and
# `total_tokens` is derived (input + output). `cost` returns None when the
# `github.copilot.cost` tag was absent or the "?" sentinel (`cost_available`
# False), so a `cost` budget gate fails CLOSED instead of silently passing on a
# run whose cost telemetry is missing.
_METRIC_ACCESSORS: dict[str, Callable[[RunMetrics], float | None]] = {
    "duration": lambda m: float(m.duration),
    "duration_seconds": lambda m: float(m.duration),
    "turn_count": lambda m: float(m.turn_count),
    "tool_count": lambda m: float(m.tool_count),
    "tool_duration": lambda m: float(m.tool_duration),
    "total_input_tokens": lambda m: float(m.total_input_tokens),
    "total_output_tokens": lambda m: float(m.total_output_tokens),
    "total_cache_tokens": lambda m: float(m.total_cache_tokens),
    "total_tokens": lambda m: float(m.total_input_tokens + m.total_output_tokens),
    "cost": lambda m: _parse_float(m.cost) if m.cost_available else None,
}

# Assertable metric names, exposed for config validation.
METRIC_FIELDS: tuple[str, ...] = tuple(_METRIC_ACCESSORS)


def metric_value(metrics: RunMetrics, name: str) -> float | None:
    """Return the numeric value of a named metric, or None if unknown/unavailable."""
    accessor = _METRIC_ACCESSORS.get(name)
    if accessor is None:
        return None
    return accessor(metrics)


def fetch_traces(
    jaeger_url: str, service: str = "github-copilot", limit: int = 2000, run_id: str | None = None
) -> list[Trace]:
    url = f"{jaeger_url}/api/traces"
    params: dict[str, str | int] = {"service": service, "limit": limit}
    # Server-side filter by run_id (stored as a process/resource tag) so large
    # runs and late-arriving traces aren't silently dropped by a small limit.
    if run_id:
        params["tags"] = json.dumps({"eval.run_id": run_id})
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    traces = []
    for t in data.get("data", []):
        resource_tags = {}
        for p in t.get("processes", {}).values():
            for tag in p.get("tags", []):
                resource_tags[tag["key"]] = tag["value"]

        spans = []
        for s in t.get("spans", []):
            parent_id = None
            for ref in s.get("references", []):
                if ref["refType"] == "CHILD_OF":
                    parent_id = ref["spanID"]
            span_tags = {tg["key"]: tg["value"] for tg in s.get("tags", [])}
            spans.append(
                Span(
                    name=s["operationName"],
                    duration_s=s["duration"] / 1_000_000,
                    span_id=s["spanID"],
                    parent_id=parent_id,
                    start_time=s.get("startTime", 0),
                    tags=span_tags,
                )
            )
        traces.append(Trace(trace_id=t["traceID"], spans=spans, resource_tags=resource_tags))

    return traces


def filter_by_run(traces: list[Trace], run_id: str) -> list[Trace]:
    return [t for t in traces if t.resource_tags.get("eval.run_id") == run_id]


def extract_metrics(trace: Trace) -> RunMetrics | None:
    root = trace.root
    if not root:
        return None

    chats = trace.chats
    tools = trace.tools

    def int_tag(span: Span, key: str) -> int:
        v = span.tags.get(key, 0)
        return int(v) if v else 0

    def float_tag(span: Span, key: str) -> tuple[float, bool]:
        # Returns (value, available). Cost may be absent or the "?" sentinel on
        # partial traces; report 0.0 (for #58's non-nullable-float rendering) but
        # flag it unavailable so a `cost` metric gate fails CLOSED rather than
        # silently passing a budget on missing telemetry.
        v = span.tags.get(key)
        if v is None:
            return 0.0, False
        try:
            return float(v), True
        except (TypeError, ValueError):
            return 0.0, False

    cost_value, cost_available = float_tag(root, "github.copilot.cost")

    return RunMetrics(
        scenario=trace.resource_tags.get("eval.scenario", "?"),
        variant=trace.resource_tags.get("eval.variant", "?"),
        epoch=trace.resource_tags.get("eval.epoch", "?"),
        test_id=trace.resource_tags.get("eval.test_id", "?")[:8],
        total_spans=len(trace.spans),
        duration=root.duration_s,
        turn_count=int(root.tags.get("github.copilot.turn_count", 0)),
        tool_count=len(tools),
        tool_names=[str(s.tags.get("gen_ai.tool.name", "?")) for s in tools],
        tool_duration=sum(s.duration_s for s in tools),
        total_input_tokens=sum(int_tag(c, "gen_ai.usage.input_tokens") for c in chats),
        total_output_tokens=sum(int_tag(c, "gen_ai.usage.output_tokens") for c in chats),
        total_cache_tokens=sum(int_tag(c, "gen_ai.usage.cache_read.input_tokens") for c in chats),
        model=str(root.tags.get("gen_ai.request.model", "?")),
        cost=cost_value,
        cost_available=cost_available,
        fixture=trace.resource_tags.get("eval.fixture", ""),
    )


def extract_conversation(trace: Trace, max_chars: int = 8000) -> str | None:
    """Extract conversation text from OTel trace (requires capture_content=true).

    Reads gen_ai.output.messages from chat spans to reconstruct the assistant's
    responses. Falls back to None if content capture was disabled.
    """
    chats = trace.chats
    if not chats:
        return None

    parts: list[str] = []
    total = 0
    for span in sorted(chats, key=lambda s: (s.start_time, s.span_id)):
        # Output messages (assistant responses + tool calls)
        output_raw = span.tags.get("gen_ai.output.messages")
        if output_raw:
            text = _parse_messages(str(output_raw))
            if text:
                if total + len(text) > max_chars:
                    remaining = max_chars - total
                    if remaining > 0:
                        parts.append(text[:remaining])
                    parts.append("... (truncated)")
                    break
                parts.append(text)
                total += len(text)

    return "\n\n".join(parts) if parts else None


def _parse_messages(raw: str) -> str | None:
    """Parse gen_ai.input/output.messages JSON into readable text."""
    try:
        messages = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(messages, list):
        return None

    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Text content
        content = msg.get("content")
        if content and isinstance(content, str):
            parts.append(content)
        elif content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("text"):
                    parts.append(item["text"])
        # Tool call results
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    parts.append(f"[tool_call: {name}]")
    return "\n".join(parts) if parts else None
