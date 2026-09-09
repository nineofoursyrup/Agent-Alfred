"""Failed connection cleanup stays owned across real SDK exception conversion."""

import gc
import weakref

import httpx2
import pytest
from anthropic import Anthropic
from openai import OpenAI

from agent_alfred.anthropic_native import AnthropicAdapter
from agent_alfred.attempt_io import ModelHTTPTransport
from agent_alfred.bounded_connect import BoundedConnector
from agent_alfred.clock import FakeClock
from agent_alfred.messages import text_message
from agent_alfred.model import ModelRef, ModelRequest
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.resource_rollback import IncompleteRollback
from agent_alfred.stream_fallback import StreamFallback


@pytest.mark.parametrize("style", ["openai", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("close_failures", [1, 2])
def test_client_retains_failed_socket_cleanup_after_adapter_error_conversion(
    style, stream, close_failures
):
    refs, closes = [], []

    class Socket:
        def __init__(self, *args):
            refs.append(weakref.ref(self))

        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            raise OSError("connection failed")

        def close(self):
            closes.append(True)
            if len(closes) <= close_failures:
                raise OSError("close failed")

    inner_closes = []

    class HTTPTransport(httpx2.HTTPTransport):
        def close(self):
            inner_closes.append(True)
            super().close()

    transport = ModelHTTPTransport(HTTPTransport())
    # The user-provided network connector is the system boundary under test.
    transport.inner._pool._network_backend._connector = BoundedConnector(
        socket_factory=Socket
    )
    http = httpx2.Client(transport=transport, trust_env=False)
    sdk = (OpenAI if style == "openai" else Anthropic)(
        api_key="fixture",
        base_url="http://127.0.0.1:1",
        http_client=http,
        max_retries=0,
    )
    adapter = (OpenAICompatibleAdapter if style == "openai" else AnthropicAdapter)(
        client=sdk, model=ModelRef("fixture", "m"), stream=stream
    )
    request = ModelRequest(
        ModelRef("fixture", "m"), None, (text_message("user", "hello"),)
    )
    result = StreamFallback(
        adapter, clock=FakeClock(), stream=stream, stream_fallback=False
    ).respond(request, deadline=5)
    assert len(result.attempts) == 1
    assert result.attempts[0].outcome == "aborted"
    del result
    gc.collect()
    try:
        assert closes == [True]
        assert refs[0]() is not None, (
            "pending cleanup owner was discarded by SDK/Adapter"
        )
        if close_failures == 1:
            http.close()
        else:
            with pytest.raises(OSError) as failed_close:
                http.close()
            pending = failed_close.value.__cause__
            assert isinstance(pending, IncompleteRollback)
            gc.collect()
            assert refs[0]() is not None
            assert pending.retry()
            assert pending.retry()
        assert closes == [True] * (close_failures + 1)
        assert inner_closes == [True]
        http.close()
        transport.close()
        assert closes == [True] * (close_failures + 1)
        assert inner_closes == [True], "successful cleanup must not run again"
    finally:
        http.close()
