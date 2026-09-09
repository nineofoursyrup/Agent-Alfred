"""Offline codec fixtures. Samples are documentation-derived, never live runs."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agent_alfred.messages import (
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agent_alfred.model import ModelRef, ModelRequest, ToolSpec
from agent_alfred.openai_compatible import OpenAICompatibleAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "issue14"


class Wire:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.chat = NS(completions=self)
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class Events:
    def __init__(self):
        self.payloads = []

    def emit(self, payload):
        self.payloads.append(payload)


def request():
    return ModelRequest(
        ModelRef("test", "m"),
        (TextBlock("system"),),
        (
            Message(
                "assistant",
                (
                    ToolCallBlock("opaque-1", "weather", {"city": "Paris"}),
                    ToolCallBlock("opaque-2", "weather", {"city": "Rome"}),
                ),
            ),
            Message(
                "user",
                (
                    ToolResultBlock("opaque-1", (TextBlock("sunny"),)),
                    ToolResultBlock("opaque-2", (TextBlock("unavailable"),), True),
                    TextBlock("compare"),
                ),
            ),
        ),
        tools=(ToolSpec("weather", "Get weather", {"type": "object"}),),
        max_tokens=100,
    )


def test_openai_encodes_tool_batch_and_decodes_documentation_blocks():
    wire = Wire(json.loads((FIXTURES / "openai-tool.json").read_text()))
    result = OpenAICompatibleAdapter(client=wire, model=request().model).respond(
        request()
    )
    sent = wire.requests[0]
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "Get weather",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert [m["role"] for m in sent["messages"]] == [
        "system",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assistant = sent["messages"][1]
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
        "city": "Paris"
    }
    assert sent["messages"][2]["tool_call_id"] == "opaque-1"
    assert "false" in sent["messages"][3]["content"]
    assert result.response.blocks == (
        TextBlock("Checking."),
        ToolCallBlock("opaque-3", "weather", {"city": "Oslo"}),
    )
    assert result.response.stop_reason == "tool_use"


def test_anthropic_encodes_one_user_result_batch_and_the_same_blocks():
    from agent_alfred.anthropic_native import AnthropicAdapter

    wire = Wire(json.loads((FIXTURES / "anthropic-tool.json").read_text()))
    result = AnthropicAdapter(client=wire, model=request().model).respond(request())
    sent = wire.requests[0]
    assert sent["system"] == [{"type": "text", "text": "system"}]
    assert sent["max_tokens"] == 100
    assert sent["tools"] == [
        {
            "name": "weather",
            "description": "Get weather",
            "input_schema": {"type": "object"},
        }
    ]
    assert [m["role"] for m in sent["messages"]] == ["assistant", "user"]
    content = sent["messages"][1]["content"]
    assert [b["type"] for b in content] == ["tool_result", "tool_result", "text"]
    assert content[0]["tool_use_id"] == "opaque-1"
    assert content[1]["is_error"] is True
    assert result.response.blocks == (
        TextBlock("Checking."),
        ToolCallBlock("opaque-3", "weather", {"city": "Oslo"}),
    )


@pytest.mark.parametrize(
    "style,raw,expected",
    [
        (
            "openai",
            {"prompt_tokens": 20, "completion_tokens": 3},
            (20, None, None, None, 3, None),
        ),
        (
            "openai",
            {
                "prompt_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 7},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            (20, 13, 7, None, None, 2),
        ),
        (
            "openai",
            {
                "prompt_tokens": 20,
                "prompt_cache_hit_tokens": 6,
                "prompt_cache_miss_tokens": 14,
            },
            (20, 14, 6, None, None, None),
        ),
        (
            "anthropic",
            {
                "input_tokens": 10,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 30,
                "output_tokens": 4,
            },
            (60, 10, 20, 30, 4, None),
        ),
        (
            "anthropic",
            {"input_tokens": 10, "output_tokens": 4},
            (None, 10, None, None, 4, None),
        ),
    ],
)
def test_usage_reports_both_input_bases_without_manufacturing_missing_details(
    style,
    raw,
    expected,
):
    from agent_alfred.anthropic_native import AnthropicAdapter

    response = json.loads((FIXTURES / f"{style}-tool.json").read_text())
    response["usage"] = raw
    cls = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter
    result = cls(client=Wire(response), model=request().model).respond(request())
    usage = result.attempts[0].usage
    assert (
        usage.total_input_tokens,
        usage.uncached_input_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
    ) == expected
    assert usage.raw == raw


@pytest.mark.parametrize(
    "style,reason,expected",
    [
        ("openai", "stop", "end_turn"),
        ("openai", "insufficient_system_resource", "unknown"),
        ("anthropic", "future_reason", "unknown"),
    ],
)
def test_stop_reasons_are_open_without_inferring_stop_sequence(style, reason, expected):
    from agent_alfred.anthropic_native import AnthropicAdapter

    response = json.loads((FIXTURES / f"{style}-tool.json").read_text())
    if style == "openai":
        response["choices"][0]["finish_reason"] = reason
    else:
        response["stop_reason"] = reason
    cls = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter
    assert (
        cls(client=Wire(response), model=request().model)
        .respond(request())
        .response.stop_reason
        == expected
    )


def chunk(delta=None, finish=None, usage=None):
    return {
        "choices": [{"delta": delta or {}, "finish_reason": finish}],
        "usage": usage,
    }


def test_openai_stream_accumulates_parallel_calls_by_index_and_last_usage():
    chunks = [
        chunk({"content": "Checking."}),
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "a",
                        "function": {"name": "weather", "arguments": '{"city":'},
                    },
                    {
                        "index": 1,
                        "id": "b",
                        "function": {"name": "weather", "arguments": '{"city":'},
                    },
                ]
            }
        ),
        chunk(
            {
                "tool_calls": [
                    {"index": 1, "function": {"arguments": '"Rome"}'}},
                    {"index": 0, "function": {"arguments": '"Paris"}'}},
                ]
            },
            "tool_calls",
        ),
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 6}},
        "[DONE]",
    ]
    events = Events()
    result = OpenAICompatibleAdapter(
        client=Wire(chunks), model=request().model, stream=True
    ).respond(request(), events=events)
    assert result.response.blocks == (
        TextBlock("Checking."),
        ToolCallBlock("a", "weather", {"city": "Paris"}),
        ToolCallBlock("b", "weather", {"city": "Rome"}),
    )
    assert result.attempts[0].usage.output_tokens == 6
    assert [p.name for p in events.payloads][-1] == "attempt.committed"


def test_openai_disconnect_preserves_raw_arguments_without_any_tool_call():
    chunks = [
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "a",
                        "function": {"name": "weather", "arguments": '{"city":'},
                    }
                ]
            }
        )
    ]
    events = Events()
    result = OpenAICompatibleAdapter(
        client=Wire(chunks), model=request().model, stream=True
    ).respond(request(), events=events)
    assert result.final_error.code == "incomplete_stream"
    aborted = events.payloads[-1]
    assert not any(isinstance(b, ToolCallBlock) for b in aborted.blocks)
    assert [(f.call_id, f.name, f.raw) for f in aborted.unparsed_tool_arguments] == [
        ("a", "weather", '{"city":')
    ]


def anthropic_stream():
    return [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                }
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reason"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "opaque"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": "Checking."},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "a",
                "name": "weather",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("complete", [True, False])
def test_anthropic_stream_preserves_indices_signatures_and_requires_message_stop(
    complete,
):
    from agent_alfred.anthropic_native import AnthropicAdapter
    from agent_alfred.messages import ThinkingBlock

    stream = anthropic_stream()
    if not complete:
        stream.pop()
    events = Events()
    result = AnthropicAdapter(
        client=Wire(stream), model=request().model, stream=True
    ).respond(request(), events=events)
    assert result.attempts[0].usage.output_tokens == 5
    assert [p.index for p in events.payloads if p.name == "block.started"] == [0, 1, 2]
    if complete:
        assert result.response.blocks == (
            ThinkingBlock("reason", "sig-opaque"),
            TextBlock("Checking."),
            ToolCallBlock("a", "weather", {"city": "Paris"}),
        )
    else:
        assert result.final_error.code == "incomplete_stream"
        assert not any(isinstance(b, ToolCallBlock) for b in events.payloads[-1].blocks)
        assert events.payloads[-1].unparsed_tool_arguments[0].raw == '{"city":"Paris"}'


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("missing", ["id", "name", "arguments"])
def test_malformed_tool_call_never_commits(style, missing):
    from agent_alfred.anthropic_native import AnthropicAdapter

    payload = json.loads((FIXTURES / f"{style}-tool.json").read_text())
    if style == "openai":
        call = payload["choices"][0]["message"]["tool_calls"][0]
        if missing == "id":
            call.pop("id")
        else:
            call["function"].pop(missing)
    else:
        call = payload["content"][1]
        call.pop("input" if missing == "arguments" else missing)
    cls = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter
    events = Events()
    result = cls(client=Wire(payload), model=request().model).respond(
        request(), events=events
    )
    assert result.response is None
    assert result.final_error.code == "invalid_response"
    assert events.payloads[-1].name == "attempt.aborted"


def test_native_reported_uncached_input_is_known_without_cache_fields():
    from agent_alfred.anthropic_native import AnthropicAdapter

    payload = json.loads((FIXTURES / "anthropic-tool.json").read_text())
    payload["usage"] = {"input_tokens": 10, "output_tokens": 4}
    usage = (
        AnthropicAdapter(client=Wire(payload), model=request().model)
        .respond(request())
        .attempts[0]
        .usage
    )
    assert usage.uncached_input_tokens == 10
    assert usage.total_input_tokens is None
    assert usage.cache_read_tokens is None


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("choice", ["auto", "none", "required", "named"])
def test_all_tool_choices_use_the_corresponding_wire_shape(style, choice):
    from dataclasses import replace

    from agent_alfred.anthropic_native import AnthropicAdapter
    from agent_alfred.model import NamedToolChoice

    req = replace(
        request(),
        tool_choice=NamedToolChoice("weather") if choice == "named" else choice,
    )
    wire = Wire(json.loads((FIXTURES / f"{style}-tool.json").read_text()))
    cls = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter
    assert cls(client=wire, model=req.model).respond(req).response is not None
    expected = (
        {"type": "function", "function": {"name": "weather"}}
        if choice == "named"
        else choice
    )
    if style == "anthropic":
        expected = (
            {"type": "tool", "name": "weather"}
            if choice == "named"
            else {"type": "any" if choice == "required" else choice}
        )
    assert wire.requests[0]["tool_choice"] == expected


def test_endpoint_reported_cost_uses_decimal_ticks_without_estimation():
    from decimal import Decimal

    payload = json.loads((FIXTURES / "openai-tool.json").read_text())
    payload["usage"] = {"cost_in_usd_ticks": 37756000}
    usage = (
        OpenAICompatibleAdapter(client=Wire(payload), model=request().model)
        .respond(request())
        .attempts[0]
        .usage
    )
    assert usage.endpoint_reported_cost_usd == Decimal("0.0037756")
    assert usage.total_input_tokens is None
    assert usage.raw == payload["usage"]


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("complete", [True, False])
def test_stream_close_failure_returns_attempt_usage_and_original_error(style, complete):
    from agent_alfred.anthropic_native import AnthropicAdapter

    stream = (
        anthropic_stream()
        if style == "anthropic"
        else [
            chunk({"content": "ok"}, "stop" if complete else None),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            "[DONE]",
        ]
    )
    if style == "anthropic" and not complete:
        stream.pop()

    class CloseFailure:
        def __iter__(self):
            return iter(stream)

        def close(self):
            raise OSError("synthetic close failure")

    cls = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter
    events = Events()
    result = cls(
        client=Wire(CloseFailure()), model=request().model, stream=True
    ).respond(request(), events=events)
    assert result.response is None
    assert result.final_error.code == (
        "stream_close_failed" if complete else "incomplete_stream"
    )
    assert len(result.attempts) == 1
    assert result.attempts[0].usage.output_tokens == 5
    assert result.attempts[0].outcome == "aborted"
    if complete:
        assert result.final_error.retryable is False
    assert events.payloads[-1].name == "attempt.aborted"
    assert events.payloads[-1].usage.output_tokens == 5
    assert not any(
        isinstance(block, ToolCallBlock) for block in events.payloads[-1].blocks
    )


@pytest.mark.parametrize("style", ["openai", "anthropic"])
def test_close_failure_usage_reaches_real_run_ledger(style, tmp_path):
    import sqlite3

    from agent_alfred import schema
    from agent_alfred.anthropic_native import AnthropicAdapter
    from agent_alfred.clock import FakeClock
    from agent_alfred.runtime.work import SubmitRequest
    from agent_alfred.wiring import build_host

    stream = (
        anthropic_stream()
        if style == "anthropic"
        else [
            chunk({"content": "ok"}, "stop"),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            "[DONE]",
        ]
    )

    class CloseFailure:
        def __iter__(self):
            return iter(stream)

        def close(self):
            raise OSError("synthetic close failure")

    cls = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter

    gate_json = '{"retrieve":false,"query":null,"reason_code":"greeting"}'
    gate_stream = (
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": gate_json}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ] if style == "anthropic" else [
            chunk({"content": gate_json}, "stop"),
            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            "[DONE]",
        ]
    )

    class GateThenCloseFailure(Wire):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            return gate_stream if len(self.requests) == 1 else CloseFailure()

    from agent_alfred.stream_fallback import StreamFallback

    clock = FakeClock()

    class Factory:
        def create(self, snapshot):
            return StreamFallback(
                cls(
                    client=GateThenCloseFailure(None),
                    model=request().model, stream=True
                ),
                clock=clock, stream=True, stream_fallback=False,
            )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    host = build_host(conn=conn, factory=Factory(), clock=clock)
    host.start()
    try:
        run = host.submit(SubmitRequest("hello")).run_id
        assert host.wait(run, timeout=2).outcome == "failed"
        evidence = host.read_run_evidence(run, trace_root=tmp_path)
        assert len(evidence["attempts"]) == 2
        assert evidence["attempts"][0]["outcome"] == "committed"
        assert evidence["attempts"][0]["usage"]["output_tokens"] == 1
        assert evidence["attempts"][1]["outcome"] == "aborted"
        assert evidence["attempts"][1]["usage"]["output_tokens"] == 5
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize(
    "signal,expected",
    [("missing", False), ("literal", True), ("sdk", True), ("sdk_false", False)],
)
def test_adapter_requires_positive_stream_completion_evidence(signal, expected):
    chunks = [chunk({"content": "ok"}, "stop")]
    if signal == "literal":
        chunks.append("[DONE]")

    class Stream:
        def __iter__(self):
            return iter(chunks)

    stream = Stream()
    if signal.startswith("sdk"):
        stream.completed = signal == "sdk"
    result = OpenAICompatibleAdapter(
        client=Wire(stream), model=request().model, stream=True
    ).respond(request())
    assert (result.response is not None) is expected
    if not expected:
        assert result.final_error.code == "incomplete_stream"


@pytest.mark.parametrize(
    "deltas",
    [
        [{"reasoning_content": "reason"}, {"content": "text"}],
        [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "a",
                        "function": {"name": "weather", "arguments": "{}"},
                    }
                ]
            }
        ],
        [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "a",
                        "function": {"name": "weather", "arguments": "{}"},
                    }
                ]
            },
            {"content": "text"},
            {"reasoning_content": "reason"},
        ],
    ],
)
def test_stream_events_and_committed_snapshot_share_contiguous_block_indices(deltas):
    from agent_alfred.messages import ThinkingBlock

    stream = [*(chunk(delta) for delta in deltas), chunk(finish="stop"), "[DONE]"]
    events = Events()
    result = OpenAICompatibleAdapter(
        client=Wire(stream), model=request().model, stream=True
    ).respond(request(), events=events)
    started = [event for event in events.payloads if event.name == "block.started"]
    assert [event.index for event in started] == list(
        range(len(result.response.blocks))
    )
    types = {ThinkingBlock: "thinking", TextBlock: "text", ToolCallBlock: "tool_use"}
    assert [event.block_type for event in started] == [
        types[type(block)] for block in result.response.blocks
    ]


@pytest.mark.parametrize("failure", ["close", "incomplete", "iteration"])
@pytest.mark.parametrize("text_first", [True, False])
def test_aborted_snapshot_preserves_received_non_tool_block_order(failure, text_first):
    from agent_alfred.messages import ThinkingBlock

    deltas = [{"content": "text"}, {"reasoning_content": "reason"}]
    if not text_first:
        deltas.reverse()

    class Stream:
        def __iter__(self):
            for delta in deltas:
                yield chunk(delta)
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "a",
                            "function": {"name": "weather", "arguments": "{}"},
                        }
                    ]
                }
            )
            if failure == "iteration":
                raise OSError("synthetic iteration failure")
            yield chunk(finish="stop")
            if failure == "close":
                yield "[DONE]"

        def close(self):
            if failure == "close":
                raise OSError("synthetic close failure")

    events = Events()
    result = OpenAICompatibleAdapter(
        client=Wire(Stream()), model=request().model, stream=True
    ).respond(request(), events=events)
    assert result.response is None
    aborted = events.payloads[-1]
    assert aborted.name == "attempt.aborted"
    types = {ThinkingBlock: "thinking", TextBlock: "text"}
    assert [types[type(block)] for block in aborted.blocks] == [
        event.block_type
        for event in events.payloads
        if event.name == "block.started" and event.block_type != "tool_use"
    ]
    assert len(aborted.unparsed_tool_arguments) == 1
    assert aborted.unparsed_tool_arguments[0].raw == "{}"


@pytest.mark.parametrize(
    "error_type,expected",
    [
        ("overloaded_error", True),
        ("rate_limit_error", True),
        ("future_error", "unknown"),
        (None, "unknown"),
    ],
)
def test_anthropic_stream_error_preserves_unknown_retryability(error_type, expected):
    from agent_alfred.anthropic_native import AnthropicAdapter

    events = Events()
    result = AnthropicAdapter(
        client=Wire(
            [
                {
                    "type": "error",
                    "error": {"type": error_type, "message": "synthetic failure"},
                }
            ]
        ),
        model=request().model,
        stream=True,
    ).respond(request(), events=events)
    assert result.response is None
    assert result.final_error.retryable == expected
    assert events.payloads[-1].error.retryable == expected


def test_registry_nested_schemas_are_json_serializable_at_wire_boundary():
    from dataclasses import replace

    from agent_alfred.clock import FakeClock
    from agent_alfred.tools import Tool, ToolRegistry, ToolSuccess

    registry = ToolRegistry((Tool("echo", "Echo", {"type": "object", "properties": {
        "text": {"type": "string"}}, "required": ["text"]},
        lambda args, ctx: ToolSuccess(()), "local_read"),), clock=FakeClock())
    wire = Wire(json.loads((FIXTURES / "openai-tool.json").read_text()))
    OpenAICompatibleAdapter(client=wire, model=request().model).respond(
        replace(request(), tools=registry.schemas()))
    encoded = json.loads(json.dumps(wire.requests[0]))
    assert encoded["tools"][0]["function"]["parameters"]["required"] == ["text"]
