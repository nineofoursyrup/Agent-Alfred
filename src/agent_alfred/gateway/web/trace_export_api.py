"""Trace export HTTP framing; the Host owns all files and task decisions."""

import hmac
import select
from urllib.parse import parse_qs

from agent_alfred.gateway.web.guard import Rejection
from agent_alfred.trace_export.errors import ExportError

PREFIX = "/api/trace-exports"


def native_authorize(handler):
    """Native POST form uses the same Host/Origin/CSRF decision as JSON writes."""
    guard = handler._context.guard
    preflight = guard.authorize(method="GET", headers=handler.headers)
    if isinstance(preflight, Rejection):
        handler._reject(preflight)
        return False
    length = int(handler.headers.get("Content-Length", "0"))
    if length is None or not 0 < length <= 4096:
        handler._send(400, {"code": "invalid_request"})
        return False
    if handler.headers.get("Origin") not in guard.allowed_origins:
        handler._send(403, {"code": "origin_not_allowed"})
        return False
    handler.connection.settimeout(5)
    raw = handler.rfile.read(length)
    handler._request_body_pending = False
    try:
        parsed = parse_qs(raw.decode("utf-8"), strict_parsing=True, max_num_fields=4)
        if set(parsed) != {"task_id", "download_token", "csrf", "instance_id"} or any(
            len(v) != 1 for v in parsed.values()
        ):
            raise ValueError()
        body = {k: v[0] for k, v in parsed.items()}
        if not hmac.compare_digest(body.pop("csrf"), guard.csrf_token):
            handler._send(403, {"code": "csrf_invalid"})
            return False
    except ValueError, UnicodeError:
        handler._send(400, {"code": "invalid_request"})
        return False
    headers = dict(handler.headers)
    headers["Content-Type"] = "application/json"
    headers["x-agent-alfred-csrf"] = guard.csrf_token
    authorization = guard.authorize(method="POST", headers=headers)
    if isinstance(authorization, Rejection):
        handler._reject(authorization)
        return False
    dispatch(handler, "POST", PREFIX + "/download", body)
    return True


def dispatch(handler, method, path, body=None):
    from agent_alfred.gateway.web.handler import BASE_HEADERS

    service = handler._context.trace_exports
    started = False
    try:
        if service is None:
            raise ExportError("shutting_down")
        if path == PREFIX and method == "POST":
            if not isinstance(body, dict) or set(body) != {
                "run_id",
                "mode",
                "instance_id",
            }:
                raise ExportError("invalid_request")
            handler._send(202, service.start(**body))
        elif path == PREFIX + "/download" and method == "POST":
            if not isinstance(body, dict) or set(body) != {
                "task_id",
                "download_token",
                "instance_id",
            }:
                raise ExportError("invalid_request")
            if body["instance_id"] != service.instance:
                raise ExportError("credential_invalid")
            if handler.headers.get("Range") is not None:
                raise ExportError("invalid_request")
            handler.connection.setblocking(False)
            handler.close_connection = True

            def headers(size):
                nonlocal started
                started = True
                handler.send_response(200)
                for key, value in BASE_HEADERS:
                    handler.send_header(key, value)
                handler.send_header("Content-Type", "application/zip")
                handler.send_header("Content-Length", str(size))
                handler.send_header(
                    "Content-Disposition",
                    f'attachment; filename="alfred-trace-{body["task_id"]}.zip"',
                )
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Connection", "close")
                handler.end_headers()

            service.download(
                body["task_id"],
                body["download_token"],
                handler.connection.send,
                headers,
                wait_writable=lambda: select.select([], [handler.connection], [], 0.1),
            )
        elif path.startswith(PREFIX + "/"):
            rest = path[len(PREFIX) + 1 :]
            if method == "POST" and rest.endswith("/cancel"):
                handler._send(200, service.cancel(rest[:-7]))
            elif method == "GET" and "/" not in rest:
                handler._send(200, service.status(rest))
            else:
                raise ExportError("not_found")
        else:
            raise ExportError("invalid_request")
    except ExportError as exc:
        if started:
            handler.close_connection = True
        else:
            handler._send(*exc.response())
    except Exception:
        # A response already started can only terminate, never append JSON.
        if started:
            handler.close_connection = True
        else:
            handler._send(*ExportError("io_failed").response())
