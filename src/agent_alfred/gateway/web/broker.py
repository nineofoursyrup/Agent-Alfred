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
from dataclasses import dataclass, field
from typing import Any, Protocol

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
from agent_alfred.gateway.web.connection import (
    ConnectionQueue,
    ConnectionWriter,
    SSEConnection,
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

@dataclass(frozen=True)
class StreamAdmissionProof:
    """The one Session fact established before an HTTP stream is opened."""

    session_id: str | None
    session_valid: bool


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
    """A lifecycle patch, expanded per connection by the dispatcher.

    It goes through the ingress rather than straight into the connection
    queues so that patches and domain events leave in one order: a client
    that receives the patch before the event it describes sees a state that
    was never published.

    It pays the same two budgets as an event. A patch that bypassed them
    would be the one item that can grow without bound while a dispatcher is
    wedged -- and it is precisely the item whose loss is invisible, because
    the state it carries is absolute rather than incremental.
    """

    snapshot: RuntimeSnapshot
    step: StepProjection | None
    cost: FrameCost

    def ingress_cost(self) -> FrameCost:
        return self.cost


@dataclass(frozen=True)
class _PublishedEvent:
    """One published domain event on its way to the dispatcher.

    Two identities, kept apart on purpose. ``published_seq`` is the
    *internal publication order*: the seq the commit critical section
    assigned, transient or not, and the number a connection's registration
    boundary is measured against. ``frames`` is the wire content exactly as
    prepared -- for a transient that means ``frames.seq`` is ``None`` and no
    ``id:`` line exists, because a transient owns no SSE checkpoint and
    must never appear to.

    Conflating the two is how a preregistration transient resurrects
    itself: a boundary taken from the replayable high water alone leaves
    every in-flight delta outside it, and a checkpoint taken from the
    internal seq would mint an ``id:`` the ring cannot honour.
    """

    published_seq: int
    frames: PreparedFrames

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
        self._items: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._usage = FrameCost(frames=0, encoded_bytes=0)

    @property
    def current_cost(self) -> FrameCost:
        """What the ingress is holding right now, in both counted units."""
        with self._lock:
            return self._usage

    def offer(self, item: IngressItem) -> bool:
        """Queue one item. Non-blocking, O(1), never partial.

        Domain events and state patches are the only two kinds of traffic
        here, and both are measured in frames *and* encoded bytes.
        """
        cost = item.ingress_cost()
        with self._lock:
            projected = self._usage + cost
            if not self.budget.fits(projected):
                return False
            self._items.put(item)
            self._usage = projected
            return True

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
        accepted an item would be a second way past the budgets.
        """
        self._items.put(_IngressKick())

    def take(
        self, timeout: float | None = None
    ) -> IngressItem | _IngressStop | _IngressKick:
        item = self._items.get(timeout=timeout)
        if isinstance(item, (_IngressStop, _IngressKick)):
            return item
        cost = item.ingress_cost()
        with self._lock:
            self._usage = self._usage - cost
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
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ConnectStartup:
    """Resources owned by ``connect`` until a writer is successfully spawned."""

    connection: SSEConnection
    handle: ConnectionHandle | None = None
    registration_open: bool = False

    def abort(self, broker: SSEBroker) -> None:
        """Revoke a startup that never handed ownership to a writer."""
        handle = self.handle
        registration_open = False
        if handle is not None:
            with broker._lock:
                if self.registration_open:
                    # A dispatcher may already have captured this registered
                    # handle. Mark its queue first so such a stale bounded
                    # offer is refused instead of retaining a frame after the
                    # startup owner drains the queue below.
                    handle.queue.request_close()
                if handle in broker._connections:
                    broker._connections.remove(handle)
                registration_open = self.registration_open
        try:
            if handle is not None:
                while True:
                    try:
                        handle.queue.take(timeout=0)
                    except queue.Empty:
                        break
            self.connection.close()
        finally:
            if handle is not None:
                handle.finished.set()
            if registration_open:
                with broker._lock:
                    broker._registrations -= 1
                    self.registration_open = False


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
        self._instance = process_instance_id
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
        self._spawn = spawn or _spawn_thread
        self._ingress = _Ingress(budget=ingress_budget)
        self._lock = threading.Lock()
        self._projection_boundary: AbstractContextManager[object] | None = None
        self._connections: list[ConnectionHandle] = []
        self._ingress_dropped = 0
        self._run_start_seq: dict[str, int] = {}
        self._dispatcher: threading.Thread | None = None
        self._on_fatal = on_fatal
        self._stopping = False
        # The registration fence: how many connects hold the space between
        # "the handle entered the registry" and "its writer thread exists".
        # A close that ignored this count could answer True in that window
        # -- and the writer would start afterwards, on a broker the caller
        # has already been told is closed. It is observable so a test can
        # pin the fence rather than guess at the window.
        self._registrations = 0
        # The dispatcher's death, if it has died: the original failure, kept
        # beside ``_stopping`` so every path that would pour work into a
        # dead fan-out -- commit, connect, state patches -- can refuse on a
        # fact instead of pretending the stream still works. Published in
        # the same critical section as ``_stopping``.
        self._fatal: BaseException | None = None
        # The disconnect generation: bumped once per ingress overflow whose
        # item nobody received. Bumping is O(1) and is the whole publish-path
        # cost of an overflow; finding the connections that predate the bump
        # is the dispatcher's job, outside the publish critical section.
        self._disconnect_generation = 0
        self._swept_generation = 0
        # Whether an unconsumed kick is already in the ingress. One pending
        # kick is the whole wake-up contract: the generation it rides with
        # says how far the sweep must go, so a second kick while the first
        # is unread carries no information the first does not -- and the
        # kicks are the one item that would otherwise grow with the
        # overflow count behind a stalled dispatcher.
        self._kick_pending = False
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

    def preflight_session(self, session_id: str | None) -> StreamAdmissionProof:
        """Establish one immutable Session fact before response ownership moves."""
        validity = parse_session_validity(self._session_is_valid(session_id))
        if validity == "unavailable":
            raise RecordingUnavailable("recording store is unavailable")
        return StreamAdmissionProof(
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

        One logical event becomes one or more physical frames here, and the
        ``seq`` is deliberately absent from the result: it does not exist
        yet, and there is nothing in the payload it needs to be written into
        because the checkpoint travels in the ``id:`` line.

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
        return frames.domain_event_frames(
            event_name=event.payload.name,
            payload={"envelope": event.envelope, "payload": event.payload},
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
        kick = False
        ring_failure: BaseException | None = None
        ring_result = None
        retired_release: Callable[[], None] | None = None
        try:
            with self._lock:
                if self._fatal is not None:
                    raise ProcessFatalSinkError(
                        "the dispatcher is down; this sink cannot deliver"
                    ) from self._fatal
                progress_module.observe(
                    event.payload, event.envelope.run_id, self._progress
                )
                self._state_epoch += 1
                self._note_run_event(event)
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
                    ring_failure = exc
                else:
                    if not self._ingress.offer(_PublishedEvent(event.seq, item)):
                        if item.replayable or item.must_deliver:
                            # Ingress overflow costs liveness, never
                            # recoverability: the fact is already in the ring,
                            # so every live connection reconnects with its
                            # cursor and gets it. The bump and the kick are the
                            # whole cost here; the sweep happens on the
                            # dispatcher.
                            self._disconnect_generation += 1
                            kick = self._arm_kick_locked()
                        else:
                            self._ingress_dropped += 1
        finally:
            if ring_result is not None:
                retired_release = ring_result.release_retired
        if ring_failure is not None:
            # The fatal handler is deliberately NOT called from here:
            # commit runs inside the FanOut's publish critical section, and
            # a handler that publishes its own notice would re-enter that
            # lock and deadlock. Publishing the fatal state is enough --
            # the typed refusal below is what makes the FanOut move this
            # sink to its process-wide disabled set and say
            # ``sink_disabled`` exactly once, to every healthy sink.
            self._publish_fatal(ring_failure)
            raise ProcessFatalSinkError(
                "the replay ring is down; this sink cannot deliver"
            ) from ring_failure
        if kick:
            self._ingress.put_kick()
        # FanOut invokes this immediately after leaving its unique publish
        # lock. Logical retirement is already linearized; only Python
        # reference release remains, and its cost may scale with the retired
        # prefix so it cannot run in either publication critical section.
        if retired_release is None:
            return None

        def release_retired() -> None:
            try:
                retired_release()
            except Exception as exc:
                # Logical publication is already complete. Preserve the
                # original cause only in the broker's in-memory fatal latch;
                # FanOut receives the fixed process-fatal policy carried by
                # PostCommit and emits machine-safe context after every
                # sibling cleanup has had its turn.
                self._publish_fatal(exc)
                raise

        return PostCommit(release_retired, failure_scope="process_fatal")

    def _arm_kick_locked(self) -> bool:
        """Decide whether this overflow must wake the dispatcher. Call holds
        the lock.

        True exactly once per pending kick: a kick already in the ingress
        will deliver the newest generation when the dispatcher takes it, so
        the overflow that found it pending bumps the generation and pays
        nothing else. A broker that is stopping or whose dispatcher has
        died arms nothing -- there is no sweep left to wake, and the item
        would be one more unread sentinel behind the close.
        """
        if self._kick_pending or self._stopping or self._fatal is not None:
            return False
        self._kick_pending = True
        return True

    def flush(self, run_id: str) -> BestEffortFlushResult:
        """SSE does not participate in the durability barrier.

        It does not block and it does not wait for the dispatcher -- a sink
        the barrier does not wait on has no business delaying a Run.
        """
        del run_id
        return BestEffortFlushResult(outcome="best_effort")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the single dispatcher thread."""
        with self._lock:
            if self._dispatcher is not None or self._closed:
                return
            self._dispatcher = self._spawn(self._dispatch_loop)

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
        with self._lock:
            if self._closed:
                return True
            self._stopping = True
            dispatcher = self._dispatcher
            handles = tuple(self._connections)
            stop_sent = self._stop_sent
            queues_stopped = self._queues_stopped
        deadline = time.monotonic() + timeout
        if dispatcher is not None and not stop_sent:
            # The stop sentinel is queued behind everything already pending,
            # so the dispatcher drains first and only then returns. Posted
            # once across every attempt: it is the step "asked the
            # dispatcher to stop", which does not become un-done by timing
            # out, and a second sentinel is an item with no reader.
            self._ingress.put_stop()
            with self._lock:
                self._stop_sent = True
        if dispatcher is not None:
            dispatcher.join(max(0.0, deadline - time.monotonic()))
        if not queues_stopped:
            for handle in handles:
                handle.queue.stop()
            with self._lock:
                self._queues_stopped = True
        for handle in handles:
            if handle.thread is None or not handle.thread.is_alive():
                continue
            handle.thread.join(max(0.0, deadline - time.monotonic()))
            if handle.thread.is_alive():
                # The writer is wedged on a socket whose peer stopped
                # reading. It stays a daemon thread so it cannot hold the
                # interpreter open, but the descriptor is ours to release.
                handle.connection.close()
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
        with self._lock:
            current = tuple(self._connections)
            registrations = self._registrations
        drained = dispatcher is None or not dispatcher.is_alive()
        drained = drained and registrations == 0
        drained = drained and all(
            handle.thread is not None and not handle.thread.is_alive()
            for handle in current
        )
        if drained:
            # Only a confirmed exit may publish "closed"; until then every
            # close() comes back here and keeps joining.
            with self._lock:
                self._closed = True
        return drained

    # -- connections -------------------------------------------------------

    def connect(
        self,
        *,
        connection: SSEConnection,
        cursor: CursorText | None = None,
        session_id: str | None = None,
        budget: frames.FrameBudget | None = None,
        admission: StreamAdmissionProof | None = None,
    ) -> ConnectionHandle:
        """Build and register a stream, retaining ownership until its writer exists."""
        acquisition = _ConnectStartup(connection=connection)
        try:
            proof = (
                self.preflight_session(session_id)
                if admission is None
                else admission
            )
            if proof.session_id != session_id:
                raise ValueError("stream admission proof does not match session_id")
            return self._connect_before_writer(
                connection=connection,
                cursor=cursor,
                session_id=session_id,
                budget=budget,
                acquisition=acquisition,
                admission=proof,
            )
        except BaseException:
            try:
                acquisition.abort(self)
            except Exception:
                # The acquisition failure is the caller-visible fact. Socket
                # closure is best-effort, while process-control exceptions
                # from cleanup still keep their normal propagation semantics.
                pass
            raise

    def _connect_before_writer(
        self,
        *,
        connection: SSEConnection,
        cursor: CursorText | None,
        session_id: str | None,
        budget: frames.FrameBudget | None,
        acquisition: _ConnectStartup,
        admission: StreamAdmissionProof,
    ) -> ConnectionHandle:
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
            refused = False
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
                        refused = True
                        break
                    epoch = self._state_epoch
                    verdict = classify_cursor(
                        cursor,
                        self._ring,
                        process_instance_id=self._instance,
                        include_entries=False,
                    )
                    replay_through = self._ring.latest_complete_seq()
                    # The gap notice's facts, captured where they are coherent:
                    # the ring and the run state cannot move inside this critical
                    # section, so the notice is built from frozen values outside.
                    gap_args: (
                        tuple[str, int | None, int | None, int, CurrentRunState]
                        | None
                    ) = None
                    if verdict.kind == "gap":
                        gap_args = (
                            verdict.reason or "malformed",
                            verdict.requested_seq,
                            self._ring.oldest_seq(),
                            self._ring.published_high_water_seq(),
                            self._current_run_state_locked(),
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
                        refused = True
                        break
                    if self._state_epoch != epoch:
                        # An event or a patch moved the world while the frames
                        # were being built: the capture is stale and the decided
                        # invariant -- snapshot, high-water, registration, one
                        # critical section -- is worth another lap, not a lie.
                        continue
                    handle.verdict = verdict
                    handle.ingress_seen = self._ingress_dropped
                    handle.published_through = (
                        self._ring.published_high_water_seq()
                    )
                    # Registered at the current disconnect generation: everything
                    # published so far is either in this opening stream or behind
                    # the published boundary above, so a later sweep must not
                    # close it.
                    handle.generation = self._disconnect_generation
                    handle.registered_monotonic = self._clock.monotonic()
                    self._connections.append(handle)
                    # The fence goes up with the registration and comes down only
                    # when the writer thread exists: in between, a close must see
                    # a registration it may not report around.
                    self._registrations += 1
                    acquisition.registration_open = True
                break
            else:
                # The world moved during every bounded encoding lap. Registering
                # the final capture would ship a stale absolute replacement, and
                # trying forever would strand the HTTP handler while rebuilding
                # snapshots and replay. Refuse this connection in the same
                # observable shape as a closing broker; a reconnect gets a fresh
                # atomic capture without poisoning the broker for other clients.
                refused = True
            if refused:
                acquisition.abort(self)
                return handle
        writer = ConnectionWriter(
            connection=connection,
            source=handle.queue,
            clock=self._clock,
            heartbeat_s=self._heartbeat_s,
            startup=built,
            startup_replay=(
                StartupReplay(
                    source=handle.queue,
                    cursor_seq=verdict.requested_seq,
                    through_seq=replay_through,
                    fetch=lambda progress, through_seq, budget: (
                        self._fetch_startup_replay(
                            handle.queue, progress, through_seq, budget
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
        handle.thread = self._spawn(lambda: self._run_writer(handle, writer))
        with self._lock:
            self._registrations -= 1
            acquisition.registration_open = False
        return handle

    def _fetch_startup_replay(
        self,
        source: ConnectionQueue,
        progress: ReplayProgress,
        through_seq: int | None,
        budget: frames.FrameBudget,
    ) -> ReplayBatch:
        """Fetch and account one immutable batch without IO or waiting."""
        with self._lock:
            batch = self._ring.bounded_entries_after(
                progress, through_seq, budget
            )
            if batch.kind != "batch":
                return batch
            if source.reserve_startup(batch.cost):
                return batch
            # Live traffic consumed the shared budget while replay was still
            # pending. Draining it first would invert wire order; retaining
            # this batch would exceed the bound. Close and reconnect instead.
            return ReplayBatch(kind="unavailable")

    @property
    def registrations_in_flight(self) -> int:
        """Connects that have registered a handle but not installed a writer.

        The fence a close waits on. While this is non-zero, ``close`` cannot
        truthfully answer True: one of these connects may still start a
        writer.
        """
        with self._lock:
            return self._registrations

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
            self.unregister(handle)
            handle.finished.set()

    def unregister(self, handle: ConnectionHandle) -> None:
        with self._lock:
            if handle in self._connections:
                self._connections.remove(handle)

    @property
    def connections(self) -> tuple[ConnectionHandle, ...]:
        with self._lock:
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
        """
        for _ in range(_MAX_PATCH_CAPTURES):
            kick = False
            with self._lock:
                if self._stopping or self._closed or self._fatal is not None:
                    return False
                latest = self._latest
                if snapshot.state_revision < latest.state_revision:
                    # An absolute replacement older than the authoritative
                    # snapshot is a regression, not an update.
                    return False
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
            patch = _BroadcastPatch(
                snapshot=snapshot,
                step=step,
                cost=FrameCost(
                    frames=1, encoded_bytes=_patch_cost(snapshot, step)
                ),
            )
            with self._lock:
                if self._stopping or self._closed or self._fatal is not None:
                    return False
                if self._state_epoch != epoch or self._latest is not latest:
                    # The world moved while the cost was being measured: the
                    # captured Step is stale, and the decided invariant --
                    # capture, encode, commit, one linearization point -- is
                    # worth another lap, not a lie.
                    continue
                self._latest = snapshot
                self._state_epoch += 1
                offered = self._ingress.offer(patch)
                if not offered:
                    self._disconnect_generation += 1
                    kick = self._arm_kick_locked()
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
        with self._lock:
            if self._stopping or self._closed or self._fatal is not None:
                return False
            if snapshot.state_revision < self._latest.state_revision:
                # Overtaken while the laps ran: a newer absolute
                # replacement is already the authority and its own patch
                # is in flight or delivered, so adopting this one would
                # move ``_latest`` -- and every reconnect -- backwards.
                return False
            self._latest = snapshot
            active = snapshot.active_run
            self._progress.note_active_run(
                None if active is None else active.run_id
            )
            # The epoch moves with the authority: an opening stream
            # captured from the previous snapshot is re-checked against it
            # at registration and recaptured, so no connection can open on
            # a revision this call has just replaced.
            self._state_epoch += 1
            self._disconnect_generation += 1
            kick = self._arm_kick_locked()
        if kick:
            self._ingress.put_kick()
        return False

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
            while self.deliver_next():
                pass
        except BaseException as exc:  # noqa: BLE001 - reported, then re-raised
            self._enter_fatal(exc)
            raise

    def _publish_fatal(
        self, exc: BaseException
    ) -> Callable[[BaseException], None] | None:
        """Publish the fatal state and stop every existing stream.

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
            self._fatal = exc
            self._stopping = True
            handles = tuple(self._connections)
            handler = self._on_fatal
        for handle in handles:
            handle.queue.request_close()
        return handler

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
        handler = self._publish_fatal(exc)
        if handler is not None:
            handler(exc)

    def deliver_next(self, timeout: float | None = None) -> bool:
        """Take one ingress item and hand it to every connection.

        The dispatcher's unit of work, named so a test can drive the whole
        fan-out without a thread: the behaviour under test is the fan-out,
        not the scheduling of it. Before fanning out it sweeps stale
        disconnect generations -- an O(1) check under the lock, doing real
        work only after an overflow. A kick returns True after its sweep,
        which ends the dispatcher loop; the stop sentinel returns False.
        """
        try:
            item = self._ingress.take(timeout=timeout)
        except queue.Empty:
            return False
        if isinstance(item, _IngressStop):
            return False
        if isinstance(item, _IngressKick):
            # Taking the kick is what re-arms the overflow path -- and the
            # same critical section captures the newest generation as the
            # sweep's target, so a generation raised after the kick was
            # queued is still swept by *this* take, not left for a kick
            # that will never come.
            self._sweep_stale_generations(clear_kick=True)
            return True
        self._sweep_stale_generations()
        self._fan_out(item)
        return True

    def _sweep_stale_generations(self, *, clear_kick: bool = False) -> None:
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
            if clear_kick:
                self._kick_pending = False
            if generation == self._swept_generation:
                return
            handles = tuple(self._connections)
            self._swept_generation = generation
        for handle in handles:
            if handle.generation < generation:
                handle.queue.request_close()

    def _fan_out(self, item: Any) -> None:
        """Hand one ingress item to every connection, atomically.

        The delivery runs inside the same critical section registration
        takes. Doing it in two steps -- snapshot the connections, release,
        then offer -- lets a connection register in between and be handed an
        item its replay had already covered, or miss one its replay did not.
        The cost of holding the lock is bounded: an offer is O(1), copies
        nothing and does no IO. Everything that is not a bounded reference
        hand-out -- the database question of whether each distinct Session
        represented by the connections still exists, and the encoding of a
        patch per distinct session-validity answer -- happens *before* the
        critical section (ADR-0015: prepare outside, commit inside).
        """
        with self._lock:
            handles = tuple(self._connections)
        if isinstance(item, _BroadcastPatch):
            validity_by_session: dict[str | None, SessionValidity] = {}
            for handle in handles:
                if handle.session_id not in validity_by_session:
                    validity_by_session[handle.session_id] = (
                        parse_session_validity(
                            self._session_is_valid(handle.session_id)
                        )
                    )
            offers: list[tuple[ConnectionHandle, PreparedFrames]] = []
            variants: dict[bool, PreparedFrames] = {}
            for handle in handles:
                validity = validity_by_session[handle.session_id]
                if validity == "unavailable":
                    # Every registered handle was validated when it joined.
                    # Keep that established fact while storage is unavailable;
                    # a handle without one is closed rather than guessed.
                    if handle.session_valid is None:
                        handle.queue.request_close()
                        continue
                    session_valid = handle.session_valid
                else:
                    session_valid = validity == "valid"
                    handle.session_valid = session_valid
                frame = variants.get(session_valid)
                if frame is None:
                    frame = _patch_frames(
                        item.snapshot, item.step, session_valid
                    )
                    variants[session_valid] = frame
                offers.append((handle, frame))
            with self._lock:
                for handle, frame in offers:
                    handle.queue.offer(frame)
            return
        with self._lock:
            dropped = self._ingress_dropped
            for handle in handles:
                self._deliver_event(handle, item, dropped)

    def _deliver_event(
        self,
        handle: ConnectionHandle,
        item: _PublishedEvent,
        dropped: int,
    ) -> None:
        """Deliver one domain event plus any drop count owed this connection."""
        self._deliver_dropped_notice(handle, dropped)
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
        handle.queue.offer(item.frames)

    def _deliver_dropped_notice(
        self, handle: ConnectionHandle, dropped: int
    ) -> None:
        if dropped <= handle.ingress_seen:
            return
        notice = frames.deltas_dropped_notice(dropped - handle.ingress_seen)
        # Only advanced when the notice was accepted, so a connection that
        # had no room is told again on the next delivery instead of losing
        # the count for good.
        if (
            handle.queue.offer(notice, recover_dropped=False).kind
            == "accepted"
        ):
            handle.ingress_seen = dropped

    def _note_run_event(self, event: SequencedEvent) -> None:
        name = getattr(event.payload, "name", None)
        run_id = event.envelope.run_id
        if name == "run.started":
            self._run_start_seq[run_id] = event.seq
        elif name == "run.finished":
            self._run_start_seq.pop(run_id, None)

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
    """A **running** thread: this one starts it before returning.

    Note the contract, because ``lifecycle.SpawnThread`` has the same shape
    and the opposite one -- it hands back an unstarted thread so that
    "cannot be created" and "cannot be started" stay distinguishable at a
    start-up step. The broker wants a running thread and no such split.
    """
    thread = threading.Thread(target=target, name="sse-dispatch", daemon=True)
    thread.start()
    return thread
