"""Closed Session-validity result shared by runtime and transport."""

from __future__ import annotations

from typing import Literal

SessionValidity = Literal["valid", "invalid", "unavailable"]


def parse_session_validity(value: object) -> SessionValidity:
    """Validate a Session-validity result at an injected boundary."""
    if not isinstance(value, str):
        raise ValueError(f"invalid Session validity: {value!r}")
    match value:
        case "valid":
            return "valid"
        case "invalid":
            return "invalid"
        case "unavailable":
            return "unavailable"
        case _:
            raise ValueError(f"invalid Session validity: {value!r}")
