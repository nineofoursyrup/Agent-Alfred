"""Bounded, resumable shutdown of Dashboard HTTP serving."""

from __future__ import annotations

import socketserver
import threading

from agent_alfred.evals.deterministic._web_close_test_helpers import (
    CloseTrackingConnection,
    CloseTrackingHost,
    DashboardCloseRig,
    RecordingServer,
)
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.gateway.web.lifecycle import read_entry_descriptor
from agent_alfred.gateway.web.server import DashboardRuntime


class _ShutdownLatchServer(RecordingServer):
    """A server whose shutdown request stays in flight until released."""

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.shutdown_entered = threading.Event()
        self.release_shutdown = threading.Event()
        self.shutdown_calls = 0
        self.close_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.shutdown_entered.set()
        self.release_shutdown.wait()
        super().shutdown()

    def server_close(self) -> None:
        self.close_calls += 1
        super().server_close()


class _ShutdownLatchRig(DashboardCloseRig):
    server: _ShutdownLatchServer | None = None

    def _server_factory(self, address, handler):
        self.binds += 1
        self.trace.append("bind")
        self.server = _ShutdownLatchServer(address, handler)
        return self.server


class _CountingServer(RecordingServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.shutdown_calls = 0
        self.close_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        super().shutdown()

    def server_close(self) -> None:
        self.close_calls += 1
        super().server_close()


class _ServingThreadLatch:
    """A serving-thread seam that exits only when the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread: threading.Thread | None = None

    def __call__(self, target):
        del target

        def serve() -> None:
            self.entered.set()
            self.release.wait()

        self.thread = threading.Thread(target=serve, daemon=True)
        return self.thread


class _ServingThreadLatchRig(DashboardCloseRig):
    server: _CountingServer | None = None

    def __init__(self, tmp_path):
        self.serving = _ServingThreadLatch()
        super().__init__(tmp_path, spawn=self.serving)

    def _server_factory(self, address, handler):
        self.binds += 1
        self.trace.append("bind")
        self.server = _CountingServer(address, handler)
        return self.server


class _ServerCloseLatchServer(_CountingServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.close_entered = threading.Event()
        self.release_close = threading.Event()

    def server_close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        self.release_close.wait()
        RecordingServer.server_close(self)


class _ServerCloseLatchRig(DashboardCloseRig):
    server: _ServerCloseLatchServer | None = None

    def _server_factory(self, address, handler):
        self.binds += 1
        self.trace.append("bind")
        self.server = _ServerCloseLatchServer(address, handler)
        return self.server


class _ClosingBroker:
    def __init__(self) -> None:
        self.close_calls = 0

    def start(self) -> None:
        return None

    def close(self, timeout: float | None = None) -> bool:
        del timeout
        self.close_calls += 1
        return True


def test_runtime_close_timeout_bounds_an_inflight_http_shutdown(tmp_path) -> None:
    """An in-flight shutdown owns the serving step without owning the tail."""
    rig = _ShutdownLatchRig(tmp_path)
    rig.runtime.start()
    assert rig.server is not None
    result: list[bool] = []
    returned = threading.Event()

    def close_runtime() -> None:
        result.append(rig.runtime.close(timeout=0))
        returned.set()

    caller = threading.Thread(target=close_runtime)
    caller.start()
    assert rig.server.shutdown_entered.wait(1.0)
    try:
        assert returned.wait(0.25), "close(timeout=0) blocked in server.shutdown()"
        assert result == [False]
        assert rig.server.shutdown_calls == 1
        assert rig.server.close_calls == 0
        assert rig.runtime.service.server is rig.server
        assert rig.host.close_calls == 0
        assert rig.broker.close_calls == 0
        assert rig.conn.close_calls == 0
        assert read_entry_descriptor(tmp_path) is not None
        assert rig.lock_is_held() is True
    finally:
        rig.server.release_shutdown.set()
        caller.join(timeout=1.0)

    assert rig.runtime.close(timeout=1.0) is True
    assert rig.server.shutdown_calls == 1, "the in-flight shutdown is not restarted"
    assert rig.server.close_calls == 1
    assert rig.host.close_calls == 1
    assert rig.broker.close_calls == 1
    assert rig.conn.close_calls == 1
    assert read_entry_descriptor(tmp_path) is None
    assert rig.lock.acquired is False
    assert rig.runtime.close(timeout=0) is True
    assert rig.server.close_calls == 1


def test_runtime_close_timeout_bounds_serving_thread_confirmation(tmp_path) -> None:
    """A live serving thread keeps the serving step and the whole tail pending."""
    rig = _ServingThreadLatchRig(tmp_path)
    rig.runtime.start()
    assert rig.server is not None
    assert rig.serving.entered.wait(1.0)
    result: list[bool] = []
    returned = threading.Event()

    def close_runtime() -> None:
        result.append(rig.runtime.close(timeout=0))
        returned.set()

    caller = threading.Thread(target=close_runtime)
    caller.start()
    try:
        assert returned.wait(0.25), "close(timeout=0) blocked joining serving thread"
        assert result == [False]
        assert rig.server.shutdown_calls == 1
        assert rig.server.close_calls == 1
        assert rig.runtime.service.server is rig.server
        assert rig.serving.thread is not None and rig.serving.thread.is_alive()
        assert rig.host.close_calls == 0
        assert rig.broker.close_calls == 0
        assert rig.conn.close_calls == 0
        assert read_entry_descriptor(tmp_path) is not None
        assert rig.lock_is_held() is True
    finally:
        rig.serving.release.set()
        caller.join(timeout=1.0)

    assert rig.runtime.close(timeout=1.0) is True
    assert rig.server.shutdown_calls == 1
    assert rig.server.close_calls == 1
    assert rig.host.close_calls == 1
    assert rig.broker.close_calls == 1
    assert rig.conn.close_calls == 1
    assert read_entry_descriptor(tmp_path) is None
    assert rig.lock.acquired is False


def test_runtime_close_timeout_bounds_an_inflight_server_close(tmp_path) -> None:
    """A closing socket keeps thread confirmation and the whole tail pending."""
    rig = _ServerCloseLatchRig(tmp_path)
    rig.runtime.start()
    assert rig.server is not None
    result: list[bool] = []
    returned = threading.Event()

    def close_runtime() -> None:
        result.append(rig.runtime.close(timeout=0))
        returned.set()

    caller = threading.Thread(target=close_runtime)
    caller.start()
    assert rig.server.close_entered.wait(1.0)
    try:
        assert returned.wait(0.25), "close(timeout=0) blocked in server_close()"
        assert result == [False]
        assert rig.server.shutdown_calls == 1
        assert rig.server.close_calls == 1
        assert rig.runtime.service.server is rig.server
        assert rig.host.close_calls == 0
        assert rig.broker.close_calls == 0
        assert rig.conn.close_calls == 0
        assert read_entry_descriptor(tmp_path) is not None
        assert rig.lock_is_held() is True
    finally:
        rig.server.release_close.set()
        caller.join(timeout=1.0)

    assert rig.runtime.close(timeout=1.0) is True
    assert rig.server.shutdown_calls == 1
    assert rig.server.close_calls == 1, "the in-flight server_close is not restarted"
    assert rig.host.close_calls == 1
    assert rig.broker.close_calls == 1
    assert rig.conn.close_calls == 1
    assert read_entry_descriptor(tmp_path) is None
    assert rig.lock.acquired is False


def test_real_stdlib_poll_does_not_hold_runtime_close_past_timeout(
    tmp_path, monkeypatch
) -> None:
    """The stdlib's 0.5s serve_forever poll is outside close(timeout=0)."""
    poll_entered = threading.Event()
    selector_type = socketserver._ServerSelector

    class _ObservedSelector(selector_type):
        def select(self, timeout=None):
            poll_entered.set()
            return super().select(timeout)

    monkeypatch.setattr(socketserver, "_ServerSelector", _ObservedSelector)
    trace: list[str] = []
    conn = CloseTrackingConnection(trace)
    host: CloseTrackingHost | None = None
    broker: _ClosingBroker | None = None

    def assemble(open_conn, instance_id):
        nonlocal host, broker
        del instance_id
        host = CloseTrackingHost(open_conn, [True], trace)
        broker = _ClosingBroker()
        return host, broker

    runtime = DashboardRuntime(
        state_dir=tmp_path,
        assemble=assemble,
        port=free_loopback_port(),
        open_database=lambda _directory: conn,
    )
    runtime.start()
    assert poll_entered.wait(1.0), "real serve_forever never entered its poll"
    result: list[bool] = []
    returned = threading.Event()

    def close_runtime() -> None:
        result.append(runtime.close(timeout=0))
        returned.set()

    caller = threading.Thread(target=close_runtime)
    caller.start()
    try:
        assert returned.wait(0.25), "close(timeout=0) waited for the 0.5s poll"
        assert result == [False]
        assert host is not None and host.close_calls == 0
        assert broker is not None and broker.close_calls == 0
        assert conn.close_calls == 0
        assert read_entry_descriptor(tmp_path) is not None
        assert runtime.service.lock_held is True
    finally:
        caller.join(timeout=1.0)

    assert runtime.close(timeout=1.0) is True
    assert host is not None and host.close_calls == 1
    assert broker is not None and broker.close_calls == 1
    assert conn.close_calls == 1
    assert read_entry_descriptor(tmp_path) is None
    assert runtime.service.lock_held is False
