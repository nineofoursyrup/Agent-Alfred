"""Public diagnostic requests at actual worker and persistent cleanup barriers."""

import os
import select
import threading
import time
from contextlib import contextmanager

import pytest

from agent_alfred.evals.deterministic.test_database_http import (
    _catalog,
    _dashboard,
    _execute,
    _issue,
    _post,
    _status,
    _wake_fifo,
    _write,
)
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin


@contextmanager
def executing_at(dashboard, tmp_path, stage, sql):
    """The FIFO only pauses a real stage; it never substitutes SQL or cleanup."""
    hold = tmp_path / f"{stage}.fifo"
    ready = tmp_path / f"{stage}.fifo.ready"
    os.mkfifo(hold)
    os.mkfifo(ready)
    catalog = _catalog(dashboard)
    query_id = _issue(dashboard)
    console = dashboard.host.database_console
    console._records[query_id].barriers = {stage: str(hold)}
    result = {}

    def execute():
        try:
            result["response"] = _post(
                dashboard,
                f"/api/database/queries/{query_id}/execute",
                {
                    **{
                        key: catalog[key]
                        for key in (
                            "instance_id",
                            "memory_revision",
                            "protection_version",
                        )
                    },
                    "sql": sql,
                },
                timeout=8,
            )
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=execute)
    ready_fd = os.open(ready, os.O_RDONLY | os.O_NONBLOCK)
    thread.start()
    try:
        assert select.select([ready_fd], [], [], 4)[0], result
        assert os.read(ready_fd, 1) == b"1"
        process = console._records[query_id].process
        assert process is not None and process.poll() is None
        yield query_id, process, result, thread
    finally:
        _wake_fifo(hold)
        os.close(ready_fd)
        thread.join(8)
        assert not thread.is_alive()


def seed_sessions(dashboard, count=1002):
    _write(
        dashboard.host,
        lambda conn: conn.executemany(
            "INSERT INTO sessions(session_id,created_at,activity_revision) "
            "VALUES (?,?,?)",
            [(f"peek-{i}", "2026-09-16T00:00:00Z", i) for i in range(count)],
        ),
    )


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_real_worker_row_limit_probe_does_not_hide_cancel_or_timeout(tmp_path, action):
    dashboard = _dashboard(tmp_path)
    try:
        seed_sessions(dashboard)
        with executing_at(
            dashboard, tmp_path, "peek", "SELECT activity_revision FROM diag_sessions"
        ) as (query_id, process, result, thread):
            if action == "cancel":
                accepted = time.monotonic()
                status, cancelled, _ = _post(
                    dashboard, f"/api/database/queries/{query_id}/cancel", {}
                )
                assert status == 200, cancelled
                thread.join(1)
                assert process.poll() is not None
                assert time.monotonic() - accepted <= 1
            else:
                thread.join(6)
            assert not thread.is_alive()
            status, body, _ = result["response"]
            assert status == (409 if action == "cancel" else 504), body
            assert body["code"] == (
                "query_cancelled" if action == "cancel" else "query_timeout"
            )
            assert "rows" not in body and "truncated" not in body
            assert process.poll() is not None
            assert _status(dashboard, query_id)[1]["cleanup"] == "released"
            assert dashboard.host.database_console.released()
    finally:
        assert dashboard.close()


def test_real_sql_error_in_tail_is_not_successful_row_truncation(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        seed_sessions(dashboard)
        status, body, _ = _execute(
            dashboard,
            """
            SELECT CASE WHEN activity_revision >= 1000
              THEN abs(-9223372036854775808) ELSE activity_revision END AS value
            FROM diag_sessions
        """,
        )
        assert status == 400 and body["code"] == "sql_error", body
        assert "rows" not in body
        assert dashboard.host.database_console.released()
    finally:
        assert dashboard.close()


@pytest.mark.parametrize("stage", ["extract", "protect", "sql", "encode"])
def test_real_forgetting_waits_for_worker_cleanup(tmp_path, stage):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    context = CommandContext(origin=ManualOrigin("web"), source="web")
    try:
        saved = host._memory_service.execute(
            {
                "operation_id": "diagnostic-save",
                "kind": "semantic",
                "action": "save",
                "payload": {
                    "subject": "fixture",
                    "fact": "forgettable-diagnostic-text",
                },
            },
            context,
        )
        assert saved["status"] == "saved"
        with executing_at(
            dashboard, tmp_path, stage, "SELECT fact FROM diag_facts"
        ) as (query_id, process, result, thread):
            deleted = host._memory_service.execute(
                {
                    "operation_id": "diagnostic-delete",
                    "kind": "semantic",
                    "action": "delete",
                    "expected_version": 1,
                    "payload": {"id": saved["memory_id"]},
                },
                context,
            )
            assert deleted["status"] == "deleted", deleted
            thread.join(2)
            assert not thread.is_alive()
            assert process.poll() is not None
            assert result["response"][0] != 200
            with host._store.reading() as conn:
                rows = conn.execute(
                    "SELECT state FROM forget_cleanup WHERE operation_id=? "
                    "AND target_id='database-console'",
                    ("diagnostic-delete",),
                ).fetchall()
                assert rows and all(row[0] == "complete" for row in rows)
                assert (
                    conn.execute(
                        "SELECT count(*) FROM facts WHERE id=?", (saved["memory_id"],)
                    ).fetchone()[0]
                    == 0
                )
            assert _status(dashboard, query_id)[1]["cleanup"] == "released"
        status, body, _ = _execute(dashboard, "SELECT fact FROM diag_facts")
        assert status == 200 and body["rows"] == []
    finally:
        assert dashboard.close()


@pytest.mark.parametrize("stage", ["before_spawn", "after_spawn", "decoded_result"])
def test_invalidation_keeps_owner_across_handoffs(tmp_path, monkeypatch, stage):
    import subprocess

    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    arrived, resume = threading.Event(), threading.Event()
    result, spawned = {}, []
    real_spawn = subprocess.Popen.__init__

    def spawn(process, *args, **kwargs):
        real_spawn(process, *args, **kwargs)
        spawned.append(process)
        if stage == "after_spawn":
            arrived.set()
            assert resume.wait(4)

    monkeypatch.setattr(subprocess.Popen, "__init__", spawn)
    if stage == "before_spawn":
        real = console._db_identity

        def identity():
            arrived.set()
            assert resume.wait(4)
            return real()

        monkeypatch.setattr(console, "_db_identity", identity)
    elif stage == "decoded_result":
        real = console._finish

        def finish(record, error, payload=None):
            if payload is not None:
                arrived.set()
                assert resume.wait(4)
            return real(record, error, payload)

        monkeypatch.setattr(console, "_finish", finish)
    thread = threading.Thread(
        target=lambda: result.update(
            response=_execute(dashboard, "SELECT 'retained-body'", timeout=8)
        )
    )
    try:
        thread.start()
        assert arrived.wait(4)
        dashboard.host._redactor.remember("new-protection-key")
        console.invalidate()
        assert not console.released()
        assert console._active is not None
        assert _execute(dashboard, "SELECT 2")[0] == 409
        resume.set()
        thread.join(5)
        assert not thread.is_alive()
        assert result["response"][0] != 200
        assert all(process.poll() is not None for process in spawned)
        assert console.released()
        assert _execute(dashboard, "SELECT 2")[0] == 200
    finally:
        resume.set()
        thread.join(8)
        assert dashboard.close()


def test_cleanup_failure_retains_worker_and_recovers_after_real_release(
    tmp_path, monkeypatch
):
    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    real_kill = os.killpg
    try:
        with executing_at(dashboard, tmp_path, "sql", "SELECT 1") as (
            _query_id,
            process,
            _result,
            thread,
        ):

            def fail_kill(*_args):
                raise OSError("synthetic termination failure")

            monkeypatch.setattr(os, "killpg", fail_kill)
            console.invalidate_and_wait()
            assert not console.released() and process.poll() is None
            status, body, _ = _post(dashboard, "/api/database/queries", {})
            assert status == 503 and body["code"] == "cleanup_failed"
            monkeypatch.setattr(os, "killpg", real_kill)
            console.invalidate_and_wait()
            thread.join(2)
            assert process.poll() is not None and console.released()
        assert _execute(dashboard, "SELECT 1")[0] == 200
    finally:
        monkeypatch.setattr(os, "killpg", real_kill)
        assert dashboard.close()


def test_shutdown_cannot_succeed_while_worker_termination_failed(tmp_path, monkeypatch):
    dashboard = _dashboard(tmp_path)
    real_kill = os.killpg
    try:
        with executing_at(dashboard, tmp_path, "sql", "SELECT 1") as (
            _query_id,
            process,
            _result,
            _thread,
        ):

            def fail_kill(*_args):
                raise OSError("synthetic termination failure")

            monkeypatch.setattr(os, "killpg", fail_kill)
            assert dashboard.close() is False
            assert process.poll() is None
            monkeypatch.setattr(os, "killpg", real_kill)
            assert dashboard.close() is True
            assert process.poll() is not None
    finally:
        monkeypatch.setattr(os, "killpg", real_kill)
        assert dashboard.close()


def byte_boundary_sql(*, error_after_probe=False):
    first = "replace('" + "a" * 1020 + "','a','" + "b" * 2048 + "')"
    sql = "SELECT " + first + " UNION ALL SELECT '" + "c" * 16384 + "'"
    if error_after_probe:
        sql += " UNION ALL SELECT abs(-9223372036854775808)"
    return sql


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_real_byte_probe_cancel_and_timeout_are_not_truncation(tmp_path, action):
    dashboard = _dashboard(tmp_path)
    try:
        with executing_at(dashboard, tmp_path, "byte_peek", byte_boundary_sql()) as (
            query_id,
            process,
            result,
            thread,
        ):
            if action == "cancel":
                status, cancelled, _ = _post(
                    dashboard, f"/api/database/queries/{query_id}/cancel", {}
                )
                assert status == 200 and cancelled["cleanup"] == "released"
            thread.join(6)
            assert not thread.is_alive() and process.poll() is not None
            status, body, _ = result["response"]
            assert status == (409 if action == "cancel" else 504)
            assert "rows" not in body and "truncated" not in body
            assert dashboard.host.database_console.released()
    finally:
        assert dashboard.close()


def test_confirmed_byte_limit_never_steps_the_following_sql_error(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        status, body, _ = _execute(dashboard, byte_boundary_sql(error_after_probe=True))
        assert status == 200, body
        assert body["returned_rows"] == 1 and body["truncated"]
        assert body["truncation_reasons"] == ["bytes"]
    finally:
        assert dashboard.close()


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_partial_process_construction_failure_still_has_cleanup_owner(
    tmp_path, monkeypatch, error_type
):
    import subprocess

    dashboard = _dashboard(tmp_path)
    spawned = []
    real = subprocess.Popen.__init__

    def fail_after_spawn(process, *args, **kwargs):
        real(process, *args, **kwargs)
        spawned.append(process)
        raise error_type("synthetic post-spawn construction failure")

    monkeypatch.setattr(subprocess.Popen, "__init__", fail_after_spawn)
    try:
        try:
            response = _execute(dashboard, "SELECT 1")
            assert response[0] != 200
        except IndexError, ValueError, OSError:
            # An interrupted request may close without an HTTP body.
            pass
        assert spawned and all(process.poll() is not None for process in spawned)
        assert dashboard.host.database_console.released()
        assert dashboard.close()
    finally:
        monkeypatch.setattr(subprocess.Popen, "__init__", real)
        assert dashboard.close()
