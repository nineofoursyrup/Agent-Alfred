"""Prove SQLite heap and temp-store capabilities, or refuse diagnosis."""

from __future__ import annotations

import ctypes
import sqlite3

from agent_alfred.database_console.budget import SQLITE_HEAP_LIMIT


def _hard_heap_limit64():
    import _sqlite3

    lib = ctypes.CDLL(_sqlite3.__file__)
    fn = lib.sqlite3_hard_heap_limit64
    fn.argtypes = [ctypes.c_int64]
    fn.restype = ctypes.c_int64
    return fn


def current_hard_heap_limit() -> int:
    return int(_hard_heap_limit64()(-1))


def hard_heap_limit_supported() -> bool:
    try:
        current_hard_heap_limit()
    except OSError, AttributeError, ValueError:
        return False
    return True


def apply_heap_limit() -> None:
    fn = _hard_heap_limit64()
    fn(SQLITE_HEAP_LIMIT)
    if fn(-1) != SQLITE_HEAP_LIMIT:
        raise OSError("sqlite3_hard_heap_limit64")


def heap_limit_available() -> bool:
    return hard_heap_limit_supported()


def json_available(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT json('[]'), json_extract('{\"a\":1}','$.a')"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row == ("[]", 1)


def required_functions_available(conn: sqlite3.Connection) -> bool:
    probes = (
        "SELECT date(), time(), datetime()",
        "SELECT abs(-1), round(1.2,1), coalesce(NULL,2)",
        "SELECT ifnull(NULL,3), nullif(1,1), typeof(1)",
        "SELECT length('a'), lower('A'), upper('a'), trim(' a ')",
        "SELECT substr('ab',1,1), replace('a','a','b'), instr('a','a'), hex(x'00')",
        "SELECT like('a','a'), glob('a','a')",
        "SELECT count(*), sum(1), total(1), avg(1), min(1), max(1)",
        "SELECT group_concat(1)",
        "SELECT date('2020-01-01'), time('00:00:00')",
        "SELECT datetime('2020-01-01'), unixepoch('2020-01-01')",
        "SELECT strftime('%Y','2020-01-01')",
        "SELECT json_array(1), json_object('a',1), json_type('[]')",
        "SELECT json_valid('[]'), json_array_length('[]'), json_quote('a')",
        "SELECT row_number() OVER (ORDER BY 1)",
    )
    for sql in probes:
        try:
            conn.execute(sql).fetchall()
        except sqlite3.Error:
            return False
    return True


def memory_temp_store(conn: sqlite3.Connection) -> None:
    """TEMP_STORE=0 ignores the memory pragma; absent flags use SQLite default 1."""
    options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    if "OMIT_SHARED_CACHE" in options:
        raise OSError("SQLite cannot share the protected memory dataset")
    settings = {option for option in options if option.startswith("TEMP_STORE=")}
    if settings and not settings <= {"TEMP_STORE=1", "TEMP_STORE=2", "TEMP_STORE=3"}:
        raise OSError("SQLite cannot guarantee memory-only temporary storage")
    conn.execute("PRAGMA temp_store=MEMORY")
    if conn.execute("PRAGMA temp_store").fetchone()[0] != 2:
        raise OSError("SQLite memory temporary storage unavailable")
