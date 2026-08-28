"""EventSink two-phase publish, FanOutSink, and the run/step payloads."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from agent_alfred.messages import Block, Message, TextBlock
from agent_alfred.model import ModelError, ModelRef, Usage
from agent_alfred.outcomes import RunOutcome

TracePolicy = Literal["transient", "persist"]
NoticeCode = Literal[
    "trace_incomplete",
    "sink_disabled",
    "redaction_failed",
    "model_support_flipped",
]
NoticeLevel = Literal["info", "warning", "error"]

_DOMAIN_NOTICE_CODES = frozenset(
    {
        "trace_incomplete",
        "sink_disabled",
        "redaction_failed",
        "model_support_flipped",
    }
)


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
    working_memory_message_count: int = 0
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


@dataclass(frozen=True)
class Notice:
    name: str = "notice"
    trace_policy: TracePolicy = "persist"
    level: NoticeLevel = "error"
    code: NoticeCode = "sink_disabled"
    detail: tuple[tuple[str, str], ...] = ()
    evidence: str | None = None

    def __post_init__(self) -> None:
        if self.code not in _DOMAIN_NOTICE_CODES:
            raise ValueError(f"not a domain notice code: {self.code}")


@dataclass(frozen=True)
class AttemptStarted:
    name: str = "attempt.started"
    trace_policy: TracePolicy = "persist"
    attempt_id: str = ""
    model: ModelRef | None = None
    streamed: bool = False
    timeout_ms: int | None = None


@dataclass(frozen=True)
class AttemptCommitted:
    name: str = "attempt.committed"
    trace_policy: TracePolicy = "persist"
    attempt_id: str = ""
    blocks: tuple[Block, ...] = ()
    stop_reason: str = "end_turn"
    usage: Usage | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class AttemptAborted:
    name: str = "attempt.aborted"
    trace_policy: TracePolicy = "persist"
    attempt_id: str = ""
    partial: bool = False
    blocks: tuple[Block, ...] = ()
    unparsed_tool_arguments: tuple[tuple[str, str], ...] = ()
    usage: Usage | None = None
    error: ModelError | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class BlockStarted:
    name: str = "block.started"
    trace_policy: TracePolicy = "transient"
    attempt_id: str = ""
    index: int = 0
    block_type: str = "text"


@dataclass(frozen=True)
class BlockDelta:
    name: str = "block.delta"
    trace_policy: TracePolicy = "transient"
    attempt_id: str = ""
    index: int = 0
    text: str = ""


@dataclass(frozen=True)
class BlockStopped:
    name: str = "block.stopped"
    trace_policy: TracePolicy = "transient"
    attempt_id: str = ""
    index: int = 0


EventPayload = (
    RunStarted
    | RunFinished
    | StepStarted
    | StepFinished
    | AttemptStarted
    | AttemptCommitted
    | AttemptAborted
    | BlockStarted
    | BlockDelta
    | BlockStopped
    | Notice
)


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
        self._disabled: dict[str, set[str]] = {}
        self._persist_lost: dict[str, list[str]] = {}
        self._last_envelope: dict[str, EventEnvelope] = {}
        self._origin: EventEnvelope | None = None

    @property
    def sinks(self) -> tuple[EventSink, ...]:
        return tuple(self._sinks)

    def bind_redactor(self, redactor: Any | None) -> None:
        self._redactor = redactor

    def bind_origin(self, envelope: EventEnvelope | None) -> None:
        self._origin = envelope

    def emit(
        self, payload: EventPayload, envelope: EventEnvelope | None = None
    ) -> SequencedEvent:
        if envelope is None:
            envelope = self._bound_envelope(payload)
        self._last_envelope[envelope.run_id] = envelope
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
                unsequenced = _fail_closed_event(unsequenced)
        return self._publish(unsequenced, notify_disabled=True)

    def _bound_envelope(self, payload: EventPayload) -> EventEnvelope:
        origin = self._origin
        attempt_id = getattr(payload, "attempt_id", None)
        if origin is None:
            return EventEnvelope(
                ts=0.0,
                run_id="",
                session_id=None,
                step_index=None,
                attempt_id=attempt_id,
                node_id=None,
            )
        return replace(origin, attempt_id=attempt_id)

    def _publish(
        self, unsequenced: UnsequencedEvent, *, notify_disabled: bool
    ) -> SequencedEvent:
        prepared: list[tuple[EventSink, object]] = []
        newly_disabled: list[tuple[str, str]] = []
        run_id = unsequenced.envelope.run_id
        for sink in self._sinks:
            with self._lock:
                if sink.name in self._disabled.get(run_id, set()):
                    continue
            try:
                prep = sink.prepare(unsequenced)
            except Exception:
                with self._lock:
                    disabled = self._disabled.setdefault(run_id, set())
                    first_failure = sink.name not in disabled
                    self._note_sink_failure_locked(
                        run_id, sink, "prepare", unsequenced.trace_policy
                    )
                if first_failure:
                    newly_disabled.append((sink.name, "prepare"))
                continue
            prepared.append((sink, prep))
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
                if sink.name in self._disabled.get(run_id, set()):
                    self._note_sink_failure_locked(
                        run_id,
                        sink,
                        "commit_skipped",
                        unsequenced.trace_policy,
                    )
                    continue
                try:
                    sink.commit(prep, sequenced)
                except Exception:
                    disabled = self._disabled.setdefault(run_id, set())
                    first_failure = sink.name not in disabled
                    self._note_sink_failure_locked(
                        run_id, sink, "commit", unsequenced.trace_policy
                    )
                    if first_failure:
                        newly_disabled.append((sink.name, "commit"))
        if notify_disabled:
            for name, stage in newly_disabled:
                self._emit_notice(
                    unsequenced.envelope,
                    code="sink_disabled",
                    level="error",
                    detail=(("sink", name), ("stage", stage)),
                )
        return sequenced

    def _note_sink_failure_locked(
        self, run_id: str, sink: EventSink, stage: str, trace_policy: TracePolicy
    ) -> None:
        self._disabled.setdefault(run_id, set()).add(sink.name)
        reasons = self._persist_lost.setdefault(run_id, [])
        if sink.flush_at_run_end:
            reasons.append(f"{sink.name} {stage} failed")
        if trace_policy == "persist":
            reasons.append(f"{sink.name} dropped persist")

    def _emit_notice(
        self,
        envelope: EventEnvelope,
        *,
        code: NoticeCode,
        level: NoticeLevel,
        detail: tuple[tuple[str, str], ...],
    ) -> None:
        notice = Notice(level=level, code=code, detail=detail)
        unsequenced = UnsequencedEvent(
            envelope=envelope,
            payload=notice,
            trace_policy="persist",
            replayable=True,
        )
        self._publish(unsequenced, notify_disabled=False)

    def flush_barrier(self, run_id: str | None = None) -> tuple[bool, str | None]:
        """Wait on flush_at_run_end sinks. Missing/failed/exception => incomplete."""
        reasons: list[str] = []
        if run_id is not None:
            reasons.extend(self._persist_lost.get(run_id, ()))
        for sink in self._sinks:
            if not sink.flush_at_run_end:
                continue
            try:
                result = sink.flush()
            except Exception as exc:
                reasons.append(f"{sink.name} flush raised {type(exc).__name__}")
                continue
            if not isinstance(result, BarrierFlushResult):
                reasons.append(f"{sink.name} returned {type(result).__name__}")
                continue
            if result.outcome != "flushed" or result.dropped_events != 0:
                reasons.append(
                    f"{sink.name} flush {result.outcome} "
                    f"dropped={result.dropped_events}"
                )
        incomplete = bool(reasons)
        reason: str | None = None
        if incomplete:
            # Keep every reason; later failures must not erase earlier ones.
            reason = "; ".join(dict.fromkeys(reasons))[:500]
            if self._redactor is not None:
                try:
                    reason = self._redactor.redact_text(reason)
                except Exception:
                    reason = "trace_incomplete"
            envelope = (
                self._last_envelope.get(run_id or "")
                or self._origin
                or EventEnvelope(
                    ts=0.0,
                    run_id=run_id or "",
                    session_id=None,
                    step_index=None,
                    attempt_id=None,
                    node_id=None,
                )
            )
            if run_id:
                envelope = replace(envelope, run_id=run_id)
            self._emit_notice(
                envelope,
                code="trace_incomplete",
                level="error",
                detail=(("reason", reason),),
            )
        if run_id is not None:
            self._disabled.pop(run_id, None)
            self._persist_lost.pop(run_id, None)
            self._last_envelope.pop(run_id, None)
        return incomplete, reason

    def close(self) -> None:
        for sink in self._sinks:
            sink.close()


def _fail_closed_event(event: UnsequencedEvent) -> UnsequencedEvent:
    return replace(
        event,
        payload=Notice(
            level="error", code="redaction_failed", detail=(), evidence=None
        ),
        trace_policy="persist",
        replayable=True,
    )
