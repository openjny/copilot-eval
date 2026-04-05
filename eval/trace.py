"""Fetch and parse OTel traces from Jaeger."""
from __future__ import annotations

from dataclasses import dataclass, field

import requests


@dataclass
class Span:
    name: str
    duration_s: float
    span_id: str
    parent_id: str | None
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
    cost: str


def fetch_traces(jaeger_url: str, service: str = "github-copilot", limit: int = 50) -> list[Trace]:
    url = f"{jaeger_url}/api/traces"
    resp = requests.get(url, params={"service": service, "limit": limit}, timeout=10)
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
            spans.append(Span(
                name=s["operationName"],
                duration_s=s["duration"] / 1_000_000,
                span_id=s["spanID"],
                parent_id=parent_id,
                tags=span_tags,
            ))
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
        cost=str(root.tags.get("github.copilot.cost", "?")),
    )
