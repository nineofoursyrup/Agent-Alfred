"""Small shared waits for legacy thread-driven deterministic tests."""

from __future__ import annotations

import threading
from collections.abc import Callable


class EnteredEvent:
    """An Event whose wait boundary is itself observable by another thread."""

    def __init__(self, *, released: bool = False) -> None:
        self.entered = threading.Event()
        self._release = threading.Event()
        if released:
            self._release.set()

    def wait(self, timeout: float | None = None) -> bool:
        self.entered.set()
        return self._release.wait(timeout)

    def set(self) -> None:
        self._release.set()

    def clear(self) -> None:
        self.entered.clear()
        self._release.clear()

    def is_set(self) -> bool:
        return self._release.is_set()


class ProbeInterruptedUnstartedThread(threading.Thread):
    """A start refusal whose first legal-effect probe is interrupted."""

    def __init__(
        self,
        *,
        start_failure: BaseException,
        probe_failure: BaseException,
        target: Callable[[], None] | None = None,
        name: str | None = None,
        daemon: bool = True,
    ) -> None:
        super().__init__(target=target, name=name, daemon=daemon)
        self.start_failure = start_failure
        self.probe_failure: BaseException | None = probe_failure
        self.start_calls = 0
        self.join_calls = 0
        self.probe_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        raise self.start_failure

    def join(self, timeout: float | None = None) -> None:
        self.join_calls += 1
        failure = self.probe_failure
        if failure is not None:
            self.probe_failure = None
            raise failure
        super().join(timeout)

    def start_effect_happened(self) -> bool:
        self.probe_calls += 1
        failure = self.probe_failure
        if failure is not None:
            self.probe_failure = None
            raise failure
        return False
