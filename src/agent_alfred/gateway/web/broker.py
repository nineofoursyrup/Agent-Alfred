"""SSEBroker: the EventSink that carries events from the FanOut to browsers.

Shape (decided in #23 §3 and §9):

- ``prepare`` serializes, splits and builds frames -- pure, lock-free, may be
  slow. Serializing inside the commit lock would let ``seq`` reorder events
  by nothing more than thread scheduling, and then a *correct* system would
  disable its own sink for a fault that never happened.
- ``commit`` is short and quantitative: append to the ring, then one
  ``put_nowait``. Its cost does not depend on how many connections exist, so
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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_alfred.clock import Clock, SystemClock
from agent_alfred.events import (
    BestEffortFlushResult,
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
)
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.progress import RunProgress, StepProjection
from agent_alfred.gateway.web.replay import (
    CursorText,
    CursorVerdict,
    ReplayRing,
    classify_cursor,
)
from agent_alfred.gateway.web.state import build_snapshot, snapshot_payload
from agent_alfred.runtime.snapshot import RuntimeSnapshot

# The decided capacity table. Constructor defaults, not user knobs.
MAX_INGRESS_FRAMES = 4096
MAX_INGRESS_BYTES = 32 * 1024 * 1024
# Each connection's own queue, so backpressure stays a per-connection fact.
# The numbers live in the connection module -- the queue that implements
# them is the one place they should be readable.
MAX_CONNECTION_FRAMES = connection_module.DEFAULT_MAX_FRAMES
MAX_CONNECTION_BYTES = connection_module.DEFAULT_MAX_BYTES
DEFAULT_HEARTBEAT_S = connection_module.DEFAULT_HEARTBEAT_S
# How long close() waits for the dispatcher and then for the writers. A
# bounded, honest "not fully drained" beats hanging on a socket whose peer
# has stopped reading; the descriptors are released either way.
_DRAIN_TIMEOUT_S = 2.0


class _IngressStop:
    """Sentinel: the dispatcher should return."""


def _unbound_session_check(session_id: str | None) -> bool:
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
    cost: tuple[int, int]

    def ingress_cost(self) -> tuple[int, int]:
        return self.cost


class IngressItem(Protocol):
    """Anything the ingress will carry: a cost in frames *and* in bytes.

    Both budgets are counted on the way in and released on the way out, so
    an item that could not say what it costs could not be accounted for --
    which is how a patch would end up growing a queue without limit.
    """

    def ingress_cost(self) -> tuple[int, int]: ...


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
        max_frames: int = MAX_INGRESS_FRAMES,
        max_bytes: int = MAX_INGRESS_BYTES,
    ):
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self._items: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._frames = 0
        self._bytes = 0

    def offer(self, item: IngressItem) -> bool:
        """Queue one item. Non-blocking, O(1), never partial.

        Domain events and state patches are the only two kinds of traffic
        here, and both are measured in frames *and* encoded bytes.
        """
        frames_used, bytes_used = item.ingress_cost()
        with self._lock:
            if (
                self._frames + frames_used > self.max_frames
                or self._bytes + bytes_used > self.max_bytes
            ):
                return False
            self._items.put(item)
            self._frames += frames_used
            self._bytes += bytes_used
            return True

    def put_stop(self) -> None:
        """The dispatcher's own end signal. The one item with no cost.

        It takes no argument on purpose: a ``put_control`` that accepted an
        arbitrary item is how a patch would have been classed as "control"
        and waved past both budgets.
        """
        self._items.put(_IngressStop())

    def take(self, timeout: float | None = None) -> IngressItem | _IngressStop:
        item = self._items.get(timeout=timeout)
        if isinstance(item, _IngressStop):
            return item
        frames_used, bytes_used = item.ingress_cost()
        with self._lock:
            self._frames -= frames_used
            self._bytes -= bytes_used
        return item


@dataclass
class ConnectionHandle:
    """One registered connection: its queue, its thread, its own facts."""

    queue: ConnectionQueue
    connection: SSEConnection
    session_id: str | None = None
    thread: threading.Thread | None = None
    # How many ingress-dropped transients this connection had already been
    # told about. Connections that arrived later are not told about drops
    # that happened before they existed.
    ingress_seen: int = 0
    # The high-water mark at registration: the upper bound of what this
    # connection's replay already covered. Live items at or below it are
    # skipped, because replay and live delivery can otherwise both carry the
    # same event to the same client.
    replay_through: int = 0
    verdict: CursorVerdict | None = None
    registered_monotonic: float = 0.0
    # Set once this connection's writer has returned: it has stopped writing
    # and closed the socket, so the thread that was holding the HTTP handler
    # open for it may return.
    finished: threading.Event = field(default_factory=threading.Event)
    extra: dict[str, Any] = field(default_factory=dict)


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
        session_is_valid: Callable[[str | None], bool] | None = None,
        ring: ReplayRing | None = None,
        progress: RunProgress | None = None,
        max_ingress_frames: int = MAX_INGRESS_FRAMES,
        max_ingress_bytes: int = MAX_INGRESS_BYTES,
        max_connection_frames: int = MAX_CONNECTION_FRAMES,
        max_connection_bytes: int = MAX_CONNECTION_BYTES,
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
        self._session_is_valid = session_is_valid or _unbound_session_check
        # ``ring or ...`` would silently swap in a fresh ring whenever the
        # caller's happens to be empty, because ReplayRing is sized. An empty
        # ring is a legal ring, so the default is chosen on None, not on falsy.
        self._ring = ring if ring is not None else ReplayRing()
        self._progress = progress if progress is not None else RunProgress()
        self._connection_limits = (max_connection_frames, max_connection_bytes)
        self._max_frame_bytes = max_frame_bytes
        self._heartbeat_s = heartbeat_s
        self._clock = clock or SystemClock()
        self._spawn = spawn or _spawn_thread
        self._ingress = _Ingress(
            max_frames=max_ingress_frames, max_bytes=max_ingress_bytes
        )
        self._lock = threading.Lock()
        self._connections: list[ConnectionHandle] = []
        self._ingress_dropped = 0
        self._run_start_seq: dict[str, int] = {}
        self._dispatcher: threading.Thread | None = None
        self._on_fatal = on_fatal
        self._stopping = False
        self._closed = False

    def bind_session_check(self, check: Callable[[str | None], bool]) -> None:
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

    # -- EventSink ---------------------------------------------------------

    def prepare(self, event: UnsequencedEvent) -> object:
        """Serialize, split, and build frames. Pure; may be slow; no IO.

        One logical event becomes one or more physical frames here, and the
        ``seq`` is deliberately absent from the result: it does not exist
        yet, and there is nothing in the payload it needs to be written into
        because the checkpoint travels in the ``id:`` line.
        """
        return frames.domain_event_frames(
            event_name=event.payload.name,
            payload={"envelope": event.envelope, "payload": event.payload},
            event_id=uuid.uuid4().hex,
            replayable=event.replayable,
            max_frame_bytes=self._max_frame_bytes,
        )

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        """Ring first, then ingress. Short, quantitative, never blocking."""
        item: PreparedFrames = prepared  # type: ignore[assignment]
        with self._lock:
            progress_module.observe(
                event.payload, event.envelope.run_id, self._progress
            )
            self._note_run_event(event)
            if event.replayable:
                # The ring is the only replay source for an active Run, so it
                # is updated under the same lock that decides a connecting
                # client's high-water mark: registration and publication can
                # never interleave into a duplicate or a hole.
                #
                # The checkpoint is only attached once the ring has accepted
                # the event. An event too large for either budget is still
                # delivered live -- the client did see it -- but it carries
                # no ``id:``, because a cursor naming a fact the ring cannot
                # reproduce would be a checkpoint this process cannot honour.
                checkpointed = item.with_checkpoint(event.seq, self._instance)
                if self._ring.append(checkpointed).accepted:
                    item = checkpointed
            if not self._ingress.offer(item):
                if item.replayable or item.must_deliver:
                    # Ingress overflow costs liveness, never recoverability:
                    # the fact is already in the ring, so every live
                    # connection reconnects with its cursor and gets it.
                    for handle in self._connections:
                        handle.queue.request_close()
                else:
                    self._ingress_dropped += 1

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

        Idempotent and bounded: a peer that stopped reading cannot hold
        ``close()`` open, and a descriptor is never left to outlive it.
        """
        with self._lock:
            if self._closed:
                return True
            self._stopping = True
            dispatcher = self._dispatcher
            handles = tuple(self._connections)
        deadline = time.monotonic() + timeout
        if dispatcher is not None:
            # The stop sentinel is queued behind everything already pending,
            # so the dispatcher drains first and only then returns.
            self._ingress.put_stop()
            dispatcher.join(max(0.0, deadline - time.monotonic()))
        for handle in handles:
            handle.queue.stop()
        for handle in handles:
            if handle.thread is None:
                continue
            handle.thread.join(max(0.0, deadline - time.monotonic()))
            if handle.thread.is_alive():
                # The writer is wedged on a socket whose peer stopped
                # reading. It stays a daemon thread so it cannot hold the
                # interpreter open, but the descriptor is ours to release.
                handle.connection.close()
        with self._lock:
            self._closed = True
        return all(
            handle.thread is None or not handle.thread.is_alive()
            for handle in handles
        ) and (dispatcher is None or not dispatcher.is_alive())

    # -- connections -------------------------------------------------------

    def connect(
        self,
        *,
        connection: SSEConnection,
        cursor: CursorText | None = None,
        session_id: str | None = None,
        max_frames: int | None = None,
        max_bytes: int | None = None,
    ) -> ConnectionHandle:
        """Register one connection and prime it with its starting frames.

        The snapshot, the high-water mark and the registration all happen in
        one critical section. Split them and a client that connects while
        events are being emitted gets either a duplicate or a hole, with no
        way to tell which.

        The opening sequence is the decided order, always, in full:
        ``retry`` -> **re-seed** -> optional ``replay_gap`` -> ``state_patch``
        / exact catch-up -> live frames. The re-seed is unconditional; why is
        argued once, at
        :data:`~agent_alfred.gateway.web.frames.STARTUP_CHECKPOINT_SEQ`.
        """
        # Read before the lock: this is a database question, and the
        # critical section below is not allowed to do IO.
        session_valid = self._session_is_valid(session_id)
        default_frames, default_bytes = self._connection_limits
        handle = ConnectionHandle(
            queue=ConnectionQueue(
                max_frames=default_frames if max_frames is None else max_frames,
                max_bytes=default_bytes if max_bytes is None else max_bytes,
            ),
            connection=connection,
            session_id=session_id,
        )
        with self._lock:
            verdict = classify_cursor(
                cursor, self._ring, process_instance_id=self._instance
            )
            handle.verdict = verdict
            handle.ingress_seen = self._ingress_dropped
            handle.replay_through = self._ring.high_water_seq()
            handle.registered_monotonic = self._clock.monotonic()
            startup: list[PreparedFrames] = [
                frames.retry_frame(frames.DEFAULT_RETRY_MS)
            ]
            # Unconditional: every verdict carries a boundary, and omitting
            # it on an empty ring is what erases the browser's cursor.
            startup.append(frames.reseed_frame(self._instance, verdict.reseed_seq))
            if verdict.kind == "gap":
                startup.append(
                    frames.replay_gap_notice(
                        reason=verdict.reason or "malformed",
                        requested_seq=verdict.requested_seq,
                        oldest_seq=self._ring.oldest_seq(),
                        high_water_seq=self._ring.high_water_seq(),
                        current_run_state=self._current_run_state_locked(),
                    )
                )
            snapshot = build_snapshot(
                self._latest,
                step=self._progress.projection(),
                session_valid=session_valid,
            )
            startup.append(frames.state_patch_frames(snapshot_payload(snapshot)))
            for item in verdict.entries:
                startup.append(item)
            # Primed rather than offered: the opening sequence is what tells
            # the client where it is, so it is not a candidate for dropping.
            for item in startup:
                handle.queue.prime(item)
            self._connections.append(handle)
        writer = ConnectionWriter(
            connection=connection,
            source=handle.queue,
            clock=self._clock,
            heartbeat_s=self._heartbeat_s,
        )
        if self._stopping or self._closed:
            # Registered into a broker that is already closing: nothing will
            # ever tell this writer to stop, so it must be told now. Without
            # this, the HTTP handler waiting on ``finished`` would block
            # until the process exits.
            handle.queue.stop()
        handle.thread = self._spawn(lambda: self._run_writer(handle, writer))
        return handle

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

        If the offer fails, the patch is not dropped and the caller is not
        made to wait: every connection already registered is asked to hang
        up, so each one reconnects and is primed with an atomic snapshot
        from the ``_latest`` that already moved. A patch that silently
        failed to arrive is the one delivery failure a client cannot detect
        -- it shows a lifetime state, not a missing event -- so it becomes a
        disconnect instead.

        Returns whether the patch was queued.
        """
        with self._lock:
            if self._stopping or self._closed:
                return False
            self._latest = snapshot
            step = self._progress.projection()
            handles = tuple(self._connections)
        patch = _BroadcastPatch(
            snapshot=snapshot, step=step, cost=(1, _patch_cost(snapshot, step))
        )
        if self._ingress.offer(patch):
            return True
        for handle in handles:
            handle.queue.request_close()
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
            with self._lock:
                self._stopping = True
                handler = self._on_fatal
            if handler is not None:
                handler(exc)
            raise

    def deliver_next(self, timeout: float | None = None) -> bool:
        """Take one ingress item and hand it to every connection.

        The dispatcher's unit of work, named so a test can drive the whole
        fan-out without a thread: the behaviour under test is the fan-out,
        not the scheduling of it. Returns False once the stop sentinel
        arrives, which ends the dispatcher loop.
        """
        try:
            item = self._ingress.take(timeout=timeout)
        except queue.Empty:
            return False
        if isinstance(item, _IngressStop):
            return False
        self._fan_out(item)
        return True

    def _fan_out(self, item: Any) -> None:
        """Hand one ingress item to every connection, atomically.

        The delivery runs inside the same critical section registration
        takes. Doing it in two steps -- snapshot the connections, release,
        then offer -- lets a connection register in between and be handed an
        item its replay had already covered, or miss one its replay did not.
        The cost of holding the lock is bounded: an offer is O(1), copies
        nothing and does no IO. The one question that may read the database
        (whether this connection's Session still exists) is answered for
        every connection *before* the critical section, never inside it.
        """
        with self._lock:
            handles = tuple(self._connections)
        valid = tuple(
            self._session_is_valid(handle.session_id) for handle in handles
        )
        with self._lock:
            dropped = self._ingress_dropped
            for handle, session_valid in zip(handles, valid):
                if isinstance(item, _BroadcastPatch):
                    self._deliver_patch(handle, item, session_valid)
                else:
                    self._deliver_event(handle, item, dropped)

    def _deliver_event(
        self,
        handle: ConnectionHandle,
        item: PreparedFrames,
        dropped: int,
    ) -> None:
        """Deliver one domain event plus any drop count owed this connection."""
        self._deliver_dropped_notice(handle, dropped)
        if item.seq is not None and item.seq <= handle.replay_through:
            # The replay this connection was primed with already carried it.
            # The item was committed (and so entered the ring) before this
            # connection registered, but it was still sitting in the ingress
            # when registration happened; delivering it here too would be a
            # duplicate with no way for the client to tell it from a new one.
            return
        outcome = handle.queue.offer(item)
        # A transient *this* connection had no room for is this connection's
        # own fact: the queue counts it and it is reported once the queue
        # recovers. The ingress-wide count above is a different fact -- it
        # covers events nobody saw because the shared hand-off was full,
        # including connections that did not exist yet.
        if outcome.recovered_dropped:
            handle.queue.offer(
                frames.deltas_dropped_notice(outcome.recovered_dropped)
            )

    def _deliver_dropped_notice(
        self, handle: ConnectionHandle, dropped: int
    ) -> None:
        if dropped <= handle.ingress_seen:
            return
        notice = frames.deltas_dropped_notice(dropped - handle.ingress_seen)
        # Only advanced when the notice was accepted, so a connection that
        # had no room is told again on the next delivery instead of losing
        # the count for good.
        if handle.queue.offer(notice).kind == "accepted":
            handle.ingress_seen = dropped

    def _deliver_patch(
        self,
        handle: ConnectionHandle,
        patch: _BroadcastPatch,
        session_valid: bool,
    ) -> None:
        # Undeliverable means the connection closes and the client comes
        # back for an atomic snapshot; a patch that silently failed to
        # arrive would leave a lifetime state nothing corrects.
        handle.queue.offer(
            _patch_frames(patch.snapshot, patch.step, session_valid)
        )

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
