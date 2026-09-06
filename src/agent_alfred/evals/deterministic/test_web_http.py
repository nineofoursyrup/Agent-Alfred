"""One real bind on a random loopback port, and the bytes that come back.

Everything else in the web layer is tested against fakes, which is the right
place for decisions. This file exists for the things a fake cannot prove:

- the bytes we write are the bytes a browser parses -- stdlib tests
  ``http.server``, not our frames;
- ``Last-Event-ID`` comes back on the next connection, which is a loop
  through a browser we do not have;
- the defences are visible on the wire, including the one that is defined by
  a header's *absence*.

Exactly one test binds; the rest share that server through a fixture, because
a port is the one thing here that cannot be faked and every extra bind is
another chance to collide.
"""

from __future__ import annotations

import dis
import json
import select
import socket
import sqlite3
import struct
import sys
import time
from dataclasses import replace
from io import BytesIO
from typing import Any
from urllib.parse import quote, urlencode

import pytest

from agent_alfred import schema
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
)
from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
    FailFinalizeWhen,
    build_runtime_host,
)
from agent_alfred.events import EventEnvelope, FanOutSink, RunStarted, SequencedEvent
from agent_alfred.gateway.web import broker as broker_module
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web import guard as guard_module
from agent_alfred.gateway.web import lifecycle as lifecycle_module
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.guard import CSRF_HEADER, RequestGuard
from agent_alfred.gateway.web.handler import DashboardHandler, HandlerContext
from agent_alfred.gateway.web.lifecycle import DEFAULT_HOST, DashboardService
from agent_alfred.gateway.web.replay import ReplayRing
from agent_alfred.runtime.cursor import MalformedCursor
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    CoordinatorState,
    RuntimeSnapshot,
)
from agent_alfred.runtime.work import SubmitResult

INSTANCE = "inst-http"
_TIMEOUT = 3.0


@pytest.mark.parametrize(
    "session_id,run_id", [("", ""), ("session/a?b#c%+中文", "run/a?b#c%+中文")],
)
def test_reply_recovery_wire_preserves_opaque_identity(server, session_id, run_id):
    from agent_alfred.runtime.work import SubmitRequest

    host, conn = build_runtime_host()
    host.start()
    server.reset(facade=host)
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        # Historic IDs are arbitrary TEXT. Seed their original values directly.
        conn.execute("UPDATE sessions SET session_id = ?", (session_id,))
        conn.execute("UPDATE runs SET session_id = ?, run_id = ?", (session_id, run_id))
        conn.execute(
            "UPDATE agent_log SET session_id = ?, run_id = ?", (session_id, run_id),
        )
        conn.commit()
        identity = {
            "process_instance_id": host.process_instance_id,
            "session_id": session_id, "run_id": run_id,
        }
        head, body = _request(
            server.port, _get(server.port, "/api/reply?" + urlencode(identity)),
        )
        assert head.startswith(b"HTTP/1.1 200")
        assert json.loads(body) == {**identity, "reply_text": "pong"}
    finally:
        host.close()
        conn.close()


def test_reply_recovery_wire_returns_a_whole_body_above_the_sse_frame_limit(server):
    text = "文" * 400_000 + " end"
    host, conn = build_runtime_host([text])
    host.start()
    server.reset(facade=host)
    try:
        from agent_alfred.runtime.work import SubmitRequest

        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        identity = {
            "process_instance_id": host.process_instance_id,
            "session_id": submitted.session_id, "run_id": submitted.run_id,
        }
        head, body = _request(
            server.port, _get(server.port, "/api/reply?" + urlencode(identity)),
        )
        assert head.startswith(b"HTTP/1.1 200")
        assert json.loads(body) == {**identity, "reply_text": text}
        assert b"Cache-Control: no-store" in head
        _assert_no_cross_origin_permission(head)
    finally:
        host.close()
        conn.close()


def _snapshot(
    *,
    coordinator_state: CoordinatorState = "idle",
    active_run: ActiveRunSummary | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        process_instance_id=INSTANCE,
        state_revision=0,
        coordinator_state=coordinator_state,
        active_run=active_run,
        unrecorded_terminal_projection=None,
    )


def _active(**kwargs: Any) -> ActiveRunSummary:
    values: dict[str, Any] = {
        "run_id": "run-already-active",
        "purpose": "chat",
        "gateway": "web",
        "phase": "running",
        "session_id": "session-from-server",
        "prompt_preview": "earlier prompt",
        "started_at": "2026-08-31T08:00:00Z",
        "recording_state": None,
        "current_step": 3,
    }
    values.update(kwargs)
    return ActiveRunSummary(**values)


class _Facade:
    """The smallest facade that lets the wire be the thing under test."""

    def __init__(self) -> None:
        self.mutating = False
        self.mutation_refusal: str | None = None
        self.created_sessions: list[str] = []
        self.state = _snapshot()
        self.submit_result = SubmitResult(
            kind="accepted",
            run_id="run-on-wire",
            session_id="session-from-server",
        )
        # Every Run the API asked this facade to admit, so a wire test can
        # prove a refusal happened before anything was asked at all.
        self.submitted: list[Any] = []
        # The Session ids the MainBar read was aimed at, so a wire test can
        # see the query parameter arrived verbatim.
        self.mainbar_sessions: list[str] = []
        # The same record for the Session group's run list.
        self.session_run_sessions: list[str] = []
        # The normalized page size observed by each real HTTP read route.
        self.read_limits: list[tuple[str, int]] = []

    # The gate's authority, in the shape the Host provides it. No Run is
    # ever in flight behind this facade, so the only thing that can busy the
    # gate here is another write -- which these tests never start.
    def try_begin_mutation(self) -> str | None:
        if self.mutation_refusal is not None:
            return self.mutation_refusal
        if self.mutating:
            return "mutation_in_flight"
        self.mutating = True
        return None

    def end_mutation(self) -> None:
        self.mutating = False

    def mutation_in_flight(self) -> bool:
        return self.mutating

    def admission_observe(self):
        if self.mutating:
            return "mutation_in_flight", self.state
        if self.submit_result.kind == "accepted":
            return "admissible", self.state
        return self.submit_result.kind, self.submit_result.snapshot or self.state

    def create_session(self) -> str:
        self.created_sessions.append("session-from-server")
        return "session-from-server"

    def submit(self, request: Any) -> Any:
        self.submitted.append(request)
        return self.submit_result

    def session_exists(self, session_id: str) -> bool:
        return True

    def snapshot(self) -> RuntimeSnapshot:
        return self.state

    def list_sessions(self, *, limit: int, cursor: str | None = None):
        from agent_alfred.runtime.sessions import SessionInboxPage

        self.read_limits.append(("sessions", limit))
        return SessionInboxPage(sessions=(), next_cursor=None)

    def open_session(
        self, session_id: str, *, page_size: int, cursor: str | None = None
    ):
        from agent_alfred.runtime.sessions import SessionMessagesPage

        self.read_limits.append(("messages", page_size))
        return SessionMessagesPage(
            session_id=session_id, title="t", messages=(), next_cursor=None
        )

    def list_runs(self, *, filter: str, limit: int, cursor: str | None = None):
        from agent_alfred.runtime.runs import RunPage

        self.read_limits.append(("runs", limit))
        return RunPage(filter=filter, runs=(), non_terminal=None, next_cursor=None)

    def locate_run(self, run_id: str, *, limit: int):
        return None

    def mainbar_pairs(self, *, session_id: str, limit: int, cursor: str | None):
        self.mainbar_sessions.append(session_id)
        self.read_limits.append(("mainbar", limit))
        from agent_alfred.runtime.runs import MainBarPage

        return MainBarPage(items=(), next_cursor=None)

    def list_session_chat_runs(
        self, *, session_id: str, limit: int, cursor: str | None
    ):
        self.session_run_sessions.append(session_id)
        self.read_limits.append(("session-runs", limit))
        from agent_alfred.runtime.runs import SessionChatRunsPage

        return SessionChatRunsPage(session_id=session_id, runs=(), next_cursor=None)


class _Server:
    def __init__(self, tmp_path, *, connection_budget=None, facade=None):
        self.bind_calls = 0
        self.service = DashboardService(
            state_dir=tmp_path,
            handler=DashboardHandler,
            instance_id=INSTANCE,
            port=lifecycle_module.DEFAULT_PORT,
            server_factory=self._bind_ephemeral_loopback,
        )
        self.service.start()
        self.port = self.service.port
        self.guard = RequestGuard(port=self.port, csrf_token="token-from-process")
        self.broker = self._new_broker(connection_budget)
        self.fanout = FanOutSink([self.broker], process_instance_id=INSTANCE)
        self.facade = _Facade() if facade is None else facade
        self.service.attach_context(self._context())
        self.thread = self.service.start_serving()

    def _bind_ephemeral_loopback(self, address, handler, owner):
        """Make the module's sole bind choose an available kernel port."""
        assert address[0] == DEFAULT_HOST
        self.bind_calls += 1
        lifecycle_module._default_server_factory(  # noqa: SLF001
            (DEFAULT_HOST, 0), handler, owner
        )

    @staticmethod
    def _new_broker(connection_budget=None) -> SSEBroker:
        broker = SSEBroker(
            process_instance_id=INSTANCE,
            snapshot=_snapshot(),
            session_is_valid=lambda _session_id: "valid",
            ring=ReplayRing(),
            **(
                {}
                if connection_budget is None
                else {"connection_budget": connection_budget}
            ),
        )
        # Start dispatch before publishing this broker into the shared server
        # context, so every request observes a broker ready for live events.
        broker.start()
        return broker

    def _context(self) -> HandlerContext:
        return HandlerContext(
            guard=self.guard,
            api=DashboardApi(facade=self.facade),
            broker=self.broker,
            instance_id=INSTANCE,
        )

    def reset(self, *, connection_budget=None, facade=None) -> None:
        """Replace test state without rebinding the module's loopback socket."""
        if not self.broker.close(timeout=2.0):
            raise RuntimeError("previous test broker did not close")
        self.broker = self._new_broker(connection_budget)
        self.fanout = FanOutSink([self.broker], process_instance_id=INSTANCE)
        self.facade = _Facade() if facade is None else facade
        self.service.attach_context(self._context())

    def emit(self, run_id: str) -> SequencedEvent:
        return self.fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=0.0,
                run_id=run_id,
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )

    def close(self) -> None:
        self.service.close()
        self.broker.close(timeout=2.0)


@pytest.fixture(scope="module")
def _bound_server(tmp_path_factory):
    instance = _Server(tmp_path_factory.mktemp("web-http-wire"))
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def server(_bound_server):
    _bound_server.reset()
    return _bound_server


# --- raw HTTP helpers -------------------------------------------------------


def _connect(port: int) -> socket.socket:
    return socket.create_connection((DEFAULT_HOST, port), timeout=_TIMEOUT)


def _split_head(buffer: bytes) -> tuple[bytes, bytes]:
    """Split a raw response into its head and whatever body has arrived.

    One ``recv`` routinely returns the head and the first frames together,
    so the part after the blank line belongs to the body and must not be
    thrown away -- dropping it is how a byte test ends up asserting against
    a fragment while believing it saw the whole stream.
    """
    head, _, body = buffer.partition(b"\r\n\r\n")
    return head, body


def _headers_of(head: bytes) -> dict[str, str]:
    lines = head.split(b"\r\n")[1:]
    out: dict[str, str] = {}
    for line in lines:
        name, _, value = line.partition(b":")
        if name:
            out[name.strip().decode().lower()] = value.strip().decode()
    return out


def _assert_no_cross_origin_permission(head: bytes) -> None:
    headers = _headers_of(head)
    assert "access-control-allow-origin" not in headers
    assert "access-control-allow-credentials" not in headers
    assert "access-control-allow-headers" not in headers


def _read_body(sock: socket.socket, head: bytes, already: bytes = b"") -> bytes:
    length = int(_headers_of(head).get("content-length", "0"))
    sock.settimeout(_TIMEOUT)
    body = already
    while len(body) < length:
        data = sock.recv(65536)
        if not data:
            break
        body += data
    return body


def _request(port: int, raw: bytes) -> tuple[bytes, bytes]:
    """One request, its head and its body. Closes when the body is complete."""
    sock = _connect(port)
    try:
        sock.sendall(raw)
        buffer = b""
        sock.settimeout(_TIMEOUT)
        while b"\r\n\r\n" not in buffer:
            data = sock.recv(65536)
            if not data:
                break
            buffer += data
        head, _, rest = buffer.partition(b"\r\n\r\n")
        return head, _read_body(sock, head, rest)
    finally:
        sock.close()


@pytest.mark.parametrize(
    "partial",
    (
        b"HTTP/1.1 200 OK\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nab",
    ),
    ids=("headers", "body"),
)
def test_keep_alive_response_reader_rejects_premature_eof(partial: bytes) -> None:
    class TruncatedSocket:
        def __init__(self) -> None:
            self.chunks = iter((partial, b""))

        def recv(self, size: int) -> bytes:
            assert size > 0
            return next(self.chunks)

    with pytest.raises(EOFError, match="incomplete HTTP response"):
        _read_response_from_socket(TruncatedSocket())


def _read_response_from_socket(
    sock: socket.socket, buffered: bytes = b""
) -> tuple[bytes, bytes, bytes]:
    """Read one keep-alive response and preserve bytes of the next one."""
    data = buffered
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("incomplete HTTP response headers")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = int(_headers_of(head).get("content-length", "0"))
    while len(rest) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("incomplete HTTP response body")
        rest += chunk
    return head, rest[:length], rest[length:]


def _read_to_eof(sock: socket.socket, already: bytes = b"") -> bytes:
    """Read a response whose contract requires the server to close.

    The socket timeout is only a failing deadlock bound.  Successful return
    requires the peer's EOF, so elapsed silence can never prove that a second
    response was absent.
    """
    sock.settimeout(_TIMEOUT)
    buffer = already
    while True:
        data = sock.recv(65536)
        if not data:
            return buffer
        buffer += data


def _read_until(
    sock: socket.socket,
    predicate,
    timeout: float = _TIMEOUT,
    already: bytes = b"",
) -> bytes:
    """Read until ``predicate`` is satisfied, or the deadline passes."""
    deadline = time.monotonic() + timeout
    buffer = already
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            break
        buffer += data
        if predicate(buffer):
            break
    return buffer


def _get(port: int, path: str, extra: str = "", host: str | None = None) -> bytes:
    host_header = host if host is not None else f"localhost:{port}"
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Connection: close\r\n{extra}\r\n"
    ).encode()


def _head(port: int, path: str, extra: str = "") -> bytes:
    return (
        f"HEAD {path} HTTP/1.1\r\n"
        f"Host: localhost:{port}\r\n"
        f"Connection: close\r\n{extra}\r\n"
    ).encode()


@pytest.mark.parametrize(
    ("facade_method", "path"),
    [
        ("list_sessions", "/api/sessions?cursor=bad"),
        (
            "open_session",
            "/api/sessions/messages?session_id=s1&cursor=bad",
        ),
        ("list_runs", "/api/runs?cursor=bad"),
        (
            "list_session_chat_runs",
            "/api/sessions/runs?session_id=s1&cursor=bad",
        ),
        ("mainbar_pairs", "/api/mainbar?session_id=s1&cursor=bad"),
    ],
)
def test_a_malformed_page_cursor_is_a_secret_free_bad_request_on_the_wire(
    server, monkeypatch, facade_method: str, path: str
) -> None:
    def reject_cursor(*_args, **_kwargs):
        raise MalformedCursor("decoder detail must not cross the wire")

    monkeypatch.setattr(server.facade, facade_method, reject_cursor)

    head, body = _request(server.port, _get(server.port, path))

    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body) == {"code": "malformed_cursor"}
    assert b"Traceback" not in body
    assert b"decoder detail" not in body
    _assert_no_cross_origin_permission(head)


@pytest.mark.parametrize(
    ("path", "route", "expected"),
    [
        ("/api/sessions?limit=%D9%A1", "sessions", 25),
        (
            "/api/sessions/messages?session_id=s1&page_size=%2B7",
            "messages",
            25,
        ),
        ("/api/runs?limit=" + "9" * 5000, "runs", 100),
        ("/api/sessions/runs?session_id=s1&limit=0", "session-runs", 1),
        (
            "/api/mainbar?session_id=s1&limit=" + "0" * 5000 + "7",
            "mainbar",
            7,
        ),
    ],
    ids=[
        "unicode-falls-back",
        "sign-falls-back",
        "huge-clamps",
        "zero-clamps",
        "leading-zeroes-preserve-value",
    ],
)
def test_pagination_routes_share_the_ascii_decimal_boundary_on_the_wire(
    server, path: str, route: str, expected: int
) -> None:
    head, _body = _request(server.port, _get(server.port, path))

    assert head.startswith(b"HTTP/1.1 200")
    assert server.facade.read_limits == [(route, expected)]


def test_unknown_purpose_is_server_escaped_on_the_real_http_wire(
    server, monkeypatch
) -> None:
    from agent_alfred.runtime.runs import RunPage, RunSummary

    unsafe = "future-<>&\"'"
    page = RunPage(
        filter="system",
        runs=(
            RunSummary(
                run_id="r-future",
                purpose=unsafe,
                filter="system",
                purpose_known=False,
                session_id=None,
                gateway="cli",
                entry_surface_id=None,
                prompt_preview=None,
                phase="finished",
                outcome="completed",
                accepted_at="2026-09-05T12:00:00Z",
                started_at="2026-09-05T12:00:00Z",
                finished_at="2026-09-05T12:00:01Z",
                activity_revision=7,
            ),
        ),
        non_terminal=None,
        next_cursor=None,
    )
    monkeypatch.setattr(server.facade, "list_runs", lambda **_kwargs: page)

    head, body = _request(server.port, _get(server.port, "/api/runs?filter=system"))
    [run] = json.loads(body)["runs"]

    assert head.startswith(b"HTTP/1.1 200")
    assert run["purpose"] == "future-&lt;&gt;&amp;&quot;&#x27;"
    assert run["purpose_known"] is False


def test_an_unknown_read_failure_remains_an_internal_error_on_the_wire(
    server, monkeypatch
) -> None:
    def fail_read(*_args, **_kwargs):
        raise RuntimeError("unexpected read failure")

    monkeypatch.setattr(server.facade, "list_sessions", fail_read)

    head, body = _request(server.port, _get(server.port, "/api/sessions"))

    assert head.startswith(b"HTTP/1.1 500")
    assert json.loads(body) == {"code": "internal_error"}
    assert b"unexpected read failure" not in body
    assert b"Traceback" not in body
    _assert_no_cross_origin_permission(head)


# --- the stream -------------------------------------------------------------


def _raw_stream(
    server, extra: str, predicate, *, path: str = "/api/events"
) -> tuple[bytes, bytes]:
    """Open the stream, read until ``predicate`` is satisfied, return all of it.

    The whole response is kept -- head and body -- because a single recv
    carries both, and a byte contract asserted against a fragment is not a
    byte contract.
    """
    sock = _connect(server.port)
    try:
        sock.sendall(_get(server.port, path, extra))
        buffer = _read_until(sock, lambda raw: predicate(_split_head(raw)[1]))
        return _split_head(buffer)
    finally:
        sock.close()


def _open_stream(
    server, cursor: str | None = None, *, until_events: int | None = None
) -> bytes:
    """Open the stream and read until it has settled.

    ``until_events`` counts complete replayable events; ``None`` waits for the
    snapshot, which is the right stopping point for a connection that has
    nothing to catch up on.
    """
    extra = f"Last-Event-ID: {cursor}\r\n" if cursor else ""
    if until_events is None:
        return _raw_stream(server, extra, lambda body: b"event: state_patch" in body)[1]
    return _raw_stream(
        server,
        extra,
        # The first complete id is the re-seed; only later ids are events.
        lambda body: len(_ids_from(body)) >= until_events + 1,
    )[1]


def _ids_from(body: bytes) -> list[int]:
    """Every ``id:`` line in complete SSE records, in wire order.

    The first one is the re-seed -- the dataless frame that keeps the cursor
    alive -- and the rest are the checkpoints of complete events. Reading
    them back out of raw bytes is the point of this file.
    """
    return [
        int(line.split(b":")[-1])
        for record in body.split(b"\n\n")[:-1]
        for line in record.split(b"\n")
        if line.startswith(b"id: ")
    ]


def test_the_stream_opens_with_retry_then_reseed_then_the_snapshot(server) -> None:
    first = server.emit("r1")
    second = server.emit("r2")
    third = server.emit("r3")
    head, body = _raw_stream(
        server, "",
        lambda data: b"event: state_patch" in data.rpartition(b"\n\n")[0],
    )

    assert b"200" in head.split(b"\r\n")[0]
    assert _headers_of(head)["content-type"].startswith("text/event-stream")
    # The decided opening order: retry, then the re-seeded cursor, then the
    # snapshot. A first connection replays nothing -- the snapshot is the
    # state, and live events follow it.
    assert body.startswith(b"retry: 1000\n\n")
    reseed, *data_ids = _ids_from(body)
    assert reseed == third.seq
    assert data_ids == []
    assert b"event: state_patch\n" in body
    assert (first.seq, second.seq, third.seq) == (1, 2, 3)


def test_events_published_while_a_stream_is_open_reach_it(server) -> None:
    """The live half of the contract.

    A first connection gets the snapshot; everything after that has to arrive
    on the open stream, which is what the dispatcher and the per-connection
    writer exist for.
    """
    sock = _connect(server.port)
    try:
        sock.sendall(_get(server.port, "/api/events"))
        _read_until(sock, lambda data: b"event: state_patch" in data)
        events = [server.emit(f"live-{index}") for index in range(3)]
        body = _read_until(
            sock, lambda data: _ids_from(data) == [event.seq for event in events]
        )
    finally:
        sock.close()
    assert _ids_from(body) == [event.seq for event in events]


def test_the_first_frame_is_the_reseed_never_a_data_frame(server) -> None:
    """A dataless frame before any data is what keeps the cursor alive.

    The SSE dispatch algorithm copies the id buffer even for a dataless
    frame, so without this the first id-less frame would erase it.
    """
    server.emit("r1")
    _head, body = _raw_stream(
        server, "", lambda data: b"event: state_patch" in data
    )
    frames = [frame for frame in body.split(b"\n\n") if frame]
    assert frames[0] == b"retry: 1000"
    assert frames[1].startswith(b"id: ")


def test_a_reconnect_sends_its_cursor_and_gets_only_the_tail(server) -> None:
    first = server.emit("r1")
    second = server.emit("r2")
    third = server.emit("r3")
    body = _open_stream(server, f"{INSTANCE}:1", until_events=2)
    # Re-seeded at the cursor the browser sent, so an id-less frame cannot
    # erase it, and only what came after it is replayed.
    assert body.startswith(b"retry: 1000\n\n")
    reseed, *data_ids = _ids_from(body)
    assert reseed == first.seq
    assert data_ids == [second.seq, third.seq]
    assert first.seq not in data_ids


def test_an_unusable_cursor_is_reported_as_a_gap_not_a_silent_resume(server) -> None:
    server.emit("r1")
    _head, body = _raw_stream(
        server, "Last-Event-ID: garbage\r\n",
        lambda data: b"replay_gap" in data.rpartition(b"\n\n")[0],
    )
    assert b'"code":"replay_gap"' in body
    assert b'"gap_reason":"malformed"' in body
    # The notice carries no checkpoint: it is this connection's fact, not an
    # event, so it must not advance any cursor.
    assert b"replay_gap" in body
    notice = next(
        frame for frame in body.split(b"\n\n") if b"replay_gap" in frame
    )
    assert b"id: " not in notice


def test_an_explicitly_empty_cursor_is_a_malformed_gap_then_snapshot(server) -> None:
    server.emit("r1")
    head, body = _raw_stream(
        server,
        "Last-Event-ID:\r\n",
        lambda data: b"event: state_patch" in data,
    )

    assert head.startswith(b"HTTP/1.1 200")
    _assert_no_cross_origin_permission(head)
    stream_frames = [frame for frame in body.split(b"\n\n") if frame]
    assert stream_frames[0] == b"retry: 1000"
    assert stream_frames[1].startswith(b"id: ")

    notices = [
        frame for frame in stream_frames if b"event: transport_notice" in frame
    ]
    assert len(notices) == 1
    notice = notices[0]
    notice_payload = json.loads(
        next(
            line.removeprefix(b"data: ")
            for line in notice.splitlines()
            if line.startswith(b"data: ")
        )
    )
    assert notice_payload["code"] == "replay_gap"
    assert notice_payload["gap_reason"] == "malformed"
    assert b"id: " not in notice
    assert body.index(notice) < body.index(b"event: state_patch")


def test_an_overlong_digit_cursor_is_a_malformed_gap_then_snapshot(server) -> None:
    sock = _connect(server.port)
    handle = None
    try:
        sock.sendall(
            _get(
                server.port,
                "/api/events",
                f"Last-Event-ID: {INSTANCE}:{'9' * 5_000}\r\n",
            )
        )
        response = _read_until(
            sock,
            lambda raw: b"event: state_patch" in _split_head(raw)[1],
        )
        head, body = _split_head(response)

        assert head.startswith(b"HTTP/1.1 200")
        assert _headers_of(head)["content-type"].startswith("text/event-stream")
        gap_at = body.index(b"event: transport_notice")
        patch_at = body.index(b"event: state_patch")
        assert gap_at < patch_at
        gap_frame = next(
            frame for frame in body.split(b"\n\n") if b"replay_gap" in frame
        )
        gap_payload = json.loads(
            next(
                line.removeprefix(b"data: ")
                for line in gap_frame.splitlines()
                if line.startswith(b"data: ")
            )
        )
        assert gap_payload["code"] == "replay_gap"
        assert gap_payload["gap_reason"] == "malformed"
        assert b'"process_instance_id":"inst-http"' in body[patch_at:]
        assert b'"state_revision":0' in body[patch_at:]
        (handle,) = server.broker.connections
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    assert handle is not None
    handle.queue.request_close()
    assert handle.finished.wait(_TIMEOUT)
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert server.broker.connections == ()
    assert server.broker.registrations_in_flight == 0


def test_a_transport_notice_never_advances_the_cursor(server) -> None:
    server.emit("r1")
    _head, body = _raw_stream(
        server, "Last-Event-ID: garbage\r\n", lambda data: b"event: state_patch" in data
    )
    for frame in body.split(b"\n\n"):
        if b"transport_notice" in frame:
            assert b"id: " not in frame


def test_an_unverifiable_session_is_refused_before_stream_ownership(
    server, monkeypatch
) -> None:
    spawned_writers = []
    real_spawn = server.broker._spawn
    monkeypatch.setattr(
        server.broker,
        "_spawn",
        lambda target: spawned_writers.append(target) or real_spawn(target),
    )
    server.broker.bind_session_check(lambda _session_id: "unavailable")

    head, body = _request(
        server.port,
        _get(server.port, "/api/events?session_id=never-committed"),
    )

    assert head.startswith(b"HTTP/1.1 503")
    assert _headers_of(head)["content-type"] == "application/json; charset=utf-8"
    _assert_no_cross_origin_permission(head)
    assert json.loads(body) == {"code": "recording_unavailable"}
    assert spawned_writers == []
    assert server.broker.connections == ()
    assert server.broker.registrations_in_flight == 0


def test_startup_budget_refusal_is_json_before_sse_headers(server) -> None:
    server.reset(
        connection_budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20),
    )
    head, body = _request(server.port, _get(server.port, "/api/events"))

    assert head.startswith(b"HTTP/1.1 503")
    assert _headers_of(head)["content-type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"code": "stream_unavailable"}
    assert server.broker.connections == ()
    assert server.broker.registrations_in_flight == 0


def test_handler_exposes_an_incomplete_pre_header_stream_rollback() -> None:
    """No HTTP error is emitted while its prepared-stream cleanup is pending."""
    from types import SimpleNamespace

    from agent_alfred.resource_rollback import IncompleteRollback
    from agent_alfred.runtime.recording import RecordingUnavailable

    failure = RecordingUnavailable("injected recording refusal")
    may_finish = False

    class Broker:
        def prepare_stream(self, *, _rollback, **kwargs):
            del kwargs
            token = object()
            _rollback.own(token, lambda: may_finish)
            raise failure

    handler = object.__new__(DashboardHandler)
    handler.server = SimpleNamespace(context=SimpleNamespace(broker=Broker()))
    handler.path = "/api/events"
    handler.headers = {}
    handler.connection = object()
    handler.wfile = BytesIO()
    handler.close_connection = False
    sent: list[tuple[int, dict[str, str]]] = []
    handler._send = lambda status, payload: sent.append((status, payload))

    with pytest.raises(RecordingUnavailable) as caught:
        handler._serve_events()

    cleanup = caught.value.__cause__
    assert isinstance(cleanup, IncompleteRollback)
    assert sent == []
    may_finish = True
    assert cleanup.retry() is True


def test_handler_recovers_a_transferred_handle_at_its_return_edge() -> None:
    """The HTTP owner waits rather than reclaiming a writer-owned socket."""
    from types import SimpleNamespace

    class Finished:
        waited = False

        def wait(self) -> None:
            self.waited = True

    finished = Finished()
    handle = SimpleNamespace(finished=finished)
    acquisition = SimpleNamespace(transferred=False)
    proof = SimpleNamespace(
        _broker=None,
        _acquisition=acquisition,
        _handle=handle,
        transferred_handle=lambda: handle if acquisition.transferred else None,
    )

    class Broker:
        def prepare_stream(self, *, _rollback, **kwargs):
            del kwargs
            proof._broker = self
            _rollback.own(proof, lambda: True)
            return proof

        def start_stream(self, offered_proof):
            assert offered_proof is proof
            acquisition.transferred = True
            return handle

    broker = Broker()
    handler = object.__new__(DashboardHandler)
    handler.server = SimpleNamespace(context=SimpleNamespace(broker=broker))
    handler.path = "/api/events"
    handler.headers = {}
    handler.connection = object()
    handler.wfile = BytesIO()
    handler.close_connection = False
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    code = DashboardHandler._serve_events.__code__
    target = next(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname == "STORE_FAST" and instruction.argval == "handle"
    )
    armed = [True]
    control = SystemExit("stream handle returned before its local store")
    with claimed_monitoring_tool(
        "handler-stream-return-owner", local_codes=(code,)
    ) as tool_id:

        def interrupt(actual_code, actual_offset):
            if armed[0] and actual_code is code and actual_offset == target:
                armed[0] = False
                raise control

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.INSTRUCTION, interrupt
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.INSTRUCTION
        )
        handler._serve_events()

    assert armed == [False]
    assert finished.waited is True
    assert handler.close_connection is True


def test_capture_exhaustion_is_json_before_sse_headers(
    server, monkeypatch
) -> None:
    captures = 0
    advancing = False
    real_encoder = broker_module._patch_frames

    def advancing_encoder(snapshot, step, session_valid):
        nonlocal captures, advancing
        if advancing:
            return real_encoder(snapshot, step, session_valid)
        captures += 1
        encoded = real_encoder(snapshot, step, session_valid)
        advancing = True
        try:
            server.broker.publish_state_patch(
                replace(_snapshot(), state_revision=captures)
            )
        finally:
            advancing = False
        return encoded

    monkeypatch.setattr(broker_module, "_patch_frames", advancing_encoder)
    head, body = _request(server.port, _get(server.port, "/api/events"))

    assert captures == broker_module._MAX_PATCH_CAPTURES
    assert head.startswith(b"HTTP/1.1 503")
    assert _headers_of(head)["content-type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"code": "stream_unavailable"}
    assert server.broker.connections == ()
    assert server.broker.registrations_in_flight == 0


def test_response_header_failure_revokes_the_prepared_stream(
    server, monkeypatch
) -> None:
    prepared = []
    real_prepare = server.broker.prepare_stream

    def capture_proof(**kwargs):
        proof = real_prepare(**kwargs)
        prepared.append(proof)
        return proof

    monkeypatch.setattr(server.broker, "prepare_stream", capture_proof)
    monkeypatch.setattr(
        DashboardHandler,
        "end_headers",
        lambda _handler: (_ for _ in ()).throw(
            OSError("injected response header failure")
        ),
    )

    head, body = _request(server.port, _get(server.port, "/api/events"))

    assert head == b""
    assert body == b""
    assert len(prepared) == 1
    handle = prepared[0]._handle
    assert handle.finished.is_set()
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert server.broker.connections == ()
    assert server.broker.registrations_in_flight == 0


# --- the defences, on the wire ----------------------------------------------


def test_a_foreign_host_is_refused(server) -> None:
    head, body = _request(server.port, _get(server.port, "/api/entry", host="evil.com"))
    assert b"400" in head.split(b"\r\n")[0]
    assert b"host_not_allowed" in body


def test_a_foreign_origin_is_refused(server) -> None:
    head, body = _request(
        server.port,
        _get(server.port, "/api/events", "Origin: http://evil.com\r\n"),
    )
    assert b"403" in head.split(b"\r\n")[0]
    assert b"origin_not_allowed" in body


@pytest.mark.parametrize(
    ("host", "origin", "status", "code"),
    (
        ("evil.example", "http://evil.example", 400, "host_not_allowed"),
        (None, "http://evil.example", 403, "origin_not_allowed"),
    ),
)
def test_connect_is_guarded_and_uses_the_common_response_headers(
    server,
    host: str | None,
    origin: str,
    status: int,
    code: str,
) -> None:
    host_header = host if host is not None else f"localhost:{server.port}"
    raw = (
        "CONNECT /api/entry HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Origin: {origin}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()

    head, body = _request(server.port, raw)

    assert head.startswith(f"HTTP/1.1 {status}".encode())
    assert json.loads(body)["code"] == code
    headers = _headers_of(head)
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )
    assert headers["x-accel-buffering"] == "no"
    _assert_no_cross_origin_permission(head)


@pytest.mark.parametrize(
    ("host", "status", "code"),
    (
        ("rebound.evil.example", 400, "host_not_allowed"),
        (None, 405, "method_not_allowed"),
    ),
)
def test_extension_methods_cannot_bypass_the_guard_or_common_headers(
    server, host: str | None, status: int, code: str
) -> None:
    host_header = host if host is not None else f"localhost:{server.port}"
    raw = (
        "PROPFIND /api/entry HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Origin: https://evil.example\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    if host is None:
        raw = raw.replace(
            b"Origin: https://evil.example\r\n", b""
        )

    head, body = _request(server.port, raw)

    assert head.startswith(f"HTTP/1.1 {status}".encode())
    assert json.loads(body)["code"] == code
    headers = _headers_of(head)
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )
    assert headers["x-accel-buffering"] == "no"
    _assert_no_cross_origin_permission(head)


def test_no_response_ever_grants_cross_origin_access(server) -> None:
    """The one header whose absence is the defence.

    A cross-origin ``EventSource`` gets no data precisely because this is
    missing. One ``*`` here would silently remove the Origin layer from
    every endpoint at once.
    """
    for path, extra in (
        ("/api/entry", ""),
        ("/api/events", "Origin: http://evil.com\r\n"),
        ("/api/sessions", ""),
        ("/api/nonsense", ""),
        ("/api/entry", "Origin: http://evil.com\r\n"),
    ):
        head, _body = _request(server.port, _get(server.port, path, extra))
        headers = _headers_of(head)
        assert "access-control-allow-origin" not in headers, path
        assert "access-control-allow-credentials" not in headers, path
        assert "access-control-allow-headers" not in headers, path


def test_a_write_without_the_process_token_is_refused(server) -> None:
    body = b'{"message":"hi"}'
    raw = (
        f"POST /api/runs HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    head, response = _request(server.port, raw)
    assert b"403" in head.split(b"\r\n")[0]
    assert b"csrf_rejected" in response


def test_a_write_with_the_token_is_accepted(server) -> None:
    # The chat names the Session the server signed for it (#28): the token
    # proves the writer, the id proves the Session, and neither may be
    # inferred on the client's behalf.
    body = b'{"message":"hi","session_id":"session-from-server"}'
    raw = (
        f"POST /api/runs HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    head, response = _request(server.port, raw)
    assert b"202" in head.split(b"\r\n")[0]
    assert b'"run_id":"run-on-wire"' in response
    _assert_no_cross_origin_permission(head)


@pytest.mark.parametrize("comparison_fails", [False, True])
def test_token_comparison_type_error_is_a_wire_rejection(
    server, monkeypatch, comparison_fails: bool,
) -> None:
    """An unavailable comparison refuses the write without losing its response."""
    compared: list[tuple[str, str]] = []
    real_compare = guard_module.hmac.compare_digest

    def compare_token(actual: str, expected: str) -> bool:
        compared.append((actual, expected))
        if comparison_fails:
            raise TypeError("injected token comparison failure")
        return real_compare(actual, expected)

    monkeypatch.setattr(guard_module.hmac, "compare_digest", compare_token)
    head, body = _post_runs(
        server, b'{"message":"hi","session_id":"session-from-server"}',
    )
    token = server.guard.csrf_token
    assert compared == [(token, token)], "comparison injection was not reached"
    if comparison_fails:
        assert head.startswith(b"HTTP/1.1 403")
        assert json.loads(body)["code"] == "csrf_rejected"
        assert server.facade.submitted == []
    else:
        assert head.startswith(b"HTTP/1.1 202")
        assert json.loads(body)["run_id"] == "run-on-wire"
        assert len(server.facade.submitted) == 1
    _assert_no_cross_origin_permission(head)


def test_a_running_run_is_a_real_http_409(server) -> None:
    snapshot = _snapshot(coordinator_state="running", active_run=_active())
    server.facade.submit_result = SubmitResult(
        kind="run_in_progress", snapshot=snapshot
    )

    head, body = _post_runs(
        server, b'{"message":"hi","session_id":"session-from-server"}'
    )

    assert head.startswith(b"HTTP/1.1 409")
    assert json.loads(body)["code"] == "run_in_progress"
    _assert_no_cross_origin_permission(head)


def test_recording_pending_is_a_real_http_409_with_saving_stage(server) -> None:
    snapshot = _snapshot(
        coordinator_state="recording_pending",
        active_run=_active(
            phase="finished", outcome="completed", recording_state="pending"
        ),
    )
    server.facade.submit_result = SubmitResult(
        kind="run_in_progress", snapshot=snapshot
    )

    head, body = _post_runs(
        server, b'{"message":"hi","session_id":"session-from-server"}'
    )

    assert head.startswith(b"HTTP/1.1 409")
    payload = json.loads(body)
    assert payload["code"] == "run_in_progress"
    assert payload["active_run_summary"]["stage"] == "正在保存"
    assert "busy" not in payload
    _assert_no_cross_origin_permission(head)


def test_recording_failed_is_a_real_http_503(server) -> None:
    snapshot = _snapshot(
        coordinator_state="recording_failed",
        active_run=_active(
            phase="finished", outcome="completed", recording_state="failed"
        ),
    )
    server.facade.submit_result = SubmitResult(
        kind="recording_unavailable", snapshot=snapshot
    )

    head, body = _post_runs(
        server, b'{"message":"hi","session_id":"session-from-server"}'
    )

    assert head.startswith(b"HTTP/1.1 503")
    payload = json.loads(body)
    assert payload["code"] == "recording_unavailable"
    assert payload["active_run_summary"]["navigation"] == {
        "href": "/runs/run-already-active?filter=chat",
        "run_id": "run-already-active",
        "filter": "chat",
    }
    _assert_no_cross_origin_permission(head)


def test_handoff_failed_is_a_real_http_503_without_a_run_id(server) -> None:
    snapshot = _snapshot(
        coordinator_state="recording_failed",
        active_run=_active(
            run_id="committed-but-unreachable",
            phase="finished",
            outcome="interrupted",
            recording_state="failed",
        ),
    )
    server.facade.submit_result = SubmitResult(
        kind="handoff_failed",
        run_id="committed-but-unreachable",
        snapshot=snapshot,
    )

    head, body = _post_runs(
        server, b'{"message":"hi","session_id":"session-from-server"}'
    )

    assert head.startswith(b"HTTP/1.1 503")
    assert json.loads(body) == {"code": "admission_failed"}
    _assert_no_cross_origin_permission(head)


def test_handoff_finalize_double_failure_never_leaks_its_id_on_later_http_refusal(
    server,
) -> None:
    flag = {"armed": False}
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    wrapped = FailFinalizeWhen(database, flag, "UPDATE runs SET phase = ?")

    def refuse_handoff(_item) -> None:
        raise RuntimeError("injected handoff failure")

    host, conn = build_runtime_host(conn=wrapped, publish_work=refuse_handoff)
    host.start()
    server.reset(facade=host)
    try:
        session_id = host.create_session()
        flag["armed"] = True
        request = json.dumps({"message": "hi", "session_id": session_id}).encode()

        first_head, first_body = _post_runs(server, request)
        row = conn.execute(
            "SELECT run_id, phase, outcome, started_at FROM runs"
        ).fetchone()
        assert row is not None
        failed_run_id, phase, outcome, started_at = row

        second_head, second_body = _post_runs(server, request)

        first = json.loads(first_body)
        second = json.loads(second_body)
        assert first_head.startswith(b"HTTP/1.1 503")
        assert first == {"code": "admission_failed"}
        assert second_head.startswith(b"HTTP/1.1 503")
        assert second == {"code": "recording_unavailable"}
        assert (phase, outcome, started_at) == ("accepted", None, None)
        for payload in (first, second):
            serialized = json.dumps(payload, sort_keys=True)
            assert failed_run_id not in serialized
            assert "/runs/" not in serialized

        reads = (
            "/api/runs",
            "/api/sessions/runs?session_id=" + quote(session_id, safe=""),
            "/api/runs/locate/" + quote(failed_run_id, safe=""),
        )
        for path in reads:
            read_head, read_body = _request(
                server.port, _get(server.port, path)
            )
            read_payload = json.loads(read_body)
            assert failed_run_id not in json.dumps(read_payload, sort_keys=True)
            if "/locate/" in path:
                assert read_head.startswith(b"HTTP/1.1 404")
                assert read_payload == {"code": "unknown_run"}
            else:
                assert read_head.startswith(b"HTTP/1.1 200")
    finally:
        host.close()


def test_known_and_raced_busy_are_the_same_json_on_a_real_socket(server) -> None:
    snapshot = _snapshot(coordinator_state="running", active_run=_active())
    request = b'{"message":"hi","session_id":"session-from-server"}'

    # Admission captured the raced-busy snapshot in its refusal.
    server.facade.submit_result = SubmitResult(
        kind="run_in_progress", snapshot=snapshot
    )
    raced_head, raced_body = _post_runs(server, request)

    # A known-busy refusal reads the same authoritative facade snapshot.
    server.facade.state = snapshot
    server.facade.submit_result = SubmitResult(kind="run_in_progress")
    known_head, known_body = _post_runs(server, request)

    raced = json.loads(raced_body)
    known = json.loads(known_body)
    assert raced_head.startswith(b"HTTP/1.1 409")
    assert known_head.startswith(b"HTTP/1.1 409")
    assert raced["active_run_summary"] == known["active_run_summary"] == {
        "purpose": "chat",
        "gateway": "web",
        "started_at": "2026-08-31T08:00:00Z",
        "current_step": 3,
        "prompt_preview": "earlier prompt",
        "stage": "运行中",
        "navigation": {
            "href": "/runs/run-already-active?filter=chat",
            "run_id": "run-already-active",
            "filter": "chat",
        },
    }
    _assert_no_cross_origin_permission(raced_head)
    _assert_no_cross_origin_permission(known_head)


def test_the_handler_consumes_the_length_already_validated_by_the_guard(server) -> None:
    """The body reader has no second, disagreeing Content-Length decision."""

    body = b'{"message":"hi","session_id":"session-from-server"}'

    class _ChangingHeaders:
        def __init__(self) -> None:
            self._length_reads = 0
            self._values = {
                "host": f"localhost:{server.port}",
                "content-type": "application/json",
                CSRF_HEADER: server.guard.csrf_token,
            }

        def get(self, name: str, default=None):
            if name.lower() == "content-length":
                self._length_reads += 1
                if self._length_reads == 1:
                    return str(len(body))
                return "not-a-byte-count"
            for key, value in self._values.items():
                if key.lower() == name.lower():
                    return value
            return default

        def items(self):
            return self._values.items()

    handler = DashboardHandler.__new__(DashboardHandler)
    handler.server = server.service.server
    handler.headers = _ChangingHeaders()
    handler.path = "/api/runs"
    handler.rfile = BytesIO(body + b"trailing")
    responses: list[tuple[int, Any]] = []

    def record_response(status: int, payload: Any = None, **_kwargs: Any) -> None:
        responses.append((status, payload))

    handler._send = record_response
    handler._handle("POST")

    assert responses == [
        (202, {"run_id": "run-on-wire", "session_id": "session-from-server"})
    ]
    assert handler.rfile.read() == b"trailing"


def _post_runs(server, body: bytes) -> tuple[bytes, bytes]:
    """A well-formed chat write: legal Host, token, content type, message."""
    raw = (
        f"POST /api/runs HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    return _request(server.port, raw)


def test_a_chat_without_a_session_id_is_refused_on_the_wire(server) -> None:
    """#28 on the wire: a Web chat names its Session, or it is refused.

    The request is otherwise perfect -- right Host, right token, right
    content type, a real message -- and the answer is still a real HTTP 400
    with the one code, because no Session means no chat. Nothing behind the
    API was asked: no SubmitRequest crossed the facade, so no run id was
    minted and no config captured, let alone a Session invented.
    """
    head, body = _post_runs(server, b'{"message":"hi"}')
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body) == {"code": "missing_session_id"}
    assert server.facade.submitted == []


def test_a_null_session_id_on_the_wire_is_missing_not_present(server) -> None:
    """A JSON ``null`` rides the same wire and gets the same refusal."""
    head, body = _post_runs(server, b'{"message":"hi","session_id":null}')
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body) == {"code": "missing_session_id"}
    assert server.facade.submitted == []


def test_a_preflight_is_never_answered(server) -> None:
    """A preflight that succeeds is a cross-origin write waiting to happen."""
    head, body = _request(
        server.port,
        (
            f"OPTIONS /api/runs HTTP/1.1\r\n"
            f"Host: localhost:{server.port}\r\n"
            f"Origin: http://localhost:{server.port}\r\n"
            f"Access-Control-Request-Method: POST\r\n\r\n"
        ).encode(),
    )
    assert b"405" in head.split(b"\r\n")[0]
    assert b"no_preflight" in body
    assert "access-control-allow-origin" not in _headers_of(head)


def test_a_cross_origin_preflight_is_stopped_by_the_origin_layer(server) -> None:
    # The Origin check comes first, and it is the one that matters: a
    # preflight from another origin must not reach the "no preflight" answer
    # at all, let alone a permissive one.
    head, body = _request(
        server.port,
        (
            f"OPTIONS /api/runs HTTP/1.1\r\n"
            f"Host: localhost:{server.port}\r\n"
            f"Origin: http://evil.com\r\n"
            f"Access-Control-Request-Method: POST\r\n\r\n"
        ).encode(),
    )
    assert b"403" in head.split(b"\r\n")[0]
    assert b"origin_not_allowed" in body
    assert "access-control-allow-origin" not in _headers_of(head)


def test_an_oversized_write_is_refused_before_its_body_is_read(server) -> None:
    raw = (
        f"POST /api/runs HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: 99999999\r\n\r\n"
    ).encode()
    head, body = _request(server.port, raw)
    assert b"413" in head.split(b"\r\n")[0]
    assert b"body_too_large" in body


@pytest.mark.parametrize(
    ("length", "status", "payload"),
    [
        (
            "9" * 5000,
            413,
            {
                "code": "body_too_large",
                "detail": "the body may be at most 1048576 bytes",
            },
        ),
        (
            "\N{SUPERSCRIPT TWO}",
            400,
            {
                "code": "bad_content_length",
                "detail": "Content-Length is not a byte count",
            },
        ),
    ],
    ids=["long-ascii", "non-ascii"],
)
def test_invalid_content_lengths_are_closed_json_answers_on_the_wire(
    server, length: str, status: int, payload: dict[str, Any]
) -> None:
    raw = (
        f"POST /api/runs HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: {length}\r\n\r\n"
    ).encode("latin-1")

    head, body = _request(server.port, raw)

    assert head.startswith(f"HTTP/1.1 {status}".encode())
    assert json.loads(body) == payload
    assert b"Traceback" not in body
    assert b"Exceeds the limit" not in body
    _assert_no_cross_origin_permission(head)


@pytest.mark.parametrize(
    ("length", "status", "code"),
    [
        ("0" * 5000 + "1048576", 201, None),
        ("0" * 5000 + "1048577", 413, "body_too_large"),
    ],
    ids=["zero-padded-max", "zero-padded-max-plus-one"],
)
def test_the_http_parser_preserves_leading_zero_body_boundaries(
    server, length: str, status: int, code: str | None
) -> None:
    declared = 1_048_576 if status == 201 else 1_048_577
    request_body = b"{}" + b" " * (declared - 2) if status == 201 else b""
    raw = (
        f"POST /api/sessions HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: {length}\r\n\r\n"
    ).encode() + request_body

    head, body = _request(server.port, raw)

    assert head.startswith(f"HTTP/1.1 {status}".encode())
    payload = json.loads(body)
    if code is None:
        assert payload == {"session_id": "session-from-server"}
    else:
        assert payload["code"] == code
    _assert_no_cross_origin_permission(head)


def test_a_write_without_content_length_is_411_on_the_wire(server) -> None:
    raw = (
        f"POST /api/runs HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n\r\n"
    ).encode()
    head, body = _request(server.port, raw)
    assert head.startswith(b"HTTP/1.1 411")
    assert json.loads(body) == {
        "code": "length_required",
        "detail": "a Content-Length is required to write",
    }


def test_a_non_numeric_content_length_is_400_on_the_wire(server) -> None:
    raw = (
        f"POST /api/runs HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: lots\r\n\r\n"
    ).encode()
    head, body = _request(server.port, raw)
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body) == {
        "code": "bad_content_length",
        "detail": "Content-Length is not a byte count",
    }


def test_every_response_carries_the_hardening_headers(server) -> None:
    head, _body = _request(server.port, _get(server.port, "/api/entry"))
    headers = _headers_of(head)
    assert headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("raw", "status"),
    (
        (b"GET / HTTP/1.1 EXTRA\r\nConnection: keep-alive\r\n\r\n", 400),
        (b"GET /" + (b"a" * 65537) + b" HTTP/1.1\r\n\r\n", 414),
        (
            b"GET / HTTP/1.1\r\n"
            + b"X-Too-Large: "
            + (b"a" * 65537)
            + b"\r\n\r\n",
            431,
        ),
    ),
)
def test_parser_level_errors_use_the_hardened_json_response(
    server, raw: bytes, status: int
) -> None:
    head, body = _request(server.port, raw)

    assert head.startswith(f"HTTP/1.1 {status}".encode())
    assert json.loads(body) == {"code": "bad_request"}
    headers = _headers_of(head)
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["content-length"] == str(len(body))
    assert headers["connection"] == "close"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )
    assert headers["x-accel-buffering"] == "no"
    _assert_no_cross_origin_permission(head)


# --- the entry endpoint and 404 ---------------------------------------------


def test_the_entry_endpoint_hands_out_the_process_token(server) -> None:
    head, body = _request(server.port, _get(server.port, "/api/entry"))
    assert b"200" in head.split(b"\r\n")[0]
    # Safe to hand out on a GET: nothing cross-origin can read this response.
    assert server.guard.csrf_token.encode() in body
    assert INSTANCE.encode() in body


def test_creating_a_session_returns_a_server_signed_id(server) -> None:
    raw = (
        f"POST /api/sessions HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: 2\r\n\r\n{{}}"
    ).encode()
    head, body = _request(server.port, raw)
    assert b"201" in head.split(b"\r\n")[0]
    assert b'"session_id":"session-from-server"' in body


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/sessions", b"{}"),
        ("/api/runs", b'{"message":"x","session_id":"s"}'),
    ],
)
def test_only_post_may_reach_write_facades(
    server, method: str, path: str, payload: bytes
) -> None:
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode() + payload

    head, body = _request(server.port, raw)

    assert head.startswith(b"HTTP/1.1 405")
    assert json.loads(body) == {"code": "method_not_allowed"}
    assert server.facade.created_sessions == []
    assert server.facade.submitted == []
    _assert_no_cross_origin_permission(head)


@pytest.mark.parametrize(
    ("payload", "code"),
    [(b"not-json", "body_not_json"), (b"[]", "body_not_object")],
)
def test_session_creation_validates_its_declared_body_before_the_facade(
    server, payload: bytes, code: str
) -> None:
    raw = (
        "POST /api/sessions HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode() + payload

    head, body = _request(server.port, raw)

    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body) == {"code": code}
    assert server.facade.created_sessions == []


def test_session_body_is_consumed_before_the_next_keepalive_request(server) -> None:
    first = (
        "POST /api/sessions HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        "Content-Length: 2\r\n\r\n{}"
    ).encode()
    second = _get(server.port, "/api/entry", extra="Connection: close\r\n")
    sock = _connect(server.port)
    try:
        sock.sendall(first + second)
        first_head, _first_body, buffered = _read_response_from_socket(sock)
        second_head, _second_body, _ = _read_response_from_socket(sock, buffered)
    finally:
        sock.close()

    assert first_head.startswith(b"HTTP/1.1 201")
    assert second_head.startswith(b"HTTP/1.1 200")


@pytest.mark.parametrize("payload", (b"{}", b"{"))
def test_a_short_declared_body_is_rejected_before_session_creation(
    server, payload: bytes
) -> None:
    raw = (
        "POST /api/sessions HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        "Content-Length: 10\r\n\r\n"
    ).encode() + payload
    sock = _connect(server.port)
    try:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        head, body, _ = _read_response_from_socket(sock)
    finally:
        sock.close()

    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body) == {"code": "body_not_json"}
    _assert_no_cross_origin_permission(head)
    assert server.facade.created_sessions == []


@pytest.mark.parametrize(
    ("method", "path", "extra", "declared_length", "expected_status"),
    [
        ("PUT", "/api/sessions", "", 64, 405),
        ("POST", "/api/sessions", "", 1_048_577, 413),
        ("POST", "/api/unknown", "", 64, 404),
        ("POST", "/api/sessions", "X-Agent-Alfred-CSRF: wrong\r\n", 64, 403),
    ],
)
def test_early_write_response_closes_before_body_can_frame_a_second_request(
    server,
    method: str,
    path: str,
    extra: str,
    declared_length: int,
    expected_status: int,
) -> None:
    embedded = _get(server.port, "/api/entry", extra="Connection: close\r\n")
    csrf = "wrong" if extra else server.guard.csrf_token
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {csrf}\r\n"
        f"Content-Length: {declared_length}\r\n\r\n"
    ).encode() + embedded
    sock = _connect(server.port)
    try:
        sock.sendall(raw)
        received = _read_to_eof(sock)
    finally:
        sock.close()

    assert received.startswith(f"HTTP/1.1 {expected_status}".encode())
    assert received.count(b"HTTP/1.1 ") == 1
    assert _headers_of(received.partition(b"\r\n\r\n")[0])["connection"] == "close"
    assert server.facade.created_sessions == []
    assert server.facade.submitted == []


@pytest.mark.parametrize(
    ("method", "path", "extra", "expected_status"),
    [
        ("GET", "/api/entry", "", 200),
        ("GET", "/api/unknown", "", 404),
        ("HEAD", "/api/events", "", 200),
        ("OPTIONS", "/api/entry", "", 405),
        ("OPTIONS", "/api/entry", "Origin: https://evil.example\r\n", 403),
        ("TRACE", "/api/entry", "", 405),
    ],
)
def test_unconsumed_body_on_any_method_closes_before_an_embedded_request(
    server, method: str, path: str, extra: str, expected_status: int
) -> None:
    embedded = _get(server.port, "/api/entry", extra="Connection: close\r\n")
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"{extra}"
        f"Content-Length: {len(embedded)}\r\n\r\n"
    ).encode() + embedded
    sock = _connect(server.port)
    try:
        sock.sendall(raw)
        received = _read_to_eof(sock)
    finally:
        sock.close()

    assert received.startswith(f"HTTP/1.1 {expected_status}".encode())
    assert received.count(b"HTTP/1.1 ") == 1
    assert _headers_of(received.partition(b"\r\n\r\n")[0])["connection"] == "close"
    _assert_no_cross_origin_permission(received)
    assert server.facade.created_sessions == []
    assert server.facade.submitted == []


def test_head_matches_get_metadata_and_preserves_the_next_response(server) -> None:
    raw = (
        "HEAD /api/entry HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n\r\n"
        "GET /api/entry HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    sock = _connect(server.port)
    try:
        sock.sendall(raw)
        received = _read_until(sock, lambda data: data.count(b"\r\n\r\n") >= 2)
        first_head, separator, next_response = received.partition(b"\r\n\r\n")
        second_head, second_body, trailing = _read_response_from_socket(
            sock, next_response
        )
    finally:
        sock.close()

    assert separator
    assert first_head.startswith(b"HTTP/1.1 200")
    assert next_response.startswith(b"HTTP/1.1 200")
    assert second_head.startswith(b"HTTP/1.1 200")
    assert _headers_of(first_head)["content-length"] == _headers_of(second_head)[
        "content-length"
    ]
    assert json.loads(second_body)["instance_id"] == INSTANCE
    assert trailing == b""


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/api/sessions", 200),
        ("/api/nonsense", 404),
    ],
)
def test_head_reuses_each_ordinary_get_route(
    server, path: str, expected_status: int
) -> None:
    head, head_body = _request(server.port, _head(server.port, path))
    get_head, get_body = _request(server.port, _get(server.port, path))

    assert head.startswith(f"HTTP/1.1 {expected_status}".encode())
    assert get_head.startswith(f"HTTP/1.1 {expected_status}".encode())
    assert head_body == b""
    assert _headers_of(head)["content-length"] == _headers_of(get_head)[
        "content-length"
    ]
    assert _headers_of(head)["content-type"] == _headers_of(get_head)[
        "content-type"
    ]
    assert json.loads(get_body)


def test_events_head_returns_sse_headers_without_owning_a_stream(
    server, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_calls = 0
    start_calls = 0

    def reject_prepare(**_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        raise AssertionError("HEAD must not prepare a stream")

    def reject_start(_proof):
        nonlocal start_calls
        start_calls += 1
        raise AssertionError("HEAD must not start a writer")

    monkeypatch.setattr(server.broker, "prepare_stream", reject_prepare)
    monkeypatch.setattr(server.broker, "start_stream", reject_start)
    raw = (
        "HEAD /api/events HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n\r\n"
        "GET /api/entry HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()

    sock = _connect(server.port)
    try:
        sock.sendall(raw)
        received = _read_until(sock, lambda data: data.count(b"\r\n\r\n") >= 2)
        first_head, separator, next_response = received.partition(b"\r\n\r\n")
        second_head, second_body, trailing = _read_response_from_socket(
            sock, next_response
        )
    finally:
        sock.close()

    assert separator
    assert first_head.startswith(b"HTTP/1.1 200")
    assert _headers_of(first_head)["content-type"].startswith("text/event-stream")
    assert "content-length" not in _headers_of(first_head)
    assert next_response.startswith(b"HTTP/1.1 200")
    assert second_head.startswith(b"HTTP/1.1 200")
    assert json.loads(second_body)["instance_id"] == INSTANCE
    assert trailing == b""
    assert prepare_calls == 0
    assert start_calls == 0
    assert server.broker.connections == ()
    assert server.broker.registrations_in_flight == 0


def test_events_head_preserves_session_preflight_failure_metadata(
    server, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_calls = 0
    start_calls = 0
    real_prepare = server.broker.prepare_stream
    real_start = server.broker.start_stream

    def observe_prepare(**kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return real_prepare(**kwargs)

    def observe_start(proof):
        nonlocal start_calls
        start_calls += 1
        return real_start(proof)

    monkeypatch.setattr(server.broker, "prepare_stream", observe_prepare)
    monkeypatch.setattr(server.broker, "start_stream", observe_start)
    server.broker.bind_session_check(lambda _session_id: "unavailable")

    head, head_body = _request(
        server.port,
        _head(server.port, "/api/events?session_id=not-recorded"),
    )
    assert prepare_calls == 0
    assert start_calls == 0

    get_head, get_body = _request(
        server.port,
        _get(server.port, "/api/events?session_id=not-recorded"),
    )

    assert head.startswith(b"HTTP/1.1 503")
    assert get_head.startswith(b"HTTP/1.1 503")
    assert head_body == b""
    assert _headers_of(head)["content-length"] == _headers_of(get_head)[
        "content-length"
    ]
    assert _headers_of(head)["content-type"] == _headers_of(get_head)[
        "content-type"
    ]
    assert json.loads(get_body) == {"code": "recording_unavailable"}
    assert prepare_calls == 1
    assert start_calls == 0
    assert server.broker.connections == ()
    assert server.broker.registrations_in_flight == 0


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ("Transfer-Encoding: chunked\r\n", "unsupported_transfer_encoding"),
        ("Content-Length: 2\r\nContent-Length: 2\r\n", "conflicting_content_length"),
        ("Content-Length: 2\r\nContent-Length: 3\r\n", "conflicting_content_length"),
    ],
)
def test_ambiguous_request_framing_is_rejected_and_closed(
    server, headers, code
) -> None:
    raw = (
        "GET /api/entry HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"{headers}\r\n"
    ).encode()
    head, body = _request(server.port, raw)
    assert head.startswith(b"HTTP/1.1 400")
    assert json.loads(body)["code"] == code
    assert _headers_of(head)["connection"] == "close"
    _assert_no_cross_origin_permission(head)


def test_recording_unavailable_refuses_session_creation_on_the_wire(server) -> None:
    server.facade.mutation_refusal = "recording_unavailable"
    raw = (
        f"POST /api/sessions HTTP/1.1\r\n"
        f"Host: localhost:{server.port}\r\n"
        f"Content-Type: application/json\r\n"
        f"{CSRF_HEADER}: {server.guard.csrf_token}\r\n"
        f"Content-Length: 2\r\n\r\n{{}}"
    ).encode()

    head, body = _request(server.port, raw)

    assert head.startswith(b"HTTP/1.1 503")
    assert json.loads(body)["code"] == "recording_unavailable"
    _assert_no_cross_origin_permission(head)
    assert server.facade.created_sessions == []


def test_an_unknown_path_is_a_json_404(server) -> None:
    head, body = _request(server.port, _get(server.port, "/api/nonsense"))
    assert b"404" in head.split(b"\r\n")[0]
    assert b'"code":"not_found"' in body


def test_the_server_binds_only_loopback(server) -> None:
    assert server.bind_calls == 1
    assert server.service.server.server_address[0] == DEFAULT_HOST


def test_a_closed_stream_lets_the_handler_thread_go(server) -> None:
    """The handler must not outlive its socket.

    An SSE handler blocks in ``finished.wait()``; if closing the peer did not
    release it, every reconnect would leak a thread.
    """
    server.emit("r1")
    sock = _connect(server.port)
    try:
        sock.sendall(_get(server.port, "/api/events"))
        _read_until(sock, lambda data: b"event: state_patch" in data)
        assert len(server.broker.connections) == 1
        handle = server.broker.connections[0]
        # Force a reset rather than relying on an arbitrary number of writes
        # to discover an ordinary FIN. The server-side descriptor becoming
        # readable is the kernel's deterministic acknowledgement of it.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    finally:
        sock.close()
    readable, _, _ = select.select(
        [handle.connection._sock], [], [], _TIMEOUT  # noqa: SLF001
    )
    assert readable, "server socket did not observe the peer reset"
    # The writer only learns the peer is gone when it next writes to it --
    # that is what the heartbeat is for, and it is why an idle connection
    # thread cannot be left blocked on get() forever.
    server.emit("closed-peer-probe")
    assert handle.finished.wait(_TIMEOUT), "writer did not retire the reset peer"
    assert server.broker.connections == ()


def test_the_cursor_round_trips_through_a_real_socket(server) -> None:
    """The loop a fake cannot prove: we write it, a client sends it back.

    ``ReplayRing`` and the broker are both tested without a socket, but only
    a real connection shows that what we put on the wire is what comes back
    in ``Last-Event-ID``.
    """
    events = [server.emit(f"r{index}") for index in range(5)]

    # A mid-ring cursor: exactly the tail it was owed, in order, once.
    reseed, *data_ids = _ids_from(
        _open_stream(server, f"{INSTANCE}:2", until_events=3)
    )
    assert reseed == 2
    assert data_ids == [3, 4, 5]

    # The cursor the last page ended on, sent back: nothing to catch up, and
    # nothing invented to fill the space.
    reseed, *data_ids = _ids_from(_open_stream(server, f"{INSTANCE}:5"))
    assert reseed == 5
    assert data_ids == []

    # An earlier cursor replays the same events again rather than a hole:
    # the ring, not the connection, is what makes a reconnect whole.
    reseed, *again = _ids_from(_open_stream(server, f"{INSTANCE}:2", until_events=3))
    assert again == [3, 4, 5]
    assert [event.seq for event in events] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("run_id", "encoded_id"),
    [
        ("run space", "run%20space"),
        ("run 中文", "run%20%E4%B8%AD%E6%96%87"),
        ("run/a?b#c", "run%2Fa%3Fb%23c"),
        ("run%2Fid", "run%252Fid"),
    ],
)
def test_run_deep_link_percent_decodes_the_opaque_id_exactly_once(
    server, monkeypatch, run_id, encoded_id
) -> None:
    from agent_alfred.runtime.runs import RunPage

    located_ids = []

    def locate(requested_id: str, *, limit: int):
        located_ids.append(requested_id)
        if requested_id != run_id:
            return None
        return RunPage(filter="chat", runs=(), non_terminal=None, next_cursor=None)

    monkeypatch.setattr(server.facade, "locate_run", locate)
    head, body = _request(
        server.port, _get(server.port, "/api/runs/locate/" + encoded_id)
    )

    assert head.startswith(b"HTTP/1.1 200")
    assert json.loads(body)["filter"] == "chat"
    assert located_ids == [run_id]


# --- the historic-session reads: an opaque id on the query string ----------

# ADR-0027: a historic ``session_id`` is an arbitrary TEXT value. It cannot
# survive as a path segment, so the fixed endpoint carries it as one query
# parameter, percent-decoded exactly once and then used verbatim.
HISTORIC_IDS = [
    "",
    "/",
    "/etc/passwd",
    "a?b",
    "a#b",
    "100%",
    "a b",
    "café",
    "a/b?c#d%20e",
    "%2F",
    "a%2Fb",
]


@pytest.mark.parametrize("session_id", HISTORIC_IDS)
def test_a_historic_session_id_rides_the_query_parameter_verbatim(
    server, session_id
) -> None:
    head, body = _request(
        server.port,
        _get(
            server.port,
            "/api/sessions/messages?session_id=" + quote(session_id, safe=""),
        ),
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert json.loads(body)["session_id"] == session_id


def test_a_missing_session_id_parameter_is_a_bad_request(server) -> None:
    """Missing and empty are different facts: the value may legitimately be
    the empty string, so only its absence is a bad request."""
    head, body = _request(
        server.port, _get(server.port, "/api/sessions/messages")
    )
    assert head.startswith(b"HTTP/1.1 400")
    assert b"missing_session_id" in body


def test_the_session_id_is_percent_decoded_exactly_once(server) -> None:
    head, body = _request(
        server.port,
        _get(server.port, "/api/sessions/messages?session_id=a%252Fb"),
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert json.loads(body)["session_id"] == "a%2Fb"


def test_the_old_path_segment_route_is_gone(server) -> None:
    """A session id is not a path segment: the old shape must not answer.

    Slicing the raw path both mangled ids containing ``/`` and left encoded
    values undecoded; every historic session the inbox returns must open
    through the one route, not through a parse of its own id.
    """
    head, _body = _request(
        server.port, _get(server.port, "/api/sessions/abc/messages")
    )
    assert head.startswith(b"HTTP/1.1 404")


@pytest.mark.parametrize("session_id", HISTORIC_IDS)
def test_the_mainbar_takes_its_session_from_the_query_parameter(
    server, session_id
) -> None:
    """Each tab's MainBar is that tab's Session's only conversation, and a
    historic id is an opaque value: it rides the query string verbatim, the
    same way the messages route carries it."""
    head, body = _request(
        server.port,
        _get(server.port, "/api/mainbar?session_id=" + quote(session_id, safe="")),
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert server.facade.mainbar_sessions == [session_id]


def test_a_mainbar_without_a_session_id_parameter_is_a_bad_request(
    server,
) -> None:
    head, body = _request(server.port, _get(server.port, "/api/mainbar"))
    assert head.startswith(b"HTTP/1.1 400")
    assert b"missing_session_id" in body
    assert server.facade.mainbar_sessions == []


@pytest.mark.parametrize("session_id", HISTORIC_IDS)
def test_the_session_run_list_takes_its_session_from_the_query_parameter(
    server, session_id
) -> None:
    """The Session group's run list is a server-side page of one Session, and
    a historic id rides the query string verbatim like everywhere else."""
    head, body = _request(
        server.port,
        _get(
            server.port,
            "/api/sessions/runs?session_id=" + quote(session_id, safe=""),
        ),
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert json.loads(body)["session_id"] == session_id
    assert server.facade.session_run_sessions == [session_id]


def test_a_session_run_list_without_a_session_id_is_a_bad_request(server) -> None:
    head, body = _request(server.port, _get(server.port, "/api/sessions/runs"))
    assert head.startswith(b"HTTP/1.1 400")
    assert b"missing_session_id" in body
    assert server.facade.session_run_sessions == []
