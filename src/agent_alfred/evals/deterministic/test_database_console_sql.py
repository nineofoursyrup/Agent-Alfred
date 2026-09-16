"""SQL boundary and result encoding without the Dashboard HTTP shroud."""

import pytest

from agent_alfred.database_console.catalog import OBJECTS, manifest
from agent_alfred.database_console.encode import cell, encode_rows
from agent_alfred.database_console.errors import ConsoleError
from agent_alfred.database_console.project import open_dataset
from agent_alfred.database_console.sql import inspect_sql, referenced_objects


def test_catalog_has_twenty_one_objects_and_exact_columns():
    names = [item.name for item in OBJECTS]
    assert len(names) == 21
    listed = manifest()
    assert [item["name"] for item in listed] == names
    sessions = next(item for item in OBJECTS if item.name == "diag_sessions")
    assert sessions.column_names == ("session_id", "created_at", "activity_revision")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO diag_sessions VALUES (1,2,3)",
        "UPDATE diag_sessions SET session_id=1",
        "DELETE FROM diag_sessions",
        "DROP TABLE diag_sessions",
        "BEGIN",
        "SELECT 1; SELECT 2",
        "EXPLAIN SELECT 1",
        "EXPLAIN QUERY PLAN SELECT 1",
        "PRAGMA query_only",
        "SELECT ? FROM diag_sessions",
        "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t) "
        "SELECT * FROM t",
        "ATTACH ':memory:' AS x",
        "SELECT * FROM main.diag_sessions",
        "SELECT json_valid('{}',1)",
        "REPLACE INTO diag_sessions VALUES (1,2,3)",
    ],
)
def test_inspect_sql_rejects_forbidden_shapes(sql):
    with pytest.raises(ConsoleError) as caught:
        inspect_sql(sql)
    assert caught.value.code == "sql_rejected"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sqlite_version()",
        "SELECT random()",
        "SELECT * FROM sqlite_schema",
        "SELECT rowid FROM diag_sessions",
        "SELECT * FROM json_each('[]')",
        "SELECT * FROM sessions",
        "SELECT * FROM main.diag_sessions",
        "SELECT json_valid('{}',1)",
        "WITH t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t) SELECT * FROM t",
    ],
)
def test_authorizer_rejects_functions_and_schema(sql):
    conn = open_dataset()
    try:
        with pytest.raises(ConsoleError) as caught:
            referenced_objects(conn, sql)
        assert caught.value.code == "sql_rejected"
    finally:
        conn.close()


def test_replace_function_is_allowed():
    inspect_sql("SELECT replace('abc','a','z')")
    conn = open_dataset()
    try:
        assert referenced_objects(conn, "SELECT replace('abc','a','z')") == ()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "WITH t(x) AS (SELECT 1) SELECT x FROM t",
        "SELECT date()",
        "SELECT time()",
        "SELECT datetime()",
        "SELECT 1 ORDER BY (1)",
        "SELECT count(*) FROM diag_sessions GROUP BY (session_id)",
        "SELECT 1 LIMIT (1)",
    ],
)
def test_cte_column_list_and_zero_arg_date_are_allowed(sql):
    inspect_sql(sql)
    conn = open_dataset()
    try:
        referenced_objects(conn, sql)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT coalesce(json_valid('{}',1),0)",
        'SELECT "json_valid"(\'{}\',1)',
        "SELECT * FROM (main.diag_sessions)",
        "SELECT * FROM diag_sessions a, main.diag_sessions b",
        "SELECT json_valid('{\"a\":1}',1) AS x",
        "SELECT `json_valid`('{}',1)",
        "SELECT * FROM 'main'.'diag_sessions'",
        "SELECT * FROM `main`.`diag_sessions`",
    ],
)
def test_nested_quoted_and_grouped_schema_are_rejected(sql):
    with pytest.raises(ConsoleError) as caught:
        inspect_sql(sql)
    assert caught.value.code == "sql_rejected"


def test_trailing_semicolon_and_comment_are_one_select():
    inspect_sql("SELECT 1 /* ; */ -- ;\n ;")


def test_literal_semicolon_is_not_a_second_statement():
    inspect_sql("SELECT 'a;b' FROM diag_sessions")


def test_referenced_objects_on_empty_dataset():
    conn = open_dataset()
    try:
        assert referenced_objects(conn, "SELECT 1") == ()
        assert referenced_objects(conn, "SELECT session_id FROM diag_sessions") == (
            "diag_sessions",
        )
        with pytest.raises(ConsoleError) as caught:
            referenced_objects(conn, "SELECT * FROM sessions")
        assert caught.value.code == "sql_rejected"
        names = referenced_objects(
            conn,
            "WITH x AS (SELECT 1 AS session_id) SELECT * FROM x",
        )
        assert names == ()
    finally:
        conn.close()


def test_cell_types_and_non_finite_real():
    assert cell(None) == {"type": "null"}
    assert cell(9223372036854775807) == {
        "type": "integer",
        "value": "9223372036854775807",
    }
    assert cell(1.25) == {"type": "real", "value": 1.25}
    assert cell("a") == {"type": "text", "value": "a"}
    assert cell(b"\x00\xff") == {"type": "blob", "hex": "00ff", "byte_length": 2}
    with pytest.raises(ConsoleError) as caught:
        cell(float("nan"))
    assert caught.value.code == "data_invalid"


def test_encode_empty_keeps_columns():
    payload = encode_rows({"read_at": "t"}, ["id"], iter(()))
    assert payload["columns"] == ["id"]
    assert payload["rows"] == []
    assert payload["returned_rows"] == 0
    assert payload["truncated"] is False


def test_encode_truncates_at_one_thousand_rows():
    def rows():
        for index in range(1001):
            yield (index,)

    payload = encode_rows({"read_at": "t"}, ["n"], rows())
    assert payload["returned_rows"] == 1000
    assert payload["truncated"] is True
    assert payload["truncation_reasons"] == ["rows"]
    assert payload["rows"][0][0]["value"] == "0"
    assert payload["rows"][-1][0]["value"] == "999"


def test_null_cell_is_not_empty_text():
    payload = encode_rows({}, ["a"], iter([(None,), ("",)]))
    assert payload["rows"][0][0] == {"type": "null"}
    assert payload["rows"][1][0] == {"type": "text", "value": ""}


def test_success_json_never_exceeds_two_mib():
    from agent_alfred.database_console.budget import RESULT_JSON_LIMIT
    from agent_alfred.database_console.encode import dump

    text = "测" * 80_000

    def rows():
        for index in range(20):
            yield (text, index)

    payload = encode_rows({"read_at": "t", "objects": []}, ["t", "i"], rows())
    raw = dump(payload)
    assert len(raw) <= RESULT_JSON_LIMIT
    assert payload["truncated"] is True
    assert "bytes" in payload["truncation_reasons"]


def test_success_json_can_land_on_exact_two_mib():
    from agent_alfred.database_console.budget import RESULT_JSON_LIMIT
    from agent_alfred.database_console.encode import dump

    envelope = {"read_at": "t", "objects": []}
    low, high = 0, RESULT_JSON_LIMIT
    exact = None
    while low <= high:
        mid = (low + high) // 2
        try:
            payload = encode_rows(envelope, ["t"], iter([("x" * mid,)]))
        except ConsoleError:
            high = mid - 1
            continue
        size = len(dump(payload))
        if size == RESULT_JSON_LIMIT:
            exact = payload
            break
        if size < RESULT_JSON_LIMIT:
            low = mid + 1
        else:
            high = mid - 1
    assert exact is not None
    assert len(dump(exact)) == RESULT_JSON_LIMIT
    assert exact["truncated"] is False


def test_first_row_over_two_mib_is_result_too_large():
    with pytest.raises(ConsoleError) as caught:
        encode_rows({"read_at": "t"}, ["t"], iter([("x" * 3_000_000,)]))
    assert caught.value.code == "result_too_large"


def test_peek_after_row_limit_does_not_become_eof():
    def rows():
        for index in range(1000):
            yield (index,)
        raise ConsoleError("query_timeout")

    with pytest.raises(ConsoleError) as caught:
        encode_rows({"read_at": "t"}, ["n"], rows())
    assert caught.value.code == "query_timeout"


def test_peek_invalid_value_after_row_limit_fails_closed():
    def rows():
        for index in range(1000):
            yield (index,)
        yield (float("nan"),)

    with pytest.raises(ConsoleError) as caught:
        encode_rows({"read_at": "t"}, ["n"], rows())
    assert caught.value.code == "data_invalid"


def test_sixty_five_columns_rejected_before_rows():
    columns = [f"c{index}" for index in range(65)]
    with pytest.raises(ConsoleError) as caught:
        encode_rows({"read_at": "t"}, columns, iter(()))
    assert caught.value.code == "invalid_request"


def test_protected_input_budget_is_sixty_four_mib():
    from agent_alfred.database_console.budget import PROTECTED_INPUT_LIMIT
    from agent_alfred.database_console.project import Budget

    budget = Budget()
    budget.add(PROTECTED_INPUT_LIMIT)
    with pytest.raises(ConsoleError) as caught:
        budget.add(1)
    assert caught.value.code == "input_too_large"


def test_open_source_rejects_corrupt_and_does_not_change_journal(tmp_path):
    import sqlite3

    from agent_alfred.database_console.project import open_source

    path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(x)")
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    identity = (path.stat().st_dev, path.stat().st_ino)
    source = open_source(str(path), identity)
    try:
        assert source.execute("PRAGMA query_only").fetchone()[0] == 1
        assert source.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert source.execute("PRAGMA journal_mode").fetchone()[0] == journal
    finally:
        source.close()
    later = sqlite3.connect(path)
    try:
        assert later.execute("PRAGMA journal_mode").fetchone()[0] == journal
    finally:
        later.close()
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not-a-database")
    with pytest.raises(ConsoleError) as caught:
        open_source(str(bad), (bad.stat().st_dev, bad.stat().st_ino))
    assert caught.value.code == "database_unavailable"


def test_truncated_payload_stays_within_two_mib_after_flags():
    from agent_alfred.database_console.budget import RESULT_JSON_LIMIT
    from agent_alfred.database_console.encode import dump

    envelope = {
        "query_id": "q",
        "instance_id": "i",
        "memory_revision": "1",
        "protection_version": "1",
        "read_at": "t",
        "objects": ["diag_sessions"],
        "coverage": {},
    }
    payload = encode_rows(envelope, ["t"], iter([("x" * 80_000,) for _ in range(40)]))
    assert len(dump(payload)) <= RESULT_JSON_LIMIT
    assert payload["truncated"] is True


def test_hard_heap_limit_applies_only_in_worker_process():
    import subprocess
    import sys

    from agent_alfred.database_console.budget import SQLITE_HEAP_LIMIT
    from agent_alfred.database_console.sqlite_limits import current_hard_heap_limit

    before = current_hard_heap_limit()
    script = (
        "from agent_alfred.database_console.sqlite_limits import "
        "apply_heap_limit, current_hard_heap_limit\n"
        "apply_heap_limit()\n"
        "print(current_hard_heap_limit())\n"
    )
    out = subprocess.check_output([sys.executable, "-c", script], env={})
    assert int(out.strip()) == SQLITE_HEAP_LIMIT
    assert current_hard_heap_limit() == before


def test_memory_temp_store_sort_leaves_no_temp_files(tmp_path, monkeypatch):
    from agent_alfred.database_console.project import open_dataset

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    conn = open_dataset()
    try:
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        options = {row[0] for row in conn.execute("PRAGMA compile_options")}
        assert any(item.startswith("TEMP_STORE=") for item in options)
        conn.executemany(
            "INSERT INTO diag_sessions(session_id, created_at, activity_revision) "
            "VALUES (?,?,?)",
            [(f"s-{index}", "2026-01-01T00:00:00Z", index) for index in range(2000)],
        )
        list(
            conn.execute(
                "SELECT session_id FROM diag_sessions "
                "ORDER BY session_id || created_at || activity_revision"
            )
        )
        leftovers = [
            path.name
            for path in tmp_path.iterdir()
            if path.name.startswith("etilqs") or path.name.startswith("sqlite")
        ]
        assert leftovers == []
    finally:
        conn.close()


def test_packaged_worker_entry_executes_select(tmp_path):
    import json
    import sqlite3
    import subprocess
    import sys
    from importlib.resources import files

    from agent_alfred import schema

    path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(path)
    schema.migrate(conn)
    conn.close()
    identity = (path.stat().st_dev, path.stat().st_ino)
    worker = files("agent_alfred").joinpath("database_console", "worker.py")
    request = {
        "sql": "SELECT 1 AS n",
        "db_path": str(path),
        "identity": list(identity),
        "secrets": [],
        "min_length": 8,
        "remaining_s": 5,
        "read_at": "t",
        "query_id": "installed",
        "instance_id": "installed",
        "memory_revision": "0",
        "protection_version": "0",
        "barriers": {},
    }
    ran = subprocess.run(
        [sys.executable, str(worker)],
        input=json.dumps(request).encode(),
        env={},
        capture_output=True,
        check=True,
    )
    body = json.loads(ran.stdout)
    assert body["rows"][0][0]["value"] == "1"
    assert "error" not in body
