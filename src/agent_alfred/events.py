"""EventSink two-phase publish, FanOutSink, and the run/step payloads."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, is_dataclass, replace
from dataclasses import fields as dc_fields
from decimal import Decimal
from typing import Any, Literal, Protocol

from agent_alfred.messages import (
    Block,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    blocks_to_jsonable,
)
from agent_alfred.model import ModelError, ModelRef, Usage
from agent_alfred.outcomes import RunOutcome

TracePolicy = Literal["transient", "persist"]
PostCommitFailureScope = Literal["run_local", "process_fatal"]
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
    # Machine-judgeable first cause when outcome is "failed". Never carries
    # payloads or paths -- the FanOutSink redacts it again anyway.
    detail: str = ""


@dataclass(frozen=True)
class BestEffortFlushResult:
    outcome: Literal["best_effort"]
    dropped_events: int = 0


FlushResult = BarrierFlushResult | BestEffortFlushResult


@dataclass(frozen=True)
class PostCommit:
    """Work owed after publication, with its failure boundary preserved.

    The owning sink is attached by :class:`FanOutSink` when ``commit``
    returns this value. ``failure_scope`` states whether the sink may be
    used by a later Run or is proven dead process-wide.
    """

    action: Callable[[], None]
    failure_scope: PostCommitFailureScope = "run_local"


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
    event_id: str
    envelope: EventEnvelope
    payload: EventPayload
    trace_policy: TracePolicy
    replayable: bool


@dataclass(frozen=True)
class SequencedEvent:
    seq: int
    process_instance_id: str
    event_id: str
    envelope: EventEnvelope
    payload: EventPayload
    trace_policy: TracePolicy
    replayable: bool


def replayable_for(trace_policy: TracePolicy) -> bool:
    """v1: replayable tracks persist. Named separately so the axes can diverge."""
    return trace_policy == "persist"


def _notice_named_sinks(payload: EventPayload) -> frozenset[str]:
    """The sinks a ``sink_disabled`` notice in flight already names.

    A process-fatal discovered while such a notice is being published must
    not publish its own -- the fact is already on its way to every healthy
    sink, and a second envelope would say it twice. Any other notice in
    flight (``trace_incomplete``, say) names nothing and covers nothing.
    """
    if not isinstance(payload, Notice) or payload.code != "sink_disabled":
        return frozenset()
    return frozenset(
        value for key, value in payload.detail if key == "sink"
    )


def _book_failure(
    exc: Exception,
    sink_name: str,
    stage_reported: str,
    newly_disabled: list[tuple[str, str]],
    fatal_notices: list[tuple[str, str]],
) -> None:
    """Route one owed notice to its list.

    Run-local notices wait for a normal publish (the caller's
    ``notify_disabled`` gate); process-fatal notices are published wherever
    they are discovered, because their once-ever set bounds any chain.
    """
    if isinstance(exc, ProcessFatalSinkError):
        fatal_notices.append((sink_name, stage_reported))
    else:
        newly_disabled.append((sink_name, stage_reported))


def event_json_default(value: object) -> object:
    """The one ``json.dumps`` default every event consumer shares.

    The trace bundle and the Dashboard stream are two renderings of the same
    event, so they must agree on the payload shape -- a reply that reads one
    way in the audit file and another in the browser is two facts where there
    is one. It lives here rather than in either sink so neither can drift.
    """
    if isinstance(value, Message):
        return {"role": value.role, "blocks": blocks_to_jsonable(value.blocks)}
    if isinstance(value, (TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock)):
        return blocks_to_jsonable([value])[0]
    if isinstance(value, Usage):
        cost = value.endpoint_reported_cost_usd
        return {
            "total_input_tokens": value.total_input_tokens,
            "uncached_input_tokens": value.uncached_input_tokens,
            "cache_read_tokens": value.cache_read_tokens,
            "cache_write_tokens": value.cache_write_tokens,
            "output_tokens": value.output_tokens,
            "reasoning_tokens": value.reasoning_tokens,
            "endpoint_reported_cost_usd": (
                None if cost is None else format(cost, "f")
            ),
            "raw": value.raw,
        }
    if isinstance(value, ModelError):
        return {
            "retryable": value.retryable,
            "status_code": value.status_code,
            "body_excerpt": value.body_excerpt,
            "attempt_id": value.attempt_id,
            "code": value.code,
        }
    if isinstance(value, ModelRef):
        return {"endpoint_id": value.endpoint_id, "model_id": value.model_id}
    if isinstance(value, Decimal):
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "_type": type(value).__name__,
            **{f.name: getattr(value, f.name) for f in dc_fields(value)},
        }
    raise TypeError(f"unserializable event payload part {type(value).__name__}")


class EventSink(Protocol):
    name: str
    flush_at_run_end: bool

    def prepare(self, event: UnsequencedEvent) -> object: ...

    def commit(
        self, prepared: object, event: SequencedEvent
    ) -> PostCommit | None: ...

    def flush(self, run_id: str) -> FlushResult:
        """Settle this sink. ``run_id`` scopes a durability-critical sink's
        barrier to the finishing Run; sinks that do not scope may ignore it.
        It is always supplied: the production barrier knows which Run is
        finishing, and a barrier that silently settles every Run at once is
        not a question anybody is allowed to ask."""
        ...

    def close(self) -> None: ...


class ProcessFatalSinkError(RuntimeError):
    """A sink reporting a failure the process cannot recover from.

    An ordinary exception a sink raises is one Run's problem: the FanOut
    disables the sink for that Run, says so once, and calls the sink again
    when the next Run begins. This error is the other class (#23 §9) -- the
    dispatcher or the replay ring dying is a process-level fact, and no Run
    that follows can use this sink either. The FanOut answers it by moving
    the sink into a process-wide disabled set that no Run's end clears and
    publishing its ``sink_disabled`` notice exactly once. It subclasses
    ``RuntimeError`` because every refusal contract around a dead
    dispatcher was already phrased in those terms; what is new is that the
    publisher can tell the two classes apart.
    """


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

    def flush(self, run_id: str) -> FlushResult:
        del run_id
        if self.flush_at_run_end:
            return BarrierFlushResult(outcome="flushed", dropped_events=0)
        return BestEffortFlushResult(outcome="best_effort")

    def close(self) -> None:
        return None


def _new_event_id() -> str:
    """Mint one logical event identity before any sink prepares it."""
    return uuid.uuid4().hex


class FanOutSink:
    """Assigns seq in a short critical section after lock-free prepare."""

    def __init__(
        self,
        sinks: Sequence[EventSink],
        *,
        process_instance_id: str,
        redactor: Any | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ):
        self._sinks = list(sinks)
        self._process_instance_id = process_instance_id
        self._redactor = redactor
        self._event_id_factory = event_id_factory or _new_event_id
        self._lock = threading.Lock()
        self._seq = 1
        self._disabled: dict[str, set[str]] = {}
        # The process-level complement to the per-Run ``_disabled``: a sink
        # that reported the typed process-fatal error is out for every Run,
        # so no Run's end may hand it back. Members entered exactly once,
        # and the ``sink_disabled`` notice for a member is published exactly
        # once in this process's life.
        self._process_disabled: set[str] = set()
        self._persist_lost: dict[str, list[str]] = {}
        self._last_envelope: dict[str, EventEnvelope] = {}
        self._origin: EventEnvelope | None = None
        # Close progress is per sink, and a sink's bit moves only after its
        # own close() has returned. A close that raises must leave the
        # FanOut unfinished: the caller may ask again, and the retry closes
        # exactly the sinks that never succeeded -- never one that did.
        self._close_lock = threading.Lock()
        self._closed_sinks = [False] * len(self._sinks)

    @property
    def sinks(self) -> tuple[EventSink, ...]:
        return tuple(self._sinks)

    def bind_redactor(self, redactor: Any | None) -> None:
        self._redactor = redactor

    def bind_projection_boundary(
        self, boundary: AbstractContextManager[object]
    ) -> None:
        """Share a peer projection's read boundary with interested sinks."""
        for sink in self._sinks:
            bind = getattr(sink, "bind_projection_boundary", None)
            if bind is not None:
                bind(boundary)

    def bind_origin(self, envelope: EventEnvelope | None) -> None:
        self._origin = envelope

    def emit(
        self, payload: EventPayload, envelope: EventEnvelope | None = None
    ) -> SequencedEvent:
        return self._emit(payload, envelope)

    def emit_linearized(
        self,
        payload: EventPayload,
        envelope: EventEnvelope | None,
        *,
        boundary: AbstractContextManager[object],
        after_commit: Callable[[SequencedEvent], None],
    ) -> SequencedEvent:
        """Publish and advance a peer projection under one read boundary.

        Sink preparation happens before ``boundary``.  Sink commits keep
        their existing short FanOut critical section; ``after_commit`` runs
        only after that lock is released, while readers sharing ``boundary``
        are still excluded.  Post-commit work remains outside both locks.
        """
        return self._emit(
            payload,
            envelope,
            boundary=boundary,
            after_commit=after_commit,
        )

    def _emit(
        self,
        payload: EventPayload,
        envelope: EventEnvelope | None,
        *,
        boundary: AbstractContextManager[object] | None = None,
        after_commit: Callable[[SequencedEvent], None] | None = None,
    ) -> SequencedEvent:
        if envelope is None:
            envelope = self._bound_envelope(payload)
        self._last_envelope[envelope.run_id] = envelope
        trace_policy: TracePolicy = payload.trace_policy
        unsequenced = UnsequencedEvent(
            event_id=self._event_id_factory(),
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
        return self._publish(
            unsequenced,
            notify_disabled=True,
            boundary=boundary,
            after_commit=after_commit,
        )

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
        self,
        unsequenced: UnsequencedEvent,
        *,
        notify_disabled: bool,
        boundary: AbstractContextManager[object] | None = None,
        after_commit: Callable[[SequencedEvent], None] | None = None,
    ) -> SequencedEvent:
        prepared: list[tuple[EventSink, object]] = []
        # Run-local discoveries wait for a normal publish to be told about;
        # emitting one from inside a notice's own publish would recurse
        # forever, because a run-local sink fails on every event -- notices
        # included. Process-fatal discoveries are different: the once-ever
        # set and the in-flight check below bound any chain of notices, so
        # they are published wherever they are discovered.
        newly_disabled: list[tuple[str, str]] = []
        fatal_notices: list[tuple[str, str]] = []
        post_commits: list[tuple[EventSink, PostCommit]] = []
        run_id = unsequenced.envelope.run_id
        # What the event in flight already says about disabled sinks: a
        # process-fatal discovered while its own notice is being published
        # must not publish that notice a second time.
        already_reported = _notice_named_sinks(unsequenced.payload)
        for sink in self._sinks:
            with self._lock:
                if (
                    sink.name in self._process_disabled
                    or sink.name in self._disabled.get(run_id, set())
                ):
                    continue
            try:
                prep = sink.prepare(unsequenced)
            except Exception as exc:
                with self._lock:
                    stage_reported = self._note_sink_call_failed_locked(
                        run_id,
                        sink,
                        "prepare",
                        unsequenced.trace_policy,
                        exc,
                        already_reported,
                    )
                if stage_reported is not None:
                    _book_failure(
                        exc, sink.name, stage_reported,
                        newly_disabled, fatal_notices,
                    )
                continue
            prepared.append((sink, prep))
        boundary_context = nullcontext() if boundary is None else boundary
        linearize_error: Exception | None = None
        with boundary_context:
            with self._lock:
                seq = self._seq
                self._seq += 1
                sequenced = SequencedEvent(
                    seq=seq,
                    process_instance_id=self._process_instance_id,
                    event_id=unsequenced.event_id,
                    envelope=unsequenced.envelope,
                    payload=unsequenced.payload,
                    trace_policy=unsequenced.trace_policy,
                    replayable=unsequenced.replayable,
                )
                for sink, prep in prepared:
                    if sink.name in self._process_disabled:
                        # A concurrently published fatal already reported why
                        # this sink is gone; skipping quietly is the contract,
                        # not a new failure to book.
                        continue
                    if sink.name in self._disabled.get(run_id, set()):
                        self._note_sink_failure_locked(
                            run_id,
                            sink,
                            "commit_skipped",
                            unsequenced.trace_policy,
                        )
                        continue
                    try:
                        post_commit = sink.commit(prep, sequenced)
                    except Exception as exc:
                        stage_reported = self._note_sink_call_failed_locked(
                            run_id,
                            sink,
                            "commit",
                            unsequenced.trace_policy,
                            exc,
                            already_reported,
                        )
                        if stage_reported is not None:
                            _book_failure(
                                exc, sink.name, stage_reported,
                                newly_disabled, fatal_notices,
                            )
                    else:
                        if post_commit is not None:
                            post_commits.append((sink, post_commit))
            if after_commit is not None:
                try:
                    after_commit(sequenced)
                except Exception as exc:
                    # Publication is already irrevocable.  Preserve the old
                    # propagation semantics, but first discharge every
                    # post-commit action owed by that committed event.
                    linearize_error = exc
        # These callbacks are produced by constant-time commit bookkeeping
        # for work that is safe only after the publication order has been
        # linearized. They must run after the FanOut lock, or a callback that
        # releases a large retired replay prefix would put that linear work
        # straight back into the unique publish critical section.
        for sink, post_commit in post_commits:
            try:
                post_commit.action()
            except Exception as exc:
                failure: Exception = exc
                if (
                    post_commit.failure_scope == "process_fatal"
                    and not isinstance(exc, ProcessFatalSinkError)
                ):
                    failure = ProcessFatalSinkError(
                        "a process-fatal post-commit action failed"
                    )
                    failure.__cause__ = exc
                with self._lock:
                    stage_reported = self._note_sink_call_failed_locked(
                        run_id,
                        sink,
                        "post_commit",
                        unsequenced.trace_policy,
                        failure,
                        already_reported,
                        event_committed=True,
                    )
                if stage_reported is not None:
                    _book_failure(
                        failure,
                        sink.name,
                        stage_reported,
                        newly_disabled,
                        fatal_notices,
                    )
        if notify_disabled:
            for name, stage in newly_disabled:
                self._emit_notice(
                    unsequenced.envelope,
                    code="sink_disabled",
                    level="error",
                    detail=(("sink", name), ("stage", stage)),
                )
        for name, stage in fatal_notices:
            self._emit_notice(
                unsequenced.envelope,
                code="sink_disabled",
                level="error",
                detail=(("sink", name), ("stage", stage)),
            )
        if linearize_error is not None:
            raise linearize_error
        return sequenced

    def _note_sink_call_failed_locked(
        self,
        run_id: str,
        sink: EventSink,
        stage: str,
        trace_policy: TracePolicy,
        exc: Exception,
        already_reported: frozenset[str] = frozenset(),
        *,
        event_committed: bool = False,
    ) -> str | None:
        """Book one failed sink call; the stage to report a notice about,
        or ``None`` when no notice is owed. Call holds the lock.

        Two failure classes stay apart. An ordinary exception is run-local:
        the sink is re-called next Run, and every Run that discovers the
        failure publishes one notice of its own. The typed process-fatal
        error is a different fact: the sink moves into the process-wide set
        that no Run's end clears, and the notice is owed exactly once in
        the process's life -- whether the discovery happens during a normal
        publish or during a notice's own. Two guards keep that "once" true:
        a sink the in-flight notice already names is covered by it, and a
        sink discovered twice is covered by the set it just joined.
        """
        process_fatal = isinstance(exc, ProcessFatalSinkError)
        if process_fatal:
            first_process_failure = sink.name not in self._process_disabled
            self._process_disabled.add(sink.name)
            if not first_process_failure or sink.name in already_reported:
                return None
            return stage
        disabled = self._disabled.setdefault(run_id, set())
        first_failure = sink.name not in disabled
        self._note_sink_failure_locked(
            run_id,
            sink,
            stage,
            trace_policy,
            event_committed=event_committed,
        )
        return stage if first_failure else None

    def _note_sink_failure_locked(
        self,
        run_id: str,
        sink: EventSink,
        stage: str,
        trace_policy: TracePolicy,
        *,
        event_committed: bool = False,
    ) -> None:
        self._disabled.setdefault(run_id, set()).add(sink.name)
        reasons = self._persist_lost.setdefault(run_id, [])
        if sink.flush_at_run_end:
            reasons.append(f"{sink.name} {stage} failed")
        if trace_policy == "persist" and not event_committed:
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
            event_id=self._event_id_factory(),
            envelope=envelope,
            payload=notice,
            trace_policy="persist",
            replayable=True,
        )
        self._publish(unsequenced, notify_disabled=False)

    def note_publish_failure(self, run_id: str, exc: BaseException) -> None:
        """Record that a central publish itself raised, so the barrier cannot
        judge the run's trace complete. The exception text is never kept --
        only its type survives into the reason string."""
        with self._lock:
            reasons = self._persist_lost.setdefault(run_id, [])
            reasons.append(f"run.finished publish failed: {type(exc).__name__}")

    def flush_barrier(self, run_id: str) -> tuple[bool, str | None]:
        """Wait on flush_at_run_end sinks. Missing/failed/exception => incomplete."""
        reasons: list[str] = []
        if run_id is not None:
            reasons.extend(self._persist_lost.get(run_id, ()))
        critical = [sink for sink in self._sinks if sink.flush_at_run_end]
        if not critical:
            # Nobody behind this FanOut promises durability, so "flushed" is
            # not a word the barrier is allowed to say. Fail closed.
            reasons.append("no flush_at_run_end sink")
        for sink in critical:
            try:
                # The finishing Run scopes a durability-critical sink's
                # barrier, so it settles exactly that Run's state.
                result = sink.flush(run_id)
            except Exception as exc:
                reasons.append(f"{sink.name} flush raised {type(exc).__name__}")
                continue
            if not isinstance(result, BarrierFlushResult):
                reasons.append(f"{sink.name} returned {type(result).__name__}")
                continue
            if result.outcome != "flushed" or result.dropped_events != 0:
                reason = (
                    f"{sink.name} flush {result.outcome} "
                    f"dropped={result.dropped_events}"
                )
                if result.detail:
                    reason += f" ({result.detail})"
                reasons.append(reason)
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
        """Close every sink, resumable per sink.

        One sink raising is an unfinished close, not a finished one: its
        exception propagates unchanged and the sinks after it are still
        unclosed, so the FanOut as a whole stays unfinished. Asking again
        resumes where the failure was -- the already-closed sinks are not
        touched a second time.
        """
        # Attempts are serialized so two racing closers cannot both walk an
        # unfinished sink; the FanOut's close is otherwise on its own lock,
        # never on the publish lock a sink's emit path shares.
        with self._close_lock:
            for index, sink in enumerate(self._sinks):
                if self._closed_sinks[index]:
                    continue
                sink.close()
                self._closed_sinks[index] = True


def _fail_closed_event(event: UnsequencedEvent) -> UnsequencedEvent:
    return replace(
        event,
        payload=Notice(
            level="error", code="redaction_failed", detail=(), evidence=None
        ),
        trace_policy="persist",
        replayable=True,
    )
