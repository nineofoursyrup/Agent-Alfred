"""Injectable clocks. Wall time is for display; monotonic is for deadlines."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_utc(self) -> datetime: ...

    def local_now(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def wall_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def local_now(self) -> datetime:
        return datetime.now().astimezone()


class FakeClock:
    def __init__(
        self,
        *,
        monotonic_value: float = 0.0,
        wall: datetime | None = None,
        local: datetime | None = None,
    ):
        self.monotonic_value = monotonic_value
        self.wall = wall or datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        self.local = local or self.wall

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall_utc(self) -> datetime:
        return self.wall

    def local_now(self) -> datetime:
        return self.local


def format_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
