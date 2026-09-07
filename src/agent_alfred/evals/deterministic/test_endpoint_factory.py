"""Offline SDK wire evidence; no vendor request or credential is used."""

from dataclasses import replace

import httpx2 as httpx
import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.endpoints import ModelEndpoint, ModelRoute
from agent_alfred.model import EndpointUnconfigured, ModelRef, ModelRequest
from agent_alfred.runtime.config import MutableAssignmentProvider


def snapshot(style="openai", model="deepseek-v4-flash", endpoint="opencode-go"):
    return MutableAssignmentProvider(
        endpoint_id=endpoint,
        model_id=model,
        wire_style=style,
        api_key="synthetic-test-key",
    ).capture()


@pytest.mark.parametrize(
    "style,model,path,header,absent,payload",
    [
        (
            "openai",
            "deepseek-v4-flash",
            "/zen/go/v1/chat/completions",
            "authorization",
            "x-api-key",
            {
                "id": "c",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        ),
        (
            "anthropic",
            "qwen3.7-max",
            "/zen/go/v1/messages",
            "x-api-key",
            "authorization",
            {
                "id": "m",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {},
            },
        ),
    ],
)
def test_factory_honors_style_path_and_single_auth(
    style, model, path, header, absent, payload
):
    from agent_alfred.endpoint_factory import EndpointClientFactory

    seen = []

    def send(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(clock=FakeClock(), http_client=http)
        client = factory.create(snapshot(style, model))
        result = client.respond(ModelRequest(ModelRef("opencode-go", model), None, ()))
    assert result.response.blocks[0].text == "ok"
    assert len(seen) == 1
    assert seen[0].url.path == path
    assert seen[0].headers[header] == (
        "Bearer synthetic-test-key"
        if header == "authorization"
        else "synthetic-test-key"
    )
    assert absent not in seen[0].headers


def test_fake_endpoint_extends_factory_without_endpoint_branches():
    from agent_alfred.endpoint_factory import EndpointClientFactory

    row = ModelEndpoint(
        "injected",
        "https://example.invalid/custom",
        "UNUSED",
        {"m": ModelRoute("openai", "/route")},
    )
    seen = []

    def send(request):
        seen.append(request)
        return httpx.Response(
            400, json={"error": {"message": "synthetic invalid request"}}
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(
            clock=FakeClock(), endpoints=(row,), http_client=http
        )
        result = factory.create(snapshot(endpoint="injected", model="m")).respond(
            ModelRequest(ModelRef("injected", "m"), None, ())
        )
    assert result.final_error.status_code == 400
    assert len(seen) == 1
    assert seen[0].url.path == "/custom/route"


def test_unconfigured_and_unsupported_fail_before_transport_construction():
    from agent_alfred.endpoint_factory import EndpointClientFactory
    from agent_alfred.model import ModelUnsupported

    class NoTransport(EndpointClientFactory):
        def _build_transport(self, snapshot):
            raise AssertionError("transport must not be built")

    factory = NoTransport(clock=FakeClock())
    with pytest.raises(EndpointUnconfigured):
        factory.create(replace(snapshot(), api_key=None))
    with pytest.raises(ModelUnsupported):
        factory.create(snapshot(model="grok-4.6"))


def test_override_survives_key_rotation_and_refuses_next_admission():
    import sqlite3

    from agent_alfred import schema
    from agent_alfred.endpoint_factory import EndpointClientFactory
    from agent_alfred.evals.deterministic.test_support_overrides import override
    from agent_alfred.events import FanOutSink
    from agent_alfred.runtime.host import RuntimeHost
    from agent_alfred.runtime.work import SubmitRequest
    from agent_alfred.settings import Settings
    from agent_alfred.support_overrides import SupportOverrides

    store = SupportOverrides()
    store.record(override())
    provider = MutableAssignmentProvider(
        endpoint_id="opencode-go",
        model_id="deepseek-v4-flash",
        wire_style="openai",
        api_key="synthetic-key-1",
    )
    provider.rotate_key("synthetic-key-2")
    factory = EndpointClientFactory(clock=FakeClock(), support_overrides=store)
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    host = RuntimeHost(
        conn=conn,
        factory=factory,
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([], process_instance_id="test"),
        process_instance_id="test",
        snapshot_provider=provider,
        support_overrides=store,
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest("hi"))
        assert submitted.kind == "model_unsupported"
        assert submitted.run_id is None
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)
        assert provider.capture().primary == snapshot().primary
    finally:
        host.close()
        conn.close()


@pytest.mark.parametrize("terminal", ["", "data: [DONE]\n\n"])
def test_real_sdk_stream_requires_done_even_after_finish_reason(terminal):
    from agent_alfred.endpoint_factory import EndpointClientFactory

    data = (
        'data: {"id":"c","choices":[{"index":0,"delta":{"content":"ok"},'
        '"finish_reason":"stop"}]}\n\n' + terminal
    )

    def send(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=data.encode()
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(clock=FakeClock(), http_client=http)
        client = factory.create(replace(snapshot(), stream=True, stream_fallback=False))
        result = client.respond(ModelRequest(ModelRef("opencode-go", "m"), None, ()))
    assert (result.response is not None) == bool(terminal)
    if not terminal:
        assert result.final_error.code == "incomplete_stream"
