"""Static response completion through controlled HTTP request/response objects."""

from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pytest

from agent_alfred.gateway.web.assets import PAGE_POLICY
from agent_alfred.gateway.web.guard import RequestGuard
from agent_alfred.gateway.web.handler import DashboardHandler, HandlerContext


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", ["/", "/assets/app.js"])
@pytest.mark.parametrize("pending", [False, True])
def test_static_response_preserves_headers_and_finishes_pending_body(
    method, path, pending,
):
    handler = object.__new__(DashboardHandler)
    handler.server = SimpleNamespace(context=HandlerContext(
        guard=RequestGuard(port=17736, csrf_token="offline-test-token"),
        api=None, broker=None, instance_id="offline-test",
    ))
    handler.command = method
    handler.path = path
    handler.headers = Message()
    handler.headers["Host"] = "localhost:17736"
    if pending:
        handler.headers["Content-Length"] = "1"
    handler.rfile = BytesIO(b"x" if pending else b"")
    handler.wfile = BytesIO()
    handler.close_connection = False
    statuses, headers = [], {}
    handler.send_response = statuses.append
    handler.send_header = lambda name, value: headers.update({name: value})
    handler.end_headers = lambda: None
    getattr(handler, f"do_{method}")()
    assert statuses == [200]
    assert handler.close_connection is pending
    assert headers.get("Connection") == ("close" if pending else None)
    assert headers["Content-Security-Policy"] == PAGE_POLICY
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Type"] == (
        "text/html; charset=utf-8" if path == "/"
        else "text/javascript; charset=utf-8"
    )
    assert int(headers["Content-Length"]) > 0
    body = handler.wfile.getvalue()
    if method == "HEAD":
        assert body == b""
    if method == "GET":
        assert len(body) == int(headers["Content-Length"])
    assert handler.rfile.tell() == 0
