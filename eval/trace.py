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
    # #121 extends #64's "distinguish genuine 0 from absent" treatment to the
    # integer telemetry-tag metrics. The int fields above stay non-nullable ints
    # (rendered as 0 when the backing tag is absent on a partial trace), while
    # these flags let the corresponding metric gates fail CLOSED — the accessor
    # yields None instead of a coerced 0 that would silently pass a `<=`/`<` gate
    # on missing telemetry. `turn_count` and the REQUIRED per-chat usage tags
    # (input/output tokens) are unavailable when the tag is missing where it
    # should appear; `cache_tokens` tracks the OPTIONAL cache_read tag, which
    # healthy traces legitimately omit, so it is unavailable only when the run has
    # no usage telemetry at all (no chat spans) — not merely because a turn had no
    # cache hit. tool_count / tool_duration / duration are derived from span
    # structure (not a tag) so they are always available and have no flag.
    turn_count_available: bool = True
    input_tokens_available: bool = True
    output_tokens_available: bool = True
    cache_tokens_available: bool = True
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
# `total_tokens` is derived (input + output). Tag-backed metrics return None when
# their backing telemetry tag was absent (or the "?" sentinel for `cost`), so the
# gate fails CLOSED instead of silently passing on a run whose telemetry is
# missing: `cost` keys off `cost_available` (#64); `turn_count` and the token
# aggregates key off the #121 availability flags. `total_tokens` is unavailable
# when either of its input/output halves is. `tool_count` / `tool_duration` /
# `duration` are structural (span counts/durations, not tags) and always avail.
_METRIC_ACCESSORS: dict[str, Callable[[RunMetrics], float | None]] = {
    "duration": lambda m: float(m.duration),
    "duration_seconds": lambda m: float(m.duration),
    "turn_count": lambda m: float(m.turn_count) if m.turn_count_available else None,
    "tool_count": lambda m: float(m.tool_count),
    "tool_duration": lambda m: float(m.tool_duration),
    "total_input_tokens": (
        lambda m: float(m.total_input_tokens) if m.input_tokens_available else None
    ),
    "total_output_tokens": (
        lambda m: float(m.total_output_tokens) if m.output_tokens_available else None
    ),
    "total_cache_tokens": (
        lambda m: float(m.total_cache_tokens) if m.cache_tokens_available else None
    ),
    "total_tokens": (
        lambda m: (
            float(m.total_input_tokens + m.total_output_tokens)
            if m.input_tokens_available and m.output_tokens_available
            else None
        )
    ),
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

    def int_tag(span: Span, key: str) -> tuple[int, bool]:
        # Returns (value, available). An absent tag reports 0 — so the integer
        # metric still renders as 0 per #58's non-nullable ints — but is flagged
        # unavailable so a `<=`/`<` gate on it fails CLOSED (#121) rather than
        # silently passing on a partial trace whose telemetry tag is missing. A
        # genuine 0 (tag present and numeric) stays available, distinguishing it
        # from an absent tag exactly as #64 did for cost.
        v = span.tags.get(key)
        if v is None or v == "":
            return 0, False
        try:
            return int(v), True
        except (TypeError, ValueError):
            return 0, False

    def sum_required_int_tag(spans: list[Span], key: str) -> tuple[int, bool]:
        # Aggregate a REQUIRED per-span usage tag (input/output tokens, which every
        # chat span emits). Available only when there is at least one contributing
        # span AND every one carries the tag numerically: an empty span set can't
        # measure the metric, and the tag missing on a span that should have it
        # means the sum under-counts, so the gate must fail CLOSED rather than pass
        # on an incomplete total.
        if not spans:
            return 0, False
        total = 0
        available = True
        for s in spans:
            value, present = int_tag(s, key)
            total += value
            available = available and present
        return total, available

    def sum_optional_int_tag(spans: list[Span], key: str) -> tuple[int, bool]:
        # Aggregate an OPTIONAL per-span usage tag. `cache_read.input_tokens` is
        # legitimately omitted on healthy traces — a turn with no cache hit (or a
        # cache_creation turn) simply doesn't emit it (see the real-trace fixture
        # tests/fixtures/file-exporter-sample.jsonl) — so a missing tag counts as 0
        # rather than flipping availability, which would spuriously fail a
        # `total_cache_tokens <= N` gate on a complete run. The metric is only
        # unavailable when the run produced no usage telemetry at all (no chat
        # spans), matching the required-tag path's "no telemetry → fail CLOSED".
        if not spans:
            return 0, False
        total = 0
        for s in spans:
            value, _ = int_tag(s, key)
            total += value
        return total, True

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

    turn_count, turn_count_available = int_tag(root, "github.copilot.turn_count")
    input_tokens, input_tokens_available = sum_required_int_tag(chats, "gen_ai.usage.input_tokens")
    output_tokens, output_tokens_available = sum_required_int_tag(
        chats, "gen_ai.usage.output_tokens"
    )
    cache_tokens, cache_tokens_available = sum_optional_int_tag(
        chats, "gen_ai.usage.cache_read.input_tokens"
    )
    cost_value, cost_available = float_tag(root, "github.copilot.cost")

    return RunMetrics(
        scenario=trace.resource_tags.get("eval.scenario", "?"),
        variant=trace.resource_tags.get("eval.variant", "?"),
        epoch=trace.resource_tags.get("eval.epoch", "?"),
        test_id=trace.resource_tags.get("eval.test_id", "?")[:8],
        total_spans=len(trace.spans),
        duration=root.duration_s,
        turn_count=turn_count,
        tool_count=len(tools),
        tool_names=[str(s.tags.get("gen_ai.tool.name", "?")) for s in tools],
        tool_duration=sum(s.duration_s for s in tools),
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_cache_tokens=cache_tokens,
        model=str(root.tags.get("gen_ai.request.model", "?")),
        cost=cost_value,
        cost_available=cost_available,
        turn_count_available=turn_count_available,
        input_tokens_available=input_tokens_available,
        output_tokens_available=output_tokens_available,
        cache_tokens_available=cache_tokens_available,
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
