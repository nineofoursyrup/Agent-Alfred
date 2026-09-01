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
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_alfred.clock import Clock
from agent_alfred.gateway.web import frames

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
    lock serialises blocking writes between writers; the close lock owns the
    closed state and the shutdown. A writer wedged inside ``write``/``flush``
    holds the write lock -- so the close path, whose whole job is to unblock
    that writer, takes the *close* lock only: it marks the connection closed
    atomically and then shuts the socket down, which is what makes the
    blocked send fail instead of waiting for the writer to release a lock it
    cannot release.

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
        self._closed = False

    def write(self, data: bytes) -> None:
        with self._write_lock:
            self._wfile.write(data)
            self._wfile.flush()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        # Past this point this thread owns the shutdown; nobody else can
        # reach it, and a second close returns above.
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


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


class ConnectionQueue:
    """The per-connection bounded queue. Frames and encoded bytes, both counted.

    The underlying queue is unbounded on purpose: the capacity that matters is
    ours, counted in the two units the decision named, and a sentinel that
    closes the connection must never be refused for want of room.
    """

    def __init__(
        self,
        *,
        budget: frames.FrameBudget = DEFAULT_BUDGET,
    ):
        self.budget = budget
        self._items: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._usage = frames.FrameCost(frames=0, encoded_bytes=0)
        self._dropped = 0
        self._closing = False

    @property
    def close_requested(self) -> bool:
        with self._lock:
            return self._closing

    @property
    def current_cost(self) -> frames.FrameCost:
        """What this queue is holding right now, in both counted units."""
        with self._lock:
            return self._usage

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
        """
        cost = item.ingress_cost()
        with self._lock:
            if self._closing:
                return OfferOutcome(kind="dropped")
            notice = (
                frames.deltas_dropped_notice(self._dropped)
                if recover_dropped and self._dropped
                else None
            )
            notice_cost = (
                notice.ingress_cost()
                if notice is not None
                else frames.FrameCost(frames=0, encoded_bytes=0)
            )
            projected = self._usage + notice_cost + cost
            if self.budget.fits(projected):
                if notice is not None:
                    self._items.put(notice)
                self._items.put(item)
                self._usage = projected
                if notice is not None:
                    self._dropped = 0
                return OfferOutcome(kind="accepted")
            if item.replayable or item.must_deliver:
                self._closing = True
                self._items.put(
                    CloseConnection(retry_ms=frames.BACKOFF_RETRY_MS)
                )
                return OfferOutcome(kind="overflowed")
            if recover_dropped:
                self._dropped += 1
            return OfferOutcome(kind="dropped")

    def request_close(self, *, retry_ms: int | None = None) -> None:
        """Ask the writer thread to hang up. Never closes anything here."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._items.put(CloseConnection(retry_ms=retry_ms))

    def stop(self) -> None:
        self._items.put(StopWriter())

    def take(self, timeout: float):
        """Take the next item, or raise ``queue.Empty`` after ``timeout``."""
        item = self._items.get(timeout=timeout)
        if isinstance(item, frames.PreparedFrames):
            with self._lock:
                self._usage = self._usage - item.ingress_cost()
        return item

    def reserve_startup(self, cost: frames.FrameCost) -> bool:
        """Reserve one writer-owned replay batch against this connection."""
        with self._lock:
            if self._closing:
                return False
            projected = self._usage + cost
            if not self.budget.fits(projected):
                return False
            self._usage = projected
            return True

    def release_startup(self, cost: frames.FrameCost) -> None:
        """Release a replay batch immediately after its final frame is written."""
        with self._lock:
            self._usage = self._usage - cost


class ConnectionSource(Protocol):
    def take(self, timeout: float): ...

    def stop(self) -> None: ...


class ReplayBatch(Protocol):
    kind: str
    entries: tuple[frames.PreparedFrames, ...]
    cost: frames.FrameCost


class StartupReplay:
    """Writer-owned cursor over a frozen replay interval.

    The fetcher returns ring-owned immutable logical events. Only one batch
    is reserved and returned at a time; advancing happens only after the
    writer releases the complete batch, so a multi-frame logical event can
    never become a half-event checkpoint.
    """

    def __init__(
        self,
        *,
        source: ConnectionQueue,
        cursor_seq: int,
        through_seq: int | None,
        fetch: Callable[[int, int | None, frames.FrameBudget], ReplayBatch],
    ):
        self._source = source
        self._cursor_seq = cursor_seq
        self._through_seq = through_seq
        self._fetch = fetch

    def take(self) -> ReplayBatch:
        return self._fetch(
            self._cursor_seq, self._through_seq, self._source.budget
        )

    def release(self, batch: ReplayBatch) -> None:
        self._source.release_startup(batch.cost)
        last = batch.entries[-1]
        assert last.seq is not None
        self._cursor_seq = last.seq

class ConnectionWriter:
    """The connection's own thread: open, take, write, heartbeat, close.

    The opening stream -- retry, re-seed, any gap notice, the snapshot and
    the exact replay tail -- is the writer's own. The fixed prefix precedes
    a cursor that fetches only one whole-event, dual-budgeted replay batch at
    a time; each batch shares the connection queue's capacity and is released
    before the next is fetched. The clock and the source are injected so a
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
        startup: Sequence[frames.PreparedFrames] = (),
        startup_replay: StartupReplay | None = None,
    ):
        self._connection = connection
        self._source = source
        self._clock = clock
        self._heartbeat_s = heartbeat_s
        self._startup = deque(startup)
        self._startup_replay = startup_replay
        self._last_write = clock.monotonic()
        self.failure: BaseException | None = None

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
        while self._startup:
            consume(self._startup.popleft())
        replay = self._startup_replay
        while replay is not None:
            batch = replay.take()
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
            finally:
                replay.release(batch)
            # A released local is still a strong reference. End both loop
            # variables before ``take`` can reserve the next batch, so the
            # two generations never overlap even for one call expression.
            del item
            del batch
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
            self._startup.clear()
            self._startup_replay = None
            self._connection.close()

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
