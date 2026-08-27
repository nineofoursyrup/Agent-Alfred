"""Retrieval gate. Slice 1 is a no-op stub: no memory is fetched."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def evaluate(_messages: Sequence[Any]) -> None:
    """Slice-1 stub. Returns None so the loop does not inject retrieved facts."""
    return None
