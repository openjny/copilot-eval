"""Tests for OTel trace parsing and metric extraction."""
from __future__ import annotations

import json

from eval import trace as trace_mod
from eval.trace import (
    Span,
    Trace,
    _parse_messages,
    extract_conversation,
    extract_metrics,
    fetch_traces,
    filter_by_run,
)


def _chat(span_id, in_tok=0, out_tok=0, cache=0, output_msgs=None):
    tags = {
        "gen_ai.usage.input_tokens": in_tok,
        "gen_ai.usage.output_tokens": out_tok,
        "gen_ai.usage.cache_read.input_tokens": cache,
    }
    if output_msgs is not None:
        tags["gen_ai.output.messages"] = json.dumps(output_msgs)
    return Span(name="chat", duration_s=1.0, span_id=span_id, parent_id="root", tags=tags)


def _root():
    return Span(name="invoke_agent", duration_s=12.5, span_id="root", parent_id=None,
                tags={"github.copilot.turn_count": 3, "gen_ai.request.model": "gpt-x"})


def _tool(span_id, name, dur=0.5):
    return Span(name="execute_tool", duration_s=dur, span_id=span_id, parent_id="root",
                tags={"gen_ai.tool.name": name})


def test_extract_metrics_aggregates_tokens_and_tools():
    spans = [
        _root(),
        _chat("c1", in_tok=100, out_tok=10, cache=5),
        _chat("c2", in_tok=200, out_tok=20, cache=15),
        _tool("t1", "bash", dur=0.5),
        _tool("t2", "view", dur=1.5),
    ]
    t = Trace(trace_id="x", spans=spans, resource_tags={
        "eval.scenario": "s", "eval.variant": "v", "eval.epoch": "1",
        "eval.test_id": "abcdef123456",
    })
    m = extract_metrics(t)
    assert m is not None
    assert m.scenario == "s" and m.variant == "v" and m.epoch == "1"
    assert m.test_id == "abcdef12"  # truncated to 8
    assert m.duration == 12.5
    assert m.turn_count == 3
    assert m.tool_count == 2
    assert sorted(m.tool_names) == ["bash", "view"]
    assert m.tool_duration == 2.0
    assert m.total_input_tokens == 300
    assert m.total_output_tokens == 30
    assert m.total_cache_tokens == 20
    assert m.model == "gpt-x"


def test_extract_metrics_no_root_returns_none():
    t = Trace(trace_id="x", spans=[_chat("c1")], resource_tags={})
    assert extract_metrics(t) is None


def test_extract_conversation_orders_by_span_id():
    spans = [
        _root(),
        _chat("c2", output_msgs=[{"role": "assistant", "content": "second"}]),
        _chat("c1", output_msgs=[{"role": "assistant", "content": "first"}]),
    ]
    t = Trace(trace_id="x", spans=spans, resource_tags={})
    convo = extract_conversation(t)
    assert convo == "first\n\nsecond"


def test_extract_conversation_none_when_no_content():
    t = Trace(trace_id="x", spans=[_root(), _chat("c1")], resource_tags={})
    assert extract_conversation(t) is None


def test_parse_messages_text_list_and_tool_calls():
    raw = json.dumps([
        {"role": "assistant", "content": "hello"},
        {"role": "assistant", "content": [{"text": "world"}]},
        {"role": "assistant", "tool_calls": [{"function": {"name": "bash"}}]},
    ])
    assert _parse_messages(raw) == "hello\nworld\n[tool_call: bash]"


def test_parse_messages_invalid_json():
    assert _parse_messages("not json") is None


def test_filter_by_run():
    t1 = Trace("a", [], {"eval.run_id": "r1"})
    t2 = Trace("b", [], {"eval.run_id": "r2"})
    assert filter_by_run([t1, t2], "r1") == [t1]


def test_fetch_traces_parses_jaeger_json(monkeypatch):
    payload = {
        "data": [{
            "traceID": "T1",
            "processes": {"p1": {"tags": [{"key": "eval.run_id", "value": "r1"}]}},
            "spans": [
                {"operationName": "invoke_agent", "duration": 2_000_000,
                 "spanID": "s1", "references": [], "tags": []},
                {"operationName": "chat", "duration": 1_000_000, "spanID": "s2",
                 "references": [{"refType": "CHILD_OF", "spanID": "s1"}],
                 "tags": [{"key": "gen_ai.usage.input_tokens", "value": 42}]},
            ],
        }]
    }

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(trace_mod.requests, "get", lambda *a, **k: FakeResp())
    traces = fetch_traces("http://jaeger")
    assert len(traces) == 1
    tr = traces[0]
    assert tr.trace_id == "T1"
    assert tr.resource_tags["eval.run_id"] == "r1"
    assert tr.root is not None
    chat = tr.chats[0]
    assert chat.parent_id == "s1"
    assert chat.duration_s == 1.0
    assert chat.tags["gen_ai.usage.input_tokens"] == 42
