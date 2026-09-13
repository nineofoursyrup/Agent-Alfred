"""EventSink two-phase publish, FanOutSink, and the run/step payloads."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, is_dataclass, replace
from dataclasses import fields as dc_fields
from decimal import Decimal
from functools import partial
from itertools import starmap
from typing import Any, Literal, Protocol, runtime_checkable

from agent_alfred.messages import (
    Block,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    blocks_to_jsonable,
)
from agent_alfred.model import (
    ModelError,
    ModelRef,
    StopReason,
    ToolChoice,
    Usage,
    parse_stop_reason,
)
from agent_alfred.outcomes import RunOutcome
from agent_alfred.resource_rollback import CloseCompletion

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


class _CapturePublicationExit:
    """Keep an asynchronous boundary exit for lock-free settlement."""

    def __init__(self, escaped: list[BaseException]):
        self._escaped = escaped

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        if exc is None:
            return False
        self._escaped.append(exc)
        return True


@dataclass(slots=True)
class _PublicationOutcomeState:
    """Outcome shared by the publication body and both delivery fences."""

    fanout: FanOutSink
    expected: BaseException | None = None
    publication_started: bool = False


class _PublicationOutcomeGuard:
    """Fence an unexpected exit while delivering the selected outcome."""

    def __init__(self, state: _PublicationOutcomeState):
        self._state = state

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        expected = self._state.expected
        if exc is None or exc is expected:
            return False
        fanout = self._state.fanout
        if self._state.publication_started:
            fanout.remember_publication_fatal(exc)
        if expected is not None:
            raise expected
        return False


def _record_post_commit(
    actions: list[tuple[EventSink, PostCommit]],
    sink: EventSink,
    action: PostCommit,
) -> None:
    """Transfer one returned action into FanOut's settlement list."""
    actions.append((sink, action))


def _finish_commit_iteration() -> None:
    """Named control boundary before FanOut advances to the next sink."""


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
    skill_notice: str | None = None
    finalization_reason: str | None = None
    not_executed_call_ids: tuple[str, ...] = ()
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
    tool_choice: ToolChoice = "auto"
    max_tokens: int | None = None


@dataclass(frozen=True)
class StepFinished:
    name: str = "step.finished"
    trace_policy: TracePolicy = "persist"
    step_index: int = 0
    stop_reason: StopReason = "end_turn"
    duration_ms: int = 0

    def __post_init__(self) -> None:
        parse_stop_reason(self.stop_reason)


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
    stop_reason: StopReason = "end_turn"
    usage: Usage | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        parse_stop_reason(self.stop_reason)


@dataclass(frozen=True)
class RawToolArgumentFragment:
    call_id: str | None
    name: str | None
    raw: str


@dataclass(frozen=True)
class AttemptAborted:
    name: str = "attempt.aborted"
    trace_policy: TracePolicy = "persist"
    attempt_id: str = ""
    partial: bool = False
    blocks: tuple[Block, ...] = ()
    unparsed_tool_arguments: tuple[RawToolArgumentFragment, ...] = ()
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


@dataclass(frozen=True)
class GateEvaluated:
    """Final body-free retrieval evidence; the business object is authoritative."""

    schema_version: int
    decision: bool
    outcome: str
    decision_source: str
    reason_code: str
    fallback_reason: str | None
    rule_version: str | None
    gate_step_index: int | None
    model_ref: dict | None
    latency_ms: int
    timing: dict
    stores: dict
    hit_count: int | None
    selected_count: int
    references: list
    input_disposition: str
    name: str = "gate.evaluated"
    trace_policy: TracePolicy = "persist"


@dataclass(frozen=True)
class ToolStarted:
    call_id: str
    tool_name: str
    effect: str
    parallel_group: None = None
    name: str = "tool.started"
    trace_policy: TracePolicy = "persist"


@dataclass(frozen=True)
class ToolFinished:
    call_id: str
    tool_name: str
    outcome: str
    error_code: str | None
    model_content: str
    audit_content: str
    truncated: bool
    original_bytes: int
    content_digest: str
    duration_ms: int
    summary: str | None = None
    cost: object | None = None
    audit_data: object | None = None
    name: str = "tool.finished"
    trace_policy: TracePolicy = "persist"


@dataclass(frozen=True)
class ToolProgress:
    call_id: str
    tool_name: str
    text: str
    name: str = "tool.progress"
    trace_policy: TracePolicy = "transient"


@dataclass(frozen=True)
class GraphStarted:
    graph_id: str
    topology_hash: str
    schema_version: int
    name: str = "graph.started"
    trace_policy: TracePolicy = "persist"


@dataclass(frozen=True)
class GraphFinished:
    outcome: str
    name: str = "graph.finished"
    trace_policy: TracePolicy = "persist"


@dataclass(frozen=True)
class NodeStarted:
    name: str = "node.started"
    trace_policy: TracePolicy = "persist"


@dataclass(frozen=True)
class NodeFinished:
    outcome: str
    name: str = "node.finished"
    trace_policy: TracePolicy = "persist"


@dataclass(frozen=True)
class NodeSkipped:
    reason: str
    name: str = "node.skipped"
    trace_policy: TracePolicy = "persist"

    def __post_init__(self):
        if self.reason not in ('all_inbound_not_taken', 'disabled_by_config'):
            raise ValueError('invalid skip reason')


@dataclass(frozen=True)
class NodeAborted:
    reason: str
    name: str = "node.aborted"
    trace_policy: TracePolicy = "persist"


EventPayload = (
    GraphStarted
    | GraphFinished
    | NodeStarted
    | NodeFinished
    | NodeSkipped
    | NodeAborted
    | RunStarted
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
    | GateEvaluated
    | ToolStarted
    | ToolFinished
    | ToolProgress
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

    def close(self) -> bool | None:
        """Release this sink.

        ``False`` reports that release is still in progress and must be
        retried. ``True`` confirms completion; legacy sinks that return
        ``None`` are also treated as confirmed. A sink must either make
        completion observable through :class:`CloseCompletion` before it
        returns or keep repeated close calls idempotent: process control can
        interrupt the callee's return after its release effect has happened.
        """
        ...


@runtime_checkable
class TimedCloseSink(Protocol):
    """Explicit capability for sinks that can honor a caller's close budget.

    The distinct method is intentional: legacy EventSinks expose only a
    no-argument ``close``. FanOut must not guess their signatures or turn a
    ``TypeError`` raised *inside* close into a retry with different arguments.
    """

    def close_with_timeout(self, timeout: float | None = None) -> bool:
        """Apply the same progress/idempotence contract as ``EventSink.close``."""
        ...


@runtime_checkable
class InterruptedCommitSink(Protocol):
    """Optional ownership handoff for an interrupted sink commit.

    A sink can make an irrevocable constant-time state change and then meet
    an exception before it can return its :class:`PostCommit`. FanOut calls
    this capability only after leaving its publication and caller-supplied
    boundary locks, so the sink can transfer the already-created cleanup
    owner without doing linear work in either critical section. The sink
    retains that owner until the returned action runs, so FanOut may repeat
    this lookup if process control interrupts the return handoff. A
    process-control exception remains FanOut's exact object to re-raise.
    """

    def settle_interrupted_commit(
        self, failure: BaseException
    ) -> PostCommit | None: ...


@dataclass(slots=True, kw_only=True)
class _OneShotTask:
    """One C-claimed invocation and its durable caller-side progress."""

    invocation: Iterator[Any]
    attempts: int = 0
    completed: bool = False


@dataclass(slots=True)
class _AfterCommitTask(_OneShotTask):
    """Projection failure policy, carried by the publication tail."""


def _new_after_commit_task(
    action: Callable[[SequencedEvent], None], event: SequencedEvent
) -> _AfterCommitTask:
    """Create a one-shot C-level claim around one projection call."""
    return _AfterCommitTask(invocation=starmap(action, ((event,),)))


def _call_task(task: _OneShotTask) -> None:
    """Drive a one-shot iterator exactly once, preserving its claim boundary."""
    next(task.invocation)


def _invoke_task(task: _OneShotTask) -> None:
    """Run or finalize a claim; the caller retains its own failure policy."""
    try:
        _call_task(task)
    except StopIteration:
        pass
    task.completed = True


@dataclass(slots=True)
class _InterruptedSettlementTask:
    """A settlement remains discoverable until its PostCommit is captured."""

    sink: InterruptedCommitSink
    failure: BaseException
    attempts: int = 0
    post_commit: PostCommit | None = None
    completed: bool = False


@dataclass(slots=True)
class _PostCommitTask(_OneShotTask):
    """One returned owner whose caller-side admission is recoverable."""

    sink: EventSink
    post_commit: PostCommit


@dataclass(slots=True)
class _CloseTask:
    """One sink close whose returned result survives a Python store exit."""

    result: list[bool | None]
    invocation: Iterator[Any]


@dataclass(slots=True)
class _SinkCloseProgress:
    """One sink's close attempt, completion observation and retained error."""

    sink: EventSink
    closed: bool = False
    task: _CloseTask | None = None
    observing: bool = False
    error: BaseException | None = None

    def retire(self) -> None:
        """Retire a confirmed sink before discarding its attempt state."""
        self.closed = True
        self.task = None
        self.observing = False
        self.error = None


def _new_close_task(
    action: Callable[..., bool | None], arguments: tuple[object, ...]
) -> _CloseTask:
    """Capture the close result in C before ``next`` returns to bytecode."""
    result: list[bool | None] = []
    invocation = map(result.append, starmap(action, (arguments,)))
    return _CloseTask(result, invocation)


def _new_post_commit_task(
    sink: EventSink, post_commit: PostCommit
) -> _PostCommitTask:
    """Create a one-shot C-level claim around one cleanup call."""
    return _PostCommitTask(
        sink,
        post_commit,
        invocation=starmap(post_commit.action, ((),)),
    )


@dataclass(slots=True)
class _NoticeTask(_OneShotTask):
    """One owed sink notice with bounded caller-side admission recovery."""

    name: str
    stage: str


def _new_notice_task(
    emit_notice: Callable[..., None],
    envelope: EventEnvelope,
    name: str,
    stage: str,
) -> _NoticeTask:
    """Create a one-shot C-level claim around recursive notice publish."""
    action = partial(
        emit_notice,
        envelope,
        code="sink_disabled",
        level="error",
        detail=(("sink", name), ("stage", stage)),
    )
    return _NoticeTask(name, stage, invocation=starmap(action, ((),)))


@dataclass(slots=True)
class _TaskPhase:
    """Durable cursor for a bounded lock-free settlement phase."""

    tasks: list[Any]
    index: int = 0

    def drain(self, step: Callable[[Any], None]) -> None:
        """Resume pending tasks; completed tasks are never invoked again."""
        while self.index < len(self.tasks):
            task = self.tasks[self.index]
            if task.completed:
                self.index += 1
                continue
            step(task)
            if task.completed:
                self.index += 1


@dataclass(slots=True)
class _PublicationTailState:
    """Authoritative progress across every post-linearization phase."""

    after_phase: _TaskPhase | None = None
    settlement_phase: _TaskPhase | None = None
    post_phase: _TaskPhase | None = None
    notice_phase: _TaskPhase | None = None
    exits: list[BaseException] = field(default_factory=list)
    exit_index: int = 0
    publication_exit_processed: bool = False
    boundary_exit_processed: bool = False

    def drain_after(
        self,
        action: Callable[[SequencedEvent], None] | None,
        event: SequencedEvent | None,
        step: Callable[[Any], None],
    ) -> None:
        """Materialize and drain the boundary-owned projection once."""
        if self.after_phase is None:
            tasks = (
                [_new_after_commit_task(action, event)]
                if action is not None and event is not None
                else []
            )
            self.after_phase = _TaskPhase(tasks)
        self.after_phase.drain(step)

    def drain(
        self,
        *,
        commit_settlements: list[tuple[EventSink, BaseException]],
        post_commits: list[tuple[EventSink, PostCommit]],
        newly_disabled: list[tuple[str, str]],
        fatal_notices: list[tuple[str, str]],
        notify_disabled: bool,
        emit_notice: Callable[..., None],
        envelope: EventEnvelope,
        settle_one: Callable[[Any], None],
        run_post: Callable[[Any], None],
        publish_notice: Callable[[Any], None],
    ) -> None:
        """Resume all tail work from durable publication-owned sources."""
        if self.settlement_phase is None:
            tasks = [
                _InterruptedSettlementTask(sink, failure)
                for sink, failure in commit_settlements
                if isinstance(sink, InterruptedCommitSink)
            ]
            self.settlement_phase = _TaskPhase(tasks)
        self.settlement_phase.drain(settle_one)

        if self.post_phase is None:
            tasks = [
                _new_post_commit_task(sink, action)
                for sink, action in post_commits
            ]
            self.post_phase = _TaskPhase(tasks)
        self.post_phase.drain(run_post)

        if self.notice_phase is None:
            notices = list(newly_disabled) if notify_disabled else []
            notices.extend(fatal_notices)
            tasks = [
                _new_notice_task(emit_notice, envelope, name, stage)
                for name, stage in notices
            ]
            self.notice_phase = _TaskPhase(tasks)
        self.notice_phase.drain(publish_notice)


def _settle_interrupted_task(
    task: _InterruptedSettlementTask,
    actions: list[tuple[EventSink, PostCommit]],
) -> None:
    """Capture a settlement action before returning to FanOut housekeeping.

    Implementations retain interrupted owners until the returned action runs;
    repeating this lookup after an asynchronous return-boundary exit is thus
    an idempotent ownership lookup, not a second publication.
    """
    task.attempts += 1
    task.post_commit = task.sink.settle_interrupted_commit(task.failure)
    if task.post_commit is not None and not any(
        sink is task.sink and action is task.post_commit
        for sink, action in actions
    ):
        actions.append((task.sink, task.post_commit))
    task.completed = True


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
        # Any unexpected exit after allocating a sequence is process-wide:
        # allowing a later publication to step over that seq would create a
        # permanent hole shared by every sink. The first cause is monotonic.
        self._publication_fatal: BaseException | None = None
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
        # own close() has returned. The task retains that returned result
        # before Python can be interrupted storing it. A close that raises
        # must leave the FanOut unfinished: the caller may ask again, and the
        # retry closes exactly the sinks that never succeeded -- never one
        # that did.
        self._close_lock = threading.Lock()
        self._close_progress = [_SinkCloseProgress(sink) for sink in self._sinks]

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

    def remember_publication_fatal(self, failure: BaseException) -> None:
        """Record the first publication-wide failure under FanOut's lock."""
        with self._lock:
            if self._publication_fatal is None:
                self._publication_fatal = failure

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
        # Establish the recovery domain before entering any publication work.
        # Its installation is intentionally before sequence allocation: an
        # exit there has no partially published event to recover.
        outcome_state = _PublicationOutcomeState(self)
        with _PublicationOutcomeGuard(outcome_state):
            return self._publish_guarded(
                unsequenced,
                notify_disabled=notify_disabled,
                boundary=boundary,
                after_commit=after_commit,
                outcome_state=outcome_state,
            )

    def _publish_guarded(
        self,
        unsequenced: UnsequencedEvent,
        *,
        notify_disabled: bool,
        boundary: AbstractContextManager[object] | None = None,
        after_commit: Callable[[SequencedEvent], None] | None = None,
        outcome_state: _PublicationOutcomeState,
    ) -> SequencedEvent:
        with self._lock:
            publication_fatal = self._publication_fatal
        if publication_fatal is not None:
            raise ProcessFatalSinkError(
                "the fan-out publication sequence is unavailable"
            ) from publication_fatal
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
        commit_settlements: list[tuple[EventSink, BaseException]] = []
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
        control_error: BaseException | None = None
        deferred_publication_fatal: BaseException | None = None
        escaped_publication: list[BaseException] = []
        escaped_boundary: list[BaseException] = []
        active_sink: EventSink | None = None
        active_index: int | None = None
        active_failure: BaseException | None = None
        commit_index = 0
        commit_pending = object()
        post_commit: Any = commit_pending
        commit_result: list[PostCommit | None] = []
        sequenced: SequencedEvent | None = None
        tail_state = _PublicationTailState()

        def drain_commits_locked() -> None:
            """Commit every remaining prepared sink under the publication lock."""
            nonlocal active_failure, active_index, active_sink, commit_index
            nonlocal commit_result, control_error, linearize_error, post_commit
            while commit_index < len(prepared):
                try:
                    while commit_index < len(prepared):
                        active_index = commit_index
                        sink, prep = prepared[active_index]
                        active_sink = sink
                        active_failure = None
                        post_commit = commit_pending
                        commit_result = []
                        if sink.name in self._process_disabled:
                            # A concurrently published fatal already reported
                            # why this sink is gone.
                            commit_index += 1
                            active_sink = None
                            active_index = None
                            continue
                        if sink.name in self._disabled.get(run_id, set()):
                            self._note_sink_failure_locked(
                                run_id,
                                sink,
                                "commit_skipped",
                                unsequenced.trace_policy,
                            )
                            commit_index += 1
                            active_sink = None
                            active_index = None
                            continue
                        try:
                            # ``map`` appends the starmap result before
                            # ``next`` returns to Python bytecode. The
                            # pre-reachable slot therefore distinguishes a
                            # returned None/PostCommit from a commit that
                            # never returned, even if STORE below is the
                            # instruction interrupted by process control.
                            post_commit = next(
                                map(
                                    commit_result.append,
                                    starmap(
                                        sink.commit,
                                        ((prep, sequenced),),
                                    ),
                                )
                            )
                            post_commit = commit_result[0]
                            if post_commit is not None:
                                _record_post_commit(
                                    post_commits, sink, post_commit
                                )
                            _finish_commit_iteration()
                        except Exception as exc:
                            active_failure = exc
                            commit_settlements.append((sink, exc))
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
                                    exc,
                                    sink.name,
                                    stage_reported,
                                    newly_disabled,
                                    fatal_notices,
                                )
                        except BaseException as exc:
                            # Process control is not a run-local sink failure.
                            # Keep its exact first object, but still commit
                            # every prepared sibling.
                            active_failure = exc
                            if control_error is None:
                                control_error = exc
                            if (
                                post_commit is commit_pending
                                and not commit_result
                            ):
                                # No result reached the C-level slot: a
                                # critical sink's effect is unknown for this
                                # Run even if flush later says "flushed".
                                self._note_critical_commit_control_locked(
                                    run_id, sink
                                )
                                commit_settlements.append((sink, exc))
                            else:
                                returned_post_commit = (
                                    commit_result[0]
                                    if commit_result
                                    else post_commit
                                )
                                if (
                                    returned_post_commit is not None
                                    and not any(
                                        owner is sink
                                        and action is returned_post_commit
                                        for owner, action in post_commits
                                    )
                                ):
                                    post_commits.append(
                                        (sink, returned_post_commit)
                                    )
                                if (
                                    post_commit is not commit_pending
                                    and self._publication_fatal is None
                                ):
                                    self._publication_fatal = exc
                        commit_index = max(commit_index, active_index + 1)
                        active_sink = None
                        active_failure = None
                        post_commit = commit_pending
                        commit_result = []
                        active_index = None
                except BaseException as exc:
                    # Explicit progress lets this recovery resume the next
                    # prepared sink without relying on a Python iterator.
                    returned_before_store = (
                        active_sink is not None
                        and post_commit is commit_pending
                        and bool(commit_result)
                    )
                    if (
                        not returned_before_store
                        and active_failure is None
                        and self._publication_fatal is None
                    ):
                        self._publication_fatal = exc
                    if isinstance(exc, Exception):
                        if linearize_error is None:
                            linearize_error = exc
                    elif control_error is None:
                        control_error = exc
                    if active_sink is not None:
                        if (
                            post_commit is not commit_pending
                            and post_commit is not None
                            and not any(
                                owner is active_sink and action is post_commit
                                for owner, action in post_commits
                            )
                        ):
                            post_commits.append((active_sink, post_commit))
                        if (
                            post_commit is commit_pending
                            and not commit_result
                        ):
                            self._note_critical_commit_control_locked(
                                run_id, active_sink
                            )
                            commit_settlements.append(
                                (active_sink, active_failure or exc)
                            )
                        elif commit_result:
                            returned_post_commit = commit_result[0]
                            if (
                                returned_post_commit is not None
                                and not any(
                                    owner is active_sink
                                    and action is returned_post_commit
                                    for owner, action in post_commits
                                )
                            ):
                                post_commits.append(
                                    (active_sink, returned_post_commit)
                                )
                    if active_index is not None:
                        commit_index = max(commit_index, active_index + 1)
                    active_sink = None
                    active_failure = None
                    post_commit = commit_pending
                    commit_result = []
                    active_index = None

        def run_after(task_value: Any) -> None:
            nonlocal control_error, deferred_publication_fatal, linearize_error
            task: _AfterCommitTask = task_value
            task.attempts += 1
            try:
                _invoke_task(task)
            except Exception as exc:
                if linearize_error is None:
                    linearize_error = exc
                deferred_publication_fatal = deferred_publication_fatal or exc
                if task.attempts < 2:
                    return
                task.completed = True
            except BaseException as exc:
                if control_error is None:
                    control_error = exc
                deferred_publication_fatal = deferred_publication_fatal or exc
                if task.attempts < 2:
                    return
                task.completed = True

        def settle_one(task_value: Any) -> None:
            nonlocal control_error, linearize_error
            task: _InterruptedSettlementTask = task_value
            try:
                _settle_interrupted_task(task, post_commits)
            except BaseException as exc:
                if isinstance(exc, Exception):
                    if linearize_error is None:
                        linearize_error = exc
                elif control_error is None:
                    control_error = exc
                if task.post_commit is not None:
                    action = task.post_commit
                    if not any(
                        sink is task.sink and pending is action
                        for sink, pending in post_commits
                    ):
                        post_commits.append((task.sink, action))
                    task.completed = True
                    tail_state.exits.append(exc)
                elif task.attempts >= 2:
                    task.completed = True

        def run_post(task_value: Any) -> None:
            nonlocal control_error, deferred_publication_fatal, linearize_error
            task: _PostCommitTask = task_value
            task.attempts += 1
            try:
                _invoke_task(task)
            except Exception as exc:
                failure: Exception = exc
                if (
                    task.post_commit.failure_scope == "process_fatal"
                    and not isinstance(exc, ProcessFatalSinkError)
                ):
                    failure = ProcessFatalSinkError(
                        "a process-fatal post-commit action failed"
                    )
                    failure.__cause__ = exc
                with self._lock:
                    stage_reported = self._note_sink_call_failed_locked(
                        run_id,
                        task.sink,
                        "post_commit",
                        unsequenced.trace_policy,
                        failure,
                        already_reported,
                        event_committed=True,
                    )
                if stage_reported is not None:
                    _book_failure(
                        failure,
                        task.sink.name,
                        stage_reported,
                        newly_disabled,
                        fatal_notices,
                    )
                if task.attempts < 2:
                    return
                task.completed = True
            except BaseException as exc:
                if control_error is None:
                    control_error = exc
                deferred_publication_fatal = deferred_publication_fatal or exc
                if task.attempts < 2:
                    return
                task.completed = True

        def publish_notice(task_value: Any) -> None:
            nonlocal control_error, deferred_publication_fatal, linearize_error
            task: _NoticeTask = task_value
            task.attempts += 1
            try:
                _invoke_task(task)
            except Exception as exc:
                if linearize_error is None:
                    linearize_error = exc
                if task.attempts < 2:
                    return
                task.completed = True
            except BaseException as exc:
                if control_error is None:
                    control_error = exc
                deferred_publication_fatal = deferred_publication_fatal or exc
                if task.attempts < 2:
                    return
                task.completed = True

        def record_tail_exit(exc: BaseException) -> None:
            nonlocal control_error, deferred_publication_fatal, linearize_error
            deferred_publication_fatal = deferred_publication_fatal or exc
            if isinstance(exc, Exception):
                if linearize_error is None:
                    linearize_error = exc
            elif control_error is None:
                control_error = exc

        def process_publication_exit_locked() -> None:
            nonlocal control_error, linearize_error
            if (
                escaped_publication
                and not tail_state.publication_exit_processed
            ):
                escaped = escaped_publication[0]
                returned_before_store = (
                    active_sink is not None
                    and post_commit is commit_pending
                    and bool(commit_result)
                )
                resumable_cursor = (
                    sequenced is not None
                    and active_sink is None
                    and active_index is None
                )
                if (
                    not returned_before_store
                    and active_failure is None
                    and not resumable_cursor
                    and self._publication_fatal is None
                ):
                    self._publication_fatal = escaped
                if active_sink is not None:
                    commit_returned = (
                        bool(commit_result)
                        or post_commit is not commit_pending
                    )
                    returned_post_commit = (
                        commit_result[0]
                        if commit_result
                        else post_commit
                    )
                    if (
                        commit_returned
                        and returned_post_commit is not None
                        and not any(
                            owner is active_sink
                            and action is returned_post_commit
                            for owner, action in post_commits
                        )
                    ):
                        post_commits.append(
                            (active_sink, returned_post_commit)
                        )
                    if not commit_returned:
                        self._note_critical_commit_control_locked(
                            run_id, active_sink
                        )
                        if not any(
                            owner is active_sink
                            and failure is (active_failure or escaped)
                            for owner, failure in commit_settlements
                        ):
                            commit_settlements.append(
                                (active_sink, active_failure or escaped)
                            )
                if isinstance(escaped, Exception):
                    if linearize_error is None:
                        linearize_error = escaped
                elif control_error is None:
                    control_error = escaped
                tail_state.publication_exit_processed = True

        def process_captured_exits() -> None:
            nonlocal control_error, linearize_error
            with self._lock:
                process_publication_exit_locked()
            if escaped_boundary and not tail_state.boundary_exit_processed:
                escaped = escaped_boundary[0]
                if sequenced is not None:
                    with self._lock:
                        if self._publication_fatal is None:
                            self._publication_fatal = escaped
                if isinstance(escaped, Exception):
                    if linearize_error is None:
                        linearize_error = escaped
                elif control_error is None:
                    control_error = escaped
                tail_state.boundary_exit_processed = True

        def drain_tail() -> None:
            process_captured_exits()
            tail_state.drain(
                commit_settlements=commit_settlements,
                post_commits=post_commits,
                newly_disabled=newly_disabled,
                fatal_notices=fatal_notices,
                notify_disabled=notify_disabled,
                emit_notice=self._emit_notice,
                envelope=unsequenced.envelope,
                settle_one=settle_one,
                run_post=run_post,
                publish_notice=publish_notice,
            )

        def finalize_tail() -> None:
            while tail_state.exit_index < len(tail_state.exits):
                exc = tail_state.exits[tail_state.exit_index]
                record_tail_exit(exc)
                tail_state.exit_index += 1
            if deferred_publication_fatal is not None:
                with self._lock:
                    if self._publication_fatal is None:
                        self._publication_fatal = deferred_publication_fatal

        with _CapturePublicationExit(escaped_boundary), boundary_context:
            with self._lock:
                captured_count = len(escaped_publication)
                with _CapturePublicationExit(escaped_publication):
                    outcome_state.publication_started = True
                    if self._publication_fatal is not None:
                        raise ProcessFatalSinkError(
                            "the fan-out publication sequence is unavailable"
                        ) from self._publication_fatal
                    try:
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
                    except BaseException as exc:
                        if self._publication_fatal is None:
                            self._publication_fatal = exc
                        raise
                    drain_commits_locked()
                if len(escaped_publication) > captured_count:
                    # The outer commit recovery itself was interrupted. Its
                    # current sink result is still durable, so settle it and
                    # resume the explicit cursor before releasing either the
                    # sequence lock or the caller's projection boundary.
                    process_publication_exit_locked()
                    if active_index is not None:
                        commit_index = max(commit_index, active_index + 1)
                        active_sink = None
                        active_failure = None
                        post_commit = commit_pending
                        commit_result = []
                        active_index = None
                    if (
                        sequenced is not None
                        and commit_index < len(prepared)
                    ):
                        drain_commits_locked()
            with _CapturePublicationExit(tail_state.exits):
                tail_state.drain_after(after_commit, sequenced, run_after)
            tail_state.drain_after(after_commit, sequenced, run_after)
        unexpected_exit: BaseException | None = None
        try:
            try:
                drain_tail()
            finally:
                drain_tail()
            finalize_tail()
        except BaseException as exc:
            unexpected_exit = exc
        if unexpected_exit is not None:
            tail_state.exits.append(unexpected_exit)
            drain_tail()
            finalize_tail()
        outcome_error: BaseException | None = control_error or linearize_error
        if outcome_error is None and sequenced is None:
            outcome_error = ProcessFatalSinkError(
                "the fan-out publication sequence is unavailable"
            )
            outcome_error.__cause__ = self._publication_fatal
        outcome_state.expected = outcome_error
        with _PublicationOutcomeGuard(outcome_state):
            if outcome_error is not None:
                raise outcome_error
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

    def _note_critical_commit_control_locked(
        self, run_id: str, sink: EventSink
    ) -> None:
        """Remember only the durability consequence of an unknown commit.

        Process-control exits remain reusable and produce no disabled notice,
        but a critical sink whose call did not return cannot later turn that
        Run's barrier green merely by reporting that its known queue is flush.
        The caller holds the publication lock.
        """
        if sink.flush_at_run_end:
            reasons = self._persist_lost.setdefault(run_id, [])
            reasons.append(f"{sink.name} commit failed")

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

    def checkpoint_barrier(self, run_id: str | None = None) -> tuple[bool, str | None]:
        """Confirm a prefix without retiring Run state or hiding earlier loss."""
        with self._lock:
            lost = (
                bool(self._persist_lost)
                if run_id is None
                else bool(self._persist_lost.get(run_id))
            )
        critical = [sink for sink in self._sinks if sink.flush_at_run_end]
        failed = lost or not critical
        for sink in critical:
            checkpoint = getattr(sink, "checkpoint", None)
            if checkpoint is None:
                failed = True
                continue
            try:
                result = checkpoint(run_id)
            except Exception:
                failed = True
                continue
            if (
                not isinstance(result, BarrierFlushResult)
                or result.outcome != "flushed"
                or result.dropped_events
            ):
                failed = True
        with self._lock:
            failed = failed or (
                bool(self._persist_lost)
                if run_id is None
                else bool(self._persist_lost.get(run_id))
            )
            if failed:
                affected = tuple(self._last_envelope) if run_id is None else (run_id,)
                for affected_run in affected:
                    self._persist_lost.setdefault(affected_run, []).append(
                        "trace_checkpoint_failed"
                    )
        return failed, "trace_checkpoint_failed" if failed else None

    def flush_barrier(self, run_id: str) -> tuple[bool, str | None]:
        """Wait on flush_at_run_end sinks. Missing/failed/exception => incomplete."""
        reasons: list[str] = []
        if run_id is not None:
            reasons.extend(self._persist_lost.get(run_id, ()))
        try:
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
                except BaseException as exc:
                    reasons.append(
                        f"{sink.name} flush raised {type(exc).__name__}"
                    )
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
            return incomplete, reason
        finally:
            if run_id is not None:
                # A sink may raise process control from its own flush or from
                # the incomplete notice.  Either way this Run's bookkeeping
                # is terminal and must not leak into later Runs.
                with self._lock:
                    self._disabled.pop(run_id, None)
                    self._persist_lost.pop(run_id, None)
                    self._last_envelope.pop(run_id, None)

    def close(self, timeout: float | None = None) -> bool:
        """Close every sink, resumable per sink.

        One sink returning False is still draining, so its completion bit
        does not move and a later close asks it again. Legacy sinks return
        None; that remains a confirmed close. When a sink raises after
        publishing :class:`CloseCompletion` completion, that fact retires
        it before the original exception propagates. Otherwise it remains
        unfinished and its close contract makes retry idempotent. Asking
        again resumes at unfinished sinks; confirmed sinks are not touched.
        """
        # Attempts are serialized so two racing closers cannot both walk an
        # unfinished sink; the FanOut's close is otherwise on its own lock,
        # never on the publish lock a sink's emit path shares.
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._close_lock:
            complete = True
            for progress in self._close_progress:
                if progress.closed:
                    continue
                sink = progress.sink
                completion = (
                    sink.close_completed
                    if isinstance(sink, CloseCompletion)
                    else None
                )
                if progress.observing:
                    assert completion is not None
                    try:
                        published_complete = completion()
                    except BaseException:
                        retained = progress.error
                        if retained is not None:
                            raise retained
                        raise
                    if published_complete:
                        progress.retire()
                        continue
                    progress.task = None
                    progress.observing = False
                    progress.error = None
                task = progress.task
                try:
                    if task is None:
                        if isinstance(sink, TimedCloseSink):
                            remaining = (
                                None
                                if deadline is None
                                else max(0.0, deadline - time.monotonic())
                            )
                            action = sink.close_with_timeout
                            arguments = (remaining,)
                        else:
                            action = sink.close
                            arguments = ()
                        task = _new_close_task(action, arguments)
                        progress.task = task
                    if not task.result:
                        if completion is not None:
                            progress.observing = True
                        closed = next(task.invocation)
                    closed = task.result[0]
                except BaseException as exc:
                    published_complete = False
                    if completion is not None:
                        progress.error = exc
                        try:
                            published_complete = completion()
                        except BaseException:
                            # This is only a recovery observation. It cannot
                            # replace the close exception already in flight.
                            pass
                    if published_complete:
                        try:
                            progress.retire()
                        except BaseException:
                            # Retirement is recovery bookkeeping too: keep
                            # the close exception already in flight.
                            pass
                    elif completion is not None:
                        # An interrupted observation is still the only legal
                        # next action. Retain both it and the original error.
                        pass
                    elif task is not None and not task.result:
                        if progress.task is task:
                            progress.task = None
                    raise
                progress.observing = False
                progress.error = None
                if closed is False:
                    progress.task = None
                    complete = False
                    continue
                progress.closed = True
                progress.task = None
            return complete


def _fail_closed_event(event: UnsequencedEvent) -> UnsequencedEvent:
    return replace(
        event,
        payload=Notice(
            level="error", code="redaction_failed", detail=(), evidence=None
        ),
        trace_policy="persist",
        replayable=True,
    )
