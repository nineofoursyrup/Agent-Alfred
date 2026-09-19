"""Fixed byte accounting for diagnostic input. Not Python objects or SQLite pages.

Framing (stable, tested at the 2 MiB / 64 MiB seams):

- NULL: 0 value bytes
- INTEGER: 8-byte two's-complement width
- REAL: 8-byte IEEE-754 width
- TEXT: strict UTF-8 byte length
- BLOB: raw byte length
- each field: 8 bytes of type/length overhead
- each row: 16 bytes of row overhead
- each prepared object: 64 bytes plus UTF-8 of the object name and each
  column name

These meters cover source values, protected rows, and column/object
metadata included in the 64 MiB input budget. Result JSON uses the actual
UTF-8 success body instead.
"""

from __future__ import annotations

INTEGER_WIDTH = 8
REAL_WIDTH = 8
FIELD_OVERHEAD = 8
ROW_OVERHEAD = 16
OBJECT_OVERHEAD = 64

SQL_TEXT_LIMIT = 64 * 1024
SOURCE_VALUE_LIMIT = 2 * 1024 * 1024
SOURCE_ROW_LIMIT = 2 * 1024 * 1024
PROTECTED_INPUT_LIMIT = 64 * 1024 * 1024
SQLITE_HEAP_LIMIT = 128 * 1024 * 1024
RESULT_JSON_LIMIT = 2 * 1024 * 1024
RESULT_ROW_LIMIT = 1000
RESULT_COLUMN_LIMIT = 64
HANDLE_TTL_S = 30.0
TERMINAL_TTL_S = 60.0
HANDLE_CAPACITY = 64
EXECUTE_BUDGET_S = 5.0
CANCEL_BUDGET_S = 1.0
HTTP_IO_S = 1.0


def value_bytes(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise TypeError("bool")
    if isinstance(value, int):
        return INTEGER_WIDTH
    if isinstance(value, float):
        return REAL_WIDTH
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if encoded.decode("utf-8") != value:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "round-trip")
        return len(encoded)
    if isinstance(value, (bytes, memoryview, bytearray)):
        return len(value)
    raise TypeError(type(value).__name__)


def field_bytes(value: object) -> int:
    return FIELD_OVERHEAD + value_bytes(value)


def row_bytes(values: tuple[object, ...]) -> int:
    return ROW_OVERHEAD + sum(field_bytes(value) for value in values)


def object_meta_bytes(name: str, columns: tuple[str, ...]) -> int:
    return (
        OBJECT_OVERHEAD
        + len(name.encode("utf-8"))
        + sum(len(column.encode("utf-8")) for column in columns)
    )
