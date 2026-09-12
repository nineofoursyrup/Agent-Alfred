"""Shutdown must retain resources borrowed by real HTTP request handlers."""

import http.client
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic.test_memory_http import read
from agent_alfred.gateway.web.handler import DashboardHandler
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_dashboard


@pytest.fixture
def dashboard(tmp_path):
    runtime = build_dashboard(
        state_dir=tmp_path / "state", port=free_loopback_port(),
        factory=ScriptedModelFactory(ScriptedModel([])),
    )
    runtime.start()
    try:
        yield runtime
    finally:
        assert runtime.close()


@pytest.mark.parametrize(
    "path", ["/api/memory/records?kind=semantic", "/api/memory/mirrors"]
)
def test_close_waits_for_http_reads_before_host_and_database(
    dashboard, monkeypatch, path
):
    entered, release = threading.Event(), threading.Event()
    original = DashboardHandler._route_get

    def blocked(handler, route):
        if route == path.split("?")[0]:
            entered.set()
            assert release.wait(5)
        return original(handler, route)

    monkeypatch.setattr(DashboardHandler, "_route_get", blocked)
    # A connection accepted before shutdown may issue its next request later.
    idle = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=3)
    idle.request("GET", "/api/entry")
    entry = idle.getresponse()
    assert entry.status == 200
    entry.read()
    conn, host = dashboard._conn, dashboard.host
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(read, dashboard, path)
        try:
            assert entered.wait(3)
            assert dashboard.close(timeout=1) is False
            assert not host.closed
            assert conn.execute("SELECT 1").fetchone() == (1,)
            assert (dashboard._state_dir / "dashboard.json").is_file()
            idle.request("GET", "/api/memory/records?kind=semantic")
            refused = idle.getresponse()
            assert refused.status == 503
            assert b"shutting_down" in refused.read()
        finally:
            release.set()
            idle.close()
        assert pending.result()[0] == 200
    assert dashboard.close(timeout=1)


def test_close_retains_database_until_inflight_sse_prepare_exits(
    dashboard, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = dashboard.broker.prepare_stream

    def blocked(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(dashboard.broker, "prepare_stream", blocked)
    conn = dashboard._conn
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(read, dashboard, "/api/events")
        try:
            assert entered.wait(3)
            assert dashboard.close(timeout=1) is False
            assert conn.execute("SELECT 1").fetchone() == (1,)
            assert (dashboard._state_dir / "dashboard.json").is_file()
        finally:
            release.set()
        assert pending.result()[0] == 503
    assert dashboard.close(timeout=1)


def test_close_drains_a_live_sse_handler_after_broker_shutdown(dashboard):
    client = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=3)
    try:
        client.request("GET", "/api/events")
        response = client.getresponse()
        assert response.status == 200
        assert dashboard.close(timeout=1)
    finally:
        client.close()


@pytest.mark.parametrize("control", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("after_effect", [False, True])
def test_http_registration_control_exception_keeps_reachable_owner(
    dashboard, monkeypatch, control, after_effect
):
    context = dashboard._handler_context
    original = context.begin_request
    registrations = []
    raised = threading.Event()

    def interrupted(*args, **kwargs):
        registrations.append(args)
        if after_effect:
            original(*args, **kwargs)
        raise control("registration return edge")

    def observe(args):
        assert args.exc_type is control
        raised.set()

    monkeypatch.setattr(context, "begin_request", interrupted)
    monkeypatch.setattr(threading, "excepthook", observe)
    client = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=3)
    try:
        client.request("GET", "/api/entry")
        with pytest.raises(http.client.RemoteDisconnected):
            client.getresponse()
        assert raised.wait(3)
        assert dashboard.close(timeout=1)
    finally:
        client.close()
        # Cleanup only after the assertion: the old implementation leaked
        # a count, while identity-based release is safe to repeat.
        for args in registrations:
            if args:
                context.end_request(*args)
            elif after_effect:
                context.end_request(stream=False)


@pytest.mark.parametrize("headers", [{"Host": "attacker.invalid"},
                                     {"Origin": "https://attacker.invalid"}])
def test_shutdown_keepalive_requests_still_pass_host_and_origin_guard(
    dashboard, headers
):
    client = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=3)
    try:
        client.request("GET", "/api/entry")
        response = client.getresponse()
        assert response.status == 200
        response.read()
        assert dashboard.close(timeout=1)
        client.request("GET", "/api/memory/records?kind=semantic", headers=headers)
        refused = client.getresponse()
        assert refused.status == (400 if "Host" in headers else 403)
        assert b"shutting_down" not in refused.read()
    finally:
        client.close()
