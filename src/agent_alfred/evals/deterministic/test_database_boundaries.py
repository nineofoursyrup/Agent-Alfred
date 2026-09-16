"""Regression cases at source, SQLite compilation and memory-only boundaries."""

import json
import sqlite3

import pytest

from agent_alfred.database_console.sqlite_limits import memory_temp_store
from agent_alfred.evals.deterministic.test_database_http import (
    _catalog,
    _dashboard,
    _execute,
    _post,
    _write,
)
from agent_alfred.runtime.work import SubmitRequest


@pytest.mark.parametrize(
    "value,code",
    [(b"bad type", "data_invalid"), ("x" * (2 * 1024 * 1024 + 1), "input_too_large")],
)
def test_mapped_error_checks_raw_source_before_mapping(tmp_path, value, code):
    dashboard = _dashboard(tmp_path)
    try:
        _write(
            dashboard.host,
            lambda conn: conn.execute("UPDATE memory_mirrors SET error=?", (value,)),
        )
        status, body, _ = _execute(
            dashboard, "SELECT error_code FROM diag_memory_mirrors"
        )
        assert status != 200 and body["code"] == code
    finally:
        assert dashboard.close()


def test_damaged_excluded_message_block_cannot_become_empty_text(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        host = dashboard.host
        host.wait(host.submit(SubmitRequest(message="hello")).run_id)
        _write(
            host,
            lambda conn: conn.execute(
                "UPDATE agent_log SET content=? WHERE role='user'",
                (json.dumps([{"type": "tool_result", "content": 17}]),),
            ),
        )
        status, body, _ = _execute(dashboard, "SELECT text FROM diag_messages")
        assert status == 503 and body["code"] == "data_invalid"
    finally:
        assert dashboard.close()


@pytest.mark.parametrize(
    "table,column",
    [
        ("runs", "telemetry"),
        ("memory_provenance", "state"),
        ("history_group_times", "occurred_at"),
        ("forget_projection_recovery", "evidence_id"),
    ],
)
def test_derived_schema_dependencies_checked_even_for_zero_objects(
    tmp_path, table, column
):
    dashboard = _dashboard(tmp_path)
    try:
        _write(
            dashboard.host,
            lambda conn: conn.execute(
                f'ALTER TABLE "{table}" RENAME COLUMN "{column}" TO missing_column'
            ),
        )
        assert _catalog(dashboard)["available"] is False
        status, body, _ = _post(dashboard, "/api/database/queries", {})
        assert status == 503 and body["code"] == "database_unavailable"
    finally:
        assert dashboard.close()


def test_discovery_does_not_evaluate_expressions_or_probe_extra_rows(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        _write(
            dashboard.host,
            lambda conn: conn.execute(
                "INSERT INTO sessions(session_id,created_at,activity_revision) "
                "VALUES ('fixture','2026-09-16',4)"
            ),
        )
        status, body, _ = _execute(
            dashboard,
            """
            SELECT abs(coalesce((SELECT min(activity_revision) FROM diag_sessions),
              -9223372036854775808))
        """,
        )
        assert status == 200 and body["rows"][0][0]["value"] == "4"
        status, body, _ = _execute(
            dashboard,
            """
            WITH d(x) AS (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)),
            n(x) AS (SELECT a.x+10*b.x+100*c.x+1000*e.x FROM d a,d b,d c,d e)
            SELECT CASE WHEN row_number() OVER ()=1002
                THEN abs(-9223372036854775808) ELSE 1 END FROM n
        """,
        )
        assert status == 200 and body["returned_rows"] == 1000 and body["truncated"]
    finally:
        assert dashboard.close()


@pytest.mark.parametrize(
    "sql,status",
    [
        ("SELECT 1+json_valid('[]',2)", 400),
        ("SELECT 1-json_valid('[]',2)", 400),
        ("WITH c(x) AS MATERIALIZED (SELECT 1) SELECT * FROM c", 200),
        ("WITH c(x) AS NOT MATERIALIZED (SELECT 1) SELECT * FROM c", 200),
        ("SELECT 1.25e+2+1, 1e-2-1, 0xA+1", 200),
    ],
)
def test_sql_operator_and_cte_interactions(tmp_path, sql, status):
    dashboard = _dashboard(tmp_path)
    try:
        actual, body, _ = _execute(dashboard, sql)
        assert actual == status, body
    finally:
        assert dashboard.close()


def test_forced_file_sqlite_build_refused_even_if_pragma_claims_memory():
    # Compile flags cannot be changed on an installed SQLite. This adapter
    # models the documented TEMP_STORE=0 build while keeping real PRAGMA I/O.
    real = sqlite3.connect(":memory:")

    class ForcedFile:
        def execute(self, sql):
            if sql == "PRAGMA compile_options":
                return iter([("TEMP_STORE=0",)])
            return real.execute(sql)

    try:
        real.execute("PRAGMA temp_store=MEMORY")
        assert real.execute("PRAGMA temp_store").fetchone()[0] == 2
        with pytest.raises(OSError):
            memory_temp_store(ForcedFile())
    finally:
        real.close()


def test_readonly_open_refuses_hot_journal_without_recovery_writes(tmp_path):
    import hashlib
    import subprocess
    import sys

    from agent_alfred.database_console.errors import ConsoleError
    from agent_alfred.database_console.project import open_source

    path = tmp_path / "hot.sqlite3"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=DELETE')
conn.execute('PRAGMA cache_size=5')
conn.execute('CREATE TABLE data(value BLOB)')
conn.executemany('INSERT INTO data VALUES (?)', [(b'a'*4096,)]*100)
conn.commit()
conn.execute('UPDATE data SET value=?', (b'b'*4096,))
os._exit(0)
""",
            str(path),
        ],
        check=True,
    )
    journal = path.with_name(path.name + "-journal")
    assert journal.exists() and journal.stat().st_size > 512
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, journal)}
    stat = path.stat()
    with pytest.raises(ConsoleError) as failure:
        open_source(str(path), (stat.st_dev, stat.st_ino))
    assert failure.value.code == "database_unavailable"
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before} == before


def test_source_boolean_flag_rejects_out_of_domain_integer(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        _write(
            dashboard.host,
            lambda conn: conn.execute("UPDATE memory_mirrors SET conflict=2"),
        )
        status, body, _ = _execute(
            dashboard, "SELECT conflict FROM diag_memory_mirrors"
        )
        assert status == 503 and body["code"] == "data_invalid"
    finally:
        assert dashboard.close()


@pytest.mark.parametrize(
    "value,code",
    [
        ("x" * (2 * 1024 * 1024 + 1), "input_too_large"),
        ("unknown_state", "data_invalid"),
    ],
)
def test_derived_forget_state_uses_bounded_validated_source(tmp_path, value, code):
    dashboard = _dashboard(tmp_path)
    try:

        def seed(conn):
            conn.execute(
                "INSERT INTO forget_operations VALUES "
                "('bounded-op','semantic','missing-id','known','2026-09-16')"
            )
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute(
                "INSERT INTO forget_cleanup VALUES "
                "('bounded-op','database-console',1,?,NULL)",
                (value,),
            )

        _write(dashboard.host, seed)
        status, body, _ = _execute(dashboard, "SELECT * FROM diag_forget_operations")
        assert status != 200 and body["code"] == code
    finally:
        assert dashboard.close()
