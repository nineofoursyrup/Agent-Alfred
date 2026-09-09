"""Actual adapter requests carry body-free input identities, independent of SSE."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_alfred.adapter_common import ResponseDecodeError
from agent_alfred.anthropic_native import AnthropicAdapter
from agent_alfred.messages import TextBlock, text_message
from agent_alfred.model import ModelRef, ModelRequest
from agent_alfred.openai_compatible import OpenAICompatibleAdapter


class FailingWire:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=self)
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        raise ConnectionError("fixture transport failed")


@pytest.mark.parametrize("adapter_type", [OpenAICompatibleAdapter, AnthropicAdapter])
@pytest.mark.parametrize("stream", [False, True])
def test_sent_failed_attempt_has_input_evidence_without_event_sink(
    adapter_type, stream
):
    wire = FailingWire()
    sent = []
    request = ModelRequest(
        model=ModelRef("test", "model"),
        system=None,
        messages=(text_message("user", "private text"),),
        on_attempt_started=sent.append,
    )
    result = adapter_type(client=wire, model=request.model, stream=stream).respond(
        request
    )
    assert len(wire.requests) == 1
    assert sent == [result.attempts[0].attempt_id]
    assert "on_attempt_started" not in wire.requests[0]


@pytest.mark.parametrize("adapter_type", [OpenAICompatibleAdapter, AnthropicAdapter])
def test_local_request_encoding_failure_has_no_input_attempt(adapter_type):
    wire = FailingWire()
    sent = []
    request = ModelRequest(
        model=ModelRef("test", "model"),
        system=None,
        messages=(text_message("user", "text"),),
        on_attempt_started=sent.append,
    )
    request = replace(request, system=(TextBlock("ok"), object()))
    with pytest.raises((TypeError, AttributeError, ValueError, ResponseDecodeError)):
        adapter_type(client=wire, model=request.model).respond(request)
    assert wire.requests == []
    assert sent == []


@pytest.mark.parametrize("adapter_type", [OpenAICompatibleAdapter, AnthropicAdapter])
@pytest.mark.parametrize("stream", [False, True])
def test_observation_failure_before_transport_propagates_without_attempt(
    adapter_type, stream
):
    wire = FailingWire()

    def unavailable(_):
        raise RuntimeError("local observer failed")

    request = ModelRequest(
        ModelRef("test", "m"),
        None,
        (text_message("user", "hi"),),
        on_attempt_started=unavailable,
    )
    with pytest.raises(Exception):
        adapter_type(client=wire, model=request.model, stream=stream).respond(request)
    assert wire.requests == []


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_real_sdk_utf8_failure_never_starts_http_attempt(style, stream):
    import httpx2
    from anthropic import Anthropic
    from openai import OpenAI

    sent, wire = [], []

    def handle(request):
        wire.append(request)
        return httpx2.Response(500)

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as http:
        sdk = (OpenAI if style == "openai" else Anthropic)(
            api_key="test",
            base_url="https://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        cls = OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter
        request = ModelRequest(
            ModelRef("test", "m"),
            None,
            (text_message("user", "\ud800"),),
            on_attempt_started=sent.append,
        )
        with pytest.raises(Exception):
            cls(client=sdk, model=request.model, stream=stream).respond(request)
    assert wire == []
    assert sent == []


@pytest.mark.parametrize("adapter_type", [OpenAICompatibleAdapter, AnthropicAdapter])
def test_event_publication_failure_precedes_input_observation(adapter_type):
    wire, sent = FailingWire(), []

    class LocalFailure:
        def emit(self, _payload):
            raise RuntimeError("local sink failure")

    request = ModelRequest(
        ModelRef("test", "m"), None, (), on_attempt_started=sent.append
    )
    with pytest.raises(Exception):
        adapter_type(client=wire, model=request.model).respond(
            request, events=LocalFailure()
        )
    assert sent == []
    assert wire.requests == []


def test_sdk_local_request_hook_failure_does_not_leak_scope_to_unrelated_http():
    import httpx2
    from openai import OpenAI

    sent, calls = [], []

    def handle(request):
        calls.append(request)
        return httpx2.Response(200, text="plain http")

    def fail(_request):
        raise RuntimeError("before HTTP transport")

    with httpx2.Client(
        transport=httpx2.MockTransport(handle), event_hooks={"request": [fail]}
    ) as http:
        sdk = OpenAI(
            api_key="test",
            base_url="https://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        request = ModelRequest(
            ModelRef("test", "m"), None, (), on_attempt_started=sent.append
        )
        with pytest.raises(Exception):
            OpenAICompatibleAdapter(client=sdk, model=request.model).respond(request)
        assert calls == [] and sent == []
        http.event_hooks["request"].clear()
        assert http.get("https://fixture.invalid/plain").text == "plain http"
        assert len(calls) == 1 and sent == []
        assert not http.is_closed
