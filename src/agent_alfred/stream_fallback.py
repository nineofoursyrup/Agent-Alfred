"""Stream-fallback decorator. Time and fallback policy live here, not in Adapters."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from agent_alfred.clock import Clock
from agent_alfred.events import AttemptAborted, AttemptCommitted
from agent_alfred.model import ModelClient, ModelRequest, ModelResult


class OverallDeadlineExceeded(Exception):
    """No network request was sent; the run-level deadline has elapsed."""


class StreamFallback:
    """One ModelClient that may spend two Attempts on a single StepLease."""

    def __init__(
        self,
        inner: ModelClient,
        *,
        clock: Clock,
        stream: bool = False,
        stream_fallback: bool = True,
        per_attempt_timeout_s: float = 60.0,
        nonstream: ModelClient | None = None,
    ):
        self._streaming = inner
        self._nonstream = inner if nonstream is None else nonstream
        self._clock = clock
        self._stream = stream
        self._stream_fallback = stream_fallback
        self._per_attempt_timeout_s = per_attempt_timeout_s

    def respond(
        self,
        request: ModelRequest,
        *,
        events: object | None = None,
        deadline: float | None = None,
    ) -> ModelResult:
        if self._deadline_elapsed(deadline):
            raise OverallDeadlineExceeded()
        target = self._streaming if self._stream else self._nonstream
        attempt_deadline = self._attempt_deadline(deadline)
        target = self._bind_timeout(target, attempt_deadline)
        first = target.respond(
            request,
            events=self._timed_events(events),
            deadline=attempt_deadline,
        )
        if first.response is not None:
            return first
        if not self._stream or not self._stream_fallback:
            return first
        if not _is_incomplete_stream(first):
            return first
        if self._deadline_elapsed(deadline):
            return _with_retryable_false(first)
        attempt_deadline = self._attempt_deadline(deadline)
        target = self._bind_timeout(self._nonstream, attempt_deadline)
        second = target.respond(
            request,
            events=self._timed_events(events),
            deadline=attempt_deadline,
        )
        return ModelResult(
            attempts=tuple(first.attempts) + tuple(second.attempts),
            response=second.response,
            final_error=second.final_error,
        )

    def _deadline_elapsed(self, overall_abs: float | None) -> bool:
        return overall_abs is not None and self._clock.monotonic() >= overall_abs

    def _attempt_deadline(self, overall_abs: float | None) -> float:
        per = self._clock.monotonic() + self._per_attempt_timeout_s
        if overall_abs is None:
            return per
        return min(overall_abs, per)

    def _bind_timeout(self, target: ModelClient, deadline: float) -> ModelClient:
        """Let a wire client carry a precomputed relative timeout.

        The strategy owns all clock reads and deadline arithmetic. Adapters may
        expose ``with_attempt_timeout`` only to encode that already-computed
        value into their transport request.
        """
        remaining = max(0.0, deadline - self._clock.monotonic())
        bind = getattr(target, "with_attempt_timeout", None)
        if not callable(bind):
            return target
        return cast(ModelClient, bind(remaining))

    def _timed_events(self, events: object | None) -> object | None:
        if events is None:
            return None
        return _TimedAttemptEvents(events, self._clock)


class _TimedAttemptEvents:
    def __init__(self, inner: object, clock: Clock):
        self._inner = inner
        self._clock = clock
        self._started = clock.monotonic()

    def emit(self, payload: object) -> None:
        if isinstance(payload, (AttemptCommitted, AttemptAborted)):
            duration_ms = max(
                0, int((self._clock.monotonic() - self._started) * 1000)
            )
            payload = replace(payload, duration_ms=duration_ms)
        emit = getattr(self._inner, "emit")
        emit(payload)


def _is_incomplete_stream(result: ModelResult) -> bool:
    error = result.final_error
    return error is not None and error.code == "incomplete_stream"


def _with_retryable_false(result: ModelResult) -> ModelResult:
    error = result.final_error
    if error is None:
        return result
    closed = replace(error, retryable=False)
    attempts = tuple(
        replace(record, error=closed)
        if record.error is not None and record.attempt_id == closed.attempt_id
        else record
        for record in result.attempts
    )
    return ModelResult(attempts=attempts, response=None, final_error=closed)
