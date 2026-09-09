"""Absolute IO deadline through real SDK streaming and an injected network backend."""

import httpcore2
import httpx2
import pytest
from anthropic import Anthropic
from openai import OpenAI

from agent_alfred.anthropic_native import AnthropicAdapter
from agent_alfred.clock import FakeClock
from agent_alfred.messages import text_message
from agent_alfred.model import ModelRef, ModelRequest
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.stream_fallback import StreamFallback


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("tls", [False, True])
def test_real_sdk_stream_read_uses_remaining_deadline_and_closes_at_five_seconds(
    style, tls
):
    from agent_alfred.attempt_io import ModelHTTPTransport

    clock = FakeClock()
    reads = []
    closed = []

    class Stream(httpcore2.NetworkStream):
        headers = True

        def read(self, max_bytes, timeout=None):
            reads.append(timeout)
            if self.headers:
                self.headers = False
                return b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
            if timeout < 2:
                clock.monotonic_value += timeout
                raise httpcore2.ReadTimeout("injected blocked read reaches deadline")
            clock.monotonic_value += 2
            if style == "anthropic":
                return (
                    b'event: message_start\ndata: {"type":"message_start",'
                    b'"message":{"usage":{"output_tokens":7}}}\n\n'
                    if len(reads) == 2
                    else b'event: ping\ndata: {"type":"ping"}\n\n'
                )
            return (
                b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}],'
                b'"usage":{"completion_tokens":7}}\n\n'
            )

        def write(self, buffer, timeout=None):
            pass

        def close(self):
            closed.append(True)

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert timeout == 4
            clock.monotonic_value += 1
            return self

        def get_extra_info(self, info):
            return None

    class Backend(httpcore2.NetworkBackend):
        def connect_tcp(self, host, port, timeout=None, **kwargs):
            assert timeout == 5
            if tls:
                clock.monotonic_value += 1
            return Stream()

    transport = ModelHTTPTransport(httpx2.HTTPTransport(), network_backend=Backend())
    with httpx2.Client(transport=transport, trust_env=False) as http:
        sdk = (OpenAI if style == "openai" else Anthropic)(
            api_key="fixture",
            base_url=("https" if tls else "http") + "://fixture.invalid",
            http_client=http,
            max_retries=0,
        )
        adapter = (OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter)(
            client=sdk, model=ModelRef("test", "m"), stream=True
        )
        client = StreamFallback(
            adapter, clock=clock, stream=True, stream_fallback=False
        )
        sent = []
        result = client.respond(
            ModelRequest(
                ModelRef("test", "m"),
                None,
                (text_message("user", "hi"),),
                on_attempt_started=sent.append,
            ),
            deadline=5.0,
        )
    assert clock.monotonic_value == 5.0
    assert reads == ([3.0, 3.0, 1.0] if tls else [5.0, 5.0, 3.0, 1.0])
    assert closed
    assert result.response is None
    assert result.attempts[0].usage.output_tokens == 7
    assert sent == [result.attempts[0].attempt_id]


def test_preexisting_unwrapped_http_connection_is_rejected_without_claiming_deadline():
    import socket
    import threading

    release = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    requests = []

    def serve():
        with listener:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                body = b""
                while b"\r\n\r\n" not in body:
                    body += connection.recv(4096)
                requests.append(body)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                release.wait(2)

    thread = threading.Thread(target=serve)
    thread.start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        with httpx2.Client(trust_env=False) as http:
            assert http.get(url).text == "ok"
            sdk = OpenAI(api_key="test", base_url=url, http_client=http, max_retries=0)
            adapter = OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m"))
            client = StreamFallback(adapter, clock=FakeClock())
            sent = []
            request = ModelRequest(
                ModelRef("test", "m"), None, (), on_attempt_started=sent.append
            )
            with pytest.raises(Exception):
                client.respond(request, deadline=5)
            assert sent == [] and len(requests) == 1
            assert not http.is_closed
    finally:
        release.set()
        thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("style", ["openai", "anthropic"])
def test_budgeted_sdk_rejects_hidden_retry_before_any_http(style):
    requests = []
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda r: requests.append(r))
    ) as http:
        sdk = (OpenAI if style == "openai" else Anthropic)(
            api_key="fixture", http_client=http, max_retries=1
        )
        adapter = (OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter)(
            client=sdk, model=ModelRef("test", "m"), stream=False
        )
        client = StreamFallback(adapter, clock=FakeClock(), stream=False)
        sent = []
        with pytest.raises(ValueError, match="max_retries"):
            client.respond(
                ModelRequest(
                    ModelRef("test", "m"),
                    None,
                    (text_message("user", "hi"),),
                    on_attempt_started=sent.append,
                ),
                deadline=5,
            )
    assert requests == []
    assert sent == []


def test_budgeted_transport_clips_phase_timeouts_and_disallows_hidden_redirect():
    requests = []

    def wire(request):
        requests.append(request)
        return httpx2.Response(307, headers={"location": "https://redirect.test/next"})

    with httpx2.Client(
        transport=httpx2.MockTransport(wire), follow_redirects=True
    ) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        adapter = OpenAICompatibleAdapter(
            client=sdk, model=ModelRef("test", "m"), stream=False
        )
        client = StreamFallback(adapter, clock=FakeClock(), stream=False)
        sent = []
        result = client.respond(
            ModelRequest(
                ModelRef("test", "m"),
                None,
                (text_message("user", "hi"),),
                on_attempt_started=sent.append,
            ),
            deadline=5,
        )
    assert len(requests) == 1
    assert requests[0].extensions["timeout"] == {
        "pool": 0.0,
        "connect": 5.0,
        "read": 5.0,
        "write": 5.0,
    }
    assert len(sent) == 1
    assert len(result.attempts) == 1
    assert result.response is None
