"""SSEBroker: the EventSink that carries events from the FanOut to browsers.

Shape (decided in #23 §3 and §9):

- ``prepare`` serializes, splits and builds frames -- pure, lock-free, may be
  slow. Serializing inside the commit lock would let ``seq`` reorder events
  by nothing more than thread scheduling, and then a *correct* system would
  disable its own sink for a fault that never happened.
- ``commit`` is short and quantitative: append to the ring, then one
  ``put_nowait``. Its cost does not depend on how many connections exist --
  not even on the overflow path, where it raises a broker-level disconnect
  generation and kicks the dispatcher instead of walking the registry -- so
  a browser tab cannot slow down a Run.
- One dispatcher thread fans out to per-connection queues; each connection
  has its own writer thread. Backpressure is thereby per connection.
- **The ring is written before the ingress.** Reversing it would make the
  authoritative recovery source the one thing that loses authority exactly
  when the system is congested.

Two failure classes are kept apart. A slow connection is that connection's
problem: transients are dropped and counted, and only an undroppable item
closes it. The dispatcher or the ring failing is a process-level fact and
escalates to ``sink_disabled`` -- which the FanOutSink decides, not this
module.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Literal, NoReturn, Protocol, TypeVar

from agent_alfred.clock import Clock, SystemClock
from agent_alfred.events import (
    BestEffortFlushResult,
    PostCommit,
    ProcessFatalSinkError,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.gateway.web import connection as connection_module
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web import progress as progress_module
from agent_alfred.gateway.web._fifo import FifoNode, FifoState
from agent_alfred.gateway.web._fifo import reverse_nodes as _reverse_ingress_nodes
from agent_alfred.gateway.web.connection import (
    ConnectionQueue,
    ConnectionWriter,
    SSEConnection,
    StartupPrefixReservation,
    StartupReplay,
)
from agent_alfred.gateway.web.frames import (
    CurrentRunState,
    FrameCost,
    PreparedFrames,
)
from agent_alfred.gateway.web.progress import RunProgress, StepProjection
from agent_alfred.gateway.web.replay import (
    CursorText,
    CursorVerdict,
    ReplayBatch,
    ReplayProgress,
    ReplayRing,
    classify_cursor,
)
from agent_alfred.gateway.web.state import build_snapshot, snapshot_payload
from agent_alfred.resource_rollback import (
    BackgroundCloseStep,
    ResumableRollback,
    thread_exit_confirmed,
    thread_start_effect_happened,
)
from agent_alfred.runtime.recording import RecordingUnavailable
from agent_alfred.runtime.snapshot import RuntimeSnapshot
from agent_alfred.session_validity import (
    SessionValidity,
    parse_session_validity,
)

# The decided capacity table. Constructor defaults, not user knobs.
INGRESS_BUDGET = frames.FrameBudget(
    frames=4096, encoded_bytes=32 * 1024 * 1024
)
# Each connection's own queue, so backpressure stays a per-connection fact.
# The numbers live in the connection module -- the queue that implements
# them is the one place they should be readable.
CONNECTION_BUDGET = connection_module.DEFAULT_BUDGET
DEFAULT_HEARTBEAT_S = connection_module.DEFAULT_HEARTBEAT_S
# How long close() waits for the dispatcher and then for the writers. A
# bounded, honest "not fully drained" beats hanging on a socket whose peer
# has stopped reading; the descriptors are released either way.
_DRAIN_TIMEOUT_S = 2.0
# How many capture laps a state patch may spend before it gives up. One lap
# is the common case; a second recaptures a world that moved during one
# encoding. Beyond a bounded few, the world is moving faster than a single
# encode and another lap would be a spin. A publish then preserves the state
# and closes existing connections explicitly; a connect closes only its new
# socket and asks the browser to try again. Neither path may ship the stale
# patch it just lost the race to register.
_MAX_PATCH_CAPTURES = 4
# A lost ``Condition.notify`` cannot leave the dispatcher asleep forever:
# it periodically rechecks the immutable ingress state and broker debts.
_INGRESS_RECHECK_S = 0.05

_RingRead = TypeVar("_RingRead")

@dataclass(frozen=True)
class _SessionAdmissionProof:
    """The one Session fact used while full stream admission is prepared."""

    session_id: str | None
    session_valid: bool


@dataclass(frozen=True)
class StreamAdmissionProof:
    """Unique ownership of a fully admitted stream before HTTP 200."""

    session_id: str | None
    session_valid: bool
    _broker: SSEBroker = field(repr=False, compare=False)
    _acquisition: _ConnectStartup = field(repr=False, compare=False)
    _handle: ConnectionHandle = field(repr=False, compare=False)
    _writer: ConnectionWriter = field(repr=False, compare=False)

    def transferred_handle(self) -> ConnectionHandle | None:
        """Return the writer-owned handle only after ownership committed."""
        with self._acquisition.ownership_lock:
            if not self._acquisition.transferred:
                return None
            return self._handle


class StreamAdmissionRejected(Exception):
    """A complete stream could not be admitted before response headers."""

    status = 503
    code = "stream_unavailable"

    def __init__(self, reason: str, handle: ConnectionHandle):
        super().__init__(reason)
        self.reason = reason
        self.handle = handle


class _IngressStop:
    """Sentinel: the dispatcher should return."""


class _IngressKick:
    """Sentinel: the dispatcher should sweep stale disconnect generations.

    It exists for liveness, not for data. An overflow that happens while the
    dispatcher is blocked on an empty ingress would otherwise leave every
    pre-incident connection open until some later event happened to arrive;
    the kick wakes it now. Like the stop sentinel it carries nothing and
    costs nothing, and it goes through its own no-argument door so no item
    can ride past the budgets on it.
    """


class _AbortFencedCommit(Exception):
    """Internal jump from a failed mutation to lock-free settlement."""


class _FenceUnexpectedCommitExit:
    """Latch an unexpected commit exit before the broker lock is released."""

    def __init__(self, broker: SSEBroker, escaped: list[BaseException]):
        self._broker = broker
        self._escaped = escaped

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        if exc is None or isinstance(exc, _AbortFencedCommit):
            return False
        if isinstance(exc, ProcessFatalSinkError):
            return False
        self._broker._latch_fatal_locked(exc)
        self._escaped.append(exc)
        return True


@dataclass(slots=True)
class _CommitCleanupDebt:
    """One ring-retirement owner from commit until successful release."""

    token: object
    release_retired: Callable[[], None]
    handoff_owner: int | None = None
    failure: BaseException | None = None
    # Registration is the durable handoff. The actual release remains outside
    # broker/FanOut locks and may be claimed by the returned PostCommit,
    # dispatcher, or shutdown immediately after this O(1) publication.
    ready: bool = True
    in_progress: bool = False
    completed: bool = False
    wake_on_settlement: bool = True
    claim: object | None = field(default=None, repr=False, compare=False)


def _unbound_session_check(session_id: str | None) -> SessionValidity:
    """The answer before :meth:`SSEBroker.bind_session_check` has run.

    Silently claiming "valid" would be a lie about a database nobody has been
    pointed at yet, and claiming "invalid" would hide every Session; refusing
    is the only answer that cannot be mistaken for a fact.
    """
    del session_id
    raise RuntimeError("bind_session_check() must run before the first connection")


@dataclass(frozen=True)
class _BroadcastPatch:
    """A lifecycle patch fully prepared before the dispatcher sees it.

    It goes through the ingress rather than straight into the connection
    queues so that patches and domain events leave in one order: a client
    that receives the patch before the event it describes sees a state that
    was never published.

    The two frames differ only in this connection's preflight-proven Session
    validity. Keeping both here leaves the dispatcher with no database read
    and no serialization work: it selects one immutable reference and offers
    it to the connection queue.

    It pays the same two budgets as an event. A patch that bypassed them
    would be the one item that can grow without bound while a dispatcher is
    wedged -- and it is precisely the item whose loss is invisible, because
    the state it carries is absolute rather than incremental.
    """

    snapshot: RuntimeSnapshot
    valid_frame: PreparedFrames
    invalid_frame: PreparedFrames
    cost: FrameCost

    def ingress_cost(self) -> FrameCost:
        return self.cost

    def for_session(self, session_valid: bool) -> PreparedFrames:
        return self.valid_frame if session_valid else self.invalid_frame


@dataclass(frozen=True)
class _PublishedEvent:
    """One published domain event on its way to the dispatcher.

    Two roles, kept apart on purpose. ``published_seq`` is the publication
    boundary used to suppress pre-registration ingress. ``frames.seq`` is
    the same number carried by every domain-event payload, transient or not;
    checkpoint ownership is represented only by ``frames.id_line``, which
    stays empty for a transient. ``ingress_dropped_before`` freezes the
    cumulative transient debt that existed before this event was admitted;
    a later overflow therefore cannot travel backwards and precede it on a
    connection.

    Conflating the two is how a preregistration transient resurrects
    itself: a boundary taken from the replayable high water alone leaves
    every in-flight delta outside it, and a checkpoint taken from the
    internal seq would mint an ``id:`` the ring cannot honour.
    """

    published_seq: int
    frames: PreparedFrames
    ingress_dropped_before: int = 0

    def ingress_cost(self) -> FrameCost:
        return self.frames.ingress_cost()


class IngressItem(Protocol):
    """Anything the ingress will carry: a cost in frames *and* in bytes.

    Both budgets are counted on the way in and released on the way out, so
    an item that could not say what it costs could not be accounted for --
    which is how a patch would end up growing a queue without limit.
    """

    def ingress_cost(self) -> FrameCost: ...


def _patch_frames(
    snapshot: RuntimeSnapshot, step: StepProjection | None, session_valid: bool
) -> PreparedFrames:
    """One connection's patch frame. The only way a patch is ever encoded.

    A patch is absolute, so what one connection gets differs from another's
    in exactly one field -- whether *this* connection's Session still exists.
    Everything else is the same state, encoded once per connection.
    """
    view = build_snapshot(snapshot, step=step, session_valid=session_valid)
    return frames.state_patch_frames(snapshot_payload(view))


def _patch_cost(snapshot: RuntimeSnapshot, step: StepProjection | None) -> int:
    """What one patch costs the ingress, in encoded bytes.

    Measured rather than estimated -- a budget that guessed at the payload
    would be a budget that did not apply -- but measured with the same
    encoder the frame uses, without building the frame: this runs inside a
    state transition, where the listener has to be bounded and must not
    fail. ``session_valid=False`` is the larger of the two encodings, so
    the charge is never less than what any connection's frame will cost.
    """
    view = build_snapshot(snapshot, step=step, session_valid=False)
    return frames.payload_cost(frames.STATE_PATCH, snapshot_payload(view))


_SnapshotRelation = Literal["older", "advance", "repeat", "conflict"]


def _snapshot_relation(
    candidate: RuntimeSnapshot,
    current: RuntimeSnapshot,
) -> _SnapshotRelation:
    """Classify every absolute replacement through one revision rule."""
    if candidate.state_revision < current.state_revision:
        return "older"
    if candidate.state_revision > current.state_revision:
        return "advance"
    return "repeat" if candidate == current else "conflict"


_IngressValue = IngressItem | _IngressStop | _IngressKick


@dataclass(frozen=True, slots=True)
class _IngressState(FifoState[_IngressValue]):
    """Queue accounting and coalesced controls published by one store."""

    kick_pending: bool
    stop_requested: bool


class _IngressQueue:
    """Persistent FIFO with atomic accounting and coalesced controls.

    Producers construct an immutable replacement and publish it with one
    assignment. An exception before that store has no effect; one afterwards
    leaves membership, accounting, kick ownership, and stop ownership mutually
    consistent. Enqueue is O(1). The one consumer occasionally reverses the
    persistent back list locally, so dequeue is amortized O(1), then publishes
    its complete removal with the same single-store rule.
    """

    def __init__(self, budget: frames.FrameBudget) -> None:
        self._budget = budget
        self._condition = threading.Condition(threading.Lock())
        self._consumer_lock = threading.Lock()
        self._state = _IngressState(
            front=None,
            back=None,
            rotation=None,
            usage=FrameCost(frames=0, encoded_bytes=0),
            size=0,
            kick_pending=False,
            stop_requested=False,
        )

    @property
    def current_cost(self) -> FrameCost:
        with self._condition:
            return self._state.usage

    def offer(self, item: IngressItem) -> bool:
        cost = item.ingress_cost()
        with self._condition:
            state = self._state
            projected = state.usage + cost
            if not self._budget.fits(projected):
                return False
            replacement = _IngressState(
                front=state.front,
                back=FifoNode(item=item, cost=cost, next=state.back),
                rotation=state.rotation,
                usage=projected,
                size=state.size + 1,
                kick_pending=state.kick_pending,
                stop_requested=state.stop_requested,
            )
            self._state = replacement
            self._condition.notify()
            return True

    def put(self, item: _IngressStop | _IngressKick) -> None:
        """Insert one free control item, coalescing by its lifetime."""
        if not isinstance(item, (_IngressStop, _IngressKick)):
            raise TypeError("only ingress controls bypass the budget")
        with self._condition:
            state = self._state
            if isinstance(item, _IngressKick) and state.kick_pending:
                # The slot may have committed immediately before its notify
                # edge was interrupted. Retrying only the wake-up is safe.
                self._condition.notify()
                return
            if isinstance(item, _IngressStop) and state.stop_requested:
                self._condition.notify()
                return
            replacement = _IngressState(
                front=state.front,
                back=FifoNode(
                    item=item,
                    cost=FrameCost(frames=0, encoded_bytes=0),
                    next=state.back,
                ),
                rotation=state.rotation,
                usage=state.usage,
                size=state.size + 1,
                kick_pending=state.kick_pending or isinstance(item, _IngressKick),
                stop_requested=(
                    state.stop_requested or isinstance(item, _IngressStop)
                ),
            )
            self._state = replacement
            self._condition.notify()

    def get(
        self,
        block: bool = True,
        timeout: float | None = None,
    ) -> _IngressValue:
        if not block and timeout is not None:
            raise ValueError("can't specify a timeout for a non-blocking get")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._consumer_lock:
            while True:
                rotation = None
                with self._condition:
                    while self._state.size == 0:
                        if not block:
                            raise queue.Empty
                        wait_for = _INGRESS_RECHECK_S
                        if deadline is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise queue.Empty
                            wait_for = min(wait_for, remaining)
                        self._condition.wait(wait_for)
                    state = self._state
                    if state.front is not None:
                        front = state.front
                        item = front.item
                        replacement = replace(
                            state.pop_front(),
                            kick_pending=(
                                False
                                if isinstance(item, _IngressKick)
                                else state.kick_pending
                            ),
                        )
                        self._state = replacement
                        return item
                    if state.rotation is None:
                        rotation = state.back
                        self._state = state.begin_rotation()
                    else:
                        rotation = state.rotation

                # The only O(N) operation is outside the producer condition.
                # If it is interrupted, ``state.rotation`` still owns the
                # untouched source and the next consumer can retry safely.
                front = _reverse_ingress_nodes(rotation)
                with self._condition:
                    state = self._state
                    self._state = state.finish_rotation(rotation, front)
                # ``state`` and ``rotation`` retain the handed-off source
                # chain. Release both outside the producer condition: on
                # refcounted runtimes, dropping the final node reference can
                # recursively dispose the complete O(N) prefix.
                del state
                del rotation

    def qsize(self) -> int:
        with self._condition:
            return self._state.size


class _Ingress:
    """The shared, bounded, dual-counted hand-off to the dispatcher.

    Everything that can be dropped, grown without bound, or left unread by a
    wedged dispatcher is counted here. The one exception is the sentinel
    that ends the dispatcher itself, which has its own door and cannot be
    used to smuggle anything else through.
    """

    def __init__(
        self,
        *,
        budget: frames.FrameBudget = INGRESS_BUDGET,
    ):
        self.budget = budget
        self._items = _IngressQueue(budget)

    @property
    def current_cost(self) -> FrameCost:
        """What the ingress is holding right now, in both counted units."""
        return self._items.current_cost

    def offer(self, item: IngressItem) -> bool:
        """Queue one item. Non-blocking, O(1), never partial.

        Domain events and state patches are the only two kinds of traffic
        here, and both are measured in frames *and* encoded bytes.
        """
        return self._items.offer(item)

    def put_stop(self) -> None:
        """The dispatcher's own end signal. The one item with no cost.

        It takes no argument on purpose: a ``put_control`` that accepted an
        arbitrary item is how a patch would have been classed as "control"
        and waved past both budgets.
        """
        self._items.put(_IngressStop())

    def put_kick(self) -> None:
        """Wake the dispatcher for a generation sweep. Also free.

        Same door, same argument: the kick carries nothing, so a method that
        accepted an item would be a second way past the budgets.  Pending
        wake-ups merge here, at the owner of the queue effect, rather than in
        the broker before this call: an interrupted caller can retry without
        inheriting a bit for a sentinel that was never inserted.
        """
        self._items.put(_IngressKick())

    def take(
        self, timeout: float | None = None
    ) -> IngressItem | _IngressStop | _IngressKick:
        item = self._items.get(timeout=timeout)
        return item


@dataclass
class ConnectionHandle:
    """One registered connection: its queue, its thread, its own facts."""

    queue: ConnectionQueue
    connection: SSEConnection
    session_id: str | None = None
    # Last fact established by the Host. When storage later becomes
    # unavailable, an existing stream keeps this fact instead of querying a
    # poisoned connection or inventing a different answer.
    session_valid: bool | None = None
    thread: threading.Thread | None = None
    writer: ConnectionWriter | None = None
    # The broker-level disconnect generation this connection registered
    # under. A generation bump marks an ingress overflow whose undroppable
    # item nobody received; connections from *earlier* generations are the
    # ones that can be missing it, and the dispatcher asks them to hang up.
    # A connection registered at the current generation recovered its
    # starting state from the snapshot and the ring, so a sweep must leave
    # it alone.
    generation: int = 0
    # How many ingress-dropped transients this connection had already been
    # told about. Connections that arrived later are not told about drops
    # that happened before they existed.
    ingress_seen: int = 0
    # The high-water mark at registration: the upper bound of what this
    # connection's replay already covered, measured in *published* seqs --
    # transients included. Live items at or below it are skipped, because
    # replay and live delivery can otherwise both carry the same event to
    # the same client; and a transient at or below it is skipped too,
    # because a delta published before the connection registered is
    # precisely the in-flight attempt ADR-0013 says a reconnect must not
    # see again.
    published_through: int = 0
    verdict: CursorVerdict | None = None
    registered_monotonic: float = 0.0
    # Set once this connection's writer has returned: it has stopped writing
    # and closed the socket, so the thread that was holding the HTTP handler
    # open for it may return.
    finished: threading.Event = field(default_factory=threading.Event)
    # A wedged writer is interrupted by a daemon close worker. The step and
    # ownership bit keep that worker reachable across bounded close attempts.
    connection_close: BackgroundCloseStep = field(
        default_factory=lambda: BackgroundCloseStep("sse-connection-close"),
        repr=False,
    )
    connection_close_owned: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ConnectStartup:
    """Resources owned by ``connect`` until a writer is successfully spawned."""

    connection: SSEConnection
    handle: ConnectionHandle | None = None
    startup_reservation: StartupPrefixReservation | None = None
    registration_open: bool = False
    registered_once: bool = False
    registration_token: object = field(default_factory=object, repr=False)
    handoff_token: object = field(default_factory=object, repr=False)
    handoff_state_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )
    transferred: bool = False
    aborted: bool = False
    abort_completed: bool = False
    close_connection_on_abort: bool = False
    abort_close: BackgroundCloseStep = field(
        default_factory=lambda: BackgroundCloseStep("sse-startup-close"),
        repr=False,
    )
    ownership_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )


class SSEBroker:
    """The Dashboard's EventSink.

    ``flush_at_run_end`` is False and the flush result is
    :class:`BestEffortFlushResult`, so this sink cannot pronounce the word
    "flushed" -- an SSE ``ok`` read as a delivery receipt is a failure mode
    the type system now refuses to express.
    """

    name = "sse"
    flush_at_run_end = False

    def __init__(
        self,
        *,
        process_instance_id: str,
        snapshot: RuntimeSnapshot,
        session_is_valid: Callable[[str | None], SessionValidity] | None = None,
        ring: ReplayRing | None = None,
        progress: RunProgress | None = None,
        ingress_budget: frames.FrameBudget = INGRESS_BUDGET,
        connection_budget: frames.FrameBudget = CONNECTION_BUDGET,
        max_frame_bytes: int = frames.MAX_FRAME_BYTES,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        clock: Clock | None = None,
        spawn: Callable[[Callable[[], None]], Any] | None = None,
        on_fatal: Callable[[BaseException], None] | None = None,
    ):
        frames.validate_max_frame_bytes(
            max_frame_bytes, minimum=frames.MIN_BROKER_FRAME_BYTES,
        )
        self._instance = frames.validate_process_instance_id(process_instance_id)
        self._latest = snapshot
        # Bound to the Host as soon as the Host exists -- see
        # :meth:`bind_session_check`. Until then there is no Session truth to
        # consult and, more to the point, no connection to answer for.
        self._session_is_valid: Callable[
            [str | None], SessionValidity
        ] = session_is_valid or _unbound_session_check
        # ``ring or ...`` would silently swap in a fresh ring whenever the
        # caller's happens to be empty, because ReplayRing is sized. An empty
        # ring is a legal ring, so the default is chosen on None, not on falsy.
        self._ring = ring if ring is not None else ReplayRing()
        self._progress = progress if progress is not None else RunProgress()
        self._connection_budget = connection_budget
        self._max_frame_bytes = max_frame_bytes
        self._heartbeat_s = heartbeat_s
        self._clock = clock or SystemClock()
        # The factory returns an unstarted thread. The broker publishes the
        # concrete identity before ``start()``, closing the only window where
        # a started target could exist without a reachable close owner.
        self._spawn = spawn or _spawn_thread
        self._ingress = _Ingress(budget=ingress_budget)
        # Publication facts and connection membership are separate lock
        # domains. The only cross-domain order is publication -> registry;
        # neither domain is held while entering a connection queue.
        self._lock = threading.Lock()
        self._registry_lock = threading.Lock()
        self._projection_boundary: AbstractContextManager[object] | None = None
        self._connections: list[ConnectionHandle] = []
        self._ingress_dropped = 0
        self._run_start_seq: dict[str, int] = {}
        self._dispatcher: threading.Thread | None = None
        self._dispatcher_started: bool | None = False
        self._on_fatal = on_fatal
        self._stopping = False
        # The registration fence: how many connects hold the space between
        # "the handle entered the registry" and "its writer thread exists".
        # A close that ignored this count could answer True in that window
        # -- and the writer would start afterwards, on a broker the caller
        # has already been told is closed. It is observable so a test can
        # pin the fence rather than guess at the window.
        self._registrations: dict[object, _ConnectStartup] = {}
        # An abort may retire registration before its cleanup thread returns.
        # Keep the concrete acquisition reachable until that thread has exited.
        self._startup_closes: dict[object, _ConnectStartup] = {}
        # ``Thread.start`` may enter its target and then raise before the caller
        # can finish the transfer. The concrete thread is already on the handle;
        # this ledger additionally keeps its startup branch reachable until it
        # has observed either the transfer or its abort and its thread has
        # verifiably exited. A target cannot attest to its own thread death.
        self._active_stream_handoffs: dict[object, threading.Thread] = {}
        # The dispatcher's death, if it has died: the original failure, kept
        # beside ``_stopping`` so every path that would pour work into a
        # dead fan-out -- commit, connect, state patches -- can refuse on a
        # fact instead of pretending the stream still works. Published in
        # the same critical section as ``_stopping``.
        self._fatal: BaseException | None = None
        # A fatal transition owns a dispatcher wake-up until ``put_kick``
        # returns normally.  This is separate from ingress coalescing: the
        # former makes an interrupted handoff resumable, while the latter
        # keeps successful concurrent handoffs bounded to one queue item.
        self._fatal_kick_owed = False
        # The disconnect generation: bumped once per ingress overflow whose
        # item nobody received. Bumping is O(1) and is the whole publish-path
        # cost of an overflow; finding the connections that predate the bump
        # is the dispatcher's job, outside the publish critical section.
        self._disconnect_generation = 0
        self._swept_generation = 0
        # Any exception can arrive after the ring committed but before this
        # sink returned its PostCommit. The cleanup owner stays in this
        # separate lock domain until cleanup itself succeeds; FanOut merely
        # marks it ready after releasing the publication boundary. The
        # authoritative registry retains each debt throughout settlement;
        # per-debt claims let callback, dispatcher, or close race without
        # losing ownership or releasing it twice.
        self._commit_cleanup_lock = threading.Lock()
        self._pending_commit_cleanup: dict[
            object, _CommitCleanupDebt
        ] = {}
        # A normal commit registers its cleanup before returning PostCommit.
        # The token remains in this per-publisher insertion-ordered map until
        # either the action claims it or FanOut reports that the commit return
        # itself was interrupted. Thus the RETURN_VALUE -> caller STORE_FAST
        # window has a durable, O(1), thread-specific recovery path.
        self._returned_commit_cleanup: dict[
            int, dict[object, _CommitCleanupDebt]
        ] = {}
        # A failed commit that acquired no cleanup owner records that fact so
        # settlement cannot accidentally take an older unclaimed return from
        # the same publisher thread.
        self._cleanup_free_commit_failures: dict[
            tuple[int, int], BaseException
        ] = {}
        # A failed ReplayRing write already has a durable owner inside the
        # poisoned ring. Keep only the one possible claim here: fatal fencing
        # prevents a second ring write, and settlement merely makes this owner
        # executable after FanOut's publication boundary is gone.
        self._ring_cleanup_failure: BaseException | None = None
        self._ring_cleanup_ready = False
        self._ring_cleanup_in_progress = False
        self._ring_cleanup_claim: object | None = None
        # Bumped once per publication (commit or state patch) under the
        # lock that did the publishing. A connect that encodes its opening
        # stream outside the lock re-checks this counter at registration:
        # unchanged means the captured snapshot is still authoritative;
        # changed means the world moved and the stream is re-captured.
        self._state_epoch = 0
        # The close's own progress, kept apart from "closed": which of its
        # steps have already run. A close that runs out of time has executed
        # its steps but not finished its job, and the next call resumes --
        # re-posting the sentinel would just be an item nobody reads, and
        # re-stopping queues is a lie about progress even when it is harmless.
        self._stop_sent = False
        self._queues_stopped = False
        self._closed = False

    def bind_session_check(
        self, check: Callable[[str | None], SessionValidity]
    ) -> None:
        """Aim the "does this Session exist?" question at the Host.

        The broker is built before the Host -- the Host's authoritative state
        store has to publish patches into it from its very first transition,
        including the ones during start-up recovery -- so the one thing the
        broker needs *from* the Host is supplied the moment the Host exists.

        Until then there is no Session truth to consult, and no connection to
        give an answer to either: the socket does not open before the Host,
        the broker and the guard are all in place.
        """
        with self._lock:
            self._session_is_valid = check

    def preflight_session(self, session_id: str | None) -> _SessionAdmissionProof:
        """Establish one immutable Session fact before response ownership moves."""
        validity = parse_session_validity(self._session_is_valid(session_id))
        if validity == "unavailable":
            raise RecordingUnavailable("recording store is unavailable")
        return _SessionAdmissionProof(
            session_id=session_id,
            session_valid=validity == "valid",
        )

    def bind_projection_boundary(
        self, boundary: AbstractContextManager[object]
    ) -> None:
        """Keep opening snapshots outside a Step publication boundary.

        Connection capture and registration take this outer boundary before
        the broker lock. Event publication uses the same order, so neither
        side reverses locks; startup encoding remains outside both.
        """
        with self._lock:
            self._projection_boundary = boundary

    # -- EventSink ---------------------------------------------------------

    def prepare(self, event: UnsequencedEvent) -> object:
        """Serialize, split, and build frames. Pure; may be slow; no IO.

        One logical event becomes one or more physical frame templates here.
        The ``seq`` does not exist yet; commit binds it into the templates in
        constant time after FanOut assigns publication order.

        A broker behind a dead dispatcher refuses here too: framing an event
        nobody can deliver is work wasted, and the refusal is the typed
        process-fatal error the FanOut needs. The check reads a flag that
        only ever moves from ``None`` to set, so a refusal seen here is
        stable without a lock, a refusal not yet seen is caught one step
        later by ``commit``, and prepare stays lock-free (ADR-0015).
        """
        if self._fatal is not None:
            raise ProcessFatalSinkError(
                "the dispatcher is down; this sink cannot deliver"
            ) from self._fatal
        from agent_alfred.events import ToolFinished, event_json_default

        public_payload = event.payload
        if isinstance(public_payload, ToolFinished):
            public_payload = event_json_default(public_payload)
            public_payload.pop("audit_content", None)
        return frames.domain_event_frames(
            event_name=event.payload.name,
            payload={"envelope": event.envelope, "payload": public_payload},
            event_id=event.event_id,
            replayable=event.replayable,
            max_frame_bytes=self._max_frame_bytes,
        )

    def commit(
        self, prepared: object, event: SequencedEvent
    ) -> PostCommit | None:
        """Ring first, then ingress. Short, quantitative, never blocking.

        Every published domain event advances the ring's published high
        water in the same step that appends it (or, for a transient, in a
        step that appends nothing) -- one atomic
        :meth:`ReplayRing.observe_published` call, so no observer can see a
        published seq whose replayable entry is not there yet.
        Eviction is the ring's bounded index update; commit consumes only
        the admission verdict, never a per-entry account of the displaced
        prefix.

        An overflow costs a generation bump and one kick: O(1) in
        connections. Walking the registry from in here would charge the
        emitting thread for the size of the crowd -- and trip over a wedged
        queue on the way -- so the dispatcher is what asks the connections
        predating the incident to hang up.

        Raising is the one honest answer behind a dead dispatcher: the
        caller -- the FanOut -- then disables this sink for every Run and
        records ``sink_disabled`` exactly once, instead of the event
        vanishing into an ingress nobody will ever drain. The same holds
        for a ring that fails to record: the recovery source going down is
        the dispatcher-dying class of failure, so the fatal state is
        published before the typed refusal is raised.
        """
        item: PreparedFrames = prepared  # type: ignore[assignment]
        if self._fatal is not None:
            raise ProcessFatalSinkError(
                "the dispatcher is down; this sink cannot deliver"
            ) from self._fatal
        kick = False
        commit_failure: BaseException | None = None
        control_failure: BaseException | None = None
        fatal_control_failure: BaseException | None = None
        ring_result = None
        returned_cleanup: _CommitCleanupDebt | None = None
        returned_post_commit: PostCommit | None = None
        escaped_commit: list[BaseException] = []
        try:
            commit_fence = _FenceUnexpectedCommitExit(self, escaped_commit)
            with self._lock, commit_fence:
                if self._fatal is not None:
                    raise ProcessFatalSinkError(
                        "the dispatcher is down; this sink cannot deliver"
                    ) from self._fatal
                if self._stopping or self._closed:
                    # Shutdown and ring publication share this fence. Once a
                    # close owns it, no later publisher may commit a ring
                    # entry (and therefore no new deferred cleanup owner may
                    # appear behind close's final empty check).
                    raise ProcessFatalSinkError(
                        "the broker is stopping; this sink cannot deliver"
                    )
                try:
                    item = item.with_sequence(event.seq)
                    progress_module.observe(
                        event.payload, event.envelope.run_id, self._progress
                    )
                    self._state_epoch += 1
                    self._note_run_event(event)
                except Exception as exc:
                    # FanOut has already allocated this seq. Any failed broker
                    # mutation or sequence binding before the ring creates a
                    # permanent ordering hole: fence every later commit while
                    # this lock is still held.
                    self._latch_fatal_locked(exc)
                    commit_failure = exc
                    try:
                        self._mark_cleanup_free_commit_failure(exc)
                    except BaseException:  # noqa: BLE001
                        pass
                    raise _AbortFencedCommit from None
                except BaseException as exc:  # noqa: BLE001
                    self._latch_fatal_locked(exc)
                    fatal_control_failure = exc
                    try:
                        self._mark_cleanup_free_commit_failure(exc)
                    except BaseException:  # noqa: BLE001
                        pass
                    raise _AbortFencedCommit from None
                try:
                    if event.replayable:
                        # The ring is the only replay source for an active Run,
                        # so it is updated under the same lock that decides a
                        # connecting client's high-water mark: registration and
                        # publication can never interleave into a duplicate or
                        # a hole.
                        #
                        # The checkpoint is only attached once the ring has
                        # accepted the event. An event too large for either
                        # budget is still delivered live -- the client did see
                        # it -- but it carries no ``id:``, because a cursor
                        # naming a fact the ring cannot reproduce would be a
                        # checkpoint this process cannot honour.
                        checkpointed = item.with_checkpoint(
                            event.seq, self._instance
                        )
                        ring_result = self._ring.observe_published(
                            event.seq,
                            checkpointed,
                            defer_retired_release=True,
                        )
                        if ring_result.accepted:
                            item = checkpointed
                    else:
                        # A transient consumes its seq and is delivered live
                        # only: O(1) metadata on the ring, no entry, no
                        # checkpoint.
                        ring_result = self._ring.observe_published(
                            event.seq, None, defer_retired_release=True
                        )
                except Exception as exc:
                    # The ring failing is not one Run's problem (#23 §9). The
                    # event is not offered to an ingress whose dispatcher is
                    # about to be declared dead; the fatal state is published
                    # below, once this critical section has been left.
                    self._latch_fatal_locked(exc)
                    self._ring_cleanup_failure = exc
                    self._ring_cleanup_ready = False
                    commit_failure = exc
                except BaseException as exc:  # noqa: BLE001
                    # A process-control exit after FanOut allocated this seq is
                    # the same recovery-source loss. Keep the exact exception,
                    # fence every later publisher before releasing this lock,
                    # and persist any retirement owner the ring retained.
                    self._latch_fatal_locked(exc)
                    self._ring_cleanup_failure = exc
                    self._ring_cleanup_ready = False
                    fatal_control_failure = exc
                else:
                    # The ring has committed, so its deferred retirement owner
                    # moves into the broker ledger before ingress can run. Any
                    # later ingress failure therefore needs only publish its
                    # liveness fence; it never performs ownership transfer from
                    # inside an exception handler.
                    try:
                        returned_cleanup = (
                            self._retain_returned_commit_cleanup(
                                ring_result.release_retired
                            )
                        )
                        ring_result.acknowledge_retired_transfer()

                        def release_retired(
                            debt: _CommitCleanupDebt = returned_cleanup,
                        ) -> None:
                            self._release_commit_cleanup(
                                debt, make_ready=True
                            )

                        returned_post_commit = PostCommit(
                            release_retired,
                            failure_scope="process_fatal",
                        )
                    except BaseException as exc:  # noqa: BLE001
                        self._latch_fatal_locked(exc)
                        # Until the broker ledger handoff and ring
                        # acknowledgement both finish, the ring's own deferred
                        # backup is the authoritative owner. Record that one
                        # recoverable capability before consulting any helper
                        # that can itself be interrupted.
                        self._ring_cleanup_failure = exc
                        self._ring_cleanup_ready = False
                        if returned_cleanup is not None:
                            returned_cleanup.failure = exc
                        if isinstance(exc, Exception):
                            commit_failure = exc
                        else:
                            fatal_control_failure = exc
                    if commit_failure is None and fatal_control_failure is None:
                        try:
                            offered = self._ingress.offer(
                                _PublishedEvent(
                                    event.seq,
                                    item,
                                    self._ingress_dropped,
                                )
                            )
                        except Exception as exc:
                            # Fatal state becomes visible before this broker
                            # lock admits a connection or another publication.
                            self._latch_fatal_locked(exc)
                            returned_cleanup.failure = exc
                            commit_failure = exc
                            offered = True
                        except BaseException as exc:  # noqa: BLE001
                            # The immutable ingress store has an unknowable
                            # before/after effect. Move old streams behind a
                            # committed generation before any fallible cleanup
                            # settlement can run.
                            returned_cleanup.failure = exc
                            try:
                                self._disconnect_generation += 1
                            except BaseException:  # noqa: BLE001
                                # The ingress exit is the first control fact.
                                # If even the reconnect fence cannot publish,
                                # fail the broker closed under this same lock
                                # without replacing that exact object.
                                self._latch_fatal_locked(exc)
                                fatal_control_failure = exc
                            else:
                                control_failure = exc
                            offered = True
                        if not offered:
                            if item.replayable or item.must_deliver:
                                try:
                                    self._disconnect_generation += 1
                                    kick = self._arm_kick_locked()
                                except Exception as exc:
                                    self._latch_fatal_locked(exc)
                                    returned_cleanup.failure = exc
                                    commit_failure = exc
                                except BaseException as exc:  # noqa: BLE001
                                    self._latch_fatal_locked(exc)
                                    returned_cleanup.failure = exc
                                    fatal_control_failure = exc
                            else:
                                self._ingress_dropped += 1
        except _AbortFencedCommit:
            pass
        except BaseException as exc:  # noqa: BLE001
            # FanOut has already assigned this event's seq. Even an exit while
            # constructing the broker's inner fence must therefore publish a
            # process fence before the exact first object keeps unwinding.
            with self._lock:
                self._latch_fatal_locked(exc)
                if self._ring.has_pending_cleanup():
                    self._ring_cleanup_failure = exc
                    self._ring_cleanup_ready = True
            raise
        if escaped_commit:
            escaped = escaped_commit[0]
            if isinstance(escaped, Exception):
                commit_failure = escaped
            else:
                fatal_control_failure = escaped
        if commit_failure is not None:
            # The fatal handler is deliberately NOT called from here:
            # commit runs inside the FanOut's publish critical section, and
            # a handler that publishes its own notice would re-enter that
            # lock and deadlock. Publishing the fatal state is enough --
            # the typed refusal below is what makes the FanOut move this
            # sink to its process-wide disabled set and say
            # ``sink_disabled`` exactly once, to every healthy sink.
            fatal_wake_failure: BaseException | None = None
            try:
                self._publish_fatal(commit_failure)
            except BaseException as exc:  # noqa: BLE001
                # Fatal state and its retryable wake-up debt were stored before
                # the kick. Keep the ingress failure authoritative; a later
                # control exception on this notification edge must not hide it
                # or prevent FanOut from matching the retained cleanup owner.
                fatal_wake_failure = exc
            failure = ProcessFatalSinkError(
                "the broker publication machinery is down; this sink cannot deliver"
            )
            if fatal_wake_failure is not None:
                failure.add_note(
                    "fatal dispatcher wake-up was interrupted by "
                    f"{type(fatal_wake_failure).__name__}"
                )
            raise failure from commit_failure
        if fatal_control_failure is not None:
            try:
                self._post_fatal_kick()
            except BaseException:  # noqa: BLE001
                pass
            raise fatal_control_failure
        if control_failure is not None:
            raise control_failure
        if kick:
            try:
                self._ingress.put_kick()
            except BaseException:  # noqa: BLE001
                # The generation is already durable. Repeating an ambiguous
                # after-effect kick during settlement would consume another
                # control exit for the same publication; the dispatcher also
                # rechecks committed generation debt periodically.
                if returned_cleanup is not None:
                    try:
                        with self._commit_cleanup_lock:
                            if (
                                self._pending_commit_cleanup.get(
                                    returned_cleanup.token
                                )
                                is returned_cleanup
                            ):
                                returned_cleanup.wake_on_settlement = False
                    except BaseException:  # noqa: BLE001
                        pass
                raise
        # The action itself still runs only after FanOut's unique publication
        # lock. Construction happened under the broker fence so an interrupted
        # return can be paired with the exact pre-registered handoff token.
        return returned_post_commit

    def _release_ring_cleanup(self, *, make_ready: bool = False) -> None:
        """Claim ring-owned retirement outside all shared locks."""
        claim = object()
        try:
            with self._lock:
                if make_ready:
                    self._ring_cleanup_ready = True
                if not self._ring.has_pending_cleanup():
                    self._ring_cleanup_failure = None
                    self._ring_cleanup_ready = False
                    return
                if (
                    not self._ring_cleanup_ready
                    or self._ring_cleanup_claim is not None
                ):
                    return
                self._ring_cleanup_claim = claim
                self._ring_cleanup_in_progress = True
            cleanup = self._ring.poisoned_cleanup()
            if cleanup is not None:
                cleanup()
            self._cleanup_callback_returned("ring")
            # Successful callback return and owner retirement belong to the
            # same protected try. An asynchronous exit between them is a
            # retry, not an immortal ``in_progress`` bit.
            with self._lock:
                if self._ring_cleanup_claim is claim:
                    self._ring_cleanup_in_progress = False
                    self._ring_cleanup_ready = False
                    self._ring_cleanup_claim = None
                    if not self._ring.has_pending_cleanup():
                        self._ring_cleanup_failure = None
        except BaseException:  # noqa: BLE001
            with self._lock:
                if self._ring_cleanup_claim is claim:
                    self._ring_cleanup_claim = None
                    self._ring_cleanup_in_progress = False
                    self._ring_cleanup_ready = True
            raise

    def _retain_returned_commit_cleanup(
        self, release_retired: Callable[[], None]
    ) -> _CommitCleanupDebt:
        """Register a normal PostCommit owner before releasing broker lock."""
        owner = threading.get_ident()
        debt = _CommitCleanupDebt(
            token=object(),
            release_retired=release_retired,
            handoff_owner=owner,
        )
        with self._commit_cleanup_lock:
            self._pending_commit_cleanup[debt.token] = debt
            self._returned_commit_cleanup.setdefault(owner, {})[
                debt.token
            ] = debt
        return debt

    def _remove_returned_cleanup_locked(
        self, debt: _CommitCleanupDebt
    ) -> None:
        """Remove one exact normal-return handoff. Cleanup lock is held."""
        owner = debt.handoff_owner
        if owner is None:
            return
        bucket = self._returned_commit_cleanup.get(owner)
        if bucket is not None and bucket.get(debt.token) is debt:
            del bucket[debt.token]
            if not bucket:
                del self._returned_commit_cleanup[owner]
        debt.handoff_owner = None

    def _mark_cleanup_free_commit_failure(
        self, failure: BaseException
    ) -> None:
        """Prevent settlement from claiming an older return for this failure."""
        key = (id(failure), threading.get_ident())
        with self._commit_cleanup_lock:
            self._cleanup_free_commit_failures[key] = failure

    def _mark_commit_cleanup_ready_locked(
        self, debt: _CommitCleanupDebt
    ) -> None:
        """Expose one authoritative pending owner to fallback drainers."""
        if debt.completed or debt.ready:
            return
        debt.ready = True

    def settle_interrupted_commit(
        self, failure: BaseException
    ) -> PostCommit | None:
        """Return every owner acquired by the interrupted commit.

        Readiness is published only when the returned action atomically claims
        the owner.  Therefore a concurrent zero-timeout close observes an
        unreturned debt and answers False; it can never steal the callback and
        block behind user-controlled FanOut settlement.
        """
        owner = threading.get_ident()
        candidates = (failure, failure.__cause__)
        debt: _CommitCleanupDebt | None = None
        ring_cleanup = False
        cleanup_free = False
        with self._lock:
            ring_failure = self._ring_cleanup_failure
            if ring_failure is not None:
                ring_cleanup = any(
                    candidate is ring_failure for candidate in candidates
                )
        with self._commit_cleanup_lock:
            for candidate in candidates:
                if candidate is None:
                    continue
                key = (id(candidate), owner)
                recorded = self._cleanup_free_commit_failures.get(key)
                if recorded is candidate:
                    del self._cleanup_free_commit_failures[key]
                    cleanup_free = True
                    break
                # ``pending`` is the owner, all other maps are fallible
                # accelerators. This scan runs only on the exceptional
                # settlement path, never in commit.
                for possible in reversed(
                    self._pending_commit_cleanup.values()
                ):
                    if (
                        possible.handoff_owner == owner
                        and possible.failure is candidate
                    ):
                        debt = possible
                        break
                if debt is not None:
                    break
            if debt is None and not cleanup_free:
                # The sink may have returned a PostCommit before a control
                # exit landed in the caller's assignment bytecode. No failure
                # identity could be recorded inside commit, so only the newest
                # unclaimed return owned by this exact publisher may settle it.
                returned = self._returned_commit_cleanup.get(owner)
                if returned:
                    possible = next(reversed(returned.values()))
                    if (
                        self._pending_commit_cleanup.get(possible.token)
                        is possible
                    ):
                        debt = possible
                if debt is None:
                    for possible in reversed(
                        self._pending_commit_cleanup.values()
                    ):
                        if possible.handoff_owner == owner:
                            debt = possible
                            break
                if debt is not None:
                    debt.failure = failure
            if debt is None and not ring_cleanup:
                return None

        def settle() -> None:
            # Wake first so a large retired prefix cannot delay reconnect.
            # If the notify itself is interrupted, ``finally`` still attempts
            # this exact captured debt and the periodic generation sweep owns
            # liveness without an external retry.
            first_error: BaseException | None = None
            try:
                should_kick = False
                if debt is not None and debt.wake_on_settlement:
                    with self._lock:
                        should_kick = self._arm_kick_locked()
                if should_kick:
                    self._ingress.put_kick()
            except BaseException as exc:  # noqa: BLE001
                first_error = exc
            if ring_cleanup:
                try:
                    self._release_ring_cleanup(make_ready=True)
                except BaseException as exc:  # noqa: BLE001
                    if first_error is None:
                        first_error = exc
            if debt is not None:
                try:
                    self._release_commit_cleanup(debt, make_ready=True)
                except BaseException as exc:  # noqa: BLE001
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

        return PostCommit(settle, failure_scope="process_fatal")

    def _release_commit_cleanup(
        self, debt: _CommitCleanupDebt, *, make_ready: bool = False
    ) -> bool:
        """Claim and release one ready debt without holding a shared lock."""
        claim = object()
        try:
            with self._commit_cleanup_lock:
                if make_ready and not debt.completed:
                    self._remove_returned_cleanup_locked(debt)
                    self._mark_commit_cleanup_ready_locked(debt)
                if (
                    self._pending_commit_cleanup.get(debt.token) is not debt
                    or debt.completed
                    or debt.claim is not None
                    or not debt.ready
                ):
                    return False
                debt.claim = claim
                debt.in_progress = True
            debt.release_retired()
            self._cleanup_callback_returned("commit")
            # Keep successful return and all bookkeeping inside the protected
            # try. If process control lands between either bytecode, pending
            # remains authoritative and the non-blocking claim lock is released
            # by ``finally`` so close/dispatcher can retry idempotently.
            with self._commit_cleanup_lock:
                debt.in_progress = False
                debt.completed = True
                self._remove_returned_cleanup_locked(debt)
                debt.ready = False
                if debt.claim is claim:
                    debt.claim = None
                # The return index is repaired before the authoritative
                # owner disappears. A retry can therefore finish any
                # interrupted bookkeeping without leaking a ghost.
                if self._pending_commit_cleanup.get(debt.token) is debt:
                    del self._pending_commit_cleanup[debt.token]
            return True
        except BaseException as exc:  # noqa: BLE001
            with self._commit_cleanup_lock:
                if (
                    self._pending_commit_cleanup.get(debt.token) is debt
                    and (debt.claim is claim or debt.claim is None)
                ):
                    if debt.claim is claim:
                        debt.claim = None
                    debt.in_progress = False
                    debt.completed = False
                    debt.ready = True
            # Retain/requeue first. Fatal publication may itself be interrupted,
            # but can no longer make the cleanup owner disappear or replace
            # the cleanup exception that owns this unwind.
            try:
                self._publish_fatal(exc)
            except BaseException:  # noqa: BLE001
                pass
            raise

    @staticmethod
    def _cleanup_callback_returned(owner: Literal["ring", "commit"]) -> None:
        """Named seam inside the callback-to-finalization recovery domain."""
        del owner

    def _drain_interrupted_cleanup(self) -> None:
        """Drain ready debt nodes; callbacks run outside every shared lock."""
        while True:
            with self._commit_cleanup_lock:
                debt = next(
                    (
                        candidate
                        for candidate in self._pending_commit_cleanup.values()
                        if candidate.ready
                        and candidate.claim is None
                        and not candidate.completed
                    ),
                    None,
                )
                if debt is None:
                    return
            if not self._release_commit_cleanup(debt):
                return

    def _promote_shutdown_cleanup(self) -> None:
        """Make every unclaimed retirement owner available to shutdown."""
        self._release_ring_cleanup(make_ready=True)
        with self._commit_cleanup_lock:
            for debt in self._pending_commit_cleanup.values():
                if debt.completed or debt.claim is not None:
                    continue
                self._remove_returned_cleanup_locked(debt)
                self._mark_commit_cleanup_ready_locked(debt)
        self._drain_interrupted_cleanup()

    def _commit_cleanup_quiescent_locked(self) -> bool:
        """Whether no commit cleanup owner remains. Cleanup lock is held."""
        return not self._pending_commit_cleanup

    def _arm_kick_locked(self) -> bool:
        """Decide whether this overflow must wake the dispatcher. Call holds
        the lock.

        Coalescing is performed atomically by the ingress that owns the queue
        effect.  This publication lock decides only whether a live dispatcher
        is still eligible to be woken; it must not publish a pending bit before
        the lock-free handoff has actually happened.
        """
        return not self._stopping and self._fatal is None

    def flush(self, run_id: str) -> BestEffortFlushResult:
        """SSE does not participate in the durability barrier.

        It does not block and it does not wait for the dispatcher -- a sink
        the barrier does not wait on has no business delaying a Run.
        """
        del run_id
        return BestEffortFlushResult(outcome="best_effort")

    # -- lifecycle ---------------------------------------------------------

    def _resolve_dispatcher_start_locked(self) -> threading.Thread | None:
        """Retry the stable probe for one retained ambiguous start effect."""
        dispatcher = self._dispatcher
        if dispatcher is None:
            self._dispatcher_started = False
            return None
        if self._dispatcher_started is None:
            self._dispatcher_started = thread_start_effect_happened(
                dispatcher
            )
        if self._dispatcher_started is False:
            self._dispatcher = None
            return None
        return dispatcher

    def start(self) -> None:
        """Start the single dispatcher thread."""
        with self._lock:
            self._resolve_dispatcher_start_locked()
            if self._dispatcher is not None or self._closed:
                return
            thread = None
            try:
                # The seam returns an unstarted thread. Losing that return is
                # harmless; once the identity is published, this protected
                # region either starts it or proves that no start occurred.
                thread = self._spawn(self._dispatch_loop)
                self._dispatcher_started = None
                self._dispatcher = thread
                thread.start()
                self._dispatcher_started = True
            except BaseException as failure:
                # The concrete Thread identity is durable before start.  A
                # legal join proves the start effect happened, even if the
                # target has already exited; a RuntimeError proves it did not.
                try:
                    self._resolve_dispatcher_start_locked()
                except BaseException:
                    # Keep the unresolved concrete identity for a later
                    # start() or close() probe without replacing the first
                    # start failure.
                    raise failure
                raise failure

    def close(self, timeout: float = _DRAIN_TIMEOUT_S) -> bool:
        """Drain what is queued, stop every thread, release every socket.

        Resumable and idempotent: a peer that stopped reading cannot hold
        ``close()`` open, a descriptor is never left to outlive it, and
        False means "still draining -- ask again". The answer is a fact
        about the threads: True only once the dispatcher and every writer
        have really exited, because a caller that takes True for an answer
        and tears down what the broker is still draining -- the database
        the frames were built from, the process lock the stream lives
        under -- is the exact failure this return value exists to prevent.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            # The lock order is the same as commit's registration path:
            # broker publication fence, then cleanup ownership. A legacy or
            # interrupted close may have published ``closed`` while a debt
            # was still settling, so even the idempotent fast path proves
            # ownership is empty before returning True.
            with self._commit_cleanup_lock:
                cleanup_quiescent = (
                    not self._ring.has_pending_cleanup()
                    and self._ring_cleanup_claim is None
                    and self._commit_cleanup_quiescent_locked()
                )
            with self._registry_lock:
                handoffs_quiescent = (
                    not self._active_stream_handoffs and not self._startup_closes
                )
            if self._closed and cleanup_quiescent and handoffs_quiescent:
                return True
            if self._closed:
                self._closed = False
            self._stopping = True
            dispatcher = self._resolve_dispatcher_start_locked()
            stop_sent = self._stop_sent
            queues_stopped = self._queues_stopped
        # A publisher may have transferred deferred ring cleanup immediately
        # before shutdown won the lifecycle lock. Claim it here as a backup
        # to the post-commit callback and periodic dispatcher.
        cleanup_interrupted = False
        try:
            self._promote_shutdown_cleanup()
        except BaseException:  # noqa: BLE001
            # The identity-claimed owner was restored to the authoritative
            # ledger before its callback exception escaped. ``False`` is the
            # close contract for unfinished work; the next close retries it.
            cleanup_interrupted = True
        # The registration ledger stores the acquisition, not merely a count.
        # A proof-producing frame may have vanished after registration, so
        # shutdown itself is the final owner that can resume its abort.
        with self._registry_lock:
            for token, acquisition in self._registrations.items():
                self._startup_closes.setdefault(token, acquisition)
            acquisitions = tuple(self._startup_closes.items())
        acquisition_cleanup_interrupted = False
        for token, acquisition in acquisitions:
            try:
                completed = acquisition.abort_close.complete(
                    lambda acquisition=acquisition: (
                        self._abort_untransferred_acquisition(acquisition)
                    ),
                    max(0.0, deadline - time.monotonic()),
                )
            except BaseException:  # noqa: BLE001 - retained in startup closes
                acquisition_cleanup_interrupted = True
            else:
                acquisition_cleanup_interrupted |= not completed
                if completed:
                    with self._registry_lock:
                        if self._startup_closes.get(token) is acquisition:
                            self._startup_closes.pop(token)
        with self._registry_lock:
            handles = tuple(self._connections)
            active_handoffs = tuple(self._active_stream_handoffs.values())
        if dispatcher is not None and not stop_sent:
            # The stop sentinel is queued behind everything already pending,
            # so the dispatcher drains first and only then returns. Posted
            # once across every attempt: it is the step "asked the
            # dispatcher to stop", which does not become un-done by timing
            # out, and a second sentinel is an item with no reader.
            self._ingress.put_stop()
            with self._lock:
                self._stop_sent = True
        dispatcher_exited = dispatcher is None
        if dispatcher is not None:
            dispatcher_exited = thread_exit_confirmed(
                dispatcher,
                max(0.0, deadline - time.monotonic()),
            )
        if not queues_stopped:
            for handle in handles:
                handle.queue.stop()
            with self._lock:
                self._queues_stopped = True
        for handle in handles:
            thread = handle.thread
            if thread is None or not thread.is_alive():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if not thread.is_alive():
                continue
            # Publish the retained close owner under the same registry lock
            # writer cleanup consults before unregistering. Whichever side
            # wins, the handle cannot disappear between this decision and the
            # background close task becoming reachable from it.
            with self._registry_lock:
                if handle in self._connections:
                    handle.connection_close_owned = True
        connection_close_incomplete = False
        for handle in handles:
            if not handle.connection_close_owned:
                continue
            try:
                closed = handle.connection_close.complete(
                    handle.connection.close,
                    max(0.0, deadline - time.monotonic()),
                )
            except BaseException:  # noqa: BLE001 - the retained step retries
                connection_close_incomplete = True
            else:
                connection_close_incomplete |= not closed
        # A dead writer can still own a queue cleanup that failed in its
        # finally block. Resume that exact writer before deciding the registry
        # is drained; successful socket cleanup alone is not completion.
        for handle in handles:
            thread = handle.thread
            if thread is not None and not thread.is_alive() and handle.writer:
                self._settle_writer_cleanup(handle, handle.writer)
        # A thread may enter its target before ``start()`` returns. If that
        # boundary raises while transfer aborts, the registry identity remains
        # the authoritative owner until the target's finally branch exits.
        # Joining a captured identity is only progress -- the final registry
        # sweep removes identities only after confirmed thread death.
        for handoff_thread in active_handoffs:
            if handoff_thread is threading.current_thread():
                continue
            thread_exit_confirmed(
                handoff_thread, max(0.0, deadline - time.monotonic())
            )
        # The completion report is a fact about the *current* registry, not
        # the attempt's snapshot: a handle registered before this close set
        # ``_stopping`` is in the snapshot, but its writer may have been
        # installed (and exited, and unregistered itself) since. Everything
        # that can still start a writer is checked as it is now -- registry
        # empty, no registration in flight, dispatcher gone -- because a
        # True handed out over any one of them releases the database and
        # the process lock under a stream that is still starting or still
        # writing. A registered handle whose thread is still ``None`` is a
        # registration the writer has not reached; it counts as undrained
        # rather than as a writer that already left.
        with self._registry_lock:
            self._reap_stream_handoffs_locked()
            current = tuple(self._connections)
            registrations = len(self._registrations)
            startup_closes = len(self._startup_closes)
            active_handoffs = len(self._active_stream_handoffs)
        drained = dispatcher_exited
        drained = drained and registrations == 0
        drained = drained and startup_closes == 0
        drained = drained and active_handoffs == 0
        drained = drained and not current
        drained = drained and all(
            handle.thread is not None and not handle.thread.is_alive()
            for handle in current
        )
        drained = drained and not cleanup_interrupted
        drained = drained and not acquisition_cleanup_interrupted
        drained = drained and not connection_close_incomplete
        if drained:
            # Settlement can make a retained owner ready while shutdown is
            # joining threads. Give close itself one last chance to repay it;
            # callbacks remain outside every shared lock.
            self._release_ring_cleanup()
            self._drain_interrupted_cleanup()
        if drained:
            # Only confirmed thread exit *and* an empty ownership ledger may
            # publish "closed". Holding the broker fence while inspecting
            # the cleanup ledger makes this atomic against commit, whose
            # stopping check and any debt registration use the same lock
            # order. The checks are O(1); cleanup itself already ran outside.
            with self._lock:
                with self._commit_cleanup_lock:
                    cleanup_quiescent = (
                        not self._ring.has_pending_cleanup()
                        and self._ring_cleanup_claim is None
                        and self._commit_cleanup_quiescent_locked()
                    )
                    if cleanup_quiescent:
                        self._closed = True
            drained = drained and cleanup_quiescent
        return drained

    # -- connections -------------------------------------------------------

    def connect(
        self,
        *,
        connection: SSEConnection,
        cursor: CursorText | None = None,
        session_id: str | None = None,
        budget: frames.FrameBudget | None = None,
        admission: _SessionAdmissionProof | None = None,
    ) -> ConnectionHandle:
        """Convenience seam that admits and immediately starts one stream."""
        rollback = ResumableRollback()
        proof: StreamAdmissionProof | None = None
        try:
            proof = self.prepare_stream(
                connection=connection,
                cursor=cursor,
                session_id=session_id,
                budget=budget,
                session_admission=admission,
                _rollback=rollback,
            )
            handle = self.start_stream(proof)
            rollback.transfer(proof)
            return handle
        except StreamAdmissionRejected as exc:
            if not rollback.retry():
                rollback.raise_incomplete(exc)
            if proof is None:
                connection.close()
            return exc.handle
        except BaseException as exc:
            if not rollback.retry():
                rollback.raise_incomplete(exc)
            if proof is None:
                connection.close()
            raise

    def prepare_stream(
        self,
        *,
        connection: SSEConnection,
        cursor: CursorText | None = None,
        session_id: str | None = None,
        budget: frames.FrameBudget | None = None,
        session_admission: _SessionAdmissionProof | None = None,
        _rollback: ResumableRollback | None = None,
    ) -> StreamAdmissionProof:
        """Atomically reserve every stream resource before HTTP 200."""
        acquisition = _ConnectStartup(connection=connection)
        try:
            admission = (
                self.preflight_session(session_id)
                if session_admission is None
                else session_admission
            )
            if admission.session_id != session_id:
                raise ValueError("stream admission proof does not match session_id")
            proof = self._prepare_before_writer(
                connection=connection,
                cursor=cursor,
                session_id=session_id,
                budget=budget,
                acquisition=acquisition,
                admission=admission,
            )
            if _rollback is not None:
                _rollback.own(
                    proof,
                    lambda: self._abort_prepared_stream(proof),
                )
            return proof
        except BaseException:
            try:
                self._abort_stream_acquisition(
                    acquisition, close_connection=False
                )
            except Exception:
                pass
            raise

    def _abort_prepared_stream(self, proof: StreamAdmissionProof) -> bool:
        """Undo only while a writer has not accepted the prepared resources."""
        self._abort_untransferred_acquisition(proof._acquisition)
        return True

    def _abort_untransferred_acquisition(self, acquisition: _ConnectStartup) -> None:
        """Settle an abandoned admission, or leave its live writer in charge."""
        with acquisition.ownership_lock:
            if not acquisition.transferred:
                self._abort_stream_acquisition(acquisition)

    def start_stream(self, proof: StreamAdmissionProof) -> ConnectionHandle:
        """Transfer one prepared admission to its writer exactly once."""
        if proof._broker is not self:
            raise ValueError("stream admission proof belongs to another broker")
        acquisition = proof._acquisition
        with acquisition.ownership_lock:
            if acquisition.aborted:
                raise RuntimeError("stream admission proof was revoked")
            if acquisition.transferred:
                raise RuntimeError(
                    "stream admission ownership already transferred"
                )
            handle = proof._handle
            try:
                handle.thread = self._spawn(
                    lambda: self._run_stream_handoff(
                        acquisition, handle, proof._writer
                    )
                )
                handle.thread.start()
                self._finish_stream_registration(acquisition)
                acquisition.transferred = True
                return handle
            except BaseException as failure:
                # The target cannot observe this reversal while this ownership
                # lock is held. Cleanup is idempotent, so a one-shot exit from
                # cleanup itself gets one bounded settlement retry without
                # replacing the publication's first control exception.
                acquisition.transferred = False
                try:
                    self._abort_stream_acquisition(acquisition)
                except BaseException:  # noqa: BLE001
                    try:
                        self._abort_stream_acquisition(acquisition)
                    except BaseException:  # noqa: BLE001
                        pass
                raise failure

    def _abort_stream_acquisition(
        self,
        acquisition: _ConnectStartup,
        *,
        close_connection: bool = True,
    ) -> bool:
        """Revoke a startup that never handed ownership to a writer."""
        acquisition.ownership_lock.acquire()
        try:
            if acquisition.transferred:
                raise RuntimeError("stream admission ownership already transferred")
            if close_connection:
                acquisition.close_connection_on_abort = True
            if acquisition.abort_completed:
                self._retire_aborted_acquisition(acquisition)
                return True
            with acquisition.handoff_state_lock:
                acquisition.aborted = True
            handle = acquisition.handle
            handoff_active = False
            if handle is not None and acquisition.registered_once:
                # Registration identity answers only whether the startup count
                # is still owed. A dispatcher may already hold a stale copy of
                # this handle after that count was settled, so close the queue
                # before removing the handle and make every later offer refuse.
                handle.queue.request_close()
                with self._registry_lock:
                    handoff_active = (
                        acquisition.handoff_token
                        in self._active_stream_handoffs
                    )
            try:
                if handle is not None:
                    if acquisition.startup_reservation is not None:
                        acquisition.startup_reservation.cancel()
                        acquisition.startup_reservation = None
                    while True:
                        try:
                            handle.queue.take(timeout=0)
                        except queue.Empty:
                            break
                    handle.queue.cancel_startup()
                if acquisition.close_connection_on_abort:
                    acquisition.connection.close()
            finally:
                if handle is not None and not handoff_active:
                    handle.finished.set()
            acquisition.abort_completed = True
            self._retire_aborted_acquisition(acquisition)
            return True
        finally:
            acquisition.ownership_lock.release()

    def _retire_aborted_acquisition(
        self, acquisition: _ConnectStartup
    ) -> None:
        """Remove only an acquisition whose owned abort work is complete."""
        if not acquisition.abort_completed:
            return
        handle = acquisition.handle
        with self._registry_lock:
            if handle is not None and handle.thread is not None:
                if (
                    handle.thread.is_alive()
                    or thread_start_effect_happened(handle.thread)
                ):
                    # start() may raise after creating the thread but before
                    # its target can register. Preserve that identity before
                    # retiring either of the startup's existing close owners.
                    self._active_stream_handoffs[
                        acquisition.handoff_token
                    ] = handle.thread
            if handle is not None and handle in self._connections:
                self._connections.remove(handle)
            self._registrations.pop(acquisition.registration_token, None)
            acquisition.registration_open = False

    def _run_stream_handoff(
        self,
        acquisition: _ConnectStartup,
        handle: ConnectionHandle,
        writer: ConnectionWriter,
    ) -> None:
        """Consume startup resources only after transfer is committed."""
        # The factory's published thread is the lifetime the broker owns;
        # its target may be driven by a thread adapter in deterministic tests.
        current = handle.thread
        assert current is not None
        writer_ran = False
        try:
            with acquisition.handoff_state_lock:
                if acquisition.aborted:
                    return
                with self._registry_lock:
                    self._active_stream_handoffs[
                        acquisition.handoff_token
                    ] = current
            with acquisition.ownership_lock:
                if acquisition.aborted or not acquisition.transferred:
                    return
                acquisition.startup_reservation = None
            writer_ran = True
            self._run_writer(handle, writer)
        finally:
            if not writer_ran:
                self._settle_writer_cleanup(handle, writer)
            with self._registry_lock:
                owner = self._active_stream_handoffs.get(
                    acquisition.handoff_token
                )
                if owner is current:
                    if writer.cleanup_complete:
                        handle.finished.set()
                    self._reap_stream_handoffs_locked()

    def _reap_stream_handoffs_locked(self) -> None:
        """Retire dead identities, never the still-running caller's identity."""
        for token, thread in tuple(self._active_stream_handoffs.items()):
            # Thread death is monotonic. The registry lock preserves the
            # exact identity until this proof and its removal both finish.
            if not thread.is_alive() and thread_exit_confirmed(thread, 0):
                self._active_stream_handoffs.pop(token, None)

    def _finish_stream_registration(
        self, acquisition: _ConnectStartup
    ) -> None:
        """Drop one in-flight registration by stable identity."""
        with self._registry_lock:
            self._registrations.pop(acquisition.registration_token, None)
            acquisition.registration_open = False

    def abort_stream(self, proof: StreamAdmissionProof) -> None:
        """Revoke a prepared stream whose HTTP/writer handoff failed."""
        if proof._broker is not self:
            raise ValueError("stream admission proof belongs to another broker")
        self._abort_stream_acquisition(proof._acquisition)

    @staticmethod
    def _capture_ring_read(
        read: Callable[[], _RingRead],
    ) -> tuple[_RingRead | None, Exception | None]:
        """Run one ring read and retain its fault for lock-exit escalation."""
        try:
            return read(), None
        except Exception as exc:
            return None, exc

    def _opening_stream_snapshot(
        self, cursor: CursorText | None
    ) -> tuple[
        CursorVerdict,
        int | None,
        int | None,
        int,
        CurrentRunState | None,
    ]:
        """Capture every replay-dependent opening fact before HTTP 200."""
        verdict = classify_cursor(
            cursor,
            self._ring,
            process_instance_id=self._instance,
            include_entries=False,
        )
        replay_through = self._ring.latest_complete_seq()
        oldest_seq = self._ring.oldest_seq() if verdict.kind == "gap" else None
        published_through = self._ring.published_high_water_seq()
        current_run_state = (
            self._current_run_state_locked() if verdict.kind == "gap" else None
        )
        return (
            verdict,
            replay_through,
            oldest_seq,
            published_through,
            current_run_state,
        )

    def _prepare_before_writer(
        self,
        *,
        connection: SSEConnection,
        cursor: CursorText | None,
        session_id: str | None,
        budget: frames.FrameBudget | None,
        acquisition: _ConnectStartup,
        admission: _SessionAdmissionProof,
    ) -> StreamAdmissionProof:
        """Register one connection and build its opening stream.

        The snapshot, the high-water mark and the registration all happen in
        one critical section. Split them and a client that connects while
        events are being emitted gets either a duplicate or a hole, with no
        way to tell which.

        The one critical section is the *registration*; the encoding of the
        opening stream happens outside it, the way ADR-0015 splits prepare
        from commit. The critical section that captures what the stream
        will say leaves behind an epoch; the registration critical section
        re-checks it, and a changed epoch -- an event or a state patch that
        moved the world while the frames were being built -- throws the
        capture away and starts again, up to the same fixed capture budget
        as state-patch publication. Exhaustion explicitly refuses this one
        socket so the browser reconnects instead of receiving a stale patch
        or waiting forever. What the client finally receives names the state
        that was authoritative when it registered, and no commit ever waited
        for the encoding of a startup frame.

        The broker still accepting connections is confirmed in the same
        critical sections, *before* anything is registered: a connect that
        arrives behind a closing broker is refused there, and the refusal's
        only actions -- closing the socket and raising ``finished`` -- are
        done outside the lock, where a close is allowed to touch IO.

        The opening sequence is the decided order, always, in full:
        ``retry`` -> **re-seed** -> optional ``replay_gap`` -> ``state_patch``
        / exact catch-up -> live frames. The re-seed is unconditional; why is
        argued once, at
        :data:`~agent_alfred.gateway.web.frames.STARTUP_CHECKPOINT_SEQ`.

        The fixed prefix is handed to the writer, while exact replay is a
        frozen high-water cursor. The writer fetches one continuous physical
        slice at a time under both connection budgets and releases it before
        fetching the next; neither the handle nor the writer pins the complete
        tail, and only a slice containing the event's final frame carries its
        checkpoint.
        """
        handle = ConnectionHandle(
            queue=ConnectionQueue(
                budget=self._connection_budget if budget is None else budget,
            ),
            connection=connection,
            session_id=session_id,
            session_valid=admission.session_valid,
        )
        acquisition.handle = handle
        if type(admission.session_valid) is not bool:
            raise TypeError("stream admission proof must carry a boolean verdict")
        else:
            session_valid = admission.session_valid
            refusal_reason: str | None = None
            replay_failure: BaseException | None = None
            for _ in range(_MAX_PATCH_CAPTURES):
                boundary = (
                    nullcontext()
                    if self._projection_boundary is None
                    else self._projection_boundary
                )
                with boundary, self._lock:
                    if self._stopping or self._closed:
                        # A broker that is closing accepts nothing new: a writer
                        # started now would never be told to stop by anyone but
                        # us, and a caller that already holds a True close is
                        # tearing down what this stream would read from.
                        refusal_reason = "broker_closing"
                        break
                    epoch = self._state_epoch
                    ring_snapshot, replay_failure = self._capture_ring_read(
                        lambda: self._opening_stream_snapshot(cursor)
                    )
                    if replay_failure is not None:
                        break
                    assert ring_snapshot is not None
                    (
                        verdict,
                        replay_through,
                        oldest_seq,
                        published_through,
                        current_run_state,
                    ) = ring_snapshot
                    # The gap notice's facts, captured where they are coherent:
                    # the ring and the run state cannot move inside this critical
                    # section, so the notice is built from frozen values outside.
                    gap_args: (
                        tuple[str, int | None, int | None, int, CurrentRunState]
                        | None
                    ) = None
                    if verdict.kind == "gap":
                        assert current_run_state is not None
                        gap_args = (
                            verdict.reason or "malformed",
                            verdict.requested_seq,
                            oldest_seq,
                            published_through,
                            current_run_state,
                        )
                    latest = self._latest
                    # Same binding as publish_state_patch: the step summary is
                    # shown only while it belongs to the snapshot's active Run.
                    active = latest.active_run
                    self._progress.note_active_run(
                        None if active is None else active.run_id
                    )
                    step = self._progress.projection(
                        None if active is None else active.current_step
                    )
                # Encoding outside the lock: pure, no IO, may be slow.
                startup: list[PreparedFrames] = [
                    frames.retry_frame(frames.DEFAULT_RETRY_MS)
                ]
                # Unconditional: every verdict carries a boundary, and omitting
                # it on an empty ring is what erases the browser's cursor.
                startup.append(
                    frames.reseed_frame(self._instance, verdict.reseed_seq)
                )
                if gap_args is not None:
                    reason, requested_seq, oldest_seq, high_water, run_state = (
                        gap_args
                    )
                    startup.append(
                        frames.replay_gap_notice(
                            reason=reason,
                            requested_seq=requested_seq,
                            oldest_seq=oldest_seq,
                            high_water_seq=high_water,
                            current_run_state=run_state,
                        )
                    )
                startup.append(_patch_frames(latest, step, session_valid))
                built = tuple(startup)
                boundary = (
                    nullcontext()
                    if self._projection_boundary is None
                    else self._projection_boundary
                )
                with boundary, self._lock:
                    if self._stopping or self._closed:
                        refusal_reason = "broker_closing"
                        break
                    if self._state_epoch != epoch:
                        # An event or a patch moved the world while the frames
                        # were being built: the capture is stale and the decided
                        # invariant -- snapshot, high-water, registration, one
                        # critical section -- is worth another lap, not a lie.
                        continue
                    needs_replay = (
                        verdict.kind == "valid"
                        and verdict.requested_seq is not None
                        and replay_through is not None
                        and verdict.requested_seq < replay_through
                    )
                    guard = FrameCost(0, 0)
                    if needs_replay:
                        guard, replay_failure = self._capture_ring_read(
                            lambda: self._ring.startup_guard_cost(
                                verdict.requested_seq, replay_through
                            )
                        )
                        if replay_failure is not None:
                            # A ring read failing is a process-level sink
                            # failure. Capture it here, but report it only
                            # after leaving the broker lock: the handler may
                            # publish ``sink_disabled`` through the FanOut.
                            break
                        if guard is None:
                            # The frozen interval ceased to be reproducible
                            # before registration. A retry will receive the
                            # ring's explicit gap verdict instead.
                            refusal_reason = "replay_unavailable"
                            break
                # The queue admission may take its own lock. It happens while
                # the handle is still private, never under either Broker lock;
                # the publication epoch below decides whether it can transfer.
                startup_reservation = handle.queue.reserve_startup_prefix(
                    built, guard
                )
                if startup_reservation is None:
                    refusal_reason = "startup_capacity"
                    break
                retry_capture = False
                boundary = (
                    nullcontext()
                    if self._projection_boundary is None
                    else self._projection_boundary
                )
                with boundary, self._lock:
                    if self._stopping or self._closed:
                        refusal_reason = "broker_closing"
                    elif self._state_epoch != epoch:
                        retry_capture = True
                    else:
                        acquisition.startup_reservation = startup_reservation
                        del built
                        handle.verdict = verdict
                        handle.ingress_seen = self._ingress_dropped
                        handle.published_through = published_through
                        # Registered at the current disconnect generation:
                        # everything published so far is either in this opening
                        # stream or behind the published boundary above.
                        handle.generation = self._disconnect_generation
                        handle.registered_monotonic = self._clock.monotonic()
                        with self._registry_lock:
                            self._reap_stream_handoffs_locked()
                            acquisition.registered_once = True
                            self._connections.append(handle)
                            # This fence falls only after the writer exists.
                            self._registrations[
                                acquisition.registration_token
                            ] = acquisition
                            acquisition.registration_open = True
                if refusal_reason is not None or retry_capture:
                    startup_reservation.cancel()
                    if retry_capture:
                        continue
                    break
                break
            else:
                # The world moved during every bounded encoding lap. Registering
                # the final capture would ship a stale absolute replacement, and
                # trying forever would strand the HTTP handler while rebuilding
                # snapshots and replay. Refuse this connection in the same
                # observable shape as a closing broker; a reconnect gets a fresh
                # atomic capture without poisoning the broker for other clients.
                refusal_reason = "capture_exhausted"
            if replay_failure is not None:
                self._enter_fatal(replay_failure)
                raise replay_failure
            if refusal_reason is not None:
                raise StreamAdmissionRejected(refusal_reason, handle)
        writer = ConnectionWriter(
            connection=connection,
            source=handle.queue,
            clock=self._clock,
            heartbeat_s=self._heartbeat_s,
            startup_reservation=acquisition.startup_reservation,
            startup_replay=(
                StartupReplay(
                    source=handle.queue,
                    cursor_seq=verdict.requested_seq,
                    through_seq=replay_through,
                    fetch=lambda progress, through_seq: (
                        self._fetch_startup_replay(
                            handle.queue,
                            progress,
                            through_seq,
                        )
                    ),
                )
                if verdict.kind == "valid"
                and verdict.requested_seq is not None
                and replay_through is not None
                and verdict.requested_seq < replay_through
                else None
            ),
        )
        handle.writer = writer
        return StreamAdmissionProof(
            session_id=session_id,
            session_valid=session_valid,
            _broker=self,
            _acquisition=acquisition,
            _handle=handle,
            _writer=writer,
        )

    def _fetch_startup_replay(
        self,
        source: ConnectionQueue,
        progress: ReplayProgress,
        through_seq: int | None,
    ) -> ReplayBatch:
        """Fetch and account one immutable batch without IO or waiting."""
        ring_failure: Exception | None = None

        def fetch(remaining: frames.FrameBudget) -> ReplayBatch:
            nonlocal ring_failure
            try:
                return self._ring.bounded_entries_after(
                    progress, through_seq, remaining
                )
            except Exception as exc:
                ring_failure = exc
                raise

        try:
            batch = source.build_and_reserve_startup(
                fetch,
                continue_when_closing=progress.event_seq is not None,
            )
            if batch.kind != "batch":
                return batch
            with self._lock:
                current = self._ring.replay_batch_is_current(batch)
            if current:
                return batch
            # Queue accounting has its own lock. Cancel only after leaving
            # the publication lock so neither lock order nor publication
            # latency depends on a connection queue.
            source.cancel_startup(batch.cost)
            return ReplayBatch(kind="unavailable")
        except Exception:
            if ring_failure is not None:
                # The callback may publish a domain notice through FanOut,
                # so it must run after the broker lock has been released.
                self._enter_fatal(ring_failure)
            raise

    @property
    def registrations_in_flight(self) -> int:
        """Connects that have registered a handle but not installed a writer.

        The fence a close waits on. While this is non-zero, ``close`` cannot
        truthfully answer True: one of these connects may still start a
        writer.
        """
        with self._registry_lock:
            return len(self._registrations)

    def _run_writer(
        self, handle: ConnectionHandle, writer: ConnectionWriter
    ) -> None:
        """The writer's whole life: write, then close, then say so.

        Unregistering here rather than leaving it to the caller is what makes
        "this connection is gone" a fact rather than an intention: the socket
        is closed by the time the handle leaves the registry, so no later
        fan-out can offer to a connection that is already dead.
        """
        try:
            writer.run()
        finally:
            self._settle_writer_cleanup(handle, writer)

    def _settle_writer_cleanup(
        self, handle: ConnectionHandle, writer: ConnectionWriter
    ) -> bool:
        """Publish completion only after every writer resource has settled."""
        if not writer.cleanup():
            return False
        # Event first: an interruption leaves a harmless overlap (a completed
        # handle still in the registry), never an unregistered waiter whose
        # completion signal nobody can recover.
        handle.finished.set()
        with self._registry_lock:
            if (
                not handle.connection_close_owned
                and handle in self._connections
            ):
                self._connections.remove(handle)
            elif (
                handle.connection_close.completed
                and handle in self._connections
            ):
                self._connections.remove(handle)
        return True

    def unregister(self, handle: ConnectionHandle) -> None:
        with self._registry_lock:
            if handle in self._connections:
                self._connections.remove(handle)

    @property
    def connections(self) -> tuple[ConnectionHandle, ...]:
        with self._registry_lock:
            return tuple(self._connections)

    def publish_state_patch(self, snapshot: RuntimeSnapshot) -> bool:
        """Broadcast an absolute lifecycle replacement. Never blocks.

        Two things happen in this order and it is the whole contract
        (ADR-0025):

        1. the authoritative ``_latest`` moves, so that anything that reads
           the state from here on -- including a connection that registers
           while the patch is being refused -- sees the new revision;
        2. the patch is offered to the bounded ingress.

        Both happen in one critical section, and that section is the *second*
        of two, the way :meth:`connect` splits capture from registration. The
        first one captures what the patch will say: the Step the progress
        view projects, the epoch, and the ``_latest`` they belong to. The
        cost measurement then runs outside the lock -- pure, bounded,
        touching nothing shared -- and the commit critical section re-checks
        the capture before anything moves. An event committed or another
        patch published while it ran advances the epoch or moves
        ``_latest``: the captured Step then describes a world that no longer
        exists, and publishing it would put an old domain fact behind a
        newer one in the one order connections read. The capture is
        discarded and taken again from the current facts instead.

        A snapshot the authoritative one has already overtaken is not
        retried at all: patches are absolute replacements, so publishing it
        would move ``_latest`` backwards, and the honest answer is False --
        the caller's newer state is already the authority here.

        The offer happens in the *same* critical section that moves
        ``_latest``: if it fails, the disconnect generation rises in that
        same step, so a connection registering afterwards sees both the new
        state (in its own opening snapshot) and the new generation, and the
        dispatcher's sweep leaves it alone -- while every connection from an
        earlier generation is asked to hang up and come back for an atomic
        snapshot from the ``_latest`` that already moved. A patch that
        silently failed to arrive is the one delivery failure a client
        cannot detect -- it shows a lifetime state, not a missing event --
        so it becomes a disconnect instead. The publish path itself touches
        no queue: bumping a generation is O(1); finding the connections that
        predate the bump is the dispatcher's job.

        The recapture is bounded: a world that outruns one encoding lap
        after lap is not one more lap can win, and an unbounded spin is a
        publish path that can starve. Exhaustion is not a silent loss,
        though: the captured Step belongs to a world that is already gone,
        so no patch is offered -- but the caller's authoritative store has
        already moved to this snapshot, so
        :meth:`_adopt_unpatchable_snapshot` moves ``_latest`` forward
        under the lock, raises the disconnect generation, and lets the
        dispatcher ask every connection from before that move to hang up
        and re-seed atomically. The answer is still False -- it says the
        patch never entered the ingress -- but the authority and the
        reconnect debt have both moved with it.

        Retrying the exact current revision is a recovery operation, not a
        second publication.  The first call may have moved ``_latest`` and
        then lost its return edge before or after offering the patch.  The
        retry therefore offers nothing: it conservatively raises the
        disconnect generation and wakes the dispatcher, so every connection
        that could have observed the uncertain delivery re-seeds from the
        already-current snapshot.  Reusing one revision for different
        contents is process-fatal because no client could order those two
        incompatible absolute states.
        """
        for _ in range(_MAX_PATCH_CAPTURES):
            kick = False
            offer_failure: Exception | None = None
            repeated = False
            retry_fatal_wake = False
            revision_conflict = False
            with self._lock:
                if self._closed:
                    return False
                if self._fatal is not None:
                    # A previous fatal transition may itself have been
                    # interrupted before its dispatcher kick took effect.
                    # The ingress owns coalescing, so this retry can safely
                    # repay that wake-up debt without duplicating a queued
                    # sentinel.
                    retry_fatal_wake = True
                elif self._stopping:
                    return False
                else:
                    latest = self._latest
                    relation = _snapshot_relation(snapshot, latest)
                    if relation == "older":
                        # An absolute replacement older than the authoritative
                        # snapshot is a regression, not an update.
                        return False
                    if relation == "conflict":
                        revision_conflict = True
                    elif relation == "repeat":
                        # Exact-once queue insertion cannot be reconstructed
                        # after an asynchronous exception.  At-least-once
                        # wake-up is the safe recovery: duplicate kick
                        # sentinels are harmless, while a missing one would
                        # leave an old connection on a lifetime state forever.
                        self._state_epoch += 1
                        self._disconnect_generation += 1
                        kick = self._arm_kick_locked()
                        repeated = True
            if retry_fatal_wake:
                self._post_fatal_kick()
                return False
            if revision_conflict:
                self._raise_revision_conflict()
            if repeated:
                if kick:
                    self._ingress.put_kick()
                return False
            with self._lock:
                # A repeated-revision recovery or another publisher may have
                # moved the world after the first capture lock was released.
                # Restart the ordinary capture instead of using stale facts.
                if self._stopping or self._closed or self._fatal is not None:
                    return False
                if self._latest is not latest:
                    continue
                # The progress view is driven under this broker's lock, so
                # its projection is read under the same lock. The projection
                # is bound to the snapshot's own active Run first: a frozen
                # terminal summary shows only while its Run is still the
                # authoritative active one, and the idle snapshot that
                # releases the lease is what cleans it.
                active = snapshot.active_run
                self._progress.note_active_run(
                    None if active is None else active.run_id
                )
                step = self._progress.projection(
                    None if active is None else active.current_step
                )
                epoch = self._state_epoch
            encoded_bytes = _patch_cost(snapshot, step)
            valid_frame = _patch_frames(snapshot, step, True)
            invalid_frame = _patch_frames(snapshot, step, False)
            patch = _BroadcastPatch(
                snapshot=snapshot,
                valid_frame=valid_frame,
                invalid_frame=invalid_frame,
                cost=FrameCost(frames=1, encoded_bytes=encoded_bytes),
            )
            with self._lock:
                if self._stopping or self._closed or self._fatal is not None:
                    return False
                if self._state_epoch != epoch or self._latest is not latest:
                    # The world moved while the frames were being prepared:
                    # the captured Step is stale, and the decided invariant
                    # -- capture, encode, commit, one linearization point --
                    # is worth another lap, not a lie.
                    continue
                self._adopt_snapshot_locked(snapshot)
                self._state_epoch += 1
                try:
                    offered = self._ingress.offer(patch)
                except Exception as exc:
                    # Latch only after leaving this non-reentrant publication
                    # lock. The fatal transition wakes its dispatcher owner;
                    # this caller never walks the registry or a queue.
                    offer_failure = exc
                    offered = False
                if not offered and offer_failure is None:
                    self._disconnect_generation += 1
                    kick = self._arm_kick_locked()
            if offer_failure is not None:
                handler = self._publish_fatal(offer_failure)
                if handler is not None:
                    # Unlike EventSink.commit, state-patch publication is not
                    # inside FanOut's lock. Report its sole process-fatal fact
                    # here: there may never be a later domain event to make
                    # FanOut discover the dead broker on its own.
                    handler(offer_failure)
                raise offer_failure
            if kick:
                self._ingress.put_kick()
            # The answer is about the *patch* -- queued or refused -- not about
            # whether this overflow had to arm the wake-up: a pending kick from
            # an earlier overflow already covers this one's sweep, and reading
            # that as "queued" would tell the caller a state arrived that every
            # connection will in fact be disconnected for missing.
            return offered
        # Every lap lost its race, so no patch exists that is safe to offer:
        # the last captured Step describes a world that moved before the
        # patch could commit. The authority does not stop here with it --
        # the close below is what keeps the exhaustion bounded *and* honest.
        return self._adopt_unpatchable_snapshot(snapshot)

    def _adopt_unpatchable_snapshot(self, snapshot: RuntimeSnapshot) -> bool:
        """The exhaustion close: move the authority, owe the reconnect.

        Called when every capture lap lost its race. Offering the last
        captured patch would reintroduce the stale-Step-after-newer-facts
        race the recapture exists to prevent, and encoding another lap
        inside the lock is the one thing this structure forbids -- so no
        patch enters the ingress. What remains is the authority itself:
        the caller's state store has already made this snapshot the truth,
        so ``_latest`` must follow it -- never backwards -- and the
        connections that were never told must be asked to reconnect,
        because a missed patch is the one loss a client cannot detect: it
        shows a lifetime state, not a missing event.

        The whole move happens in one critical section, so a connection
        registering after it both re-seeds from the new snapshot (a
        connect that captured its opening stream earlier re-checks the
        epoch at registration and recaptures) and belongs to the new
        generation the dispatcher's sweep will spare. Asking the
        connections to hang up stays the dispatcher's job, off this
        thread; the kick is posted once the lock is left. The answer is
        still False -- no patch was queued -- but it no longer means
        "nothing happened".
        """
        kick = False
        revision_conflict = False
        with self._lock:
            if self._stopping or self._closed or self._fatal is not None:
                return False
            relation = _snapshot_relation(snapshot, self._latest)
            if relation == "older":
                # Overtaken while the laps ran: a newer absolute
                # replacement is already the authority and its own patch
                # is in flight or delivered, so adopting this one would
                # move ``_latest`` -- and every reconnect -- backwards.
                return False
            if relation == "conflict":
                revision_conflict = True
            else:
                if relation == "advance":
                    self._adopt_snapshot_locked(snapshot)
                    active = snapshot.active_run
                    self._progress.note_active_run(
                        None if active is None else active.run_id
                    )
                # Both a new authority and an identical retry invalidate an
                # opening capture and owe old connections a re-seed.  An
                # identical retry deliberately preserves the current object's
                # authority, matching the ordinary repeat path above.
                self._state_epoch += 1
                self._disconnect_generation += 1
                kick = self._arm_kick_locked()
        if revision_conflict:
            self._raise_revision_conflict()
        if kick:
            self._ingress.put_kick()
        return False

    def _raise_revision_conflict(self) -> NoReturn:
        """Publish and raise the single same-revision conflict policy."""
        failure = ProcessFatalSinkError(
            "one state revision named incompatible snapshots"
        )
        handler = self._publish_fatal(failure)
        if handler is not None:
            # State-patch validation runs outside FanOut commit, so reporting
            # through the exactly-once callback cannot re-enter its lock.
            handler(failure)
        raise failure

    # -- internals ---------------------------------------------------------

    def bind_fatal_handler(
        self, handler: Callable[[BaseException], None] | None
    ) -> None:
        """Where to report the dispatcher dying. See :meth:`bind_session_check`."""
        with self._lock:
            self._on_fatal = handler

    def _dispatch_loop(self) -> None:
        """Fan out until told to stop -- or until the fan-out itself breaks.

        A dispatcher that dies is not one connection's problem: nothing
        published from then on reaches any browser, and the ring stops being
        the recovery source it claims to be. That is a process-level fact
        (#23 §9), so it is reported upward rather than swallowed here.
        """
        try:
            while True:
                # FanOut makes cleanup ready only after its publication locks
                # are gone. Check before taking an item so a fatal kick cannot
                # make the dispatcher return while leaving its ring owner
                # behind; the same bounded loop also repays a debt after an
                # earlier cleanup control exception requeued it.
                self._release_ring_cleanup()
                self._drain_interrupted_cleanup()
                try:
                    item = self._ingress.take(timeout=_INGRESS_RECHECK_S)
                except queue.Empty:
                    # Generation/fatal state commits before its best-effort
                    # notification.  A bounded recheck therefore closes the
                    # last possible before-effect interruption window for an
                    # event publisher that has no retry owner.
                    self._sweep_fatal_connections()
                    self._sweep_stale_generations()
                    with self._lock:
                        if self._fatal is not None:
                            return
                    continue
                if not self._deliver_taken(item):
                    return
        except BaseException as exc:  # noqa: BLE001 - reported, then re-raised
            self._enter_fatal(exc)
            raise

    def _latch_fatal_locked(
        self, exc: BaseException, *, wake_dispatcher: bool = True
    ) -> Callable[[BaseException], None] | None:
        """Publish the first fatal fence. Broker lock is held."""
        if self._fatal is not None:
            return None
        self._fatal = exc
        self._stopping = True
        self._fatal_kick_owed = wake_dispatcher
        return self._on_fatal

    def _publish_fatal(
        self, exc: BaseException, *, wake_dispatcher: bool = True
    ) -> Callable[[BaseException], None] | None:
        """Latch fatal state and wake the dispatcher in constant time.

        The fatal state and ``_stopping`` land in one critical section, so
        no reader can see one without the other. Asking the connections to
        hang up happens *outside* that section -- a request-close is a mark
        and a sentinel, never IO, but the walk is proportional to the crowd
        and the lock is the publish path's. A later ``commit`` raises the
        typed process-fatal error on this state, which is what lets the
        FanOut disable this sink for every Run and tell the other sinks why
        exactly once; ``connect`` and state patches refuse for the same
        reason.
        """
        with self._lock:
            handler = self._latch_fatal_locked(
                exc, wake_dispatcher=wake_dispatcher
            )
        if wake_dispatcher:
            self._post_fatal_kick()
        return handler

    def _post_fatal_kick(self) -> None:
        """Pay a fatal wake-up debt, leaving it retryable on interruption."""
        with self._lock:
            if not self._fatal_kick_owed or self._closed:
                return
        self._ingress.put_kick()
        with self._lock:
            self._fatal_kick_owed = False

    def _enter_fatal(self, exc: BaseException) -> None:
        """Publish the dispatcher's death and report it upward.

        Only for a death discovered off the publish path (the dispatcher's
        own thread): the fatal handler's notice is published through the
        FanOut, and calling it from inside a sink's ``commit`` -- where the
        FanOut's publish lock is held -- would deadlock on that lock. The
        ring failure that surfaces inside ``commit`` publishes the same
        fatal state through :meth:`_publish_fatal` and lets the FanOut's
        process-fatal handling say ``sink_disabled`` instead.
        """
        handler = self._publish_fatal(exc, wake_dispatcher=False)
        self._sweep_fatal_connections()
        if handler is not None:
            handler(exc)

    def deliver_next(self, timeout: float | None = None) -> bool:
        """Take one ingress item and hand it to every connection.

        The dispatcher's unit of work, named so a test can drive the whole
        fan-out without a thread: the behaviour under test is the fan-out,
        not the scheduling of it. Before fanning out it sweeps stale
        disconnect generations -- an O(1) check under the lock, doing real
        work only after an overflow. A fatal kick sweeps and ends the loop;
        an ordinary overflow kick continues it. The stop sentinel ends it.
        """
        # Ready cleanup was transferred only after FanOut unlocked. Claiming
        # it here is therefore safe even when this dispatcher won the race
        # with the publisher's own post-commit action.
        self._release_ring_cleanup()
        self._drain_interrupted_cleanup()
        try:
            item = self._ingress.take(timeout=timeout)
        except queue.Empty:
            # Tests and synchronous users get the same debt recovery as the
            # real dispatch loop, even though False still means no queue item.
            self._sweep_fatal_connections()
            self._sweep_stale_generations()
            return False
        return self._deliver_taken(item)

    def _deliver_taken(self, item: _IngressValue) -> bool:
        """Deliver one item whose atomic ingress removal already committed."""
        if isinstance(item, _IngressStop):
            return False
        if isinstance(item, _IngressKick):
            # Taking the kick re-arms its ingress-owned coalescing bit.  The
            # sweep then captures the newest generation, so a generation
            # raised before that capture is still paid by this wake-up; one
            # raised afterwards can enqueue the next bounded kick.
            self._sweep_fatal_connections()
            self._sweep_stale_generations()
            return self._fatal is None
        # A process-fatal publication may be queued behind already admitted
        # data. Retire that bounded backlog without delivering it; the fatal
        # kick behind it owns the eventual sweep and dispatcher exit.
        if self._fatal is not None:
            return True
        self._sweep_stale_generations()
        self._fan_out(item)
        return True

    def close_with_timeout(self, timeout: float | None = None) -> bool:
        """TimedCloseSink capability for a Host's remaining close budget."""
        return self.close(_DRAIN_TIMEOUT_S if timeout is None else timeout)

    def _sweep_stale_generations(self) -> None:
        """Close every connection registered before an unpaid overflow.

        Runs on the dispatcher, outside the publish critical section: the
        emitting thread only raised the generation. A connection from an
        earlier generation may be missing the very item that would not fit,
        so it is asked to hang up and reconnect with its cursor; a
        connection at the current generation recovered its starting state
        from the snapshot and the ring and is left alone. Each generation is
        swept once -- the O(1) check under the lock makes every later call
        free -- and ``request_close`` is a mark, so touching it here never
        closes a socket from this thread.
        """
        with self._lock:
            generation = self._disconnect_generation
            if generation == self._swept_generation:
                return
            self._swept_generation = generation
        with self._registry_lock:
            handles = tuple(self._connections)
        for handle in handles:
            if handle.generation < generation:
                handle.queue.request_close(retry_ms=frames.BACKOFF_RETRY_MS)

    def _sweep_fatal_connections(self) -> None:
        """Ask every pre-fatal connection to stop, outside Broker locks."""
        with self._lock:
            if self._fatal is None:
                return
            # Observation by the dispatcher itself pays the wake-up debt even
            # if the notifying publisher was interrupted before queue effect.
            self._fatal_kick_owed = False
        with self._registry_lock:
            handles = tuple(self._connections)
        for handle in handles:
            handle.queue.request_close()

    def _fan_out(self, item: Any) -> None:
        """Capture a delivery plan, then offer it outside Broker locks.

        Registration is linearized with publication: a handle registered
        before this item's commit records its published boundary and is
        skipped when appropriate; one registered afterwards is absent from
        this immutable plan. Writer exit unregisters only after closing its
        connection, while an aborted startup marks its queue before draining
        it, so a captured stale handle cannot retain live work. Those facts
        let the dispatcher release both Broker locks before queue offer or
        close request. Session lookup and patch encoding have already happened
        before the item entered ingress.
        """
        with self._lock:
            if self._fatal is not None:
                return
        with self._registry_lock:
            handles = tuple(self._connections)
        if isinstance(item, _BroadcastPatch):
            for handle in handles:
                if handle.session_valid is None:
                    # Registration always proves this fact. A malformed legacy
                    # handle is closed rather than making the dispatcher query
                    # storage or invent an answer.
                    handle.queue.request_close()
                    continue
                handle.queue.offer(item.for_session(handle.session_valid))
            return
        for handle in handles:
            self._deliver_event(
                handle,
                item,
                item.ingress_dropped_before,
            )

    def _deliver_event(
        self,
        handle: ConnectionHandle,
        item: _PublishedEvent,
        dropped: int,
    ) -> None:
        """Deliver one domain event plus any drop count owed this connection."""
        if item.published_seq <= handle.published_through:
            # The registration boundary already covers it. For a replayable
            # event that means the writer-owned replay cursor carries it; for
            # a transient it means the delta was published
            # before this connection registered -- the half-finished
            # attempt ADR-0013 says a reconnect discards. The event was
            # committed (and so entered the ring, or moved the published
            # high water) before registration, but it was still sitting in
            # the ingress; delivering it here too would be a duplicate or a
            # resurrection, with no way for the client to tell either from
            # a new one.
            return
        ingress_dropped = max(dropped - handle.ingress_seen, 0)
        outcome = (
            handle.queue.offer(
                item.frames, ingress_dropped=ingress_dropped
            )
            if ingress_dropped
            else handle.queue.offer(item.frames)
        )
        # Accepted means the merged notice (when owed) and candidate were
        # admitted in that order under one queue lock. Refusal leaves both the
        # broker-owned ingress debt and any queue-local debt outstanding.
        if ingress_dropped and outcome.kind == "accepted":
            handle.ingress_seen = dropped

    def _adopt_snapshot_locked(self, snapshot: RuntimeSnapshot) -> None:
        active = snapshot.active_run
        start = (
            None if active is None else self._run_start_seq.get(active.run_id)
        )
        self._latest = snapshot
        # The authority, not run.finished, retires the recovery boundary:
        # a terminal Run still owns the lease while recording is pending/failed.
        self._run_start_seq = (
            {} if active is None or start is None else {active.run_id: start}
        )

    def _note_run_event(self, event: SequencedEvent) -> None:
        name = getattr(event.payload, "name", None)
        run_id = event.envelope.run_id
        if name == "run.started":
            self._run_start_seq[run_id] = event.seq

    def _current_run_state_locked(self) -> frames.CurrentRunState:
        active = self._latest.active_run
        if active is None:
            return "absent"
        start = self._run_start_seq.get(active.run_id)
        if start is None:
            # No event of this Run has been published yet, so nothing of it
            # could have been walked over.
            return "recoverable"
        floor = self._ring.replay_floor_seq()
        return "recoverable" if start > floor else "unrecoverable"


def _spawn_thread(target: Callable[[], None]) -> threading.Thread:
    """Return an unstarted daemon whose identity the broker can own first."""
    return threading.Thread(target=target, name="sse-dispatch", daemon=True)
