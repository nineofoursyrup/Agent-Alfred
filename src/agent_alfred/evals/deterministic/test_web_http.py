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

import json
import socket
import time
from io import BytesIO
from typing import Any
from urllib.parse import quote

import pytest

from agent_alfred.events import EventEnvelope, FanOutSink, RunStarted, SequencedEvent
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.guard import CSRF_HEADER, RequestGuard
from agent_alfred.gateway.web.handler import DashboardHandler, HandlerContext
from agent_alfred.gateway.web.lifecycle import DEFAULT_HOST, DashboardService
from agent_alfred.gateway.web.replay import ReplayRing
from agent_alfred.runtime.snapshot import RuntimeSnapshot

INSTANCE = "inst-http"
_TIMEOUT = 3.0


def _snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        process_instance_id=INSTANCE,
        state_revision=0,
        coordinator_state="idle",
        active_run=None,
        unrecorded_terminal_projection=None,
    )


class _Facade:
    """The smallest facade that lets the wire be the thing under test."""

    def __init__(self) -> None:
        self.mutating = False
        # Every Run the API asked this facade to admit, so a wire test can
        # prove a refusal happened before anything was asked at all.
        self.submitted: list[Any] = []
        # The Session ids the MainBar read was aimed at, so a wire test can
        # see the query parameter arrived verbatim.
        self.mainbar_sessions: list[str] = []
        # The same record for the Session group's run list.
        self.session_run_sessions: list[str] = []

    # The gate's authority, in the shape the Host provides it. No Run is
    # ever in flight behind this facade, so the only thing that can busy the
    # gate here is another write -- which these tests never start.
    def try_begin_mutation(self) -> str | None:
        if self.mutating:
            return "mutation_in_flight"
        self.mutating = True
        return None

    def end_mutation(self) -> None:
        self.mutating = False

    def mutation_in_flight(self) -> bool:
        return self.mutating

    def create_session(self) -> str:
        return "session-from-server"

    def submit(self, request: Any) -> Any:
        self.submitted.append(request)
        from agent_alfred.runtime.work import SubmitResult

        return SubmitResult(
            kind="accepted", run_id="run-on-wire", session_id="session-from-server"
        )

    def session_exists(self, session_id: str) -> bool:
        return True

    def snapshot(self) -> RuntimeSnapshot:
        return _snapshot()

    def list_sessions(self, *, limit: int, cursor: str | None = None):
        from agent_alfred.runtime.sessions import SessionInboxPage

        return SessionInboxPage(sessions=(), next_cursor=None)

    def open_session(
        self, session_id: str, *, page_size: int, cursor: str | None = None
    ):
        from agent_alfred.runtime.sessions import SessionMessagesPage

        return SessionMessagesPage(
            session_id=session_id, title="t", messages=(), next_cursor=None
        )

    def list_runs(self, *, filter: str, limit: int, cursor: str | None = None):
        from agent_alfred.runtime.runs import RunPage

        return RunPage(filter=filter, runs=(), non_terminal=None, next_cursor=None)

    def locate_run(self, run_id: str, *, limit: int):
        return None

    def mainbar_pairs(self, *, session_id: str, limit: int, cursor: str | None):
        self.mainbar_sessions.append(session_id)
        from agent_alfred.runtime.runs import MainBarPage

        return MainBarPage(pairs=(), next_cursor=None)

    def list_session_chat_runs(
        self, *, session_id: str, limit: int, cursor: str | None
    ):
        self.session_run_sessions.append(session_id)
        from agent_alfred.runtime.runs import SessionChatRunsPage

        return SessionChatRunsPage(session_id=session_id, runs=(), next_cursor=None)


def _free_port() -> int:
    probe = socket.socket()
    try:
        probe.bind((DEFAULT_HOST, 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class _Server:
    def __init__(self, tmp_path):
        self.port = _free_port()
        self.broker = SSEBroker(
            process_instance_id=INSTANCE,
            snapshot=_snapshot(),
            session_is_valid=lambda session_id: True,
            ring=ReplayRing(),
        )
        # The dispatcher runs before the socket exists, so an event published
        # while a stream is open reaches that stream rather than waiting for
        # the next connection to replay it.
        self.broker.start()
        self.fanout = FanOutSink([self.broker], process_instance_id=INSTANCE)
        self.guard = RequestGuard(port=self.port, csrf_token="token-from-process")
        self.facade = _Facade()
        context = HandlerContext(
            guard=self.guard,
            api=DashboardApi(facade=self.facade),
            broker=self.broker,
            instance_id=INSTANCE,
        )
        self.service = DashboardService(
            state_dir=tmp_path,
            handler=DashboardHandler,
            context=context,
            instance_id=INSTANCE,
                port=self.port,
        )
        self.service.start()
        self.thread = self.service.start_serving()

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


@pytest.fixture()
def server(tmp_path):
    instance = _Server(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


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


def _read_stream(
    sock: socket.socket, predicate, timeout: float = _TIMEOUT
) -> bytes:
    """Read stream bytes from a socket whose headers are already consumed.

    No head/body split is applied here: the terminator was read by whoever
    consumed the headers, so splitting again would treat the whole stream as
    a header and report an empty body forever.
    """
    return _read_until(sock, predicate, timeout=timeout)


def _get(port: int, path: str, extra: str = "", host: str | None = None) -> bytes:
    host_header = host if host is not None else f"localhost:{port}"
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Connection: close\r\n{extra}\r\n"
    ).encode()


# --- the stream -------------------------------------------------------------


def _raw_stream(server, extra: str, predicate) -> tuple[bytes, bytes]:
    """Open the stream, read until ``predicate`` is satisfied, return all of it.

    The whole response is kept -- head and body -- because a single recv
    carries both, and a byte contract asserted against a fragment is not a
    byte contract.
    """
    sock = _connect(server.port)
    try:
        sock.sendall(_get(server.port, "/api/events", extra))
        buffer = _read_until(sock, lambda raw: predicate(_split_head(raw)[1]))
        return _split_head(buffer)
    finally:
        sock.close()


def _open_stream(
    server, cursor: str | None = None, *, until_events: int | None = None
) -> bytes:
    """Open the stream and read until it has settled.

    ``until_events`` counts domain events; ``None`` waits only for the
    snapshot, which is the right stopping point for a connection that has
    nothing to catch up on.
    """
    extra = f"Last-Event-ID: {cursor}\r\n" if cursor else ""
    if until_events is None:
        return _raw_stream(server, extra, lambda body: b"event: state_patch" in body)[1]
    return _raw_stream(
        server,
        extra,
        lambda body: body.count(b"event: domain_event") >= until_events,
    )[1]


def _ids_from(body: bytes) -> list[int]:
    """Every ``id:`` line on the wire, in order.

    The first one is the re-seed -- the dataless frame that keeps the cursor
    alive -- and the rest are the checkpoints of complete events. Reading
    them back out of raw bytes is the point of this file.
    """
    return [
        int(line.split(b":")[-1])
        for line in body.split(b"\n")
        if line.startswith(b"id: ")
    ]


def test_the_stream_opens_with_retry_then_reseed_then_the_snapshot(server) -> None:
    first = server.emit("r1")
    second = server.emit("r2")
    third = server.emit("r3")
    head, body = _raw_stream(server, "", lambda data: b"event: state_patch" in data)

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
        _read_stream(sock, lambda data: b"event: state_patch" in data)
        events = [server.emit(f"live-{index}") for index in range(3)]
        body = _read_stream(
            sock, lambda data: data.count(b"event: domain_event") >= 3
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
        server, "Last-Event-ID: garbage\r\n", lambda data: b"replay_gap" in data
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


def test_a_transport_notice_never_advances_the_cursor(server) -> None:
    server.emit("r1")
    _head, body = _raw_stream(
        server, "Last-Event-ID: garbage\r\n", lambda data: b"event: state_patch" in data
    )
    for frame in body.split(b"\n\n"):
        if b"transport_notice" in frame:
            assert b"id: " not in frame


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


def test_an_unknown_path_is_a_json_404(server) -> None:
    head, body = _request(server.port, _get(server.port, "/api/nonsense"))
    assert b"404" in head.split(b"\r\n")[0]
    assert b'"code":"not_found"' in body


def test_the_server_binds_only_loopback(server) -> None:
    assert server.service.server.server_address[0] == DEFAULT_HOST
    # Nothing else is even listening: a connection attempt to the same port
    # on any other local address must not reach this server.
    other = socket.socket()
    other.settimeout(0.5)
    try:
        other.bind((DEFAULT_HOST, 0))
        assert other.getsockname()[1] != server.port
    finally:
        other.close()


def test_a_closed_stream_lets_the_handler_thread_go(server) -> None:
    """The handler must not outlive its socket.

    An SSE handler blocks in ``finished.wait()``; if closing the peer did not
    release it, every reconnect would leak a thread.
    """
    server.emit("r1")
    sock = _connect(server.port)
    try:
        sock.sendall(_get(server.port, "/api/events"))
        _read_stream(sock, lambda data: b"event: state_patch" in data)
        assert len(server.broker.connections) == 1
    finally:
        sock.close()
    sock.close()
    # The writer only learns the peer is gone when it next writes to it --
    # that is what the heartbeat is for, and it is why an idle connection
    # thread cannot be left blocked on get() forever.
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        server.emit(f"probe-{time.monotonic()}")
        if not server.broker.connections:
            break
        time.sleep(0.02)
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
