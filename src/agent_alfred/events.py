"""EventSink two-phase publish, FanOutSink, and the run/step payloads."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agent_alfred.messages import Message, TextBlock

TracePolicy = Literal["transient", "persist"]
RunOutcome = Literal["completed", "max_steps", "failed", "interrupted"]


@dataclass(frozen=True)
class BarrierFlushResult:
    outcome: Literal["flushed", "failed"]
    dropped_events: int = 0


@dataclass(frozen=True)
class BestEffortFlushResult:
    outcome: Literal["best_effort"]
    dropped_events: int = 0


FlushResult = BarrierFlushResult | BestEffortFlushResult


@dataclass(frozen=True)
class EventEnvelope:
    ts: float
    run_id: str
    session_id: str | None
    step_index: int | None
    attempt_id: str | None
    node_id: str | None
    source: str | None = None


@dataclass(frozen=True)
class RunStarted:
    name: str = "run.started"
    trace_policy: TracePolicy = "persist"
    user_message: Message | None = None
    history_message_count: int = 0
    persona_id: str | None = None
    purpose: str = "chat"


@dataclass(frozen=True)
class RunFinished:
    name: str = "run.finished"
    trace_policy: TracePolicy = "persist"
    outcome: RunOutcome = "completed"
    reply: Message | None = None
    error: str | None = None
    step_count: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class StepStarted:
    name: str = "step.started"
    trace_policy: TracePolicy = "persist"
    step_index: int = 0
    system: tuple[TextBlock, ...] | None = None
    message_count: int = 0
    tool_names: tuple[str, ...] = ()
    max_tokens: int | None = None


@dataclass(frozen=True)
class StepFinished:
    name: str = "step.finished"
    trace_policy: TracePolicy = "persist"
    step_index: int = 0
    stop_reason: str = "end_turn"
    duration_ms: int = 0


EventPayload = RunStarted | RunFinished | StepStarted | StepFinished


@dataclass(frozen=True)
class UnsequencedEvent:
    envelope: EventEnvelope
    payload: EventPayload
    trace_policy: TracePolicy
    replayable: bool


@dataclass(frozen=True)
class SequencedEvent:
    seq: int
    process_instance_id: str
    envelope: EventEnvelope
    payload: EventPayload
    trace_policy: TracePolicy
    replayable: bool


def replayable_for(trace_policy: TracePolicy) -> bool:
    """v1: replayable tracks persist. Named separately so the axes can diverge."""
    return trace_policy == "persist"


class EventSink(Protocol):
    name: str
    flush_at_run_end: bool

    def prepare(self, event: UnsequencedEvent) -> object: ...

    def commit(self, prepared: object, event: SequencedEvent) -> None: ...

    def flush(self) -> FlushResult: ...

    def close(self) -> None: ...


class CapturingSink:
    """Records sequenced events. Tests inject this; it is not a production sink."""

    def __init__(self, *, name: str = "capture", flush_at_run_end: bool = False):
        self.name = name
        self.flush_at_run_end = flush_at_run_end
        self.events: list[SequencedEvent] = []

    def prepare(self, event: UnsequencedEvent) -> object:
        del event
        return None

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        del prepared
        self.events.append(event)

    def flush(self) -> FlushResult:
        if self.flush_at_run_end:
            return BarrierFlushResult(outcome="flushed", dropped_events=0)
        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        return None


class FanOutSink:
    """Assigns seq in a short critical section after lock-free prepare."""

    def __init__(
        self,
        sinks: Sequence[EventSink],
        *,
        process_instance_id: str,
        redactor: Any | None = None,
    ):
        self._sinks = list(sinks)
        self._process_instance_id = process_instance_id
        self._redactor = redactor
        self._lock = threading.Lock()
        self._seq = 1
        self._disabled: set[str] = set()

    @property
    def sinks(self) -> tuple[EventSink, ...]:
        return tuple(self._sinks)

    def emit(self, payload: EventPayload, envelope: EventEnvelope) -> SequencedEvent:
        trace_policy: TracePolicy = payload.trace_policy
        unsequenced = UnsequencedEvent(
            envelope=envelope,
            payload=payload,
            trace_policy=trace_policy,
            replayable=replayable_for(trace_policy),
        )
        if self._redactor is not None:
            try:
                unsequenced = self._redactor.redact(unsequenced)
            except Exception:
                unsequenced = UnsequencedEvent(
                    envelope=envelope,
                    payload=RunStarted(user_message=None),
                    trace_policy="persist",
                    replayable=True,
                )
        prepared: list[tuple[EventSink, object]] = []
        for sink in self._sinks:
            if sink.name in self._disabled:
                continue
            prepared.append((sink, sink.prepare(unsequenced)))
        with self._lock:
            seq = self._seq
            self._seq += 1
            sequenced = SequencedEvent(
                seq=seq,
                process_instance_id=self._process_instance_id,
                envelope=unsequenced.envelope,
                payload=unsequenced.payload,
                trace_policy=unsequenced.trace_policy,
                replayable=unsequenced.replayable,
            )
            for sink, prep in prepared:
                try:
                    sink.commit(prep, sequenced)
                except Exception:
                    self._disabled.add(sink.name)
        return sequenced

    def flush_barrier(self) -> tuple[bool, str | None]:
        """Wait on flush_at_run_end sinks. Missing/failed/exception => incomplete."""
        incomplete = False
        reason: str | None = None
        for sink in self._sinks:
            if not sink.flush_at_run_end:
                continue
            try:
                result = sink.flush()
            except Exception as exc:
                incomplete = True
                reason = f"{sink.name} flush raised {type(exc).__name__}"
                continue
            if not isinstance(result, BarrierFlushResult):
                incomplete = True
                reason = f"{sink.name} returned {type(result).__name__}"
                continue
            if result.outcome != "flushed" or result.dropped_events != 0:
                incomplete = True
                reason = (
                    f"{sink.name} flush {result.outcome} "
                    f"dropped={result.dropped_events}"
                )
        return incomplete, reason

    def close(self) -> None:
        for sink in self._sinks:
            sink.close()
