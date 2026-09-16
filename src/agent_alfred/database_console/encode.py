"""Typed diagnostic cells and bounded success JSON. Size is UTF-8 of the body."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from typing import Any

from agent_alfred.database_console.budget import (
    RESULT_COLUMN_LIMIT,
    RESULT_JSON_LIMIT,
    RESULT_ROW_LIMIT,
)
from agent_alfred.database_console.errors import ConsoleError


def cell(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        raise ConsoleError("data_invalid")
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConsoleError("data_invalid")
        return {"type": "real", "value": value}
    if isinstance(value, str):
        value.encode("utf-8").decode("utf-8")
        return {"type": "text", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {"type": "blob", "hex": raw.hex(), "byte_length": len(raw)}
    raise ConsoleError("data_invalid")


def dump(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _payload(
    envelope: dict[str, Any],
    columns: list[str],
    rows: list[list[dict[str, object]]],
    *,
    truncated: bool,
    reasons: list[str],
) -> dict[str, Any]:
    return {
        **envelope,
        "columns": columns,
        "rows": rows,
        "returned_rows": len(rows),
        "truncated": truncated,
        "truncation_reasons": reasons,
    }


def encode_rows(
    envelope: dict[str, Any],
    columns: list[str],
    rows: Iterator[tuple[object, ...]],
) -> dict[str, Any]:
    if len(columns) > RESULT_COLUMN_LIMIT:
        raise ConsoleError("invalid_request")
    encoded: list[list[dict[str, object]]] = []
    empty = _payload(envelope, columns, [], truncated=False, reasons=[])
    if len(dump(empty)) > RESULT_JSON_LIMIT:
        raise ConsoleError("result_too_large")
    while True:
        try:
            row = next(rows)
        except StopIteration:
            return (
                empty
                if not encoded
                else _payload(envelope, columns, encoded, truncated=False, reasons=[])
            )
        encoded_row = [cell(value) for value in row]
        if len(encoded_row) != len(columns):
            raise ConsoleError("data_invalid")
        candidate = encoded + [encoded_row]
        complete = _payload(envelope, columns, candidate, truncated=False, reasons=[])
        if len(dump(complete)) > RESULT_JSON_LIMIT:
            if not encoded:
                raise ConsoleError("result_too_large")
            return _fit_truncated(envelope, columns, encoded, ["bytes"])
        encoded = candidate
        if len(encoded) < RESULT_ROW_LIMIT:
            continue
        try:
            extra = next(rows)
        except StopIteration:
            return _payload(envelope, columns, encoded, truncated=False, reasons=[])
        for value in extra:
            cell(value)
        return _fit_truncated(envelope, columns, encoded, ["rows"])


def _fit_truncated(
    envelope: dict[str, Any],
    columns: list[str],
    encoded: list[list[dict[str, object]]],
    reasons: list[str],
) -> dict[str, Any]:
    while True:
        payload = _payload(envelope, columns, encoded, truncated=True, reasons=reasons)
        if len(dump(payload)) <= RESULT_JSON_LIMIT:
            if not encoded:
                raise ConsoleError("result_too_large")
            return payload
        if not encoded:
            raise ConsoleError("result_too_large")
        encoded = encoded[:-1]
