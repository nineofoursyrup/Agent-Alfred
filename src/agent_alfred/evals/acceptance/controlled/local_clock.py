"""Fail-closed discontinuity sampling for a live macOS trusted-host session."""

import ctypes
import sys
from datetime import UTC, datetime
from time import monotonic


class MacClock:
    """Read paired awake/continuous clocks; no system settings are changed."""

    def __init__(self):
        if sys.platform != "darwin":
            raise ValueError("local_macos_clock_required")
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self.absolute = library.mach_absolute_time
        self.continuous = library.mach_continuous_time
        for function in (self.absolute, self.continuous):
            function.argtypes = []
            function.restype = ctypes.c_uint64

    def sample(self):
        before = self.absolute()
        continuous = self.continuous()
        after = self.absolute()
        return {
            "wall": datetime.now(UTC).timestamp(),
            "awake": monotonic(),
            "sleep_interval": [continuous - after, continuous - before],
        }


class ContinuityGuard:
    def __init__(self):
        self.previous = None
        self.failed = False

    def observe(self, wall_seconds, awake_seconds, sleep_interval=None):
        # macOS Python monotonic uses mach_absolute_time (awake time). Wall time
        # also advances while asleep. A >2s mismatch is conservatively stopped,
        # including forward clock changes; it never extends the original grant.
        if self.failed:
            raise ValueError("local_clock_continuity_lost")
        current = (wall_seconds, awake_seconds, sleep_interval)
        if self.previous is not None:
            wall = wall_seconds - self.previous[0]
            awake = awake_seconds - self.previous[1]
            if wall < 0 or awake < 0 or abs(wall - awake) > 2:
                self.failed = True
                raise ValueError("local_clock_continuity_lost")
            prior_interval = self.previous[2]
            if (
                prior_interval is not None
                and sleep_interval is not None
                and (
                    sleep_interval[0] > prior_interval[1]
                    or sleep_interval[1] < prior_interval[0]
                )
            ):
                self.failed = True
                raise ValueError("local_clock_continuity_lost")
        self.previous = current

    def sample(self, value):
        self.observe(value["wall"], value["awake"], value["sleep_interval"])
