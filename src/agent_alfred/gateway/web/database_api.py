"""HTTP mapping for the Database console. No sockets."""

from __future__ import annotations

from urllib.parse import unquote

from agent_alfred.database_console.errors import HTTP_STATUS, SAFE_DETAIL, ConsoleError


def dispatch(console, method: str, path: str, body: dict | None) -> tuple[int, dict]:
    try:
        if console is None:
            raise ConsoleError("database_unavailable")
        if method == "GET" and path == "/api/database":
            return console.catalog()
        if method == "POST" and path == "/api/database/queries":
            return console.issue(body)
        prefix = "/api/database/queries/"
        if not path.startswith(prefix):
            return 404, {"code": "not_found", "detail": SAFE_DETAIL["not_found"]}
        rest = path[len(prefix) :]
        if rest.endswith("/execute"):
            query_id = unquote(rest[: -len("/execute")])
            if method != "POST":
                return 405, {"code": "method_not_allowed", "detail": "请求不被接受。"}
            return console.execute(query_id, body or {})
        if rest.endswith("/cancel"):
            query_id = unquote(rest[: -len("/cancel")])
            if method != "POST":
                return 405, {"code": "method_not_allowed", "detail": "请求不被接受。"}
            return console.cancel(query_id, body)
        query_id = unquote(rest)
        if "/" in rest or method != "GET":
            return 404, {"code": "not_found", "detail": SAFE_DETAIL["not_found"]}
        return console.status(query_id)
    except ConsoleError as exc:
        return exc.http()


__all__ = ["HTTP_STATUS", "dispatch"]
