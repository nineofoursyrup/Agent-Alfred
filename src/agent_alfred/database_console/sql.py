"""SQLite authorizer plus syntax-aware statement checks. Not a keyword denylist."""

from __future__ import annotations

import re
import sqlite3

from agent_alfred.database_console.catalog import OBJECT_BY_NAME
from agent_alfred.database_console.errors import ConsoleError

ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "round",
        "coalesce",
        "ifnull",
        "nullif",
        "typeof",
        "min",
        "max",
        "length",
        "lower",
        "upper",
        "trim",
        "ltrim",
        "rtrim",
        "substr",
        "replace",
        "instr",
        "hex",
        "like",
        "glob",
        "count",
        "sum",
        "total",
        "avg",
        "group_concat",
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        "date",
        "time",
        "datetime",
        "julianday",
        "unixepoch",
        "strftime",
        "json",
        "json_array",
        "json_object",
        "json_extract",
        "json_type",
        "json_valid",
        "json_array_length",
        "json_quote",
    }
)

_HIDDEN_COLUMNS = frozenset({"rowid", "oid", "_rowid_"})
_SCHEMA_TABLES = frozenset(
    {"sqlite_schema", "sqlite_master", "sqlite_temp_master", "sqlite_temp_schema"}
)
_DENIED_TABLES = frozenset({"json_each", "json_tree", "pragma_table_info"})

_STRING = 1
_IDENT = 2
_OTHER = 3


def _tokens(sql: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i = sql.find("\n", i)
            i = n if i < 0 else i + 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ConsoleError("sql_rejected")
            i = end + 2
            continue
        if ch == "'":
            i += 1
            buf = ["'"]
            while i < n:
                if sql[i] == "'":
                    buf.append("'")
                    i += 1
                    if i < n and sql[i] == "'":
                        buf.append("'")
                        i += 1
                        continue
                    break
                buf.append(sql[i])
                i += 1
            else:
                raise ConsoleError("sql_rejected")
            out.append((_STRING, "".join(buf)))
            continue
        if ch in '"`[':
            close = "]" if ch == "[" else ch
            i += 1
            buf = [ch]
            while i < n:
                if sql[i] == close:
                    buf.append(sql[i])
                    i += 1
                    if close in {'"', "`"} and i < n and sql[i] == close:
                        buf.append(close)
                        i += 1
                        continue
                    break
                buf.append(sql[i])
                i += 1
            else:
                raise ConsoleError("sql_rejected")
            out.append((_IDENT, "".join(buf)))
            continue
        if ch in "?:@$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if ch == "?" or j > i + 1:
                raise ConsoleError("sql_rejected")
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            number = re.match(
                r"(?:0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*|"
                r"(?:[0-9](?:_?[0-9])*)?(?:\.[0-9](?:_?[0-9])*)"
                r"(?:[eE][+-]?[0-9](?:_?[0-9])*)?|"
                r"[0-9](?:_?[0-9])*(?:\.)?(?:[eE][+-]?[0-9](?:_?[0-9])*)?)",
                sql[i:],
            )
            assert number is not None
            j = i + len(number[0])
            out.append((_OTHER, sql[i:j]))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            out.append((_OTHER, sql[i:j]))
            i = j
            continue
        out.append((_OTHER, ch))
        i += 1
    return out


_KEYWORD_PARENS = frozenset(
    {
        "as",
        "materialized",
        "select",
        "from",
        "where",
        "on",
        "and",
        "or",
        "not",
        "in",
        "exists",
        "cast",
        "case",
        "when",
        "then",
        "else",
        "end",
        "over",
        "partition",
        "union",
        "except",
        "intersect",
        "filter",
        "window",
        "using",
        "values",
        "between",
        "is",
        "by",
        "limit",
        "offset",
        "group",
        "order",
        "having",
        "where",
        "from",
        "join",
        "on",
        "into",
        "set",
        "values",
        "with",
        "select",
        "as",
        "over",
        "partition",
        "rows",
        "range",
        "unbounded",
        "preceding",
        "following",
        "current",
        "row",
        "filter",
        "window",
        "when",
        "then",
        "else",
        "end",
        "case",
        "cast",
        "and",
        "or",
        "not",
        "in",
        "exists",
        "using",
        "union",
        "except",
        "intersect",
        "all",
        "distinct",
        "left",
        "right",
        "inner",
        "outer",
        "cross",
        "natural",
        "full",
        "nulls",
        "first",
        "last",
        "asc",
        "desc",
        "collate",
        "escape",
        "isnull",
        "notnull",
        "is",
        "between",
        "returning",
    }
)

# name -> (min_argc, max_argc or None for unbounded)
ALLOWED_ARITY: dict[str, tuple[int, int | None]] = {
    "abs": (1, 1),
    "round": (1, 2),
    "coalesce": (2, None),
    "ifnull": (2, 2),
    "nullif": (2, 2),
    "typeof": (1, 1),
    "min": (1, None),
    "max": (1, None),
    "length": (1, 1),
    "lower": (1, 1),
    "upper": (1, 1),
    "trim": (1, 2),
    "ltrim": (1, 2),
    "rtrim": (1, 2),
    "substr": (2, 3),
    "replace": (3, 3),
    "instr": (2, 2),
    "hex": (1, 1),
    "like": (2, 3),
    "glob": (2, 2),
    "count": (1, 1),
    "sum": (1, 1),
    "total": (1, 1),
    "avg": (1, 1),
    "group_concat": (1, 2),
    "row_number": (0, 0),
    "rank": (0, 0),
    "dense_rank": (0, 0),
    "percent_rank": (0, 0),
    "cume_dist": (0, 0),
    "ntile": (1, 1),
    "lag": (1, 3),
    "lead": (1, 3),
    "first_value": (1, 1),
    "last_value": (1, 1),
    "nth_value": (2, 2),
    "date": (0, None),
    "time": (0, None),
    "datetime": (0, None),
    "julianday": (0, None),
    "unixepoch": (0, None),
    "strftime": (1, None),
    "json": (1, 1),
    "json_array": (0, None),
    "json_object": (0, None),
    "json_extract": (2, None),
    "json_type": (1, 2),
    "json_valid": (1, 1),
    "json_array_length": (1, 2),
    "json_quote": (1, 1),
}
assert set(ALLOWED_ARITY) == ALLOWED_FUNCTIONS


def _matching_paren(tokens: list[tuple[int, str]], open_at: int) -> int:
    depth = 0
    for index in range(open_at, len(tokens)):
        kind, value = tokens[index]
        if kind == _OTHER and value == "(":
            depth += 1
        elif kind == _OTHER and value == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ConsoleError("sql_rejected")


def _top_argc(inner: list[tuple[int, str]]) -> int:
    if not inner:
        return 0
    depth = 0
    args = 1
    for kind, value in inner:
        if kind == _OTHER and value == "(":
            depth += 1
        elif kind == _OTHER and value == ")":
            depth -= 1
        elif kind == _OTHER and value == "," and depth == 0:
            args += 1
    return args


def _bare_name(token: tuple[int, str] | None) -> str | None:
    if token is None:
        return None
    kind, value = token
    if kind == _IDENT:
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return value[1:-1].replace('""', '"').lower()
        if len(value) >= 2 and value[0] == "`" and value[-1] == "`":
            return value[1:-1].replace("``", "`").lower()
        if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
            return value[1:-1].lower()
        return value.lower()
    if kind == _STRING and len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'").lower()
    if kind == _OTHER and value and (value[0].isalpha() or value[0] == "_"):
        return value.lower()
    return None


def _check_calls(tokens: list[tuple[int, str]]) -> None:
    i = 0
    n = len(tokens)
    while i < n:
        name = _bare_name(tokens[i])
        if name is not None and i + 1 < n and tokens[i + 1] == (_OTHER, "("):
            close = _matching_paren(tokens, i + 1)
            inner = tokens[i + 2 : close]
            after = tokens[close + 1] if close + 1 < n else None
            as_subquery = (
                _bare_name(after) == "as"
                and close + 2 < n
                and (
                    tokens[close + 2] == (_OTHER, "(")
                    or _bare_name(tokens[close + 2]) in {"materialized", "not"}
                )
            )
            if as_subquery:
                _check_calls(inner)
                i = close + 1
                continue
            if name == "cast" and tokens[i][0] == _OTHER:
                # CAST's AS at this nesting level separates an expression from
                # SQLite's type-name grammar (including precision/length).
                # Only the expression can contain function calls. SQLite still
                # parses the complete statement and authorizes every operation.
                depth = 0
                for split, token in enumerate(inner):
                    if token == (_OTHER, "("):
                        depth += 1
                    elif token == (_OTHER, ")"):
                        depth -= 1
                    elif depth == 0 and token[0] == _OTHER and token[1].lower() == "as":
                        _check_calls(inner[:split])
                        break
                else:
                    raise ConsoleError("sql_rejected")
                i = close + 1
                continue
            if name in _KEYWORD_PARENS:
                _check_calls(inner)
                i = close + 1
                continue
            argc = _top_argc(inner)
            if name not in ALLOWED_FUNCTIONS:
                raise ConsoleError("sql_rejected")
            low, high = ALLOWED_ARITY[name]
            if argc < low or (high is not None and argc > high):
                raise ConsoleError("sql_rejected")
            if name == "json_object" and argc % 2 != 0:
                raise ConsoleError("sql_rejected")
            _check_calls(inner)
            i = close + 1
            continue
        i += 1


def _reject_schema_qualification(tokens: list[tuple[int, str]]) -> None:
    i = 0
    n = len(tokens)
    while i + 2 < n:
        left = _bare_name(tokens[i])
        if left is not None and tokens[i + 1] == (_OTHER, "."):
            right = _bare_name(tokens[i + 2])
            if right is not None and (
                right in OBJECT_BY_NAME
                or right in _SCHEMA_TABLES
                or left in {"main", "temp", "file"}
            ):
                raise ConsoleError("sql_rejected")
        i += 1


def inspect_sql(sql: str) -> None:
    if not isinstance(sql, str) or "\0" in sql:
        raise ConsoleError("invalid_request")
    encoded = sql.encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ConsoleError("invalid_request")
    if encoded.decode("utf-8") != sql:
        raise ConsoleError("invalid_request")
    tokens = _tokens(sql)
    words = [value.upper() for kind, value in tokens if kind == _OTHER]
    if not words:
        raise ConsoleError("sql_rejected")
    statements = 0
    start = True
    starters: list[str] = []
    for kind, value in tokens:
        if kind == _OTHER and value == ";":
            start = True
            continue
        if start:
            statements += 1
            start = False
            if kind == _OTHER:
                starters.append(value.upper())
            else:
                raise ConsoleError("sql_rejected")
    if statements != 1:
        raise ConsoleError("sql_rejected")
    if starters[0] not in {"SELECT", "WITH"}:
        raise ConsoleError("sql_rejected")
    for index, word in enumerate(words):
        if (
            word == "WITH"
            and index + 1 < len(words)
            and words[index + 1] == "RECURSIVE"
        ):
            raise ConsoleError("sql_rejected")
    _reject_schema_qualification(tokens)
    _check_calls(tokens)


class SqlGuard:
    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.denied = False

    def __call__(
        self,
        action: int,
        arg1: str | None,
        arg2: str | None,
        db: str | None,
        _source: str | None,
    ) -> int:
        if action in (sqlite3.SQLITE_SELECT,):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            column = (arg2 or "").lower()
            database = (db or "main").lower()
            if database not in {"main", ""}:
                self.denied = True
                return sqlite3.SQLITE_DENY
            if table in _SCHEMA_TABLES or table in _DENIED_TABLES:
                self.denied = True
                return sqlite3.SQLITE_DENY
            if table in OBJECT_BY_NAME:
                spec = OBJECT_BY_NAME[table]
                if column in _HIDDEN_COLUMNS or (
                    column and column not in spec.column_names
                ):
                    self.denied = True
                    return sqlite3.SQLITE_DENY
                self.tables.add(table)
                return sqlite3.SQLITE_OK
            # CTE or empty FROM; unknown production names are denied.
            if (
                table
                and table not in OBJECT_BY_NAME
                and not re.fullmatch(r"[a-z_][a-z0-9_]*", table)
            ):
                self.denied = True
                return sqlite3.SQLITE_DENY
            if table in OBJECT_BY_NAME:
                self.tables.add(table)
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            name = (arg2 or arg1 or "").lower()
            if name not in ALLOWED_FUNCTIONS:
                self.denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.denied = True
        return sqlite3.SQLITE_DENY


def referenced_objects(conn: sqlite3.Connection, sql: str) -> tuple[str, ...]:
    inspect_sql(sql)
    guard = SqlGuard()
    conn.set_authorizer(guard)
    try:
        # EXPLAIN prepares the real statement and invokes SQLite's authorizer,
        # but never evaluates user expressions against the empty discovery DB.
        conn.execute("EXPLAIN " + sql).close()
    except sqlite3.DatabaseError as exc:
        message = str(exc).lower()
        if guard.denied or "recursive" in message or "circular" in message:
            raise ConsoleError("sql_rejected") from None
        if (
            "no such table" in message
            or "no such column" in message
            or "no such function" in message
        ):
            raise ConsoleError("sql_rejected") from None
        raise ConsoleError("sql_error") from None
    finally:
        conn.set_authorizer(None)
    if guard.denied:
        raise ConsoleError("sql_rejected")
    return tuple(sorted(guard.tables))
