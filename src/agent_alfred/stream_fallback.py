"""Stream-fallback decorator. Time and fallback policy live here, not in Adapters."""

from __future__ import annotations

from dataclasses import replace

from agent_alfred.clock import Clock
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
        first = target.respond(
            request,
            events=events,
            deadline=self._attempt_deadline(deadline),
        )
        if first.response is not None:
            return first
        if not self._stream or not self._stream_fallback:
            return first
        if not _is_incomplete_stream(first):
            return first
        if self._deadline_elapsed(deadline):
            return _with_retryable_false(first)
        second = self._nonstream.respond(
            request,
            events=events,
            deadline=self._attempt_deadline(deadline),
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
