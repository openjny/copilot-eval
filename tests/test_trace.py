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
    return Span(
        name="invoke_agent",
        duration_s=12.5,
        span_id="root",
        parent_id=None,
        tags={"github.copilot.turn_count": 3, "gen_ai.request.model": "gpt-x"},
    )


def _root_with_cost(cost):
    return Span(
        name="invoke_agent",
        duration_s=12.5,
        span_id="root",
        parent_id=None,
        tags={
            "github.copilot.turn_count": 3,
            "gen_ai.request.model": "gpt-x",
            "github.copilot.cost": cost,
        },
    )


def _tool(span_id, name, dur=0.5):
    return Span(
        name="execute_tool",
        duration_s=dur,
        span_id=span_id,
        parent_id="root",
        tags={"gen_ai.tool.name": name},
    )


def test_extract_metrics_aggregates_tokens_and_tools():
    spans = [
        _root(),
        _chat("c1", in_tok=100, out_tok=10, cache=5),
        _chat("c2", in_tok=200, out_tok=20, cache=15),
        _tool("t1", "bash", dur=0.5),
        _tool("t2", "view", dur=1.5),
    ]
    t = Trace(
        trace_id="x",
        spans=spans,
        resource_tags={
            "eval.scenario": "s",
            "eval.variant": "v",
            "eval.epoch": "1",
            "eval.test_id": "abcdef123456",
        },
    )
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


def test_extract_metrics_parses_cost_as_float():
    t = Trace(trace_id="x", spans=[_root_with_cost(15.0)], resource_tags={})
    m = extract_metrics(t)
    assert m is not None
    assert m.cost == 15.0
    assert isinstance(m.cost, float)


def test_extract_metrics_cost_falls_back_when_missing_or_sentinel():
    # Tag absent entirely: cost renders 0.0 (for #58) but is flagged unavailable.
    m_absent = extract_metrics(Trace(trace_id="x", spans=[_root()], resource_tags={}))
    assert m_absent is not None and m_absent.cost == 0.0
    assert m_absent.cost_available is False
    # Non-numeric "?" sentinel: same — 0.0 for rendering, unavailable for gating.
    m_sentinel = extract_metrics(
        Trace(trace_id="y", spans=[_root_with_cost("?")], resource_tags={})
    )
    assert m_sentinel is not None and m_sentinel.cost == 0.0
    assert m_sentinel.cost_available is False


def test_cost_metric_gate_none_when_tag_absent_or_sentinel():
    """A `cost` gate must fail CLOSED (metric_value → None) when the cost tag is
    absent or the "?" sentinel, instead of silently passing on a coerced 0.0."""
    from eval.trace import metric_value

    # Genuine numeric cost (even 0.0) is available for gating.
    m_zero = extract_metrics(Trace(trace_id="a", spans=[_root_with_cost(0.0)], resource_tags={}))
    assert m_zero is not None and m_zero.cost_available is True
    assert metric_value(m_zero, "cost") == 0.0
    # Absent tag → gating value is None (→ failed gate), NOT 0.0.
    m_absent = extract_metrics(Trace(trace_id="b", spans=[_root()], resource_tags={}))
    assert m_absent is not None
    assert metric_value(m_absent, "cost") is None
    # "?" sentinel on a partial trace → None as well.
    m_sentinel = extract_metrics(
        Trace(trace_id="c", spans=[_root_with_cost("?")], resource_tags={})
    )
    assert m_sentinel is not None
    assert metric_value(m_sentinel, "cost") is None


def _root_no_turn_count():
    """A root span with the model tag but NO `github.copilot.turn_count` tag."""
    return Span(
        name="invoke_agent",
        duration_s=12.5,
        span_id="root",
        parent_id=None,
        tags={"gen_ai.request.model": "gpt-x"},
    )


def test_extract_metrics_token_availability_flags():
    """Token aggregates are flagged available only when the backing tag is present
    on every contributing chat span, distinguishing a genuine 0 from absent."""
    # Genuine zeros: tags present with 0 → available (a real measurement).
    m_zero = extract_metrics(
        Trace(trace_id="z", spans=[_root(), _chat("c1", in_tok=0, out_tok=0, cache=0)])
    )
    assert m_zero is not None
    assert m_zero.total_input_tokens == 0
    assert m_zero.input_tokens_available is True
    assert m_zero.output_tokens_available is True
    assert m_zero.cache_tokens_available is True

    # No chat spans at all → the token telemetry can't be measured → unavailable.
    m_no_chats = extract_metrics(Trace(trace_id="n", spans=[_root()]))
    assert m_no_chats is not None
    assert m_no_chats.total_input_tokens == 0
    assert m_no_chats.input_tokens_available is False
    assert m_no_chats.output_tokens_available is False
    assert m_no_chats.cache_tokens_available is False


def test_extract_metrics_token_unavailable_when_tag_missing_on_a_span():
    """If a contributing chat span is missing a token tag, the summed metric is
    flagged unavailable so a `<=` gate on it fails CLOSED (the sum under-counts)."""
    chat_missing_input = Span(
        name="chat",
        duration_s=1.0,
        span_id="c1",
        parent_id="root",
        tags={
            # gen_ai.usage.input_tokens intentionally absent
            "gen_ai.usage.output_tokens": 10,
            "gen_ai.usage.cache_read.input_tokens": 5,
        },
    )
    m = extract_metrics(Trace(trace_id="p", spans=[_root(), chat_missing_input]))
    assert m is not None
    assert m.input_tokens_available is False
    assert m.output_tokens_available is True
    assert m.cache_tokens_available is True


def test_int_tag_metric_gate_none_when_tag_absent():
    """int-tag metrics (turn_count / total_tokens) must fail CLOSED via
    metric_value → None when the backing tag is absent — the #121 fix — instead
    of silently passing a `<=` gate on a coerced 0."""
    from eval.trace import metric_value

    # turn_count: present on _root() → available; absent → None (not 0).
    m_turn = extract_metrics(Trace(trace_id="t1", spans=[_root()]))
    assert m_turn is not None and m_turn.turn_count == 3
    assert metric_value(m_turn, "turn_count") == 3.0
    m_no_turn = extract_metrics(Trace(trace_id="t2", spans=[_root_no_turn_count()]))
    assert m_no_turn is not None and m_no_turn.turn_count == 0
    assert metric_value(m_no_turn, "turn_count") is None

    # total_tokens: available only when both input and output halves are present.
    m_tokens = extract_metrics(
        Trace(trace_id="t3", spans=[_root(), _chat("c1", in_tok=100, out_tok=50)])
    )
    assert m_tokens is not None
    assert metric_value(m_tokens, "total_tokens") == 150.0
    # No chats → token tag absent → total_tokens gate value is None, NOT 0.
    m_tokens_absent = extract_metrics(Trace(trace_id="t4", spans=[_root()]))
    assert m_tokens_absent is not None and m_tokens_absent.total_input_tokens == 0
    assert metric_value(m_tokens_absent, "total_tokens") is None
    assert metric_value(m_tokens_absent, "total_input_tokens") is None
    assert metric_value(m_tokens_absent, "total_output_tokens") is None
    assert metric_value(m_tokens_absent, "total_cache_tokens") is None


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
    raw = json.dumps(
        [
            {"role": "assistant", "content": "hello"},
            {"role": "assistant", "content": [{"text": "world"}]},
            {"role": "assistant", "tool_calls": [{"function": {"name": "bash"}}]},
        ]
    )
    assert _parse_messages(raw) == "hello\nworld\n[tool_call: bash]"


def test_parse_messages_invalid_json():
    assert _parse_messages("not json") is None


def test_filter_by_run():
    t1 = Trace("a", [], {"eval.run_id": "r1"})
    t2 = Trace("b", [], {"eval.run_id": "r2"})
    assert filter_by_run([t1, t2], "r1") == [t1]


def test_fetch_traces_parses_jaeger_json(monkeypatch):
    payload = {
        "data": [
            {
                "traceID": "T1",
                "processes": {"p1": {"tags": [{"key": "eval.run_id", "value": "r1"}]}},
                "spans": [
                    {
                        "operationName": "invoke_agent",
                        "duration": 2_000_000,
                        "spanID": "s1",
                        "references": [],
                        "tags": [],
                    },
                    {
                        "operationName": "chat",
                        "duration": 1_000_000,
                        "spanID": "s2",
                        "references": [{"refType": "CHILD_OF", "spanID": "s1"}],
                        "tags": [{"key": "gen_ai.usage.input_tokens", "value": 42}],
                    },
                ],
            }
        ]
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
