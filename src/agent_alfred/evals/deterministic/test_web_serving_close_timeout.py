"""Bounded, resumable shutdown of Dashboard HTTP serving."""

from __future__ import annotations

import socketserver
import threading

import pytest

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
from agent_alfred.gateway.web.server import ROLLBACK_STEP_TIMEOUT_S, DashboardRuntime


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


class _ServerRig(DashboardCloseRig):
    """Publish and record whichever controlled server a lifecycle test needs."""

    server_type: type[RecordingServer]

    def _server_factory(self, address, handler, owner):
        self.binds += 1
        self.trace.append("bind")
        self.server = self.server_type(address, handler)
        owner.publish(self.server)


class _ShutdownLatchRig(_ServerRig):
    server_type = _ShutdownLatchServer
    server: _ShutdownLatchServer | None = None


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


class _ServingThreadLatchRig(_ServerRig):
    server_type = _CountingServer
    server: _CountingServer | None = None

    def __init__(self, tmp_path):
        self.serving = _ServingThreadLatch()
        super().__init__(tmp_path, spawn=self.serving)


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


class _ServerCloseLatchRig(_ServerRig):
    server_type = _ServerCloseLatchServer
    server: _ServerCloseLatchServer | None = None


class _ClosingBroker:
    def __init__(self) -> None:
        self.close_calls = 0

    def start(self) -> None:
        return None

    def close(self, timeout: float | None = None) -> bool:
        del timeout
        self.close_calls += 1
        return True


@pytest.mark.parametrize("use_default_budget", [False, True])
@pytest.mark.parametrize("failure_stage", ["descriptor", "binding"])
def test_entry_start_failure_bounds_an_inflight_server_close(
    tmp_path, use_default_budget: bool, failure_stage: str,
) -> None:
    """Entry rollback reports failure while retaining a blocked socket close."""
    start_error = RuntimeError(f"injected {failure_stage} failure")
    failure_attempted = threading.Event()

    class EntryFailureRig(_ServerCloseLatchRig):
        def _server_factory(self, address, handler, owner):
            super()._server_factory(address, handler, owner)
            if failure_stage == "binding":
                failure_attempted.set()
                raise start_error

        def _write_descriptor(self, directory, descriptor):
            failure_attempted.set()
            raise start_error

    rig = (
        EntryFailureRig(tmp_path)
        if use_default_budget
        else EntryFailureRig(tmp_path, rollback_step_timeout=0.0)
    )
    failures: list[BaseException] = []
    returned = threading.Event()

    def start_runtime() -> None:
        try:
            rig.runtime.start()
        except BaseException as exc:
            failures.append(exc)
        finally:
            returned.set()

    caller = threading.Thread(target=start_runtime)
    caller.start()
    try:
        assert failure_attempted.wait(1.0), "entry failure was not reached"
        assert rig.server is not None
        assert rig.server.close_entered.wait(1.0), "socket close was not reached"
        watchdog = 2 * ROLLBACK_STEP_TIMEOUT_S + 1.0 if use_default_budget else 1.0
        assert returned.wait(watchdog), "entry rollback blocked in server_close()"
        assert failures == [start_error]
        assert rig.runtime.state == "closing"
        assert rig.runtime.service.server is rig.server
        assert rig.server.close_calls == 1
        assert rig.server.closed is False
        assert rig.lock_is_held() is True
        assert rig.database_opens == 0
        assert rig.assemblies == 0
        assert read_entry_descriptor(tmp_path) is None
    finally:
        if rig.server is not None:
            rig.server.release_close.set()
        caller.join(timeout=1.0)
        assert not caller.is_alive(), "test retained its startup caller"
        assert rig.runtime.close(timeout=1.0) is True

    assert rig.server.close_calls == 1, "the in-flight socket close is not restarted"
    assert rig.server.closed is True
    assert rig.lock.acquired is False
    assert rig.runtime.state == "closed"
    assert rig.runtime.close(timeout=0) is True
    assert rig.server.close_calls == 1


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
    try:
        # Settle shutdown and socket close before testing only the still-live
        # serving thread's zero-budget confirmation.
        assert rig.runtime.close(timeout=1.0) is False
        caller.start()
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
        if caller.ident is not None:
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
    try:
        # Advance past shutdown and start the controlled socket close before
        # checking that a zero-budget retry does not wait for it.
        assert rig.runtime.close(timeout=1.0) is False
        assert rig.server.close_entered.wait(1.0)
        caller.start()
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
        if caller.ident is not None:
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
    release_poll = threading.Event()
    selector_type = socketserver._ServerSelector

    class _ObservedSelector(selector_type):
        def select(self, timeout=None):
            poll_entered.set()
            ready = super().select(timeout)
            # Retain the observed poll even if its OS wait finishes before
            # the zero-budget caller gets scheduled.
            release_poll.wait()
            return ready

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

    def open_database(_directory, *, _rollback):
        _rollback.own(conn)
        return conn

    runtime = DashboardRuntime(
        state_dir=tmp_path,
        assemble=assemble,
        port=free_loopback_port(),
        open_database=open_database,
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
        release_poll.set()
        caller.join(timeout=1.0)

    assert runtime.close(timeout=1.0) is True
    assert host is not None and host.close_calls == 1
    assert broker is not None and broker.close_calls == 1
    assert conn.close_calls == 1
    assert read_entry_descriptor(tmp_path) is None
    assert runtime.service.lock_held is False
