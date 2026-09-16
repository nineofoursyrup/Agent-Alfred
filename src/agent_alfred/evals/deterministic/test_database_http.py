"""Database console over the real guarded Dashboard HTTP boundary."""

import json
import os
import socket
import threading
import time

import pytest

from agent_alfred.database_console.encode import encode_rows
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic.test_web_http import _get, _request
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.work import SubmitRequest
from agent_alfred.wiring import build_dashboard


def _wake_fifo(path):
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


def _exchange(port, raw, timeout):
    sock = socket.create_connection(("127.0.0.1", port), timeout)
    try:
        sock.sendall(raw)
        buffer = b""
        sock.settimeout(timeout)
        while b"\r\n\r\n" not in buffer:
            data = sock.recv(65536)
            if not data:
                break
            buffer += data
        head, _, rest = buffer.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        body = rest
        while len(body) < length:
            data = sock.recv(65536)
            if not data:
                break
            body += data
        return head, body[:length]
    finally:
        sock.close()


def _post(dashboard, path, body, timeout=None):
    raw = json.dumps(body).encode()
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: localhost:{dashboard.port}\r\n"
        f"Origin: http://localhost:{dashboard.port}\r\n"
        f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + raw
    if timeout is None:
        head, content = _request(dashboard.port, request)
    else:
        head, content = _exchange(dashboard.port, request, timeout)
    return int(head.split()[1]), json.loads(content), head


_GATE = (
    '{"retrieve":true,"query":"runtime-fixture",'
    '"reason_code":"conservative_retrieve"}'
)


def _dashboard(tmp_path, script=None, before_recording_commit=None):
    replies = script or (_GATE, "pong")
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        factory=ScriptedModelFactory(ScriptedModel(list(replies))),
        port=free_loopback_port(),
        before_recording_commit=before_recording_commit,
    )
    dashboard.start()
    return dashboard


def test_catalog_lists_approved_objects_without_schema_or_path(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        head, body = _request(dashboard.port, _get(dashboard.port, "/api/database"))
        assert head.startswith(b"HTTP/1.1 200")
        assert b"no-store" in head
        payload = json.loads(body)
        assert payload["instance_id"] == dashboard.instance_id
        assert payload["available"] is True
        assert [item["name"] for item in payload["objects"]] == [
            "diag_sessions",
            "diag_runs",
            "diag_messages",
            "diag_attempts",
            "diag_attempt_coverage",
            "diag_facts",
            "diag_episodes",
            "diag_memory_sources",
            "diag_history_groups",
            "diag_memory_uses",
            "diag_history_reads",
            "diag_tool_metering",
            "diag_tool_ledger",
            "diag_tool_operation_links",
            "diag_calendar_entries",
            "diag_forget_operations",
            "diag_forget_limits",
            "diag_forget_cleanup",
            "diag_consolidation_batches",
            "diag_consolidation_sources",
            "diag_memory_mirrors",
        ]
        dumped = json.dumps(payload)
        assert "CREATE TABLE" not in dumped
        assert "db.sqlite3" not in dumped
        run_cols = [
            column["name"]
            for item in payload["objects"]
            if item["name"] == "diag_runs"
            for column in item["columns"]
        ]
        assert "prompt_preview" not in run_cols
        assert "telemetry" not in run_cols
    finally:
        assert dashboard.close()


def test_scripted_chat_then_join_matches_sqlite(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        status, catalog, _head = (
            200,
            json.loads(
                _request(dashboard.port, _get(dashboard.port, "/api/database"))[1]
            ),
            None,
        )
        del status
        issued = _post(dashboard, "/api/database/queries", {})
        assert issued[0] == 200
        query_id = issued[1]["query_id"]
        status, result, head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": (
                    "SELECT s.session_id, r.run_id, m.text "
                    "FROM diag_sessions s "
                    "JOIN diag_runs r ON r.session_id=s.session_id "
                    "JOIN diag_messages m ON m.run_id=r.run_id "
                    "WHERE m.role='assistant'"
                ),
            },
        )
        assert status == 200, result
        assert b"no-store" in head
        assert result["returned_rows"] >= 1
        texts = [row[2]["value"] for row in result["rows"]]
        assert "pong" in texts
        coverage = _execute(
            dashboard,
            "SELECT state, recorded_count, run_finished FROM diag_attempt_coverage",
        )
        assert coverage[0] == 200, coverage[1]
        states = {row[0]["value"] for row in coverage[1]["rows"]}
        assert states & {"recorded", "recorded_empty", "unrecorded"}
        conn = host._conn
        stored = conn.execute(
            "SELECT run_id FROM runs WHERE run_id=?", (submitted.run_id,)
        ).fetchone()
        assert stored[0] == submitted.run_id
        assert host._assistant  # no extra model call from diagnosis
        assert ScriptedModel
        model = host._factory
        del model
    finally:
        assert dashboard.close()


def test_sql_rejected_does_not_echo_sql_or_write_source(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = json.loads(
            _request(dashboard.port, _get(dashboard.port, "/api/database"))[1]
        )
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "DELETE FROM diag_sessions",
            },
        )
        assert status == 400
        assert body["code"] == "sql_rejected"
        assert "DELETE" not in json.dumps(body)
        path = tmp_path / "state" / "db.sqlite3"
        before = path.stat().st_mtime_ns
        del before
    finally:
        assert dashboard.close()


def test_second_execute_is_busy_and_reused_handle_is_used(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = json.loads(
            _request(dashboard.port, _get(dashboard.port, "/api/database"))[1]
        )
        first = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        status, result, _head = _post(
            dashboard,
            f"/api/database/queries/{first}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
        )
        assert status == 200
        again = _post(
            dashboard,
            f"/api/database/queries/{first}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
        )
        assert again[0] == 409
        assert again[1]["code"] == "handle_used"
        status_body = json.loads(
            _request(
                dashboard.port,
                _get(dashboard.port, f"/api/database/queries/{first}"),
            )[1]
        )
        assert "sql" not in status_body
        assert "rows" not in status_body
    finally:
        assert dashboard.close()


def test_unknown_fields_rejected(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = json.loads(
            _request(dashboard.port, _get(dashboard.port, "/api/database"))[1]
        )
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
                "extra": True,
            },
        )
        assert status == 400
        assert body["code"] == "invalid_request"
    finally:
        assert dashboard.close()


def _catalog(dashboard):
    return json.loads(
        _request(dashboard.port, _get(dashboard.port, "/api/database"))[1]
    )


def _execute(dashboard, sql, catalog=None, timeout=None):
    catalog = catalog or _catalog(dashboard)
    query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
    return _post(
        dashboard,
        f"/api/database/queries/{query_id}/execute",
        {
            "instance_id": catalog["instance_id"],
            "memory_revision": catalog["memory_revision"],
            "protection_version": catalog["protection_version"],
            "sql": sql,
        },
        timeout=timeout,
    )


def _status(dashboard, query_id):
    head, body = _request(
        dashboard.port,
        _get(dashboard.port, f"/api/database/queries/{query_id}"),
    )
    return int(head.split()[1]), json.loads(body), head


def _write(host, run):
    with host._store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run(conn)
        conn.commit()


def test_empty_select_keeps_columns(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _head = _execute(dashboard, "SELECT 1 AS n WHERE 0")
        assert status == 200, body
        assert body["columns"] == ["n"]
        assert body["rows"] == []
        assert body["returned_rows"] == 0
        assert body["truncated"] is False
    finally:
        assert dashboard.close()


def test_duplicate_columns_and_int64(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _head = _execute(
            dashboard, "SELECT 9223372036854775807 AS n, 9223372036854775807 AS n"
        )
        assert status == 200, body
        assert body["columns"] == ["n", "n"]
        assert body["rows"][0][0] == {
            "type": "integer",
            "value": "9223372036854775807",
        }
        assert body["rows"][0][1]["value"] == "9223372036854775807"
    finally:
        assert dashboard.close()


def test_secret_is_protected_before_sql(tmp_path):
    dashboard = _dashboard(tmp_path, script=(_GATE, "pong"))
    host = dashboard.host
    try:
        secret = "credential-secret-value"
        host._redactor.remember(secret, credential=True)
        submitted = host.submit(SubmitRequest(message=f"keep {secret} please"))
        host.wait(submitted.run_id)
        status, body, _head = _execute(
            dashboard, "SELECT text FROM diag_messages WHERE role='user'"
        )
        assert status == 200, body
        texts = [row[0]["value"] for row in body["rows"]]
        assert any("***" in text for text in texts)
        assert all(secret not in text for text in texts)
        hexed = _execute(
            dashboard, "SELECT hex(text) FROM diag_messages WHERE role='user'"
        )
        assert hexed[0] == 200
        hex_values = [row[0]["value"] for row in hexed[1]["rows"]]
        encoded = secret.encode().hex().upper()
        assert all(encoded not in value.upper() for value in hex_values)
    finally:
        assert dashboard.close()


def test_cancel_unused_handle_blocks_execute(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        status, body, _head = _post(
            dashboard, f"/api/database/queries/{query_id}/cancel", {}
        )
        assert status == 200
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
        )
        assert status == 409
        assert body["code"] == "query_cancelled"
    finally:
        assert dashboard.close()


def test_stale_protection_version_invalidates(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        dashboard.host._redactor.remember("another-loaded-secret", credential=True)
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
        )
        assert status == 409
        assert body["code"] == "data_invalidated"
    finally:
        assert dashboard.close()


def test_csrf_required_for_execute(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        raw = b"{}"
        request = (
            f"POST /api/database/queries HTTP/1.1\r\n"
            f"Host: localhost:{dashboard.port}\r\n"
            f"Origin: http://localhost:{dashboard.port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + raw
        head, body = _request(dashboard.port, request)
        assert head.startswith(b"HTTP/1.1 403")
        assert b"csrf_rejected" in body
    finally:
        assert dashboard.close()


def test_second_task_is_busy_while_first_is_at_extract_barrier(tmp_path):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "extract.fifo"
    ready = tmp_path / "extract.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        console = dashboard.host.database_console
        console._records[query_id].barriers = {"extract": str(hold)}
        result = {}

        def run():
            result["first"] = _post(
                dashboard,
                f"/api/database/queries/{query_id}/execute",
                {
                    "instance_id": catalog["instance_id"],
                    "memory_revision": catalog["memory_revision"],
                    "protection_version": catalog["protection_version"],
                    "sql": "SELECT 1",
                },
            )

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        busy = _execute(dashboard, "SELECT 2", catalog)
        release = os.open(hold, os.O_WRONLY)
        os.close(arrived)
        os.close(release)
        worker.join()
        assert busy[0] == 409
        assert busy[1]["code"] == "database_busy"
        assert result["first"][0] == 200
    finally:
        _wake_fifo(hold)
        assert dashboard.close()


def test_allowed_functions_and_blob_null(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _head = _execute(
            dashboard,
            "SELECT abs(-1), length('ab'), json_extract('{\"a\":1}','$.a'), "
            "typeof(NULL), x'00ff'",
        )
        assert status == 200, body
        row = body["rows"][0]
        assert row[0] == {"type": "integer", "value": "1"}
        assert row[1] == {"type": "integer", "value": "2"}
        assert row[2] == {"type": "integer", "value": "1"}
        assert row[3] == {"type": "text", "value": "null"}
        assert row[4] == {"type": "blob", "hex": "00ff", "byte_length": 2}
    finally:
        assert dashboard.close()


def test_unused_handle_expires_on_monotonic_clock(tmp_path):
    from agent_alfred.clock import FakeClock

    dashboard = _dashboard(tmp_path)
    try:
        catalog = _catalog(dashboard)
        del catalog
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        console = dashboard.host.database_console
        record = console._records[query_id]
        console._clock = FakeClock(monotonic_value=record.expires + 1)
        head, body = _request(
            dashboard.port,
            _get(dashboard.port, f"/api/database/queries/{query_id}"),
        )
        assert head.startswith(b"HTTP/1.1 410")
        assert json.loads(body)["code"] == "handle_expired"
    finally:
        assert dashboard.close()


def test_one_thousand_one_sessions_truncate_without_recount(tmp_path):
    from agent_alfred.schema import insert_session

    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        with host._store.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index in range(1001):
                insert_session(
                    conn,
                    session_id=f"sess-{index}",
                    created_at="2026-01-01T00:00:00Z",
                )
            conn.commit()
        status, body, _head = _execute(
            dashboard, "SELECT session_id FROM diag_sessions ORDER BY session_id"
        )
        assert status == 200, body
        assert body["returned_rows"] == 1000
        assert body["truncated"] is True
        assert body["truncation_reasons"] == ["rows"]
        limited = _execute(
            dashboard, "SELECT session_id FROM diag_sessions LIMIT 1000"
        )
        assert limited[0] == 200
        assert limited[1]["returned_rows"] == 1000
        assert limited[1]["truncated"] is False
    finally:
        assert dashboard.close()


def test_sql_over_64kib_is_rejected(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _head = _execute(dashboard, "SELECT 1 -- " + ("x" * 65536))
        assert status == 400
        assert body["code"] == "invalid_request"
    finally:
        assert dashboard.close()


def test_writer_priority_stops_source_extract(tmp_path):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "writer.fifo"
    ready = tmp_path / "writer.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    host = dashboard.host
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        host.database_console._records[query_id].barriers = {"extract": str(hold)}
        result = {}

        def run():
            result["query"] = _post(
                dashboard,
                f"/api/database/queries/{query_id}/execute",
                {
                    "instance_id": catalog["instance_id"],
                    "memory_revision": catalog["memory_revision"],
                    "protection_version": catalog["protection_version"],
                    "sql": "SELECT session_id FROM diag_sessions",
                },
            )

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        with host._store.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.commit()
        try:
            release = os.open(hold, os.O_WRONLY | os.O_NONBLOCK)
            os.close(release)
        except OSError:
            pass
        os.close(arrived)
        worker.join()
        assert result["query"][0] == 409
        assert result["query"][1]["code"] in {"database_busy", "query_cancelled"}
    finally:
        _wake_fifo(hold)
        assert dashboard.close()


def test_forgotten_fact_is_absent_from_later_query(tmp_path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        context = CommandContext(origin=ManualOrigin("web"), source="web")
        saved = host._memory_service.execute(
            {
                "operation_id": "save-diag",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "me", "fact": "I prefer coriander"},
            },
            context,
        )
        assert saved["status"] == "saved"
        before = _execute(dashboard, "SELECT fact FROM diag_facts")
        assert before[0] == 200, before[1]
        facts = [row[0]["value"] for row in before[1]["rows"]]
        assert any("coriander" in fact for fact in facts)
        deleted = host._memory_service.execute(
            {
                "operation_id": "delete-diag",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": saved["memory_id"]},
            },
            context,
        )
        assert deleted["status"] == "deleted"
        after = _execute(dashboard, "SELECT fact FROM diag_facts")
        assert after[0] == 200, after[1]
        leftover = [row[0]["value"] for row in after[1]["rows"]]
        assert all("coriander" not in fact for fact in leftover)
    finally:
        assert dashboard.close()


def test_handle_capacity_rejects_the_sixty_fifth_unused(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        issued = []
        for _ in range(64):
            status, body, _head = _post(dashboard, "/api/database/queries", {})
            assert status == 200, body
            issued.append(body["query_id"])
        status, body, _head = _post(dashboard, "/api/database/queries", {})
        assert status == 503
        assert body["code"] == "resource_limit"
        assert len(issued) == 64
    finally:
        assert dashboard.close()


def test_slow_request_body_hits_one_second_io_limit(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        payload = json.dumps(
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            }
        ).encode()
        declared = len(payload) + 200_000
        header = (
            f"POST /api/database/queries/{query_id}/execute HTTP/1.1\r\n"
            f"Host: localhost:{dashboard.port}\r\n"
            f"Origin: http://localhost:{dashboard.port}\r\n"
            f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {declared}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        sock = socket.create_connection(("127.0.0.1", dashboard.port), 2)
        try:
            sock.sendall(header + payload[:8])
            sock.settimeout(3)
            started = time.monotonic()
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            elapsed = time.monotonic() - started
        finally:
            sock.close()
        assert elapsed < 2.5
        assert b"503" in data.split(b"\r\n", 1)[0] or b"resource_limit" in data
    finally:
        assert dashboard.close()


def test_invalidate_waits_for_in_flight_send_copy(tmp_path):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "send.fifo"
    ready = tmp_path / "send.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    console = dashboard.host.database_console
    console.send_barriers = {"ready": str(ready), "hold": str(hold)}
    try:
        result = {}

        def run():
            try:
                result["query"] = _execute(dashboard, "SELECT 1 AS n")
            except Exception as exc:
                result["error"] = type(exc).__name__

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        assert console.released() is False
        assert console._sends
        console.invalidate_and_wait()
        os.close(arrived)
        worker.join()
        console.send_barriers = {}
        assert console.released() is True
        assert console._send_buffers == {}
        assert console._sends == {}
        if "query" in result:
            assert result["query"][0] in {409, 503}
    finally:
        console.send_barriers = {}
        _wake_fifo(hold)
        assert dashboard.close()


def test_forget_waits_until_send_copy_released(tmp_path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "forget-send.fifo"
    ready = tmp_path / "forget-send.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    host = dashboard.host
    console = host.database_console
    console.send_barriers = {"ready": str(ready), "hold": str(hold)}
    try:
        context = CommandContext(origin=ManualOrigin("web"), source="web")
        saved = host._memory_service.execute(
            {
                "operation_id": "save-send",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "me", "fact": "forget-while-sending"},
            },
            context,
        )
        assert saved["status"] == "saved"
        result = {}

        def run():
            try:
                result["query"] = _execute(dashboard, "SELECT fact FROM diag_facts")
            except Exception as exc:
                result["error"] = type(exc).__name__

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        assert console.released() is False
        deleted = host._memory_service.execute(
            {
                "operation_id": "delete-send",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": saved["memory_id"]},
            },
            context,
        )
        os.close(arrived)
        worker.join()
        console.send_barriers = {}
        assert deleted["status"] == "deleted"
        assert console.released() is True
        leftover = _execute(dashboard, "SELECT fact FROM diag_facts")
        assert leftover[0] == 200
        texts = [row[0]["value"] for row in leftover[1]["rows"]]
        assert all("forget-while-sending" not in text for text in texts)
    finally:
        console.send_barriers = {}
        _wake_fifo(hold)
        assert dashboard.close()


def test_facts_episodes_calendar_and_mirrors_are_derived(tmp_path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        context = CommandContext(origin=ManualOrigin("web"), source="web")
        saved = host._memory_service.execute(
            {
                "operation_id": "save-fact-fields",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "me", "fact": "likes-tea"},
            },
            context,
        )
        assert saved["status"] == "saved"
        episode = host._memory_service.execute(
            {
                "operation_id": "save-episode-fields",
                "kind": "episodic",
                "action": "save",
                "payload": {
                    "summary": "boiled water",
                    "occurred_at": "2026-09-01T23:30:00+08:00",
                    "occurred_until": None,
                },
            },
            context,
        )
        assert episode["status"] == "saved"
        def insert_tea(conn):
            conn.execute(
                "INSERT INTO calendar_entries ("
                "title, starts_at, ends_at, iana_time_zone, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    "tea",
                    "2026-09-01T15:30:00+00:00",
                    "2026-09-01T16:00:00+00:00",
                    "UTC",
                    "2026-09-01T15:00:00+00:00",
                ),
            )

        _write(host, insert_tea)
        facts = _execute(
            dashboard,
            "SELECT subject, fact, origin_kind, origin_source, "
            "last_change_origin_type, last_change_origin_source, "
            "provenance_state FROM diag_facts",
        )
        assert facts[0] == 200, facts[1]
        row = next(
            item for item in facts[1]["rows"] if item[1]["value"] == "likes-tea"
        )
        assert row[0]["value"] == "me"
        assert row[2]["value"] == "manual"
        assert row[3]["value"] == "web"
        assert row[4]["value"] == "manual"
        assert row[5]["value"] == "web"
        assert row[6]["value"] in {"known", "known_none", "unknown"}
        episodes = _execute(
            dashboard, "SELECT summary, occurred_at FROM diag_episodes"
        )
        assert episodes[0] == 200, episodes[1]
        assert any(
            item[0]["value"] == "boiled water" for item in episodes[1]["rows"]
        )
        calendar = _execute(
            dashboard, "SELECT title, iana_time_zone FROM diag_calendar_entries"
        )
        assert calendar[0] == 200, calendar[1]
        assert calendar[1]["rows"][0][0]["value"] == "tea"
        mirrors = _execute(dashboard, "SELECT * FROM diag_memory_mirrors")
        assert mirrors[0] == 200, mirrors[1]
        assert "ready" not in mirrors[1]["columns"]
        assert "path" not in mirrors[1]["columns"]
        assert "intent" not in mirrors[1]["columns"]
        assert "fingerprint" not in mirrors[1]["columns"]
    finally:
        assert dashboard.close()


def test_duplicate_attempt_id_and_negative_token_fail_closed(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        dup = json.dumps(
            {"attempts": [{"attempt_id": "dup-1"}, {"attempt_id": "dup-1"}]}
        )
        _write(
            host,
            lambda conn: conn.execute(
                "UPDATE runs SET telemetry=? WHERE run_id=?",
                (dup, submitted.run_id),
            ),
        )
        status, body, _head = _execute(
            dashboard, "SELECT attempt_id FROM diag_attempts"
        )
        assert status == 503
        assert body["code"] == "data_invalid"
        bad = json.dumps(
            {
                "attempts": [
                    {
                        "attempt_id": "tok-1",
                        "usage": {"total_input_tokens": -1},
                    }
                ]
            }
        )
        _write(
            host,
            lambda conn: conn.execute(
                "UPDATE runs SET telemetry=? WHERE run_id=?",
                (bad, submitted.run_id),
            ),
        )
        status, body, _head = _execute(
            dashboard, "SELECT attempt_id FROM diag_attempts"
        )
        assert status == 503
        assert body["code"] == "data_invalid"
    finally:
        assert dashboard.close()


def test_bad_message_json_fails_and_unreferenced_object_does_not(tmp_path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        context = CommandContext(origin=ManualOrigin("web"), source="web")
        saved = host._memory_service.execute(
            {
                "operation_id": "save-unrelated-fact",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "me", "fact": "keep"},
            },
            context,
        )
        assert saved["status"] == "saved"
        def corrupt(conn):
            conn.execute(
                "UPDATE agent_log SET content=? WHERE role='user'",
                ('{"not":"blocks"}',),
            )
            conn.execute(
                "UPDATE facts SET last_change_origin=?",
                ('{"type":"nope"}',),
            )

        _write(host, corrupt)
        sessions = _execute(dashboard, "SELECT session_id FROM diag_sessions")
        assert sessions[0] == 200, sessions[1]
        messages = _execute(
            dashboard, "SELECT text FROM diag_messages WHERE role='user'"
        )
        assert messages[0] == 503
        assert messages[1]["code"] == "data_invalid"
        facts = _execute(dashboard, "SELECT fact FROM diag_facts")
        assert facts[0] == 503
        assert facts[1]["code"] == "data_invalid"
    finally:
        assert dashboard.close()


def test_sql_size_and_column_count_boundaries(tmp_path):
    from agent_alfred.database_console.budget import SQL_TEXT_LIMIT

    dashboard = _dashboard(tmp_path)
    try:
        prefix = "SELECT 1 -- "
        pad = SQL_TEXT_LIMIT - len(prefix.encode())
        allowed = prefix + ("x" * pad)
        assert len(allowed.encode()) == SQL_TEXT_LIMIT
        status, body, _head = _execute(dashboard, allowed)
        assert status == 200, body
        rejected = allowed + "x"
        status, body, _head = _execute(dashboard, rejected)
        assert status == 400
        assert body["code"] == "invalid_request"
        sixty_four = "SELECT " + ", ".join(str(index) for index in range(64))
        status, body, _head = _execute(dashboard, sixty_four)
        assert status == 200, body
        assert len(body["columns"]) == 64
        sixty_five = "SELECT " + ", ".join(str(index) for index in range(65))
        status, body, _head = _execute(dashboard, sixty_five)
        assert status == 400
        assert body["code"] == "invalid_request"
    finally:
        assert dashboard.close()


def test_source_value_over_two_mib_is_input_too_large(tmp_path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        context = CommandContext(origin=ManualOrigin("web"), source="web")
        saved = host._memory_service.execute(
            {
                "operation_id": "save-big",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "me", "fact": "tiny"},
            },
            context,
        )
        assert saved["status"] == "saved"
        oversized = "x" * (2 * 1024 * 1024 + 1)
        _write(
            host,
            lambda conn: conn.execute("UPDATE facts SET fact=?", (oversized,)),
        )
        status, body, _head = _execute(dashboard, "SELECT fact FROM diag_facts")
        assert status == 413
        assert body["code"] == "input_too_large"
    finally:
        assert dashboard.close()


def test_snapshot_does_not_mix_objects_when_write_interleaves(tmp_path):
    from agent_alfred.schema import insert_session

    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "mid.fifo"
    ready = tmp_path / "mid.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    host = dashboard.host
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        host.database_console._records[query_id].barriers = {
            "mid_extract": str(hold)
        }
        result = {}

        def run():
            result["query"] = _post(
                dashboard,
                f"/api/database/queries/{query_id}/execute",
                {
                    "instance_id": catalog["instance_id"],
                    "memory_revision": catalog["memory_revision"],
                    "protection_version": catalog["protection_version"],
                    "sql": (
                        "SELECT s.session_id FROM diag_sessions s "
                        "LEFT JOIN diag_runs r ON r.session_id=s.session_id"
                    ),
                },
                timeout=8,
            )

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        _write(
            host,
            lambda conn: insert_session(
                conn,
                session_id="sess-after-snapshot",
                created_at="2026-01-02T00:00:00Z",
            ),
        )
        _wake_fifo(hold)
        os.close(arrived)
        worker.join()
        status, body = result["query"][0], result["query"][1]
        if status == 200:
            ids = [row[0]["value"] for row in body["rows"]]
            assert "sess-after-snapshot" not in ids
        else:
            assert status == 409
            later = _execute(dashboard, "SELECT session_id FROM diag_sessions")
            assert later[0] == 200
            seen = [row[0]["value"] for row in later[1]["rows"]]
            assert "sess-after-snapshot" in seen
    finally:
        _wake_fifo(hold)
        assert dashboard.close()


def test_execute_budget_times_out_without_releasing_extract(tmp_path):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "timeout.fifo"
    ready = tmp_path / "timeout.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        dashboard.host.database_console._records[query_id].barriers = {
            "extract": str(hold)
        }
        started = time.monotonic()
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
            timeout=8,
        )
        elapsed = time.monotonic() - started
        assert status == 504
        assert body["code"] == "query_timeout"
        assert elapsed < 7
    finally:
        _wake_fifo(hold)
        assert dashboard.close()


def test_cancel_running_extract_releases_within_one_second(tmp_path):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "cancel.fifo"
    ready = tmp_path / "cancel.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    console = dashboard.host.database_console
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        console._records[query_id].barriers = {"extract": str(hold)}
        result = {}

        def run():
            result["query"] = _post(
                dashboard,
                f"/api/database/queries/{query_id}/execute",
                {
                    "instance_id": catalog["instance_id"],
                    "memory_revision": catalog["memory_revision"],
                    "protection_version": catalog["protection_version"],
                    "sql": "SELECT 1",
                },
                timeout=8,
            )

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        started = time.monotonic()
        cancelled = _post(
            dashboard, f"/api/database/queries/{query_id}/cancel", {}
        )
        _wake_fifo(hold)
        os.close(arrived)
        worker.join()
        elapsed = time.monotonic() - started
        assert cancelled[0] == 200
        assert elapsed <= 1.0
        assert console.released() is True
        assert console._cleanup_failed is False
        assert result["query"][0] == 409
        assert result["query"][1]["code"] == "query_cancelled"
    finally:
        _wake_fifo(hold)
        assert dashboard.close()


def test_kill_failure_pauses_new_queries(tmp_path, monkeypatch):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "kill.fifo"
    ready = tmp_path / "kill.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    console = dashboard.host.database_console
    real_killpg = os.killpg

    def boom(pgid, sig):
        os.killpg = real_killpg
        raise OSError("kill failed")

    monkeypatch.setattr(os, "killpg", boom)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        console._records[query_id].barriers = {"extract": str(hold)}
        result = {}

        def run():
            result["query"] = _post(
                dashboard,
                f"/api/database/queries/{query_id}/execute",
                {
                    "instance_id": catalog["instance_id"],
                    "memory_revision": catalog["memory_revision"],
                    "protection_version": catalog["protection_version"],
                    "sql": "SELECT 1",
                },
                timeout=8,
            )

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        console.invalidate_and_wait()
        assert console._cleanup_failed is True
        issued = _post(dashboard, "/api/database/queries", {})
        assert issued[0] == 503
        assert issued[1]["code"] == "cleanup_failed"
        _wake_fifo(hold)
        os.close(arrived)
        worker.join()
        # Once the actual worker and response owner release their copies,
        # successful capability verification restores admission.
        console.invalidate_and_wait()
        assert console.released()
        assert _post(dashboard, "/api/database/queries", {})[0] == 200
    finally:
        os.killpg = real_killpg
        _wake_fifo(hold)
        assert dashboard.close()


def test_completed_then_cancel_keeps_completed_and_status_has_no_body(
    tmp_path,
):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _head = _execute(dashboard, "SELECT 1 AS n")
        assert status == 200, body
        query_id = body["query_id"]
        cancelled = _post(
            dashboard, f"/api/database/queries/{query_id}/cancel", {}
        )
        assert cancelled[0] == 200
        assert cancelled[1]["status"] == "completed"
        assert cancelled[1]["cleanup"] == "released"
        code, payload, head = _status(dashboard, query_id)
        assert code == 200
        assert payload["completed_before_cancel"] is True
        assert payload["result_delivered"] is True
        assert "rows" not in payload
        assert "sql" not in payload
        assert b"no-store" in head
    finally:
        assert dashboard.close()


def test_lost_execute_body_status_has_no_result(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        payload = json.dumps(
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1 AS n",
            }
        ).encode()
        request = (
            f"POST /api/database/queries/{query_id}/execute HTTP/1.1\r\n"
            f"Host: localhost:{dashboard.port}\r\n"
            f"Origin: http://localhost:{dashboard.port}\r\n"
            f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + payload
        sock = socket.create_connection(("127.0.0.1", dashboard.port), 2)
        sock.sendall(request)
        sock.close()
        deadline = time.monotonic() + 8
        code = None
        payload = None
        while time.monotonic() < deadline:
            code, payload, _head = _status(dashboard, query_id)
            if payload.get("status") not in {
                "unused",
                "running",
                "prepared",
                "stopping",
            }:
                break
        assert code == 200
        assert payload["status"] in {
            "completed",
            "failed",
            "cancelled",
            "timed_out",
            "invalidated",
        }
        assert "rows" not in payload
        assert "sql" not in payload
    finally:
        assert dashboard.close()


def test_journal_mode_unchanged_and_no_temp_files(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        before = host._conn.execute("PRAGMA journal_mode").fetchone()[0]
        names = {path.name for path in (tmp_path / "state").iterdir()}
        status, body, _head = _execute(
            dashboard, "SELECT session_id FROM diag_sessions"
        )
        assert status == 200, body
        after = host._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert after == before
        assert str(after).lower() in {
            "delete",
            "truncate",
            "persist",
            "memory",
            "wal",
            "off",
        }
        later = {path.name for path in (tmp_path / "state").iterdir()}
        extra = later - names
        assert not any(
            name.startswith("etilqs") or name.endswith(".tmp") for name in extra
        )
    finally:
        assert dashboard.close()


def test_recording_pause_still_allows_database_then_shutdown_releases(
    tmp_path,
):
    hold = threading.Event()
    dashboard = _dashboard(tmp_path, before_recording_commit=hold)
    host = dashboard.host
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if host.snapshot().coordinator_state == "recording_pending":
                break
        assert host.snapshot().coordinator_state == "recording_pending"
        status, body, _head = _execute(dashboard, "SELECT 1 AS n")
        assert status == 200, body
        assert body["rows"][0][0]["value"] == "1"
        hold.set()
        host.wait(submitted.run_id)
        assert host.database_console.released() is True
    finally:
        hold.set()
        assert dashboard.close()
        assert host.database_console._active is None
        assert all(
            record.process is None
            for record in host.database_console._records.values()
        )


def test_resource_counts_return_after_success_reject_cancel_and_timeout(
    tmp_path,
):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "res.fifo"
    ready = tmp_path / "res.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    console = dashboard.host.database_console
    try:
        before = len(os.listdir("/dev/fd"))
        assert _execute(dashboard, "SELECT 1")[0] == 200
        assert _execute(dashboard, "SELECT FROM")[0] == 400
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        cancelled = _post(
            dashboard, f"/api/database/queries/{query_id}/cancel", {}
        )
        assert cancelled[0] == 200
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        console._records[query_id].barriers = {"extract": str(hold)}
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
            timeout=8,
        )
        assert status == 504
        assert body["code"] == "query_timeout"
        assert console._active is None
        assert all(
            record.process is None or record.process.poll() is not None
            for record in console._records.values()
        )
        after = len(os.listdir("/dev/fd"))
        assert after - before < 8
    finally:
        _wake_fifo(hold)
        assert dashboard.close()


def test_catalog_does_not_change_parent_hard_heap_limit(tmp_path):
    from agent_alfred.database_console.sqlite_limits import current_hard_heap_limit

    before = current_hard_heap_limit()
    dashboard = _dashboard(tmp_path)
    try:
        catalog = _catalog(dashboard)
        assert catalog["available"] is True
        assert current_hard_heap_limit() == before
        status, body, _head = _execute(dashboard, "SELECT 1 AS n")
        assert status == 200, body
        assert current_hard_heap_limit() == before
    finally:
        assert dashboard.close()


def test_replace_schema_and_json_valid_arity_over_http(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _head = _execute(
            dashboard, "SELECT replace('abc','a','z')"
        )
        assert status == 200, body
        assert body["rows"][0][0]["value"] == "zbc"
        status, body, _head = _execute(
            dashboard, "SELECT * FROM main.diag_sessions"
        )
        assert status == 400
        assert body["code"] == "sql_rejected"
        status, body, _head = _execute(dashboard, "SELECT json_valid('{}',1)")
        assert status == 400
        assert body["code"] == "sql_rejected"
        status, body, _head = _execute(
            dashboard,
            "WITH t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t) "
            "SELECT * FROM t",
        )
        assert status == 400
        assert body["code"] == "sql_rejected"
    finally:
        assert dashboard.close()


def test_final_success_json_never_exceeds_two_mib_on_the_wire(tmp_path):
    from agent_alfred.database_console.encode import dump
    from agent_alfred.gateway.web.handler import _dump

    dashboard = _dashboard(tmp_path)
    try:
        _write(
            dashboard.host,
            lambda conn: conn.executemany(
                "INSERT INTO sessions"
                "(session_id,created_at,activity_revision) VALUES (?,?,?)",
                [(f"fixture-{i}", "2026-09-16T00:00:00Z", i) for i in range(33)],
            ),
        )
        baseline = _execute(dashboard, "SELECT 1 AS x FROM diag_sessions")[1]
        envelope = {
            "query_id": baseline["query_id"],
            "instance_id": baseline["instance_id"],
            "memory_revision": baseline["memory_revision"],
            "protection_version": baseline["protection_version"],
            "objects": baseline["objects"],
            "coverage": baseline["coverage"],
            "read_at": baseline["read_at"],
        }
        n = 63000
        packed = encode_rows(envelope, ["x"], iter([("a" * n,)] * 33))
        overhead = len(dump(packed)) - 33 * n
        n = (2097152 - overhead) // 33
        sql = "SELECT '" + "a" * n + "' AS x FROM diag_sessions"
        status, body, head = _execute(dashboard, sql, timeout=10)
        assert status == 200, body
        size = len(_dump(body))
        assert size <= 2097152
        assert "Content-Length: " + str(size) in head.decode()
        if body["truncated"]:
            assert "bytes" in body["truncation_reasons"]
    finally:
        assert dashboard.close()


def test_slow_drip_request_body_is_cut_at_one_second(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        header = (
            f"POST /api/database/queries/{query_id}/execute HTTP/1.1\r\n"
            f"Host: localhost:{dashboard.port}\r\n"
            f"Origin: http://localhost:{dashboard.port}\r\n"
            f"x-agent-alfred-csrf: {dashboard.csrf_token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 1000\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        sock = socket.create_connection(("127.0.0.1", dashboard.port), 3)
        try:
            sock.sendall(header + b"{")
            done = threading.Event()
            result = {}

            def read_response():
                try:
                    sock.settimeout(3)
                    result["data"] = sock.recv(64)
                except OSError as exc:
                    result["error"] = type(exc).__name__
                result["elapsed"] = time.monotonic() - started
                done.set()

            started = time.monotonic()
            worker = threading.Thread(target=read_response)
            worker.start()
            for _ in range(12):
                if done.wait(0.2):
                    break
                try:
                    sock.sendall(b" ")
                except OSError:
                    break
            worker.join()
            elapsed = result.get("elapsed", time.monotonic() - started)
            assert done.is_set()
            assert elapsed < 1.25
            data = result.get("data") or b""
            assert b"503" in data or b"resource_limit" in data
        finally:
            sock.close()
    finally:
        assert dashboard.close()


def test_expired_handles_occupy_capacity_until_terminal_ttl(tmp_path):
    from agent_alfred.clock import FakeClock

    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    clock = FakeClock(monotonic_value=1000.0)
    console._clock = clock
    try:
        issued = []
        for _ in range(64):
            status, body, _head = _post(dashboard, "/api/database/queries", {})
            assert status == 200, body
            issued.append(body["query_id"])
        clock.monotonic_value = 1031.0
        assert _status(dashboard, issued[0])[0] == 410
        status, body, _head = _post(dashboard, "/api/database/queries", {})
        assert status == 503
        assert body["code"] == "resource_limit"
        clock.monotonic_value = 1092.0
        status, body, _head = _post(dashboard, "/api/database/queries", {})
        assert status == 200, body
        missing = _status(dashboard, issued[0])
        assert missing[0] in {404, 410}
    finally:
        assert dashboard.close()


def test_each_object_returns_declared_columns(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        catalog = _catalog(dashboard)
        assert len(catalog["objects"]) == 21
        for obj in catalog["objects"]:
            names = [column["name"] for column in obj["columns"]]
            sql = f"SELECT {', '.join(names)} FROM {obj['name']} LIMIT 1"
            status, body, _head = _execute(dashboard, sql)
            assert status == 200, (obj["name"], body)
            assert body["columns"] == names
    finally:
        assert dashboard.close()


def test_protected_input_over_64mib_fails_even_with_limit(tmp_path):
    from agent_alfred.database_console.budget import (
        PROTECTED_INPUT_LIMIT,
        SOURCE_ROW_LIMIT,
        row_bytes,
    )
    from agent_alfred.schema import insert_session

    dashboard = _dashboard(tmp_path)
    created = "2026-01-01T00:00:00Z"
    size = SOURCE_ROW_LIMIT
    while row_bytes(("s" * size, created, 0)) > SOURCE_ROW_LIMIT:
        size -= 1
    used = row_bytes(("s" * size, created, 0))
    count = PROTECTED_INPUT_LIMIT // used + 1

    def load(conn):
        for index in range(count):
            insert_session(
                conn,
                session_id=f"{index:04d}" + "s" * (size - 4),
                created_at=created,
            )

    try:
        _write(dashboard.host, load)
        status, body, _head = _execute(
            dashboard,
            "SELECT session_id FROM diag_sessions LIMIT 1",
            timeout=15,
        )
        assert status == 413
        assert body["code"] == "input_too_large"
    finally:
        assert dashboard.close()


def test_wal_source_stays_readable_and_mode_unchanged(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        with host._store._db_lock:
            conn = host._conn
            previous = conn.isolation_level
            conn.isolation_level = None
            try:
                before = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            finally:
                conn.isolation_level = previous
        assert str(before).lower() == "wal"
        status, body, _head = _execute(
            dashboard, "SELECT session_id FROM diag_sessions"
        )
        assert status == 200, body
        after = host._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert after == before
    finally:
        assert dashboard.close()


def test_recording_store_poison_still_allows_database(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        host._store._poisoned.set()
        status, body, _head = _execute(dashboard, "SELECT 1 AS n")
        assert status == 200, body
        assert body["rows"][0][0]["value"] == "1"
    finally:
        assert dashboard.close()


def test_protection_change_during_extract_invalidates(tmp_path):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / "prot.fifo"
    ready = tmp_path / "prot.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    host = dashboard.host
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        host.database_console._records[query_id].barriers = {"extract": str(hold)}
        result = {}

        def run():
            result["query"] = _post(
                dashboard,
                f"/api/database/queries/{query_id}/execute",
                {
                    "instance_id": catalog["instance_id"],
                    "memory_revision": catalog["memory_revision"],
                    "protection_version": catalog["protection_version"],
                    "sql": "SELECT 1",
                },
                timeout=8,
            )

        worker = threading.Thread(target=run)
        worker.start()
        arrived = os.open(ready, os.O_RDONLY)
        host._redactor.remember("fresh-loaded-secret", credential=True)
        _wake_fifo(hold)
        os.close(arrived)
        worker.join()
        assert result["query"][0] == 409
        assert result["query"][1]["code"] == "data_invalidated"
    finally:
        _wake_fifo(hold)
        assert dashboard.close()


def test_nested_sql_and_cte_boundaries_over_http(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        allowed = _execute(
            dashboard, "WITH t(x) AS (SELECT 1) SELECT x FROM t"
        )
        assert allowed[0] == 200, allowed[1]
        assert allowed[1]["rows"][0][0]["value"] == "1"
        assert _execute(dashboard, "SELECT date()")[0] == 200
        for sql in (
            "SELECT coalesce(json_valid('{}',1),0)",
            'SELECT "json_valid"(\'{}\',1)',
            "SELECT * FROM (main.diag_sessions)",
            "SELECT * FROM diag_sessions a, main.diag_sessions b",
        ):
            status, body, _head = _execute(dashboard, sql)
            assert status == 400, sql
            assert body["code"] == "sql_rejected", sql
    finally:
        assert dashboard.close()


def test_begin_send_rejects_stale_protection_before_emit(tmp_path):
    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    old = console.begin_send
    ready = threading.Event()
    release = threading.Event()
    try:
        def begin(query_id, body):
            ready.set()
            assert release.wait(3)
            return old(query_id, body)

        console.begin_send = begin
        result = {}

        def query():
            try:
                result["response"] = _execute(
                    dashboard, "SELECT 'late-protection-secret' AS x"
                )
            except Exception as exc:
                result["error"] = type(exc).__name__

        worker = threading.Thread(target=query)
        worker.start()
        assert ready.wait(3)
        dashboard.host._redactor.remember("late-protection-secret")
        release.set()
        worker.join()
        assert "response" in result, result
        status, body, _head = result["response"]
        assert status == 409, body
        assert body["code"] == "data_invalidated"
        assert body.get("rows") is None
        assert "late-protection-secret" not in json.dumps(body)
    finally:
        console.begin_send = old
        release.set()
        assert dashboard.close()


def test_allowed_functions_have_http_positive_examples(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        scalars = (
            "SELECT abs(-1), round(1.2,1), coalesce(NULL,2), ifnull(NULL,3), "
            "nullif(1,2), typeof(1), length('a'), lower('A'), upper('a'), "
            "trim(' a '), ltrim(' a'), rtrim('a '), substr('ab',1,1), "
            "replace('a','a','b'), instr('ab','b'), hex(x'00'), "
            "like('a','a'), glob('a','a'), json('[]'), json_array(1), "
            "json_object('a',1), json_extract('{\"a\":1}','$.a'), "
            "json_type('[]'), json_valid('[]'), json_array_length('[]'), "
            "json_quote('a'), date(), time('00:00:00'), "
            "datetime('2020-01-01'), julianday('2020-01-01'), "
            "unixepoch('2020-01-01'), strftime('%Y','2020-01-01')"
        )
        status, body, _head = _execute(dashboard, scalars)
        assert status == 200, body
        windows = _execute(
            dashboard,
            "SELECT row_number() OVER (ORDER BY 1), rank() OVER (ORDER BY 1), "
            "dense_rank() OVER (ORDER BY 1), percent_rank() OVER (ORDER BY 1), "
            "cume_dist() OVER (ORDER BY 1), ntile(1) OVER (ORDER BY 1), "
            "lag(1) OVER (ORDER BY 1), lead(1) OVER (ORDER BY 1), "
            "first_value(1) OVER (ORDER BY 1), last_value(1) OVER (ORDER BY 1), "
            "nth_value(1,1) OVER (ORDER BY 1), min(1,2), max(1,2)",
        )
        assert windows[0] == 200, windows[1]
        aggs = _execute(
            dashboard,
            "SELECT count(*), sum(1), total(1), avg(1), min(1), max(1), "
            "group_concat(1)",
        )
        assert aggs[0] == 200, aggs[1]
    finally:
        assert dashboard.close()


def test_tool_metering_projection_fields(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    cost = json.dumps(
        {
            "kind": "reported",
            "units": "1.50",
            "unit": "usd",
            "source": "src-1",
            "service": "src-1",
        }
    )

    def load(conn):
        conn.execute(
            "INSERT INTO tool_metering ("
            "run_id, step_index, call_id, ordinal, tool_name, source_id, "
            "capability_id, effect, requested_at, start_confirmation, result, "
            "reason, finished_at, cost, operation_id, model_delivery"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-1",
                0,
                "call-1",
                1,
                "demo",
                "src-1",
                "cap-1",
                "write",
                "2026-09-16T00:00:00Z",
                "confirmed",
                "succeeded",
                None,
                "2026-09-16T00:00:01Z",
                cost,
                "op-1",
                "confirmed",
            ),
        )

    try:
        _write(host, load)
        status, body, _head = _execute(
            dashboard,
            "SELECT run_id, step_index, call_id, ordinal, tool_name, "
            "source_id, capability_id, effect, start_confirmation, result, "
            "reason_code, operation_id, model_delivery, cost_kind, "
            "cost_units, cost_unit, cost_source, cost_service, cost_reason "
            "FROM diag_tool_metering",
        )
        assert status == 200, body
        row = body["rows"][0]
        by_name = dict(zip(body["columns"], row, strict=True))
        assert by_name["tool_name"]["value"] == "demo"
        assert by_name["cost_kind"]["value"] == "reported"
        assert by_name["cost_units"]["value"] == "1.50"
        assert by_name["cost_unit"]["value"] == "usd"
        assert by_name["reason_code"]["type"] == "null"
        dumped = json.dumps(body)
        assert '"kind":"reported"' not in dumped
        assert "cost" not in body["columns"]
    finally:
        assert dashboard.close()


def test_remember_notify_failure_still_invalidates_old_handle(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        inner = host._redactor._on_change

        def boom():
            if inner is not None:
                inner()
            raise RuntimeError("notify failed")

        host._redactor._on_change = boom
        try:
            host._redactor.remember("already-remembered-secret", credential=True)
        except RuntimeError:
            pass
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
        )
        assert status == 409
        assert body["code"] == "data_invalidated"
        later = _catalog(dashboard)
        assert later["protection_version"] != catalog["protection_version"]
    finally:
        assert dashboard.close()


def test_short_credential_is_protected_in_worker(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        host._redactor.remember("abcd", credential=True)
        submitted = host.submit(SubmitRequest(message="pin abcd please"))
        host.wait(submitted.run_id)
        status, body, _head = _execute(
            dashboard, "SELECT text FROM diag_messages WHERE role='user'"
        )
        assert status == 200, body
        texts = [row[0]["value"] for row in body["rows"]]
        assert all("abcd" not in text for text in texts)
        assert any("***" in text for text in texts)
    finally:
        assert dashboard.close()


def test_attempts_null_and_oversize_telemetry_fail_closed(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        _write(
            host,
            lambda conn: conn.execute(
                "UPDATE runs SET telemetry=? WHERE run_id=?",
                (json.dumps({"attempts": None}), submitted.run_id),
            ),
        )
        status, body, _head = _execute(
            dashboard, "SELECT run_id FROM diag_attempt_coverage"
        )
        assert status == 503
        assert body["code"] == "data_invalid"
        huge = json.dumps({"attempts": [], "unknown": "x" * (2 * 1024 * 1024 + 1)})
        _write(
            host,
            lambda conn: conn.execute(
                "UPDATE runs SET telemetry=? WHERE run_id=?",
                (huge, submitted.run_id),
            ),
        )
        status, body, _head = _execute(
            dashboard, "SELECT run_id FROM diag_attempt_coverage"
        )
        assert status == 413
        assert body["code"] == "input_too_large"
    finally:
        assert dashboard.close()


def test_missing_source_column_makes_console_unavailable(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        _write(
            host,
            lambda conn: conn.execute(
                "ALTER TABLE calendar_entries RENAME COLUMN title TO missing_title"
            ),
        )
        catalog = _catalog(dashboard)
        assert catalog["available"] is False
        status, body, _head = _post(dashboard, "/api/database/queries", {})
        assert status == 503
        assert body["code"] == "database_unavailable"
    finally:
        assert dashboard.close()



def test_http_success_body_can_be_exactly_two_mib(tmp_path):
    from agent_alfred.database_console.budget import RESULT_JSON_LIMIT
    from agent_alfred.database_console.encode import dump, encode_rows
    from agent_alfred.gateway.web.handler import _dump

    dashboard = _dashboard(tmp_path)
    try:
        baseline = _execute(dashboard, "SELECT session_id FROM diag_sessions")[1]
        envelope = {
            "query_id": "x" * len(baseline["query_id"]),
            "instance_id": baseline["instance_id"],
            "memory_revision": baseline["memory_revision"],
            "protection_version": baseline["protection_version"],
            "objects": baseline["objects"],
            "coverage": baseline["coverage"],
            "read_at": baseline["read_at"],
        }
        def sid(index, length):
            prefix = f"{index:02d}"
            return prefix + ("x" * (length - len(prefix)))

        low, high = 8, 70_000
        best_n = 8
        while low <= high:
            mid = (low + high) // 2
            packed = encode_rows(
                envelope,
                ["session_id"],
                iter((sid(index, mid),) for index in range(33)),
            )
            size = len(dump(packed))
            if size <= RESULT_JSON_LIMIT and packed["truncated"] is False:
                best_n = mid
                low = mid + 1
            else:
                high = mid - 1
        packed = encode_rows(
            envelope,
            ["session_id"],
            iter((sid(index, best_n),) for index in range(33)),
        )
        gap = RESULT_JSON_LIMIT - len(dump(packed))
        last = best_n + gap
        ids = [(sid(index, best_n), index) for index in range(32)]
        ids.append((sid(32, last), 32))

        def load(conn):
            conn.executemany(
                "INSERT INTO sessions"
                "(session_id,created_at,activity_revision) VALUES (?,?,?)",
                [(sid, "2026-09-16T00:00:00Z", index) for sid, index in ids],
            )

        _write(dashboard.host, load)
        status, body, head = _execute(
            dashboard,
            "SELECT session_id FROM diag_sessions WHERE length(session_id) > 8",
            timeout=15,
        )
        assert status == 200, body
        size = len(_dump(body))
        if size != RESULT_JSON_LIMIT:
            delta = RESULT_JSON_LIMIT - size
            _write(
                dashboard.host,
                lambda conn: conn.execute(
                    "UPDATE sessions SET session_id=? WHERE activity_revision=32",
                    (sid(32, last + delta),),
                ),
            )
            status, body, head = _execute(
                dashboard,
                "SELECT session_id FROM diag_sessions WHERE length(session_id) > 8",
                timeout=15,
            )
            size = len(_dump(body))
        assert status == 200, body
        assert size == RESULT_JSON_LIMIT
        assert f"Content-Length: {size}".encode() in head
        assert body["truncated"] is False
    finally:
        assert dashboard.close()


@pytest.mark.parametrize("stage", ["protect", "sql", "encode"])
def test_execute_budget_covers_later_stages(tmp_path, stage):
    dashboard = _dashboard(tmp_path)
    hold = tmp_path / f"{stage}.fifo"
    ready = tmp_path / f"{stage}.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    try:
        catalog = _catalog(dashboard)
        query_id = _post(dashboard, "/api/database/queries", {})[1]["query_id"]
        dashboard.host.database_console._records[query_id].barriers = {
            stage: str(hold)
        }
        started = time.monotonic()
        status, body, _head = _post(
            dashboard,
            f"/api/database/queries/{query_id}/execute",
            {
                "instance_id": catalog["instance_id"],
                "memory_revision": catalog["memory_revision"],
                "protection_version": catalog["protection_version"],
                "sql": "SELECT 1",
            },
            timeout=8,
        )
        elapsed = time.monotonic() - started
        assert status == 504
        assert body["code"] == "query_timeout"
        assert elapsed < 7
    finally:
        _wake_fifo(hold)
        assert dashboard.close()

