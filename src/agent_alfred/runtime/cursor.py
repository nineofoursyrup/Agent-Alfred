"""The one cursor codec shared by every paged read.

The reads that page -- the runs page, the MainBar, the Session inbox and a
Session's messages -- each own their payload (their version, their kind,
their position fields) but none of them owns the envelope or the SQLite
integer domain shared by every position: canonical JSON, URL-safe base64,
and the one malformed exception are the same for all of them, and private
copies are how versions, limits and error words drift one at a time. Session
binding and paired-position shape remain facts owned by each read.
"""

from __future__ import annotations

import json
from base64 import b64decode, urlsafe_b64encode
from typing import Any

__all__ = [
    "MAX_SQLITE_CURSOR_POSITION",
    "MalformedCursor",
    "decode_cursor",
    "encode_cursor",
    "parse_cursor_position_int",
]

MAX_SQLITE_CURSOR_POSITION = 2**63 - 1


class MalformedCursor(ValueError):
    """The cursor cannot be decoded or does not belong to this read."""


def parse_cursor_position_int(value: Any) -> int:
    """Return one exact JSON integer in SQLite's non-negative key domain."""
    if (
        type(value) is not int
        or value < 0
        or value > MAX_SQLITE_CURSOR_POSITION
    ):
        raise MalformedCursor("cursor position is outside the SQLite integer domain")
    return value


def encode_cursor(payload: dict[str, Any]) -> str:
    """The opaque token: canonical JSON, URL-safe base64.

    The separators are part of the wire -- a token a browser already holds
    must decode to exactly the payload that produced it.
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str, *, version: int, kind: str) -> dict[str, Any]:
    """Decode one token, refusing anything that is not this read's.

    A token from another read (a different ``kind`` or ``version``) and a
    token that cannot be decoded at all fail the same way -- closed, with
    :class:`MalformedCursor` -- because both are the same sentence: this is
    not a position this read can continue from.
    """
    try:
        encoded = cursor.encode("ascii")
        raw = b64decode(encoded, altchars=b"-_", validate=True).decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        raise MalformedCursor("cursor is not a readable token") from None
    if (
        not isinstance(payload, dict)
        or type(payload.get("v")) is not int
        or payload.get("v") != version
        or payload.get("k") != kind
    ):
        raise MalformedCursor("cursor does not belong to this read")
    try:
        canonical = encode_cursor(payload)
    except Exception:
        raise MalformedCursor("cursor is not canonical") from None
    if canonical != cursor:
        raise MalformedCursor("cursor is not canonical")
    return payload
