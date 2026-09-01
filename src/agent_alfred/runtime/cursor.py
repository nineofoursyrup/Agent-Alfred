"""The one cursor codec shared by every paged read.

The reads that page -- the runs page, the MainBar, the Session inbox and a
Session's messages -- each own their payload (their version, their kind,
their position fields) but none of them owns the envelope: canonical JSON,
URL-safe base64, and the one malformed exception are the same for all of
them, and two private copies are how versions and error words drift one at
a time. What this module deliberately does *not* do is validate positions
or bind cursors to Sessions: those are per-read facts, and a codec that
"helped" with them would grow into a paging framework nobody decided on.
"""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

__all__ = ["MalformedCursor", "decode_cursor", "encode_cursor"]


class MalformedCursor(ValueError):
    """The cursor cannot be decoded or does not belong to this read."""


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
        raw = urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
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
    return payload
