"""The schema instant parser; storage writers retain their original contracts."""
from __future__ import annotations

from datetime import datetime


def parse_instant(value: str) -> datetime:
    """Parse a stored instant. Naive values are rejected, not localized."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not an aware ISO8601 instant: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"naive datetime is not an instant: {value!r}")
    return parsed
