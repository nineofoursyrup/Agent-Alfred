"""One SSE connection: its bounded queue, its writer thread, its socket.

Backpressure is a *per-connection* property, so each connection owns a queue
and a thread that does the writing. A background tab that stops consuming
therefore fills its own queue and nobody else's -- with one shared queue a
slow tab would starve the foreground one, and "drop transients, close on a
replayable overflow" could not be made to punish only the slow one.

Two rules shape everything here:

- **Nothing that emits may do IO.** :meth:`ConnectionQueue.offer` is O(1) and
  never touches the socket. A connection that must be closed is *marked* and
  handed a sentinel; its own writer thread performs the close.
- **The socket has exactly one owner.** :class:`SocketConnection` is it. The
  HTTP handler thread hands the socket over and never closes it, so there is
  no second close racing the writer's.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from agent_alfred.clock import Clock
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web._fifo import FifoNode, FifoState
from agent_alfred.gateway.web._fifo import reverse_nodes as _reverse_connection_nodes
from agent_alfred.gateway.web.replay import ReplayBatch, ReplayProgress
from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot

# The decided capacity table: frames *and* encoded bytes, counted together.
DEFAULT_BUDGET = frames.FrameBudget(frames=512, encoded_bytes=8 * 1024 * 1024)
# 15 s: long enough to be free, short enough to notice a dead peer. The write
# is what discovers a closed socket -- without it an idle connection thread
# would sit on get() forever and leak.
DEFAULT_HEARTBEAT_S = 15.0


class SSEConnection(Protocol):
    """The only IO point of a connection."""

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class FakeConnection:
    """Records everything written. Drives the same code a socket would."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    @property
    def written(self) -> bytes:
        return b"".join(self.writes)

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True


class SocketConnection:
    """Owns the socket: it writes, it flushes, it is the only closer.

    Two locks, because two things must never wait on each other. The write
    lock serialises blocking writes between writers; the close condition owns
    one shutdown claim and the confirmed-closed state. A writer wedged inside
    ``write``/``flush`` holds the write lock -- so the close path, whose whole
    job is to unblock that writer, takes the *close* lock only: it claims the
    shutdown atomically, performs it outside the lock, and publishes closed
    only after the descriptor effect returns. That shutdown makes the blocked
    send fail instead of waiting for the writer to release a lock it cannot
    release.

    For the same reason close never flushes: a flush on a socket whose peer
    stopped reading is exactly the block this close exists to break.

    Closing is idempotent and best-effort. A peer that already vanished makes
    ``shutdown`` fail and that is not our problem -- the point of closing is
    to release the descriptor, which happens either way, exactly once.
    """

    def __init__(self, sock: socket.socket, wfile, *, lock=None):
        self._sock = sock
        self._wfile = wfile
        self._write_lock = lock or threading.Lock()
        self._close_lock = threading.Lock()
        self._close_condition = threading.Condition(self._close_lock)
        self._close_claim: object | None = None
        self._closed = False

    def write(self, data: bytes) -> None:
        with self._write_lock:
            self._wfile.write(data)
            self._wfile.flush()

    def close(self) -> None:
        claim = object()
        owns_claim = False
        try:
            with self._close_condition:
                while self._close_claim is not None and not self._closed:
                    self._close_condition.wait()
                if self._closed:
                    return
                # Mark the local recovery responsibility before publishing
                # the shared claim: an exit on either side of the assignment
                # can then only clear this exact token, never another closer's.
                owns_claim = True
                self._close_claim = claim
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()
            with self._close_condition:
                if self._close_claim is claim:
                    self._closed = True
        finally:
            if owns_claim:
                with self._close_condition:
                    if self._close_claim is claim:
                        self._close_claim = None
                        self._close_condition.notify_all()


@dataclass(frozen=True)
class CloseConnection:
    """Sentinel: stop after this, optionally raising the client's backoff."""

    retry_ms: int | None = None


@dataclass(frozen=True)
class StopWriter:
    """Sentinel: the writer thread should return."""


@dataclass(frozen=True)
class OfferOutcome:
    kind: Literal["accepted", "dropped", "overflowed"]


_ConnectionValue = frames.PreparedFrames | CloseConnection | StopWriter


@dataclass(frozen=True, slots=True)
class _ConnectionQueueState(FifoState[_ConnectionValue]):
    """Membership, budgets and control ownership published by one store."""

    prefix_usage: frames.FrameCost
    replay_usage: frames.FrameCost
    startup_guard: frames.FrameCost
    dropped: int
    closing: bool
    stop_requested: bool


def _connection_item_cost(item: _ConnectionValue) -> frames.FrameCost:
    if isinstance(item, frames.PreparedFrames):
        return item.ingress_cost()
    return frames.FrameCost(frames=0, encoded_bytes=0)


def _append_connection_items(
    back: FifoNode[_ConnectionValue] | None,
    items: Sequence[_ConnectionValue],
) -> FifoNode[_ConnectionValue] | None:
    """Append a short ordered batch to the persistent reverse list."""
    for item in items:
        back = FifoNode(
            item=item,
            cost=_connection_item_cost(item),
            next=back,
        )
    return back


class ConnectionQueue:
    """The per-connection bounded queue. Frames and encoded bytes, both counted.

    The persistent FIFO is unbounded on purpose: the capacity that matters is
    ours, counted in the two units the decision named, and a sentinel that
    closes the connection must never be refused for want of room. Producers
    build an immutable replacement and publish membership, accounting, drop
    debt and control ownership with one assignment. The sole consumer rotates
    the reverse list outside the producer condition, preserving O(1) offers.
    """

    def __init__(
        self,
        *,
        budget: frames.FrameBudget = DEFAULT_BUDGET,
    ):
        self.budget = budget
        self._condition = threading.Condition(threading.Lock())
        self._consumer_lock = threading.Lock()
        zero = frames.FrameCost(frames=0, encoded_bytes=0)
        self._state = _ConnectionQueueState(
            front=None,
            back=None,
            rotation=None,
            size=0,
            usage=zero,
            prefix_usage=zero,
            replay_usage=zero,
            startup_guard=zero,
            dropped=0,
            closing=False,
            stop_requested=False,
        )

    @property
    def close_requested(self) -> bool:
        with self._condition:
            return self._state.closing

    @property
    def current_cost(self) -> frames.FrameCost:
        """What this queue is holding right now, in both counted units."""
        with self._condition:
            return self._state.usage

    @property
    def current_frames(self) -> int:
        """The frames this queue is holding right now."""
        return self.current_cost.frames

    @property
    def current_bytes(self) -> int:
        """The encoded bytes this queue is holding right now."""
        return self.current_cost.encoded_bytes

    def offer(
        self,
        item: frames.PreparedFrames,
        *,
        recover_dropped: bool = True,
        ingress_dropped: int = 0,
    ) -> OfferOutcome:
        """Queue one logical event. O(1), non-blocking, no IO.

        A transient event that does not fit is dropped and counted -- it is
        presentation, and its audit value rides in the terminal snapshot.
        Anything undroppable (a replayable event, a state patch) closes the
        connection instead: a replayable event is already in the replay ring,
        so reconnecting with the client's ``Last-Event-ID`` recovers it
        exactly, and a lost state patch is corrected by the snapshot a
        reconnect asks for. Dropping either would leave the client with a
        hole it cannot see.

        ``recover_dropped=False`` is for connection-local control frames whose
        own retry/accounting is owned by their caller. They neither consume a
        pending domain-drop count nor become part of it when refused.

        ``ingress_dropped`` adds shared-ingress debt owned by the broker. It is
        combined with this queue's local debt into one notice, and that notice
        plus ``item`` receives one admission verdict under this queue's lock.
        The caller may acknowledge its debt only when the pair is accepted.
        """
        if ingress_dropped < 0:
            raise ValueError("ingress_dropped must be >= 0")
        if ingress_dropped and not recover_dropped:
            raise ValueError(
                "ingress_dropped requires recover_dropped=True"
            )
        cost = item.ingress_cost()
        with self._condition:
            state = self._state
            if state.closing:
                return OfferOutcome(kind="dropped")
            dropped = state.dropped + ingress_dropped
            notice = (
                frames.deltas_dropped_notice(dropped)
                if recover_dropped and dropped
                else None
            )
            notice_cost = (
                notice.ingress_cost()
                if notice is not None
                else frames.FrameCost(frames=0, encoded_bytes=0)
            )
            admitted = state.usage + notice_cost + cost
            protected = frames.FrameCost(
                frames=max(
                    state.startup_guard.frames - state.replay_usage.frames,
                    0,
                ),
                encoded_bytes=max(
                    state.startup_guard.encoded_bytes
                    - state.replay_usage.encoded_bytes,
                    0,
                ),
            )
            projected = admitted + protected
            if self.budget.fits(projected):
                entries: tuple[_ConnectionValue, ...] = (
                    (notice, item) if notice is not None else (item,)
                )
                replacement = replace(
                    state,
                    back=_append_connection_items(state.back, entries),
                    size=state.size + len(entries),
                    usage=admitted,
                    dropped=0 if notice is not None else state.dropped,
                )
                outcome = OfferOutcome(kind="accepted")
            elif item.replayable or item.must_deliver:
                closing = CloseConnection(retry_ms=frames.BACKOFF_RETRY_MS)
                replacement = replace(
                    state,
                    back=_append_connection_items(state.back, (closing,)),
                    size=state.size + 1,
                    closing=True,
                )
                outcome = OfferOutcome(kind="overflowed")
            else:
                replacement = replace(
                    state,
                    dropped=state.dropped + int(recover_dropped),
                )
                outcome = OfferOutcome(kind="dropped")
            self._state = replacement
            if replacement.size > state.size:
                self._condition.notify()
            return outcome

    def request_close(self, *, retry_ms: int | None = None) -> None:
        """Ask the writer thread to hang up. Never closes anything here."""
        with self._condition:
            state = self._state
            if state.closing:
                # The state may have committed immediately before its notify
                # edge was interrupted. Retrying only the wake-up is safe.
                self._condition.notify()
                return
            closing = CloseConnection(retry_ms=retry_ms)
            self._state = replace(
                state,
                back=_append_connection_items(state.back, (closing,)),
                size=state.size + 1,
                closing=True,
            )
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            state = self._state
            if state.stop_requested:
                self._condition.notify()
                return
            stop = StopWriter()
            self._state = replace(
                state,
                back=_append_connection_items(state.back, (stop,)),
                size=state.size + 1,
                stop_requested=True,
            )
            self._condition.notify()

    def take(self, timeout: float):
        """Take the next item, or raise ``queue.Empty`` after ``timeout``."""
        if timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        deadline = time.monotonic() + timeout
        with self._consumer_lock:
            while True:
                rotation = None
                with self._condition:
                    while self._state.size == 0:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise queue.Empty
                        self._condition.wait(remaining)
                    state = self._state
                    if state.front is not None:
                        front = state.front
                        self._state = state.pop_front()
                        return front.item
                    if state.rotation is None:
                        rotation = state.back
                        self._state = state.begin_rotation()
                    else:
                        rotation = state.rotation

                front = _reverse_connection_nodes(rotation)
                with self._condition:
                    state = self._state
                    self._state = state.finish_rotation(rotation, front)
                del state
                del rotation

    def reserve_startup_prefix(
        self,
        startup: Sequence[frames.PreparedFrames],
        guard: frames.FrameCost,
    ) -> StartupPrefixReservation | None:
        """Atomically admit the fixed prefix and frozen-replay protection."""
        entries = tuple(startup)
        cost = frames.FrameCost(frames=0, encoded_bytes=0)
        for item in entries:
            cost = cost + item.ingress_cost()
        with self._condition:
            state = self._state
            projected = state.usage + cost + guard
            if state.closing or not self.budget.fits(projected):
                return None
            self._state = replace(
                state,
                usage=state.usage + cost,
                prefix_usage=state.prefix_usage + cost,
                startup_guard=guard,
            )
        return StartupPrefixReservation(self, entries)

    def _release_startup_prefix(self, cost: frames.FrameCost) -> None:
        with self._condition:
            state = self._state
            self._state = replace(
                state,
                usage=state.usage - cost,
                prefix_usage=state.prefix_usage - cost,
            )

    def _cancel_startup_prefix(self, cost: frames.FrameCost) -> None:
        with self._condition:
            state = self._state
            self._state = replace(
                state,
                usage=state.usage - cost,
                prefix_usage=state.prefix_usage - cost,
                startup_guard=frames.FrameCost(0, 0),
            )

    def build_and_reserve_startup(
        self,
        build: Callable[[frames.FrameBudget], ReplayBatch],
        *,
        continue_when_closing: bool = False,
    ) -> ReplayBatch:
        """Build one replay slice against the capacity that is free now.

        The caller owns the broker lock before entering this queue lock.  The
        remaining-budget observation, slice choice and reservation are one
        queue critical section, so a live offer cannot make the chosen slice
        stale between those steps.
        """
        with self._condition:
            state = self._state
            if state.closing and not continue_when_closing:
                self._state = replace(
                    state,
                    startup_guard=frames.FrameCost(0, 0),
                )
                return ReplayBatch(kind="unavailable")
            remaining_frames = self.budget.frames - state.usage.frames
            remaining_bytes = (
                self.budget.encoded_bytes - state.usage.encoded_bytes
            )
            if remaining_frames < 1 or remaining_bytes < 1:
                return ReplayBatch(kind="unavailable")
            batch = build(
                frames.FrameBudget(
                    frames=remaining_frames,
                    encoded_bytes=remaining_bytes,
                )
            )
            if batch.kind == "batch":
                projected = state.usage + batch.cost
                if not self.budget.fits(projected):
                    raise RuntimeError("startup builder exceeded remaining budget")
                replacement = replace(
                    state,
                    usage=projected,
                    replay_usage=state.replay_usage + batch.cost,
                )
            else:
                replacement = replace(
                    state,
                    startup_guard=frames.FrameCost(0, 0),
                )
            self._state = replacement
            return batch

    def release_startup(self, cost: frames.FrameCost) -> None:
        """Release a replay batch immediately after its final frame is written."""
        with self._condition:
            state = self._state
            self._state = replace(
                state,
                usage=state.usage - cost,
                replay_usage=state.replay_usage - cost,
            )

    def cancel_startup(
        self, cost: frames.FrameCost = frames.FrameCost(0, 0)
    ) -> None:
        """Release a writer's final local slice and its priority guard."""
        with self._condition:
            state = self._state
            self._state = replace(
                state,
                usage=state.usage - cost,
                replay_usage=state.replay_usage - cost,
                startup_guard=frames.FrameCost(0, 0),
            )

    def finish(self) -> None:
        """Revoke admission and release every reference the dead writer owned."""
        with self._condition:
            state = self._state
            zero = frames.FrameCost(0, 0)
            self._state = replace(
                state,
                front=None,
                back=None,
                rotation=None,
                size=0,
                usage=zero,
                prefix_usage=zero,
                replay_usage=zero,
                startup_guard=zero,
                dropped=0,
                closing=True,
                stop_requested=True,
            )
            self._condition.notify_all()


class StartupPrefixReservation:
    """Unique ownership of an already-accounted fixed startup prefix."""

    def __init__(
        self,
        source: ConnectionQueue,
        entries: Sequence[frames.PreparedFrames],
    ) -> None:
        self._source = source
        self._entries = deque(entries)

    def peek(self) -> frames.PreparedFrames | None:
        return self._entries[0] if self._entries else None

    def release_current(self, item: frames.PreparedFrames) -> None:
        if not self._entries or self._entries[0] is not item:
            raise RuntimeError("startup prefix released out of order")
        released = self._entries.popleft()
        self._source._release_startup_prefix(released.ingress_cost())

    def cancel(self) -> None:
        cost = frames.FrameCost(frames=0, encoded_bytes=0)
        while self._entries:
            cost = cost + self._entries.popleft().ingress_cost()
        self._source._cancel_startup_prefix(cost)


class ConnectionSource(Protocol):
    def take(self, timeout: float): ...

    def stop(self) -> None: ...

    def finish(self) -> None: ...


class StartupReplay:
    """Writer-owned cursor over a frozen replay interval.

    The fetcher returns immutable physical-frame slices. Only one slice is
    reserved at a time; advancing happens only after the writer releases it,
    and the explicit progress keeps a partial logical event distinct from
    the browser's last complete checkpoint.
    """

    def __init__(
        self,
        *,
        source: ConnectionQueue,
        cursor_seq: int,
        through_seq: int | None,
        fetch: Callable[[ReplayProgress, int | None], ReplayBatch],
    ):
        self._source = source
        self._progress = ReplayProgress(completed_seq=cursor_seq)
        self._through_seq = through_seq
        self._fetch = fetch

    def take(self) -> ReplayBatch:
        return self._fetch(
            self._progress,
            self._through_seq,
        )

    def release(self, batch: ReplayBatch) -> None:
        """Turn this slice back into the protected next-frame capacity."""
        if batch.next_progress is None:
            raise RuntimeError("replay batch did not declare its next progress")
        self._source.release_startup(batch.cost)
        self._progress = batch.next_progress

    def cancel(self, batch: ReplayBatch) -> None:
        self._source.cancel_startup(batch.cost)


class ConnectionWriter:
    """The connection's own thread: open, take, write, heartbeat, close.

    The opening stream -- retry, re-seed, any gap notice, the snapshot and
    the exact replay tail -- is the writer's own. The fixed prefix precedes
    a cursor that fetches only one dual-budgeted replay slice at a time; each
    slice shares the connection queue's capacity and is released before the
    next is fetched. The clock and the source are injected so a
    heartbeat can be tested deterministically -- a test must never wait
    fifteen seconds to find out whether a comment line was written.
    """

    def __init__(
        self,
        *,
        connection: SSEConnection,
        source: ConnectionSource,
        clock: Clock,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        startup_reservation: StartupPrefixReservation | None = None,
        startup_replay: StartupReplay | None = None,
    ):
        self._connection = connection
        self._source = source
        self._clock = clock
        self._heartbeat_s = heartbeat_s
        self._startup_reservation = startup_reservation
        self._startup_replay = startup_replay
        self._last_write = clock.monotonic()
        self.failure: BaseException | None = None
        self._cleanup_lock = threading.Lock()
        self._cleanup = RollbackSlot()
        connection_owner = ResumableRollback()
        connection_owner.own(connection, lambda: self._connection.close())
        source_owner = ResumableRollback()
        source_owner.own(source, lambda: self._source.finish())
        startup_owner = ResumableRollback()
        startup_owner.own(self, self._cancel_startup)
        # Independent owners let a queue failure coexist with a socket close.
        # Reverse registration preserves startup -> queue -> connection order.
        self._cleanup.begin(connection_owner)
        self._cleanup.begin(source_owner)
        self._cleanup.begin(startup_owner)

    def stop(self) -> None:
        """Ask the thread to return. Safe from any thread."""
        self._source.stop()

    def deliver_startup(
        self, consume: Callable[[frames.PreparedFrames], None]
    ) -> bool:
        """Stream the opening sequence to one consumer, one batch at a time.

        The consumer is the socket writer in production and may be an
        external observer in a deterministic rig. This writer never collects
        what it has delivered: a replay batch remains reserved only while its
        logical events are being consumed, then is released before the next
        batch is fetched.

        ``False`` means the frozen tail could no longer be delivered exactly;
        the final item consumed is the retry instruction for reconnect.
        """
        reservation = self._startup_reservation
        item = reservation.peek() if reservation is not None else None
        while reservation is not None and item is not None:
            try:
                consume(item)
            except BaseException:
                reservation.cancel()
                raise
            reservation.release_current(item)
            del item
            item = reservation.peek()
        self._startup_reservation = None
        replay = self._startup_replay
        batch = replay.take() if replay is not None else None
        while replay is not None and batch is not None:
            if batch.kind == "complete":
                self._startup_replay = None
                return True
            if batch.kind != "batch":
                consume(frames.retry_frame(frames.BACKOFF_RETRY_MS))
                self._startup_replay = None
                return False
            try:
                for item in batch.entries:
                    consume(item)
            except BaseException:
                replay.cancel(batch)
                raise
            replay.release(batch)
            # The queue's guard now protects the next physical record. End
            # the old strong references before fetching it, so two replay
            # generations never coexist in this writer.
            del item
            del batch
            batch = replay.take()
        return True

    def run(self) -> None:
        """Block until told to stop, then close the connection exactly once."""
        try:
            # The opening sequence first, always: it is what tells the
            # client where it is, so it precedes anything the dispatcher
            # may already have queued behind it.
            if not self.deliver_startup(self._write_frames):
                return
            while True:
                try:
                    item = self._source.take(self._heartbeat_s)
                except queue.Empty:
                    self._maybe_heartbeat()
                    continue
                if isinstance(item, StopWriter):
                    return
                if isinstance(item, CloseConnection):
                    # The raised backoff is written before hanging up: a
                    # degraded delivery that reconnected immediately would be
                    # a reconnect storm rather than a backoff.
                    if item.retry_ms is not None:
                        self._write_frames(frames.retry_frame(item.retry_ms))
                    return
                self._write_frames(item)
        except OSError as exc:
            # The peer is gone. That is not a fault in this process and it is
            # not news: the connection is closing either way. Recorded rather
            # than swallowed, and never re-raised -- an unhandled exception
            # here would only print a traceback for a browser that left.
            self.failure = exc
        finally:
            self.cleanup()

    @property
    def cleanup_complete(self) -> bool:
        """Whether every resource branch has returned successfully."""
        with self._cleanup_lock:
            return self._cleanup.settled

    def cleanup(self) -> bool:
        """Resume all independent writer resources without hiding refusal."""
        with self._cleanup_lock:
            try:
                complete = self._cleanup.retry()
            except BaseException as exc:  # noqa: BLE001 - broker retains owner
                if self.failure is None:
                    self.failure = exc
                return False
            if not complete and self.failure is None:
                self.failure = self._cleanup.process_control or next(
                    iter(self._cleanup.errors),
                    RuntimeError("writer cleanup remains incomplete"),
                )
            return complete

    def _cancel_startup(self) -> None:
        reservation = self._startup_reservation
        if reservation is not None:
            reservation.cancel()
            self._startup_reservation = None
        self._startup_replay = None

    def _maybe_heartbeat(self) -> None:
        now = self._clock.monotonic()
        if now - self._last_write < self._heartbeat_s:
            # A timeout that returned early is not an interval.
            return
        self._write_frames(frames.heartbeat_frame())

    def _write_frames(self, item: frames.PreparedFrames) -> None:
        for wire_frame in item.wire_frames():
            self._write(wire_frame)

    def _write(self, data: bytes) -> None:
        self._connection.write(data)
        self._last_write = self._clock.monotonic()
