"""The Dashboard assembled into a process: Host, broker, socket, descriptor.

Two things can only be checked here:

- the dependency loop between the Host and the broker is actually cut in the
  right direction -- the Host publishes patches into a broker that exists
  before its first transition, and the broker asks the Host the one question
  it cannot answer alone;
- a second instance on the same state directory is refused, and refusing
  leaves nothing running behind.
"""

from __future__ import annotations

import io
import json
import socket
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_web_lifecycle import _free_port
from agent_alfred.events import event_json_default
from agent_alfred.gateway.cli import serve_dashboard
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.lifecycle import (
    DESCRIPTOR_NAME,
    read_entry_descriptor,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.work import SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.wiring import build_dashboard


def _factory() -> ScriptedModelFactory:
    return ScriptedModelFactory(ScriptedModel(["pong"]))


def _database(directory: Path) -> sqlite3.Connection:
    """The production database seam, on a real file in the state directory.

    A file rather than ``:memory:`` so that "the database appeared after the
    lock was taken" is a question a test can ask of the disk.
    """
    from agent_alfred.wiring import open_database

    return open_database(directory)


def _wait_until(predicate, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def test_build_dashboard_gives_the_host_and_the_broker_one_identity(tmp_path) -> None:
    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=_factory(),
        clock=FakeClock(),
        port=_free_port(),
        open_database=_database,
    )
    try:
        descriptor = dashboard.start()
        host = dashboard.host
        # One instance id, shared by the cursor a browser sends back and by
        # the descriptor a bookmark reads.
        assert descriptor.instance_id == host.process_instance_id
        assert read_entry_descriptor(tmp_path) == descriptor
        assert dashboard.started is True
        # The Host came up inside start(), not before it: no lock, no socket
        # and no database are touched by assembly.
        assert host.started is True
    finally:
        dashboard.close()


def test_the_broker_sees_events_and_patches_from_the_real_host(tmp_path) -> None:
    seen: list = []
    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=_factory(),
        clock=FakeClock(),
        port=_free_port(),
        open_database=_database,
    )
    try:
        dashboard.start()
        host = dashboard.host
        session_id = host.create_session()
        result = host.submit(
            SubmitRequest(message="hello", session_id=session_id, gateway="web")
        )
        assert result.kind == "accepted"
        host.wait(result.run_id)
        # The broker is an event sink: the Run's events reached the ring.
        _wait_until(lambda: dashboard.broker._ring.high_water_seq() >= 2)
        # And it is the snapshot listener: the authoritative state moved
        # under it, in order.
        assert dashboard.broker._latest.state_revision >= 1
        # Session validity is answered by the Host, not guessed.
        assert host.session_exists(session_id) is True
        assert host.session_exists("never-existed") is False
        seen.append(dashboard.broker._latest.state_revision)
    finally:
        dashboard.close()
    assert seen


def test_a_second_instance_on_the_same_state_dir_is_refused(tmp_path) -> None:
    """Single-instance, proven without a second process.

    The lock is advisory and kernel-scoped, so a second attempt inside the
    same process fails for exactly the reason a second process would: the
    holder still owns the state directory.
    """
    stop = threading.Event()
    outcomes: list[int] = []
    errors = io.StringIO()

    def run() -> None:
        outcomes.append(
            serve_dashboard(
                state_dir=tmp_path,
                settings=Settings(),
                port=_free_port(),
                out=io.StringIO(),
                stop=stop,
            )
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        _wait_until(lambda: (tmp_path / DESCRIPTOR_NAME).exists())
        # A second instance, same state directory, different port: the port
        # is not the problem and changing it would not help.
        second = serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            port=_free_port(),
            out=errors,
        )
        assert second == 1
        assert "another instance holds" in errors.getvalue()
        # The first instance is untouched: still described, still bound.
        assert read_entry_descriptor(tmp_path) is not None
    finally:
        stop.set()
        thread.join(timeout=10.0)
    assert outcomes == [0]
    # Closing removed the descriptor, so the next instance can start.
    assert read_entry_descriptor(tmp_path) is None


def test_a_busy_port_fails_the_serve_path_without_leaving_a_host(tmp_path) -> None:
    squatter = socket.socket()
    port = _free_port()
    squatter.bind(("127.0.0.1", port))
    squatter.listen(1)
    errors = io.StringIO()
    try:
        assert (
            serve_dashboard(
                state_dir=tmp_path, settings=Settings(), port=port, out=errors
            )
            == 1
        )
    finally:
        squatter.close()
    assert "cannot bind" in errors.getvalue()
    # Rolled back all the way: no descriptor claiming a port nobody answers.
    assert read_entry_descriptor(tmp_path) is None


@pytest.mark.filterwarnings(
    # The dispatcher dies loudly on purpose: swallowing the exception would
    # leave a stream that says it is live while publishing nothing. The noise
    # here is the point, so it is not promoted to a test failure.
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
def test_a_dead_dispatcher_reports_only_machine_safe_context(
    tmp_path, monkeypatch
) -> None:
    """#23 §9: the dispatcher dying is a process-level fact.

    It is not a transport notice -- those describe one connection. This one
    says the stream itself stopped, so it is published like any other sink
    failure: into the trace, with a seq, visible to every consumer.
    """
    from agent_alfred.events import CapturingSink
    from agent_alfred.gateway.web.connection import FakeConnection

    fatal_secret = "credential-" + "never-persist-this"
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(SSEBroker, "name", "dashboard-stream")
    dispatch_notice_seen = threading.Event()

    class DispatchNoticeCapture(CapturingSink):
        def commit(self, prepared, event) -> None:
            super().commit(prepared, event)
            if (
                getattr(event.payload, "code", None) == "sink_disabled"
                and dict(event.payload.detail).get("stage") == "dispatch"
            ):
                dispatch_notice_seen.set()

    capture = DispatchNoticeCapture(name="capture", flush_at_run_end=True)
    trace_root = tmp_path / "traces"
    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=_factory(),
        clock=FakeClock(),
        port=_free_port(),
        extra_sinks=[capture],
        trace_root=trace_root,
        open_database=_database,
    )
    try:
        dashboard.start()
        host = dashboard.host

        class Exploding:
            def offer(self, item):
                raise RuntimeError(fatal_secret)

            def request_close(self, **kwargs):
                return None

            def stop(self):
                return None

        handle = dashboard.broker.connect(connection=FakeConnection())
        handle.queue = Exploding()
        submitted = host.submit(
            SubmitRequest(message="hello", session_id=host.create_session())
        )
        assert submitted.kind == "accepted"

        assert dispatch_notice_seen.wait(5.0), (
            "dispatcher notice was not published"
        )
        host.wait(submitted.run_id)

        event_document = json.dumps(capture.events, default=event_json_default)
        trace_documents = [
            path.read_text(encoding="utf-8")
            for path in trace_root.rglob("*")
            if path.is_file()
        ]
        assert trace_documents, "the Run must have produced a real trace bundle"
        for document in [event_document, *trace_documents]:
            if fatal_secret in document:
                raise AssertionError(
                    "fatal exception text leaked into a domain event or trace"
                )

        dispatcher_notices = [
            event
            for event in capture.events
            if getattr(event.payload, "code", None) == "sink_disabled"
            and dict(event.payload.detail).get("stage") == "dispatch"
        ]
        assert len(dispatcher_notices) == 1, (
            "one dispatcher death publishes one dispatcher notice"
        )
        assert dict(dispatcher_notices[0].payload.detail) == {
            "sink": "dashboard-stream",
            "stage": "dispatch",
        }
        # The broker stopped taking work instead of limping on behind a
        # dispatcher that no longer exists.
        assert dashboard.broker._stopping is True  # noqa: SLF001
    finally:
        dashboard.close()


def test_the_assembled_dashboard_serves_the_real_host_over_http(tmp_path) -> None:
    """The production assembly answers from the Host it was built on.

    The API is constructed on the Host itself at the assembly point, so the
    one round trip worth proving across the real socket is that a write the
    wire accepted exists in the database the Host owns -- and that a read
    the wire serves answers from the same store.
    """
    from agent_alfred.gateway.web.guard import CSRF_HEADER

    dashboard = build_dashboard(
        state_dir=tmp_path,
        factory=_factory(),
        clock=FakeClock(),
        port=_free_port(),
        open_database=_database,
    )
    try:
        dashboard.start()
        port = dashboard.port

        def request(path: str, *, data: bytes | None = None) -> tuple[int, bytes]:
            from urllib.request import Request, urlopen

            headers = {"Content-Type": "application/json"}
            if data is not None:
                headers[CSRF_HEADER] = dashboard.csrf_token
            call = Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                headers=headers,
                method="POST" if data is not None else "GET",
            )
            with urlopen(call, timeout=3.0) as response:
                return response.status, response.read()

        status, body = request("/api/sessions", data=b"{}")
        assert status == 201
        session_id = json.loads(body)["session_id"]
        assert session_id
        # The write the wire accepted is a row in the Host's database.
        assert dashboard.host.session_exists(session_id) is True

        # And the served read answers from that same store.
        status, body = request(f"/api/sessions/messages?session_id={session_id}")
        assert status == 200
        assert json.loads(body)["session_id"] == session_id
    finally:
        dashboard.close()
