"""Stream-fallback decorator. Time and fallback policy live here, not in Adapters."""

from __future__ import annotations

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
    ):
        self._inner = inner
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
        stream: bool | None = None,
    ) -> ModelResult:
        use_stream = self._stream if stream is None else stream
        if self._deadline_elapsed(deadline):
            raise OverallDeadlineExceeded()
        first = self._inner.respond(
            request,
            events=events,
            deadline=self._attempt_deadline(deadline),
            stream=use_stream,
        )
        if first.response is not None or not use_stream or not self._stream_fallback:
            return first
        if self._deadline_elapsed(deadline):
            return first
        second = self._inner.respond(
            request,
            events=events,
            deadline=self._attempt_deadline(deadline),
            stream=False,
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
