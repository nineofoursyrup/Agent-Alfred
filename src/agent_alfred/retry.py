"""RetryPolicy for retryable ModelResults under one absolute deadline."""

from __future__ import annotations

from typing import Protocol

from agent_alfred.clock import Clock
from agent_alfred.model import (
    ModelClient,
    ModelRequest,
    ModelResult,
    mark_final_error_non_retryable,
)
from agent_alfred.stream_fallback import OverallDeadlineExceeded


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)


class RetryPolicy:
    """Retry known-retryable failures without resetting the Run deadline."""

    def __init__(
        self,
        inner: ModelClient,
        *,
        clock: Clock,
        sleeper: Sleeper,
        max_retries: int = 1,
        retry_delay_s: float = 0.25,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_delay_s < 0:
            raise ValueError("retry_delay_s must be non-negative")
        self._inner = inner
        self._clock = clock
        self._sleeper = sleeper
        self._max_retries = max_retries
        self._retry_delay_s = retry_delay_s

    def respond(
        self,
        request: ModelRequest,
        *,
        events: object | None = None,
        deadline: float | None = None,
    ) -> ModelResult:
        attempts = []
        last_failure: ModelResult | None = None
        for retry_index in range(self._max_retries + 1):
            try:
                result = self._inner.respond(
                    request, events=events, deadline=deadline
                )
            except OverallDeadlineExceeded:
                if last_failure is None:
                    raise
                return mark_final_error_non_retryable(last_failure)
            attempts.extend(result.attempts)
            combined = ModelResult(
                attempts=tuple(attempts),
                response=result.response,
                final_error=result.final_error,
            )
            if result.response is not None:
                return combined
            last_failure = combined
            error = result.final_error
            if (
                error is None
                or error.retryable is not True
                or retry_index == self._max_retries
            ):
                return combined
            if self._deadline_elapsed(deadline):
                return mark_final_error_non_retryable(combined)
            if not self._sleep_before_retry(deadline):
                return mark_final_error_non_retryable(combined)
            if self._deadline_elapsed(deadline):
                return mark_final_error_non_retryable(combined)
        raise AssertionError("retry loop exhausted without a ModelResult")

    def _deadline_elapsed(self, deadline: float | None) -> bool:
        return deadline is not None and self._clock.monotonic() >= deadline

    def _sleep_before_retry(self, deadline: float | None) -> bool:
        delay = self._retry_delay_s
        if deadline is not None:
            remaining = deadline - self._clock.monotonic()
            if remaining <= delay:
                return False
        if delay:
            self._sleeper.sleep(delay)
        return True
