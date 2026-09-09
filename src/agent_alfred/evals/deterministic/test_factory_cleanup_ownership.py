"""Factory cache eviction retains failed cleanup until an explicit retry succeeds."""

import gc
import sqlite3
import weakref
from contextlib import ExitStack
from dataclasses import replace

import httpx2
import pytest

from agent_alfred import attempt_io, schema
from agent_alfred.bounded_connect import BoundedConnector
from agent_alfred.clock import FakeClock
from agent_alfred.endpoint_factory import EndpointClientFactory
from agent_alfred.endpoints import ModelEndpoint, ModelRoute
from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
from agent_alfred.model import ModelRef, ModelRequest


@pytest.fixture(
    params=[
        ("openai", OSError),
        ("anthropic", OSError),
        ("openai", KeyboardInterrupt),
        ("anthropic", KeyboardInterrupt),
    ]
)
def failed_factory(monkeypatch, request):
    style, cleanup_error = request.param
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
            if len(closes) == 1:
                raise OSError("close failed")
            if len(closes) == 2:
                raise cleanup_error("close failed")

    monkeypatch.setattr(
        attempt_io, "BoundedConnector", lambda: BoundedConnector(socket_factory=Socket)
    )
    factory = EndpointClientFactory(
        clock=FakeClock(),
        endpoints=(
            ModelEndpoint(
                "opencode-go",
                "http://127.0.0.1:1",
                "UNUSED",
                {
                    "m": ModelRoute(
                        style, "/chat/completions" if style == "openai" else "/messages"
                    )
                },
            ),
        ),
    )
    config = replace(
        snapshot(style=style, endpoint="opencode-go", model="m"), stream=False
    )
    client = factory.create(config)
    result = client.respond(
        ModelRequest(ModelRef("opencode-go", "m"), None, ()), deadline=5
    )
    assert result.attempts[0].outcome == "aborted"
    del result, client
    gc.collect()
    assert len(closes) == 1 and refs[0]() is not None
    yield factory, config, refs, closes, cleanup_error
    factory.invalidate_all()


@pytest.mark.parametrize(
    "operation",
    ["all", "endpoint", "version", "close", "host_close", "dotenv", "settings"],
)
def test_factory_invalidation_keeps_cleanup_reachable(
    failed_factory, operation, tmp_path
):
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.events import FanOutSink
    from agent_alfred.runtime.host import RuntimeHost
    from agent_alfred.runtime.model_settings import ModelSettingsStore
    from agent_alfred.settings import Settings

    factory, config, refs, closes, cleanup_error = failed_factory
    route_ref = weakref.ref(factory.transport_pool.client_for(config))
    with ExitStack() as resources:
        if operation in ("host_close", "dotenv", "settings"):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            resources.callback(conn.close)
            schema.migrate(conn)
            store = ModelSettingsStore(tmp_path / "models.json")
            store.load()
            env_file = tmp_path / ".env"
            env_file.write_text("UNUSED=synthetic-key\n")
            host = RuntimeHost(
                conn=conn,
                factory=factory,
                clock=FakeClock(),
                settings=Settings(),
                fanout=FanOutSink([], process_instance_id="cache"),
                process_instance_id="cache",
                model_settings=store,
                credentials=CredentialOverlay(
                    process_env={}, dotenv_path=str(env_file)
                ),
            )
            host.start()
            resources.callback(host.close)
            if operation == "host_close":
                invalidate = host.close
            elif operation == "dotenv":
                invalidate = host.reread_dotenv
            else:

                def invalidate():
                    assert host.try_begin_mutation() is None
                    try:
                        return host.mutate_model_settings(
                            store.snapshot().revision, lambda s: s
                        )
                    finally:
                        host.end_mutation()
        elif operation == "version":

            def invalidate():
                return factory.create(replace(config, config_version="next"))
        elif operation == "endpoint":

            def invalidate():
                factory.invalidate_endpoint(config.endpoint_id)
        elif operation == "close":
            invalidate = factory.close
        else:
            invalidate = factory.invalidate_all
        # The caller may discard the exception; the factory still owns retry.
        with pytest.raises(cleanup_error, match="close failed"):
            invalidate()
        gc.collect()
        assert len(closes) == 2
        assert refs[0]() is not None, (
            "cache eviction discarded the pending cleanup owner"
        )
        invalidate()
        gc.collect()
        assert len(closes) == 3
        assert refs[0]() is None, "successful cleanup must release its resource"
        invalidate()
        assert len(closes) == 3
        gc.collect()
        assert route_ref() is None, "retired clients must be released after cleanup"


@pytest.mark.parametrize("style", ["openai", "anthropic"])
def test_injected_http_client_remains_caller_owned(style):
    closes = []

    class Transport(httpx2.MockTransport):
        def close(self):
            closes.append(True)

    http = httpx2.Client(transport=Transport(lambda r: httpx2.Response(200, json={})))
    factory = EndpointClientFactory(clock=FakeClock(), http_client=http)
    config = snapshot(style=style)
    for invalidate in (
        factory.invalidate_all,
        lambda: factory.invalidate_endpoint(config.endpoint_id),
    ):
        client = factory.create(config)
        route_ref = weakref.ref(factory.transport_pool.client_for(config))
        del client
        invalidate()
        gc.collect()
        assert route_ref() is None
        assert not http.is_closed and closes == []
        assert http.get("http://fixture.invalid").status_code == 200
    factory.create(config)
    factory.create(replace(config, config_version="next"))
    factory.close()
    gc.collect()
    assert not http.is_closed and closes == []
    http.close()
    assert closes == [True]
    factory.close()
    assert closes == [True]
    with pytest.raises(RuntimeError, match="closed"):
        factory.create(config)


@pytest.mark.parametrize("operation", ["all", "endpoint", "version", "close"])
def test_completed_connection_keeps_owner_after_pool_removes_it(monkeypatch, operation):
    import json
    import os

    refs, closes = [], []
    body = json.dumps(
        {
            "id": "answer",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
    ).encode()
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    read_fd, write_fd = os.pipe()

    class Socket:
        def __init__(self, *args):
            self.response = response
            refs.append(weakref.ref(self))

        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            pass

        def send(self, value):
            return len(value)

        def recv(self, count):
            result, self.response = self.response[:count], self.response[count:]
            return result

        def fileno(self):
            return read_fd

        def close(self):
            closes.append(True)
            if len(closes) <= 2:
                raise OSError("close failed")

    monkeypatch.setattr(
        attempt_io, "BoundedConnector", lambda: BoundedConnector(socket_factory=Socket)
    )
    factory = EndpointClientFactory(
        clock=FakeClock(),
        endpoints=(
            ModelEndpoint(
                "fixture",
                "http://127.0.0.1:1",
                "UNUSED",
                {"m": ModelRoute("openai", "/chat/completions")},
            ),
        ),
    )
    config = replace(snapshot(endpoint="fixture", model="m"), stream=False)
    try:
        client = factory.create(config)
        result = client.respond(
            ModelRequest(ModelRef("fixture", "m"), None, ()), deadline=5
        )
        assert result.response is not None
        del result, client
        if operation == "version":

            def invalidate():
                return factory.create(replace(config, config_version="next"))
        elif operation == "endpoint":

            def invalidate():
                factory.invalidate_endpoint("fixture")
        else:
            invalidate = (
                factory.close if operation == "close" else factory.invalidate_all
            )
        for count in (1, 2):
            with pytest.raises(OSError, match="close failed"):
                invalidate()
            gc.collect()
            assert len(closes) == count
            assert refs[0]() is not None
        invalidate()
        gc.collect()
        assert len(closes) == 3 and refs[0]() is None
        invalidate()
        assert len(closes) == 3
    finally:
        factory.close()
        os.close(read_fd)
        os.close(write_fd)
