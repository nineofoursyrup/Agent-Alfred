"""Independent review counterexamples through real SQLite/HTTP/workers."""

import json

import pytest

from agent_alfred.evals.deterministic.test_database_http import (
    _catalog,
    _dashboard,
    _execute,
    _write,
)
from agent_alfred.runtime.work import SubmitRequest


def test_catalog_source_strings_are_protected_and_versioned(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        original = _catalog(dashboard)
        assert original["available"] and original["schema"]["migrations"]
        secret = "sensitive-migration-fixture"
        dashboard.host._redactor.remember(secret)
        _write(
            dashboard.host,
            lambda c: c.execute("UPDATE schema_migrations SET applied_at=?", (secret,)),
        )
        body = _catalog(dashboard)
        assert body["available"] and body["schema"]["compatible"]
        assert secret not in json.dumps(body)
        assert {m["applied_at"] for m in body["schema"]["migrations"]} == {"***"}
        assert body["protection_version"] == dashboard.host._redactor.protection_version
    finally:
        assert dashboard.close()


@pytest.mark.parametrize(
    "block",
    [
        {"type": "tool_call", "id": 4, "name": [], "input": {}},
        {"type": "tool_call", "id": "c", "name": [], "input": {}},
        {"type": "thinking", "text": "valid", "signature": 17},
        {"type": "tool_result", "call_id": [], "content": []},
    ],
)
def test_known_excluded_blocks_cannot_hide_corruption(tmp_path, block):
    dashboard = _dashboard(tmp_path)
    try:
        dashboard.host.wait(
            dashboard.host.submit(SubmitRequest(message="hello")).run_id
        )
        _write(
            dashboard.host,
            lambda c: c.execute(
                "UPDATE agent_log SET content=? WHERE role='user'",
                (json.dumps([block]),),
            ),
        )
        for sql in [
            "SELECT role,text FROM diag_messages",
            "SELECT text FROM diag_messages WHERE role='assistant' LIMIT 1",
            "SELECT text FROM diag_messages WHERE 0 LIMIT 0",
        ]:
            status, body, _ = _execute(dashboard, sql)
            assert status == 503 and body["code"] == "data_invalid", (sql, body)
        status, body, _ = _execute(dashboard, "SELECT count(*) FROM diag_sessions")
        assert status == 200, body
    finally:
        assert dashboard.close()


@pytest.mark.parametrize(
    "block",
    [
        {"type": "thinking", "text": "private"},
        {"type": "thinking", "text": "private", "signature": None},
        {"type": "thinking", "text": "private", "signature": "signature"},
        {"type": "tool_call", "id": "c", "name": "tool", "input": {}},
        {"type": "tool_result", "call_id": "c", "content": []},
        {
            "type": "tool_result",
            "call_id": "c",
            "content": [{"type": "text", "text": "private"}],
        },
    ],
)
def test_legal_nontext_blocks_and_optional_defaults_remain_empty(tmp_path, block):
    dashboard = _dashboard(tmp_path)
    try:
        dashboard.host.wait(
            dashboard.host.submit(SubmitRequest(message="hello")).run_id
        )
        _write(
            dashboard.host,
            lambda c: c.execute(
                "UPDATE agent_log SET content=? WHERE role='user'",
                (json.dumps([block]),),
            ),
        )
        status, body, _ = _execute(
            dashboard, "SELECT text FROM diag_messages WHERE role='user'"
        )
        assert status == 200, body
        assert body["rows"] == [[{"type": "text", "value": ""}]]
    finally:
        assert dashboard.close()


@pytest.mark.parametrize(
    ("sql", "cell"),
    [
        ("SELECT CAST('12.5' AS DECIMAL(10,2))", {"type": "real", "value": 12.5}),
        ("SELECT CAST(12 AS VARCHAR(20))", {"type": "text", "value": "12"}),
        (
            "SELECT CAST(CAST('12.5' AS DECIMAL(10,2)) AS VARCHAR(20))",
            {"type": "text", "value": "12.5"},
        ),
    ],
)
def test_cast_type_parameters_are_not_function_calls(tmp_path, sql, cell):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _ = _execute(dashboard, sql)
        assert status == 200, body
        assert body["rows"] == [[cell]]
    finally:
        assert dashboard.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CAST(random() AS DECIMAL(10,2))",
        "SELECT CAST(json_valid('{}',2) AS VARCHAR(20))",
        "SELECT CAST(version AS VARCHAR(20)) FROM main.schema_migrations",
        "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) "
        "SELECT CAST(n AS DECIMAL(10,2)) FROM x",
    ],
)
def test_cast_does_not_open_closed_sql_contract(tmp_path, sql):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _ = _execute(dashboard, sql)
        assert status == 400 and body["code"] == "sql_rejected", body
    finally:
        assert dashboard.close()


def test_slow_tcp_receiver_releases_response_within_total_send_deadline(
    tmp_path, monkeypatch
):
    import socket
    import threading
    import time

    from agent_alfred.evals.deterministic.test_database_http import _post
    from agent_alfred.gateway.web.handler import DashboardHandler as Handler

    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    started, released = threading.Event(), threading.Event()
    observations = {}
    original_write = Handler._write_limited
    original_release = console.release_response

    def write(handler, body, deadline, clock, lease):
        if lease is not None:
            # Real TCP backpressure; no replacement of socket writes/results.
            handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            observations.update(
                start=time.monotonic(), deadline=deadline, bytes=len(body), lease=lease
            )
            started.set()
        try:
            return original_write(handler, body, deadline, clock, lease)
        except TimeoutError:
            observations["timeout_at"] = time.monotonic()
            raise

    def release(query_id):
        original_release(query_id)
        observations["released_at"] = time.monotonic()
        released.set()

    monkeypatch.setattr(Handler, "_write_limited", write)
    console.release_response = release
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    sock.settimeout(4)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        # Allowed SQL below 64KiB creates a 1.6MiB result, much larger than
        # the receiver's actual TCP window and the sender's socket buffer.
        raw = {
            "instance_id": catalog["instance_id"],
            "memory_revision": catalog["memory_revision"],
            "protection_version": catalog["protection_version"],
            "sql": "SELECT replace(hex(X'"
            + ("00" * 20000)
            + "'), '0', '"
            + ("x" * 40)
            + "')",
        }
        payload = json.dumps(raw).encode()
        header = (
            f"POST /api/database/queries/{query_id}/execute HTTP/1.1\r\n"
            f"Host: localhost:{dashboard.port}\r\n"
            f"Origin: http://localhost:{dashboard.port}\r\n"
            f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        sock.connect(("127.0.0.1", dashboard.port))
        sock.sendall(header + payload)
        assert started.wait(3), observations
        assert not console.released() and console._sends
        # Withhold body reads until actual cleanup. The peer's small receive
        # window fills, so the real sendall must reach its own deadline.
        assert released.wait(3), observations
        elapsed = observations["released_at"] - observations["start"]
        lease = observations.pop("lease")
        print(
            "slow_tcp_receiver",
            json.dumps(observations),
            "elapsed",
            elapsed,
            flush=True,
        )
        assert 0.8 <= observations["timeout_at"] - observations["start"] <= 1.5
        assert elapsed <= 1.5, "response owner survived the one-second send deadline"
        assert lease.done.is_set() and lease.body == b""
        assert console.released() and not console._sends and not console._send_buffers
        record = console._records[query_id]
        assert record.process is None and record.response_owner is None
        assert record.result is None and record.cleanup == "released"
        assert record.result_delivered is False
        wire = bytearray()
        while chunk := sock.recv(65536):
            wire.extend(chunk)
        head, _, received = wire.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")
        assert 0 < len(received) < observations["bytes"]
        assert b"HTTP/1.1 503" not in received
        status, body, _ = _execute(dashboard, "SELECT 1")
        assert status == 200, body
    finally:
        sock.close()
        assert dashboard.close()


def test_catalog_rejects_damaged_metadata_without_echo(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        _write(
            dashboard.host,
            lambda c: c.execute(
                "UPDATE schema_migrations SET applied_at=?", (b"broken-secret-source",)
            ),
        )
        body = _catalog(dashboard)
        assert body["available"] is False
        assert body["schema"] == {"compatible": False, "migrations": []}
        assert "broken-secret-source" not in json.dumps(body)
    finally:
        assert dashboard.close()


def test_catalog_drops_old_protection_version_before_publication(tmp_path):
    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    original = console._capability
    secret = "newly-loaded-migration-fixture"
    try:
        _write(
            dashboard.host,
            lambda c: c.execute("UPDATE schema_migrations SET applied_at=?", (secret,)),
        )

        def change_before_publish():
            result = original()
            console._capability = original
            dashboard.host._redactor.remember(secret)
            return result

        console._capability = change_before_publish
        body = _catalog(dashboard)
        assert not body["available"]
        assert body["schema"] == {"compatible": False, "migrations": []}
        assert secret not in json.dumps(body)
        current = _catalog(dashboard)
        assert current["available"]
        assert current["protection_version"] == console.protection_version()
        assert secret not in json.dumps(current)
    finally:
        console._capability = original
        assert dashboard.close()


def test_source_revision_is_numeric_metadata_not_an_open_string(tmp_path):
    from agent_alfred.evals.deterministic.test_web_http import _get, _request

    dashboard = _dashboard(tmp_path)
    try:
        secret = "damaged-revision-fixture"
        _write(
            dashboard.host,
            lambda c: c.execute("UPDATE memory_revision SET revision=?", (secret,)),
        )
        head, wire = _request(dashboard.port, _get(dashboard.port, "/api/database"))
        assert head.startswith(b"HTTP/1.1 503")
        assert secret.encode() not in wire
        assert json.loads(wire)["code"] == "database_unavailable"
    finally:
        assert dashboard.close()
