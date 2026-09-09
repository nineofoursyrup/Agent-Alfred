"""OpenCode compatibility observed at real SDK HTTP requests, entirely offline."""

from dataclasses import replace

import httpx2 as httpx
import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.endpoint_factory import EndpointClientFactory
from agent_alfred.endpoints import ModelEndpoint, ModelRoute
from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
from agent_alfred.model import ModelRef, ModelRequest


def test_default_factory_sends_truthful_client_and_stable_session_headers():
    seen = []

    def send(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(clock=FakeClock(), http_client=http)
        client = factory.create(snapshot())
        request = ModelRequest(ModelRef("opencode-go", "deepseek-v4-flash"), None, ())
        client.respond(request)
        client.respond(request)
        factory.close()
    assert len(seen) == 2
    assert seen[0].headers["user-agent"].startswith("agent-alfred/")
    assert seen[0].headers["x-opencode-session"]
    assert (
        seen[0].headers["x-opencode-session"] == seen[1].headers["x-opencode-session"]
    )
    assert b"extra_headers" not in seen[0].content


def test_default_host_keeps_session_across_gate_steps_runs_and_restart(
    tmp_path, monkeypatch
):
    import json
    import os
    from collections import deque

    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.settings import OPENCODE_API_KEY_ENV, Settings
    from agent_alfred.wiring import build_default_host

    monkeypatch.setenv(OPENCODE_API_KEY_ENV, "synthetic-offline-key")
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    gate = {"content": '{"retrieve":false,"query":null,"reason_code":"greeting"}'}
    responses = deque(
        [
            gate,
            {
                "tool_calls": [
                    {
                        "id": "create",
                        "type": "function",
                        "function": {
                            "name": "create_event",
                            "arguments": json.dumps(
                                {
                                    "title": "synthetic",
                                    "starts_at": "2026-09-10T10:00:00Z",
                                }
                            ),
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "id": "query",
                        "type": "function",
                        "function": {
                            "name": "query_events",
                            "arguments": "{}",
                        },
                    }
                ]
            },
            {"content": "created and queried"},
            gate,
            {"content": "same session"},
            gate,
            {"content": "different session"},
            {"content": "probe one"},
            {"content": "probe two"},
            gate,
            {"content": "restored session"},
        ]
    )
    seen = []

    def send(self, request):
        assert str(request.url) == "https://opencode.ai/zen/go/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-offline-key"
        seen.append(request)
        message = responses.popleft()
        return httpx.Response(
            200,
            json={
                "id": "c",
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls"
                        if "tool_calls" in message
                        else "stop",
                    }
                ],
            },
        )

    # Replace only network IO, retaining the default Host, config, factory,
    # pooled SDK clients, codecs, retry policy and real tool execution.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    state = tmp_path / "state"
    host = build_default_host(state_dir=state, settings=Settings())
    host.start()
    try:
        session = host.create_session()
        other = host.create_session()
        for prompt, selected in [
            ("create and query", session),
            ("again", session),
            ("hello", other),
        ]:
            accepted = host.submit(SubmitRequest(prompt, session_id=selected))
            assert host.wait(accepted.run_id, timeout=10).outcome == "completed"
        for _ in range(2):
            accepted = host.submit(
                SubmitRequest(
                    "ping",
                    purpose="inference_probe",
                    endpoint_id="opencode-go",
                    model_id="deepseek-v4-flash",
                )
            )
            assert host.wait(accepted.run_id, timeout=10).outcome == "completed"
    finally:
        assert host.close()
    restored = build_default_host(state_dir=state, settings=Settings())
    restored.start()
    try:
        accepted = restored.submit(
            SubmitRequest("again after restart", session_id=session)
        )
        assert restored.wait(accepted.run_id, timeout=10).outcome == "completed"
    finally:
        assert restored.close()
    assert not responses
    headers = [request.headers["x-opencode-session"] for request in seen]
    assert len(set(headers[:6] + headers[10:12])) == 1
    assert headers[6] == headers[7]
    assert len({headers[0], headers[6], headers[8], headers[9]}) == 4
    assert all(
        request.headers["user-agent"].startswith("agent-alfred/") for request in seen
    )
    assert all(session not in value and other not in value for value in headers)
    assert all("conversation_id" not in json.loads(request.content) for request in seen)




@pytest.mark.parametrize(
    "style,model", [("openai", "deepseek-v4-flash"), ("anthropic", "qwen3.7-max")]
)
@pytest.mark.parametrize("mode", ["nonstream", "retry", "fallback"])
def test_headers_survive_both_codecs_retry_and_stream_fallback(style, model, mode):
    seen = []

    def send(request):
        seen.append(request)
        if len(seen) == 1 and mode == "retry":
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        if len(seen) == 1 and mode == "fallback":
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=b""
            )
        payload = (
            {
                "id": "c",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
            if style == "openai"
            else {
                "id": "m",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        )
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(clock=FakeClock(), http_client=http)
        client = factory.create(
            replace(snapshot(style, model), stream=mode == "fallback")
        )
        result = client.respond(
            ModelRequest(
                ModelRef("opencode-go", model),
                None,
                (),
                conversation_id="session:synthetic",
            )
        )
        assert result.response is not None
        assert result.response.blocks[0].text == "ok"
        factory.close()
    assert len(seen) == (1 if mode == "nonstream" else 2)
    assert len({request.headers["x-opencode-session"] for request in seen}) == 1
    assert all(
        request.headers["user-agent"].startswith("agent-alfred/") for request in seen
    )
    assert all(b"extra_headers" not in request.content for request in seen)


@pytest.mark.parametrize(
    "base,path",
    [
        ("https://example.invalid/v1", "/chat/completions"),
        ("https://opencode.ai.evil.invalid/zen/go/v1", "/chat/completions"),
        ("http://opencode.ai/zen/go/v1", "/chat/completions"),
        ("https://opencode.ai/zen/v1", "/chat/completions"),
        ("https://opencode.ai/zen/go/v1-extra", "/chat/completions"),
        ("https://opencode.ai:444/zen/go/v1", "/chat/completions"),
        ("https://opencode.ai/zen/go/v1", "https://example.invalid/chat/completions"),
    ],
)
def test_other_destinations_do_not_receive_opencode_identity(base, path):
    seen = []

    def send(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"},
                ],
            },
        )

    row = ModelEndpoint(
        "opencode-go", base, "UNUSED", {"m": ModelRoute("openai", path)}
    )
    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(
            clock=FakeClock(), endpoints=(row,), http_client=http
        )
        client = factory.create(snapshot(model="m"))
        result = client.respond(
            ModelRequest(
                ModelRef("opencode-go", "m"),
                None,
                (),
                conversation_id="session:private",
            )
        )
        assert result.response is not None
        factory.close()
    assert len(seen) == 1
    assert "x-opencode-session" not in seen[0].headers
    assert not seen[0].headers["user-agent"].startswith("agent-alfred/")


def test_pooled_transport_does_not_hold_session_state():
    seen = []

    def send(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(clock=FakeClock(), http_client=http)
        first = factory.create(snapshot())
        second = factory.create(snapshot())
        request = ModelRequest(ModelRef("opencode-go", "deepseek-v4-flash"), None, ())
        for client, identity in [
            (first, "session:a"),
            (second, "session:b"),
            (first, "session:a"),
            (first, None),
            (second, None),
        ]:
            assert client.respond(replace(request, conversation_id=identity)).response
        factory.close()
    ids = [request.headers["x-opencode-session"] for request in seen]
    assert ids[0] == ids[2]
    assert len({ids[0], ids[1], ids[3], ids[4]}) == 4
