"""Worker-only SQLite cursor with one sqlite3_step per requested row.

Python's DB-API cursor prefetches the next row in fetchone(), which can evaluate
an error *after* a truncation probe. The public SQLite C API avoids that extra
step. It opens only the worker's fixed, already-protected memory dataset, using
the very same SQLite library and heap limit as Python's extraction connection.
See https://www.sqlite.org/c3ref/step.html and /c3ref/column_blob.html.
"""

from __future__ import annotations

import _sqlite3
import ctypes as c
import sqlite3
from contextlib import contextmanager

from agent_alfred.database_console.errors import ConsoleError
from agent_alfred.database_console.sql import SqlGuard

DATASET_URI = "file:alfred-diagnostic?mode=memory&cache=shared"
_AUTH = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_int, *([c.c_char_p] * 4))


def _api():
    lib = c.CDLL(_sqlite3.__file__)
    signatures = {
        "open_v2": (c.c_int, [c.c_char_p, c.POINTER(c.c_void_p), c.c_int, c.c_char_p]),
        "close_v2": (c.c_int, [c.c_void_p]),
        "prepare_v2": (
            c.c_int,
            [
                c.c_void_p,
                c.c_char_p,
                c.c_int,
                c.POINTER(c.c_void_p),
                c.POINTER(c.c_char_p),
            ],
        ),
        "set_authorizer": (c.c_int, [c.c_void_p, _AUTH, c.c_void_p]),
        "step": (c.c_int, [c.c_void_p]),
        "finalize": (c.c_int, [c.c_void_p]),
        "column_count": (c.c_int, [c.c_void_p]),
        "column_name": (c.c_char_p, [c.c_void_p, c.c_int]),
        "column_type": (c.c_int, [c.c_void_p, c.c_int]),
        "column_int64": (c.c_int64, [c.c_void_p, c.c_int]),
        "column_double": (c.c_double, [c.c_void_p, c.c_int]),
        "column_text": (c.c_void_p, [c.c_void_p, c.c_int]),
        "column_blob": (c.c_void_p, [c.c_void_p, c.c_int]),
        "column_bytes": (c.c_int, [c.c_void_p, c.c_int]),
        "errcode": (c.c_int, [c.c_void_p]),
    }
    for name, (result, args) in signatures.items():
        fn = getattr(lib, "sqlite3_" + name)
        fn.restype, fn.argtypes = result, args
    return lib


class Cursor:
    def __init__(self, lib, database, statement):
        self.lib, self.database, self.statement = lib, database, statement
        self.columns = [
            lib.sqlite3_column_name(statement, i).decode("utf-8")
            for i in range(lib.sqlite3_column_count(statement))
        ]
        self.done = False

    def fetchone(self):
        if self.done:
            return None
        rc = self.lib.sqlite3_step(self.statement)
        if rc == sqlite3.SQLITE_DONE:
            self.done = True
            return None
        if rc != sqlite3.SQLITE_ROW:
            raise ConsoleError(
                "resource_limit" if rc == sqlite3.SQLITE_NOMEM else "sql_error"
            )
        return tuple(self._value(i) for i in range(len(self.columns)))

    def _value(self, index):
        lib, stmt = self.lib, self.statement
        kind = lib.sqlite3_column_type(stmt, index)
        if kind == 5:  # SQLITE_NULL
            return None
        if kind == 1:  # SQLITE_INTEGER
            return lib.sqlite3_column_int64(stmt, index)
        if kind == 2:  # SQLITE_FLOAT
            return lib.sqlite3_column_double(stmt, index)
        pointer = (
            lib.sqlite3_column_text(stmt, index)
            if kind == 3
            else lib.sqlite3_column_blob(stmt, index)
        )
        size = lib.sqlite3_column_bytes(stmt, index)
        if lib.sqlite3_errcode(self.database) == sqlite3.SQLITE_NOMEM:
            raise ConsoleError("resource_limit")
        raw = c.string_at(pointer, size) if size else b""
        try:
            return raw.decode("utf-8") if kind == 3 else raw
        except UnicodeError:
            raise ConsoleError("data_invalid") from None


@contextmanager
def query(sql: str):
    lib = _api()
    database, statement = c.c_void_p(), c.c_void_p()
    guard = SqlGuard()

    @_AUTH
    def authorize(_context, action, a, b, db, origin):
        try:
            return guard(
                action, *(v.decode("utf-8") if v else None for v in (a, b, db, origin))
            )
        except Exception:
            return sqlite3.SQLITE_DENY

    try:
        # READWRITE | URI | MEMORY | SHAREDCACHE: no CREATE, no source path.
        flags = 0x2 | 0x40 | 0x80 | 0x20000
        if lib.sqlite3_open_v2(DATASET_URI.encode(), c.byref(database), flags, None):
            raise ConsoleError("database_unavailable")
        # Trusted initialization, before the user statement is prepared.
        for init in ("PRAGMA temp_store=MEMORY", "PRAGMA query_only=ON"):
            if lib.sqlite3_prepare_v2(
                database, init.encode(), -1, c.byref(statement), None
            ):
                raise ConsoleError("database_unavailable")
            if lib.sqlite3_step(statement) != sqlite3.SQLITE_DONE:
                raise ConsoleError("database_unavailable")
            lib.sqlite3_finalize(statement)
            statement = c.c_void_p()
        if lib.sqlite3_set_authorizer(database, authorize, None):
            raise ConsoleError("database_unavailable")
        encoded = sql.encode("utf-8")
        tail = c.c_char_p()
        rc = lib.sqlite3_prepare_v2(
            database, encoded, len(encoded) + 1, c.byref(statement), c.byref(tail)
        )
        if rc or not statement:
            raise ConsoleError("sql_rejected" if guard.denied else "sql_error")
        yield Cursor(lib, database, statement)
    finally:
        if statement:
            lib.sqlite3_finalize(statement)
        if database:
            lib.sqlite3_close_v2(database)
