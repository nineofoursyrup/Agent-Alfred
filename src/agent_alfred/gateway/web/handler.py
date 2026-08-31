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
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.connection import SocketConnection
from agent_alfred.gateway.web.guard import (
    AuthorizedRequest,
    Rejection,
    RequestGuard,
)
from agent_alfred.gateway.web.replay import CursorText

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
    ):
        self.guard = guard
        self.api = api
        self.broker = broker
        self.instance_id = instance_id


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

    def _send(
        self,
        status: int,
        payload: Any = None,
        *,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> None:
        body = b"" if payload is None else _dump(payload)
        self.send_response(status)
        for name, value in BASE_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
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
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
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

    def _handle(self, method: str) -> None:
        context = self._context
        authorization = context.guard.authorize(method=method, headers=self.headers)
        if isinstance(authorization, Rejection):
            self._reject(authorization)
            return
        if method == "OPTIONS":
            self._send(405, {"code": "no_preflight"})
            return
        path = urlsplit(self.path).path
        if path == EVENTS_PATH and method == "GET":
            self._serve_events()
            return
        try:
            if method == "GET":
                self._route_get(path)
            else:
                self._route_write(path, authorization)
        except Exception:  # noqa: BLE001 - see below
            # An unhandled error in one request must not take the process
            # down or leak a traceback into a browser. 500 is the honest
            # answer: something went wrong here, and nothing about what.
            self._send(500, {"code": "internal_error"})

    def _route_get(self, path: str) -> None:
        context = self._context
        api = context.api
        params = self._params()
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
            # Same rule as the messages read: the Session rides the query
            # string verbatim, and only its absence is a bad request.
            if "session_id" not in params:
                self._send(400, {"code": "missing_session_id"})
                return
            status, payload = api.session_runs(params)
            self._send(status, payload)
            return
        if path == RUNS_PATH:
            status, payload = api.runs_page(params)
            self._send(status, payload)
            return
        if path.startswith(RUNS_PATH + "/locate/"):
            run_id = path[len(RUNS_PATH) + len("/locate/") :]
            status, payload = api.locate_run(run_id, params)
            self._send(status, payload)
            return
        if path == MAINBAR_PATH:
            # Same rule as the messages read: the session_id rides the query
            # string verbatim, and only its absence is a bad request.
            if "session_id" not in params:
                self._send(400, {"code": "missing_session_id"})
                return
            status, payload = api.mainbar(params)
            self._send(status, payload)
            return
        self._send(404, {"code": "not_found"})

    def _route_write(self, path: str, authorization: AuthorizedRequest) -> None:
        context = self._context
        if path == SESSIONS_PATH:
            result = context.api.create_session()
            if result.session_id is None:
                # The write gate is busy. Refused rather than queued, so the
                # browser knows to try again instead of waiting on a
                # connection that will never answer. The status is the
                # gate's -- 409, not 503: the process is busy, not broken.
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
        self._send(404, {"code": "not_found"})

    # -- the stream --------------------------------------------------------

    def _serve_events(self) -> None:
        """Hand the socket to the broker and wait for the writer to end.

        The handler thread does no writing: from here on the socket has
        exactly one owner, the connection's writer thread. This thread only
        holds the HTTP response open, and returns when the writer has closed
        the peer -- which is the only way a stream ends.
        """
        context = self._context
        self.send_response(200)
        for name, value in BASE_HEADERS:
            self.send_header(name, value)
        for name, value in SSE_HEADERS:
            self.send_header(name, value)
        self.end_headers()
        connection = SocketConnection(self.connection, self.wfile)
        cursor = self.headers.get("last-event-id")
        session_id = self._params().get("session_id")
        handle = context.broker.connect(
            connection=connection,
            cursor=CursorText(cursor) if cursor else None,
            session_id=session_id,
        )
        # The writer closes the socket; this thread must not touch it again,
        # so it only waits for the writer to say it is done.
        handle.finished.wait()
        self.close_connection = True


def _dump(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
