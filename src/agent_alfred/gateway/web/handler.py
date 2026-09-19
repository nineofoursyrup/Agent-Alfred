"""The HTTP shroud: routes, headers, and the SSE stream. No logic.

Everything a request *means* lives in :mod:`~agent_alfred.gateway.web.api`
and :mod:`~agent_alfred.gateway.web.guard`; this module only turns bytes into
a call and a call back into bytes. That split is deliberate: the defences and
the contract are the parts worth testing, and neither needs a socket, while
the parts that do need a socket are exactly the parts with no decisions in
them.

Two rules shape the response headers:

- every response carries ``nosniff`` and a policy that forbids embedding, so
  a response nobody asked for cannot be reinterpreted or framed;
- ``Access-Control-Allow-Origin`` is never emitted, not even for a refused
  request. Its absence is what stops a cross-origin ``EventSource`` from
  reading the stream, and adding it "just for the error path" would remove
  the whole Origin layer on the one path an attacker controls.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from agent_alfred.database_console.budget import HTTP_IO_S
from agent_alfred.database_console.errors import SAFE_DETAIL
from agent_alfred.gateway.web import database_api
from agent_alfred.gateway.web.accounting_api import READS as ACCOUNTING_READS
from agent_alfred.gateway.web.accounting_api import WRITES as ACCOUNTING_WRITES
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.assets import PAGE_POLICY, page_asset
from agent_alfred.gateway.web.broker import StreamAdmissionRejected
from agent_alfred.gateway.web.connection import SocketConnection
from agent_alfred.gateway.web.guard import (
    AuthorizedRequest,
    Rejection,
    RequestGuard,
)
from agent_alfred.gateway.web.memory_api import (
    PAGE_READS as MEMORY_PAGE_READS,
)
from agent_alfred.gateway.web.memory_api import (
    PAGE_WRITES as MEMORY_PAGE_WRITES,
)
from agent_alfred.gateway.web.replay import CursorText
from agent_alfred.resource_rollback import ResumableRollback
from agent_alfred.runtime.recording import RecordingUnavailable

EVENTS_PATH = "/api/events"
ENTRY_PATH = "/api/entry"
SESSIONS_PATH = "/api/sessions"
# The one messages endpoint. A historic ``session_id`` is an arbitrary TEXT
# value (ADR-0027): it cannot survive as a path segment, so it rides the
# query string and is used verbatim -- see ``_route_get``.
SESSION_MESSAGES_PATH = "/api/sessions/messages"
SESSION_RUNS_PATH = "/api/sessions/runs"
RUNS_PATH = "/api/runs"
MAINBAR_PATH = "/api/mainbar"
REPLY_PATH = "/api/reply"
CONNECTIONS_PATH = "/api/connections"
MODELS_PATH = "/api/models"
SETTINGS_PATH = "/api/settings"
REREAD_ENV_PATH = "/api/connections/reread"
AUTH_PROBE_PATH = "/api/connections/probe"

# Sent on every response. ``nosniff`` stops a browser from reinterpreting a
# JSON body as something executable; the CSP forbids framing and every
# subresource, which is the right policy for a service that serves an API
# and nothing else.
BASE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    # A reverse proxy that buffers would hold an event stream hostage; this
    # is the conventional way to tell one not to.
    ("X-Accel-Buffering", "no"),
)

SSE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Type", "text/event-stream; charset=utf-8"),
    ("Connection", "keep-alive"),
)

__all__ = [
    "DashboardHandler",
    "HandlerContext",
]


class HandlerContext:
    """What every request needs, assembled once after the bind succeeded.

    The guard depends on the port, so it cannot exist before the bind -- one
    more reason the descriptor is the last step of the lifecycle.
    """

    def __init__(
        self,
        *,
        guard: RequestGuard,
        api: DashboardApi,
        broker: Any,
        instance_id: str,
        database: Any = None,
    ):
        self.guard = guard
        self.api = api
        self.broker = broker
        self.instance_id = instance_id
        self.database = database
        self._requests = threading.Condition()
        self._stopping = False
        self._active_requests: dict[object, bool] = {}

    def begin_request(self, registration: object, *, stream: bool) -> bool:
        """Register under the caller's already-owned, unique release identity."""
        with self._requests:
            if self._stopping:
                return False
            self._active_requests[registration] = stream
            return True

    def end_request(self, registration: object) -> None:
        """Idempotent even for rejection or interruption before registration."""
        with self._requests:
            self._active_requests.pop(registration, None)
            self._requests.notify_all()

    def stop_requests(self) -> None:
        with self._requests:
            self._stopping = True

    def wait_requests(self, *, streams: bool, timeout: float | None) -> bool:
        """Ordinary reads drain before Host resources; SSE after broker stop.

        A five-second default bounds a stuck client; False retains resources
        so a later Dashboard.close can resume without declaring shutdown done.
        """
        with self._requests:
            return self._requests.wait_for(
                lambda: (
                    not self._active_requests
                    if streams
                    else all(self._active_requests.values())
                ),
                timeout=5.0 if timeout is None else max(0.0, timeout),
            )


class DashboardHandler(BaseHTTPRequestHandler):
    """One request. The process-wide context hangs off ``self.server``.

    ``http.server`` instantiates the handler itself, once per connection, so
    the context cannot be a constructor argument; attaching it to the server
    is the idiomatic place, and the lifecycle guarantees it is set before the
    socket can accept anything.
    """

    server_version = "AgentAlfredDashboard/0.1"
    # HTTP/1.1 so the SSE response can be a plain stream; every other
    # response therefore has to carry a Content-Length, which _send does.
    protocol_version = "HTTP/1.1"

    @property
    def _context(self) -> HandlerContext:
        return self.server.context  # type: ignore[attr-defined]

    # -- plumbing ----------------------------------------------------------

    def log_message(self, *args: object) -> None:
        """Silence the default stderr log.

        It writes one line per request from a handler thread, which is noise
        in a terminal that is already running the CLI.
        """

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Keep parser failures inside the dashboard response contract.

        ``BaseHTTPRequestHandler`` calls this before ``command`` and ``path``
        necessarily exist.  Those failures cannot pass through the request
        guard, but they still need the headers promised for every response.
        """
        if code == 501 and getattr(self, "command", None):
            self._handle(self.command)
            return
        del message, explain
        self.close_connection = True
        self._send(
            code,
            {"code": "bad_request"},
            extra=(("Connection", "close"),),
        )

    def _send(
        self,
        status: int,
        payload: Any = None,
        *,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        body = b"" if payload is None else _dump(payload)
        if getattr(self, "_request_body_pending", False):
            self.close_connection = True
            if not any(name.lower() == "connection" for name, _value in extra):
                extra = (*extra, ("Connection", "close"))
        self.send_response(status)
        for name, value in BASE_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def _reject(self, rejection) -> None:
        self._send(
            rejection.status, {"code": rejection.code, "detail": rejection.detail}
        )

    def _params(self) -> dict[str, str]:
        query = urlsplit(self.path).query
        return {
            key: values[0]
            for key, values in parse_qs(query, keep_blank_values=True).items()
            if values
        }

    def _read_body(self, length: int) -> tuple[dict[str, Any] | None, str | None]:
        body = self.rfile.read(length)
        self._request_body_pending = False
        if len(body) != length:
            return None, "body_not_json"
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError, UnicodeDecodeError:
            return None, "body_not_json"
        if not isinstance(payload, dict):
            return None, "body_not_object"
        return payload, None

    # -- verbs -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server's own spelling
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        """No preflight is ever answered.

        A cross-origin write needs a preflight to succeed, and succeeding is
        precisely what must not happen. Refused explicitly rather than left
        to fall through, so the answer is a decision and not an accident of
        ``http.server``.
        """
        self._handle("OPTIONS")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_TRACE(self) -> None:  # noqa: N802
        self._handle("TRACE")

    def do_CONNECT(self) -> None:  # noqa: N802
        self._handle("CONNECT")

    def _handle(self, method: str) -> None:
        context = self._context
        authorization = context.guard.authorize(method=method, headers=self.headers)
        self._request_body_pending = authorization.body_declared
        if isinstance(authorization, Rejection):
            self._reject(authorization)
            return
        stream = method == "GET" and urlsplit(self.path).path == EVENTS_PATH
        registration = object()
        # Ownership starts before begin_request: its effect may succeed and
        # then be interrupted before the call returns to this frame.
        try:
            if not context.begin_request(registration, stream=stream):
                self.close_connection = True
                self._send(503, {"code": "shutting_down"})
                return
            self._handle_admitted(method, authorization)
        finally:
            context.end_request(registration)

    def _handle_admitted(self, method: str, authorization: AuthorizedRequest) -> None:
        if method == "OPTIONS":
            self._send(405, {"code": "no_preflight"})
            return
        path = urlsplit(self.path).path
        if path == EVENTS_PATH and method == "GET":
            self._serve_events()
            return
        if path == EVENTS_PATH and method == "HEAD":
            self._serve_events_head()
            return
        try:
            if method in {"GET", "HEAD"}:
                self._route_get(path)
            else:
                self._route_write(method, path, authorization)
        except Exception:  # noqa: BLE001 - see below
            # An unhandled error in one request must not take the process
            # down or leak a traceback into a browser. 500 is the honest
            # answer: something went wrong here, and nothing about what.
            self._send(500, {"code": "internal_error"})

    def _route_get(self, path: str) -> None:
        asset = page_asset(path)
        if asset is not None:
            body, content_type = asset
            if getattr(self, "_request_body_pending", False):
                self.close_connection = True
            self.send_response(200)
            if getattr(self, "_request_body_pending", False):
                self.send_header("Connection", "close")
            for name, value in BASE_HEADERS:
                if name == "Content-Security-Policy":
                    value = PAGE_POLICY
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        context = self._context
        api = context.api
        params = self._params()
        if path in ACCOUNTING_READS:
            status, payload = api.accounting_read(path, params)
            self._send(status, payload)
            return
        if path == "/api/run-evidence":
            status, payload = api.run_evidence(params)
            self._send(status, payload)
            return
        if path == ENTRY_PATH:
            # The bootstrap: the instance and the process CSRF token. A
            # cross-origin page cannot read this -- we never send
            # Access-Control-Allow-Origin -- which is exactly why it is safe
            # to hand the token out on a GET.
            self._send(
                200,
                {
                    "instance_id": context.instance_id,
                    "csrf_token": context.guard.csrf_token,
                },
            )
            return
        if path == SESSIONS_PATH:
            status, payload = api.session_inbox(params)
            self._send(status, payload)
            return
        if path == REPLY_PATH:
            status, payload = api.recover_reply(params)
            self._send(status, payload)
            return
        if path == SESSION_MESSAGES_PATH:
            # A historic session_id is an opaque value (ADR-0027): it may
            # contain "/", "?", "#", "%" or anything else, so it is carried
            # as one query parameter -- ``parse_qs`` with
            # ``keep_blank_values`` percent-decodes it exactly once -- and
            # passed on verbatim. Absent and empty are different facts: the
            # empty string is a value the database may legitimately hold, so
            # only a missing parameter is a bad request.
            if "session_id" not in params:
                self._send(400, {"code": "missing_session_id"})
                return
            status, payload = api.session_messages(params["session_id"], params)
            self._send(status, payload)
            return
        if path == SESSION_RUNS_PATH:
            status, payload = api.session_runs(params)
            self._send(status, payload)
            return
        if path == RUNS_PATH:
            status, payload = api.runs_page(params)
            self._send(status, payload)
            return
        if path.startswith(RUNS_PATH + "/locate/"):
            # The opaque identifier is the entire suffix, decoded once;
            # encoded separators are data, not further routing structure.
            run_id = unquote(path[len(RUNS_PATH) + len("/locate/") :])
            status, payload = api.locate_run(run_id, params)
            self._send(status, payload)
            return
        if path == MAINBAR_PATH:
            status, payload = api.mainbar(params)
            self._send(status, payload)
            return
        if path in {"/api/memory/consolidation", "/api/memory/mirrors"}:
            status, payload = api.memory_read(params, mirrors=path.endswith("/mirrors"))
            self._send(status, payload)
            return
        if path in MEMORY_PAGE_READS:
            status, payload = api.memory_page_read(path, params)
            self._send(status, payload)
            return
        if path == "/api/connections/mcp/operation":
            status, payload = api.mcp_operation(params)
            self._send(status, payload)
            return
        if path == CONNECTIONS_PATH:
            status, payload = api.connections()
            self._send(status, payload)
            return
        if path == "/api/behaviour":
            status, payload = api.behaviour()
            self._send(status, payload)
            return
        if path == MODELS_PATH:
            status, payload = api.models(params)
            self._send(status, payload)
            return
        if path == "/api/database" or path.startswith("/api/database/"):
            self._database_call("GET", path, None)
            return
        self._send(404, {"code": "not_found"})

    def _route_write(
        self, method: str, path: str, authorization: AuthorizedRequest
    ) -> None:
        context = self._context
        if method != "POST":
            self._send(405, {"code": "method_not_allowed"})
            return
        if path in ACCOUNTING_WRITES:
            body, error = self._read_body(authorization.body_length)
            if error:
                self._send(400, {"error": {"code": error}})
                return
            status, payload = context.api.accounting_write(path, body)
            self._send(status, payload)
            return
        if path == "/api/memory/consolidation/actions":
            assert authorization.body_length is not None
            body, error = self._read_body(authorization.body_length)
            if error is not None:
                self._send(400, {"code": error})
                return
            status, payload = context.api.memory_action(body)
            self._send(status, payload)
            return
        if path in MEMORY_PAGE_WRITES:
            assert authorization.body_length is not None
            body, error = self._read_body(authorization.body_length)
            if error is not None:
                self._send(400, {"error": {"code": error}})
                return
            status, payload = context.api.memory_page_write(path, body)
            self._send(status, payload)
            return
        if path == SESSIONS_PATH:
            assert authorization.body_length is not None
            body, error = self._read_body(authorization.body_length)
            if error is not None:
                self._send(400, {"code": error})
                return
            assert body is not None
            if body:
                self._send(400, {"code": "unexpected_fields"})
                return
            result = context.api.create_session()
            if result.session_id is None:
                # Refused rather than queued. The API distinguishes a held
                # gate (409) from recording-closed admission (503).
                self._send(result.status, {"code": result.code})
                return
            self._send(result.status, {"session_id": result.session_id})
            return
        if path == RUNS_PATH:
            assert authorization.body_length is not None
            body, error = self._read_body(authorization.body_length)
            if error is not None:
                self._send(400, {"code": error})
                return
            assert body is not None
            outcome = context.api.submit(body)
            self._send(outcome.status, outcome.payload())
            return
        if path in (SETTINGS_PATH, "/api/behaviour"):
            assert authorization.body_length is not None
            body, error = self._read_body(authorization.body_length)
            if error is not None:
                self._send(400, {"code": error})
                return
            assert body is not None
            status, payload = (
                context.api.mutate_behaviour(body)
                if path == "/api/behaviour"
                else context.api.mutate_settings(body)
            )
            self._send(status, payload)
            return
        if path == REREAD_ENV_PATH:
            assert authorization.body_length is not None
            body, error = self._read_body(authorization.body_length)
            if error is not None:
                self._send(400, {"code": error})
                return
            assert body is not None
            if body:
                self._send(400, {"code": "unexpected_fields"})
                return
            status, payload = context.api.reread_env()
            self._send(status, payload)
            return
        if path == "/api/database" or path.startswith("/api/database/"):
            assert authorization.body_length is not None
            self._database_call("POST", path, authorization.body_length)
            return
        if path in (AUTH_PROBE_PATH, "/api/connections/mcp"):
            assert authorization.body_length is not None
            body, error = self._read_body(authorization.body_length)
            if error is not None:
                self._send(400, {"code": error})
                return
            assert body is not None
            status, payload = (
                context.api.probe_auth(body)
                if path == AUTH_PROBE_PATH
                else context.api.mcp_control(body)
            )
            self._send(status, payload)
            return
        self._send(404, {"code": "not_found"})

    def _database_call(self, method: str, path: str, body_length: int | None) -> None:
        context = self._context
        sock = self.connection
        previous = sock.gettimeout()
        clock = time.monotonic
        read_deadline = clock() + HTTP_IO_S
        lease = None
        delivered = False
        sending = False
        database = context.database
        try:
            body = None
            if method == "POST":
                assert body_length is not None
                body, error = self._read_limited(body_length, read_deadline, clock)
                if error is not None:
                    sending = True
                    self._send_limited(
                        400,
                        {"code": error, "detail": "请求不被接受。"},
                        clock() + HTTP_IO_S,
                        clock,
                    )
                    return
            status, payload = database_api.dispatch(database, method, path, body)
            raw = None if payload is None else _dump(payload)
            send_lease = None
            if (
                method == "POST"
                and path.endswith("/execute")
                and status == 200
                and isinstance(payload, dict)
                and payload.get("query_id")
                and database is not None
            ):
                lease = database.begin_send(payload["query_id"], raw)
                lease.sock = sock
                if lease.cancel.is_set():
                    status = 409
                    payload = {
                        "code": "data_invalidated",
                        "detail": SAFE_DETAIL["data_invalidated"],
                        "query_id": payload["query_id"],
                    }
                    raw = _dump(payload)
                else:
                    send_lease = lease
            send_deadline = clock() + HTTP_IO_S
            sending = True
            self._send_limited(
                status, payload, send_deadline, clock, raw=raw, lease=send_lease
            )
            delivered = True
        except TimeoutError, socket.timeout:
            self.close_connection = True
            if sending:
                # A partial response cannot be followed by a second HTTP
                # response or a fresh IO budget on the same blocked socket.
                return
            error_deadline = clock() + HTTP_IO_S
            try:
                self._send_limited(
                    503,
                    {
                        "code": "resource_limit",
                        "detail": SAFE_DETAIL["resource_limit"],
                    },
                    error_deadline,
                    clock,
                )
            except TimeoutError, OSError, socket.timeout:
                pass
        finally:
            # Keep response ownership through encoding and socket handoff, then
            # drop the actual body references before releasing the cleanup gate.
            payload = raw = body = send_lease = None
            if lease is not None and database is not None:
                database.finish_send(lease, delivered=delivered)
            lease = None
            if database is not None and method == "POST" and path.endswith("/execute"):
                from urllib.parse import unquote

                database.release_response(unquote(path.split("/")[-2]))
            try:
                sock.settimeout(previous)
            except OSError:
                pass

    def _read_limited(self, length: int, deadline: float, clock) -> tuple:
        chunks = bytearray()
        while len(chunks) < length:
            left = deadline - clock()
            if left <= 0:
                raise TimeoutError
            self.connection.settimeout(left)
            remaining = length - len(chunks)
            read1 = getattr(self.rfile, "read1", None)
            if read1 is not None:
                piece = read1(min(remaining, 65536))
            else:
                piece = self.rfile.read(min(remaining, 1))
            if not piece:
                self._request_body_pending = False
                return None, "body_not_json"
            chunks.extend(piece)
        self._request_body_pending = False
        try:
            payload = json.loads(bytes(chunks).decode("utf-8"))
        except ValueError, UnicodeDecodeError:
            return None, "body_not_json"
        if not isinstance(payload, dict):
            return None, "body_not_object"
        return payload, None

    def _send_limited(
        self,
        status: int,
        payload: Any,
        deadline: float,
        clock,
        *,
        raw: bytes | None = None,
        lease=None,
    ) -> None:
        if lease is not None and lease.cancel.is_set() and status == 200:
            self.close_connection = True
            raise TimeoutError
        body = (
            b""
            if payload is None and raw is None
            else raw
            if raw is not None
            else _dump(payload)
        )
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError
        self.connection.settimeout(left)
        extra: tuple[tuple[str, str], ...] = ()
        if getattr(self, "_request_body_pending", False):
            self.close_connection = True
            extra = (("Connection", "close"),)
        self.send_response(status)
        for name, value in BASE_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        if payload is not None or raw is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and getattr(self, "command", None) != "HEAD":
            self._write_limited(body, deadline, clock, lease)

    def _write_limited(self, body: bytes, deadline: float, clock, lease) -> None:
        view = memoryview(body)
        offset = 0
        while offset < len(view):
            if lease is not None and lease.cancel.is_set():
                self.close_connection = True
                raise TimeoutError
            left = deadline - clock()
            if left <= 0:
                raise TimeoutError
            self.connection.settimeout(left)
            written = self.wfile.write(view[offset : offset + 65536])
            if not written:
                raise TimeoutError
            offset += written
            self.wfile.flush()

    # -- the stream --------------------------------------------------------

    def _serve_events_head(self) -> None:
        """Return the stream's current metadata without owning a stream."""
        context = self._context
        session_id = self._params().get("session_id")
        try:
            context.broker.preflight_session(session_id)
        except RecordingUnavailable:
            self._send(503, {"code": "recording_unavailable"})
            return
        except Exception:  # noqa: BLE001 - no detail crosses the HTTP boundary
            self._send(500, {"code": "internal_error"})
            return

        body_pending = getattr(self, "_request_body_pending", False)
        if body_pending:
            self.close_connection = True
        self.send_response(200)
        for name, value in BASE_HEADERS:
            self.send_header(name, value)
        for name, value in SSE_HEADERS:
            if body_pending and name.lower() == "connection":
                continue
            self.send_header(name, value)
        if body_pending:
            self.send_header("Connection", "close")
        self.end_headers()

    def _serve_events(self) -> None:
        """Hand the socket to the broker and wait for the writer to end.

        The handler thread does no writing: from here on the socket has
        exactly one owner, the connection's writer thread. This thread only
        holds the HTTP response open, and returns when the writer has closed
        the peer -- which is the only way a stream ends.
        """
        context = self._context
        session_id = self._params().get("session_id")
        connection = SocketConnection(self.connection, self.wfile)
        cursor = self.headers.get("last-event-id")
        proof_owner = ResumableRollback()
        try:
            proof = context.broker.prepare_stream(
                connection=connection,
                cursor=CursorText(cursor) if cursor is not None else None,
                session_id=session_id,
                _rollback=proof_owner,
            )
        except RecordingUnavailable as exc:
            if not proof_owner.retry():
                proof_owner.raise_incomplete(exc)
            self._send(503, {"code": "recording_unavailable"})
            return
        except StreamAdmissionRejected as rejection:
            if not proof_owner.retry():
                proof_owner.raise_incomplete(rejection)
            self._send(rejection.status, {"code": rejection.code})
            return
        except Exception as exc:  # noqa: BLE001 - no detail crosses the HTTP boundary
            if not proof_owner.retry():
                proof_owner.raise_incomplete(exc)
            self._send(500, {"code": "internal_error"})
            return
        except BaseException as exc:
            proof_owner.raise_failure(exc)
        try:
            self.send_response(200)
            for name, value in BASE_HEADERS:
                self.send_header(name, value)
            for name, value in SSE_HEADERS:
                self.send_header(name, value)
            self.end_headers()
        except BaseException as exc:
            if not proof_owner.retry():
                proof_owner.raise_incomplete(exc)
            self.close_connection = True
            return
        try:
            handle = context.broker.start_stream(proof)
            proof_owner.transfer(proof)
        except BaseException as exc:
            # ``start_stream`` revokes the proof if spawning fails. A second
            # response is impossible after 200; closing is the only honest
            # outcome, and no reservation or registration survives it.
            handle = proof.transferred_handle()
            if not proof_owner.retry():
                proof_owner.raise_incomplete(exc)
            if handle is None:
                self.close_connection = True
                return
        # The writer closes the socket; this thread must not touch it again,
        # so it only waits for the writer to say it is done.
        handle.finished.wait()
        self.close_connection = True


def _dump(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
