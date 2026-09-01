"""Per-connection queueing, the writer thread, and the closing discipline."""

from __future__ import annotations

import queue
import socket
import threading
import time

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_broker_test_helpers import Harness
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web.connection import (
    CloseConnection,
    ConnectionQueue,
    ConnectionWriter,
    FakeConnection,
    SocketConnection,
    StartupReplay,
    StopWriter,
)
from agent_alfred.gateway.web.replay import ReplayBatch, ReplayProgress


def _frames(seq: int = 1, *, size: int = 8, replayable: bool = False):
    return frames.measured_frames(
        seq=seq,
        frames=(b"data: " + b"x" * size,),
        id_line=b"id: inst:%d\n" % seq if replayable else b"",
        replayable=replayable,
    )


# --- the per-connection queue ----------------------------------------------


def test_a_transient_frame_that_fits_is_accepted() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=4, encoded_bytes=1 << 20))
    assert conn.offer(_frames(replayable=False)).kind == "accepted"


def test_a_full_queue_drops_transients_and_counts_them() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    assert conn.offer(_frames(1, replayable=False)).kind == "accepted"
    assert conn.offer(_frames(2, replayable=False)).kind == "accepted"
    outcome = conn.offer(_frames(3, replayable=False))
    assert outcome.kind == "dropped"
    # The connection stays open: a slow tab loses presentation, not facts.
    assert conn.close_requested is False


def test_recovery_queues_the_exact_drop_notice_before_the_first_domain_frame() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    conn.offer(_frames(1, replayable=False))
    conn.offer(_frames(2, replayable=False))
    for seq in (3, 4, 5):
        assert conn.offer(_frames(seq, replayable=False)).kind == "dropped"
    # Drain the full queue, then recover. The notice and candidate are one
    # ordered admission: callers cannot observe the candidate first.
    conn.take(timeout=0)
    conn.take(timeout=0)
    candidate = _frames(6, replayable=False)
    assert conn.offer(candidate).kind == "accepted"
    expected_notice = frames.deltas_dropped_notice(3)
    assert conn.current_cost == (
        expected_notice.ingress_cost() + candidate.ingress_cost()
    )
    assert conn.budget.fits(conn.current_cost)

    notice = conn.take(timeout=0)
    assert b'"code":"deltas_dropped"' in notice.wire_bytes()
    assert b'"count":3' in notice.wire_bytes()
    assert conn.take(timeout=0) is candidate


def test_recovery_pair_waits_for_shared_startup_capacity_without_losing_count() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    startup_entry = _frames(99, replayable=True)
    startup = startup_entry.ingress_cost()
    batch = ReplayBatch(kind="batch", entries=(startup_entry,), cost=startup)
    assert conn.build_and_reserve_startup(lambda _remaining: batch) is batch

    queued = _frames(1)
    assert conn.offer(queued).kind == "accepted"
    assert conn.offer(_frames(2)).kind == "dropped"
    assert conn.take(timeout=0) is queued

    # Only one physical-frame slot is free while startup owns the other.
    # Neither half of the recovery pair may appear on its own.
    assert conn.offer(_frames(3)).kind == "dropped"
    with pytest.raises(queue.Empty):
        conn.take(timeout=0)
    assert conn.current_cost == startup

    conn.release_startup(startup)
    candidate = _frames(4)
    assert conn.offer(candidate).kind == "accepted"
    notice = conn.take(timeout=0)
    assert b'"count":2' in notice.wire_bytes()
    assert conn.take(timeout=0) is candidate
    assert conn.current_cost == frames.FrameCost(frames=0, encoded_bytes=0)


def test_recovery_pair_also_waits_for_shared_startup_byte_capacity() -> None:
    candidate = _frames(10)
    notice_cost = frames.deltas_dropped_notice(2).ingress_cost()
    startup_entry = _frames(99, replayable=True)
    startup = startup_entry.ingress_cost()
    budget = frames.FrameBudget(
        frames=4,
        encoded_bytes=(notice_cost + candidate.ingress_cost()).encoded_bytes,
    )
    conn = ConnectionQueue(budget=budget)
    batch = ReplayBatch(kind="batch", entries=(startup_entry,), cost=startup)
    assert conn.build_and_reserve_startup(lambda _remaining: batch) is batch

    # Fill every remaining byte, then establish one missed transient.
    empty_filler_cost = _frames(1, size=0).ingress_cost().encoded_bytes
    filler = _frames(
        1,
        size=(
            budget.encoded_bytes
            - startup.encoded_bytes
            - empty_filler_cost
        ),
    )
    assert conn.offer(filler).kind == "accepted"
    assert conn.offer(_frames(2)).kind == "dropped"
    assert conn.take(timeout=0) is filler

    # Both frames fit, but the shared byte reserve keeps the pair one byte
    # over budget. The candidate is not admitted alone.
    assert conn.offer(_frames(3)).kind == "dropped"
    with pytest.raises(queue.Empty):
        conn.take(timeout=0)
    assert conn.current_cost == startup

    conn.release_startup(startup)
    assert conn.offer(candidate).kind == "accepted"
    notice = conn.take(timeout=0)
    assert b'"count":2' in notice.wire_bytes()
    assert conn.take(timeout=0) is candidate
    assert conn.current_cost == frames.FrameCost(frames=0, encoded_bytes=0)


def test_a_control_notice_does_not_clear_an_unreported_domain_drop() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    conn.offer(_frames(1))
    conn.offer(_frames(2))
    assert conn.offer(_frames(3)).kind == "dropped"
    conn.take(timeout=0)
    conn.take(timeout=0)

    ingress_notice = frames.deltas_dropped_notice(7)
    assert (
        conn.offer(ingress_notice, recover_dropped=False).kind == "accepted"
    )
    assert conn.take(timeout=0) is ingress_notice

    candidate = _frames(4)
    assert conn.offer(candidate).kind == "accepted"
    local_notice = conn.take(timeout=0)
    assert b'"count":1' in local_notice.wire_bytes()
    assert conn.take(timeout=0) is candidate


def test_concurrent_recovery_offers_cannot_enter_ahead_of_the_notice() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=3, encoded_bytes=1 << 20))
    for seq in (1, 2, 3):
        assert conn.offer(_frames(seq)).kind == "accepted"
    assert conn.offer(_frames(4)).kind == "dropped"
    for _ in range(3):
        conn.take(timeout=0)

    candidates = (_frames(5), _frames(6))
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def offer(candidate) -> None:
        barrier.wait()
        outcomes.append(conn.offer(candidate).kind)

    workers = [threading.Thread(target=offer, args=(item,)) for item in candidates]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()

    queued = [conn.take(timeout=0) for _ in range(3)]
    assert b'"code":"deltas_dropped"' in queued[0].wire_bytes()
    assert b'"count":1' in queued[0].wire_bytes()
    assert set(queued[1:]) == set(candidates)
    assert outcomes == ["accepted", "accepted"]


def test_a_replayable_frame_that_does_not_fit_closes_the_connection() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20))
    conn.offer(_frames(1, replayable=False))
    outcome = conn.offer(_frames(2, replayable=True))
    assert outcome.kind == "overflowed"
    assert conn.close_requested is True
    # The event that did not fit is not silently dropped: the connection is
    # hung up so the client reconnects with its cursor and recovers it from
    # the replay ring. The closing carries a raised backoff, or a degraded
    # delivery turns into a reconnect storm.
    assert isinstance(conn.take(timeout=0), frames.PreparedFrames)
    assert conn.take(timeout=0) == CloseConnection(retry_ms=frames.BACKOFF_RETRY_MS)


def test_the_byte_budget_binds_before_the_frame_count() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=100, encoded_bytes=64))
    assert conn.offer(_frames(1, replayable=False, size=40)).kind == "accepted"
    assert conn.offer(_frames(2, replayable=False, size=40)).kind == "dropped"


def test_capacity_defaults_match_the_decided_table() -> None:
    conn = ConnectionQueue()
    assert conn.budget == frames.FrameBudget(512, 8 * 1024 * 1024)


def test_startup_reservation_has_only_the_atomic_builder_entrypoint() -> None:
    assert "reserve_startup" not in vars(ConnectionQueue)


def test_startup_replay_fetch_receives_only_progress_and_frozen_boundary() -> None:
    source = ConnectionQueue()
    calls = []

    def fetch(progress, through_seq):
        calls.append((progress, through_seq))
        return ReplayBatch(kind="complete")

    replay = StartupReplay(
        source=source,
        cursor_seq=3,
        through_seq=7,
        fetch=fetch,
    )

    assert replay.take() == ReplayBatch(kind="complete")
    assert calls == [(ReplayProgress(completed_seq=3), 7)]


# --- the named cost of the queue's accounting --------------------------------


def test_the_queue_capacity_contract_holds_per_dimension() -> None:
    capacity = frames.FrameBudget(frames=512, encoded_bytes=8 * 1024 * 1024)
    conn = ConnectionQueue()
    assert conn.budget == capacity


def test_a_queue_judges_each_dimension_of_the_cost_independently() -> None:
    # The frame dimension binds while the bytes sit far under their budget.
    frame_bound = ConnectionQueue(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    assert frame_bound.offer(_frames(1)).kind == "accepted"
    assert frame_bound.offer(_frames(2)).kind == "accepted"
    third = _frames(3)
    projected = frame_bound.current_cost + third.ingress_cost()
    assert projected.frames > 2 and projected.encoded_bytes < (1 << 20)
    assert frame_bound.offer(third).kind == "dropped"
    # The byte dimension binds while the frame count sits far under its
    # budget.
    byte_bound = ConnectionQueue(
        budget=frames.FrameBudget(frames=100, encoded_bytes=64)
    )
    assert byte_bound.offer(_frames(1, size=40)).kind == "accepted"
    second = _frames(2, size=40)
    projected = byte_bound.current_cost + second.ingress_cost()
    assert projected.encoded_bytes > 64 and projected.frames < 100
    assert byte_bound.offer(second).kind == "dropped"


def test_queue_accounting_returns_exactly_to_zero() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=4, encoded_bytes=1 << 20))
    first, second = _frames(1), _frames(2)
    assert conn.offer(first).kind == "accepted"
    assert conn.offer(second).kind == "accepted"
    assert conn.current_cost == first.ingress_cost() + second.ingress_cost()
    conn.take(timeout=0)
    conn.take(timeout=0)
    assert conn.current_cost == frames.FrameCost(frames=0, encoded_bytes=0)


def test_a_closed_queue_refuses_further_offers() -> None:
    conn = ConnectionQueue(budget=frames.FrameBudget(frames=4, encoded_bytes=1 << 20))
    conn.request_close()
    assert conn.close_requested is True
    assert conn.offer(_frames(1, replayable=True)).kind == "dropped"


def test_take_times_out_with_the_injected_wait() -> None:
    conn = ConnectionQueue()
    with pytest.raises(queue.Empty):
        conn.take(timeout=0)


def test_stop_delivers_a_sentinel_the_writer_acts_on() -> None:
    conn = ConnectionQueue()
    conn.stop()
    assert conn.take(timeout=0) == StopWriter()


# --- the writer thread -----------------------------------------------------


class _ScriptedSource:
    """A queue stand-in that hands out a script, then reports timeouts."""

    def __init__(self, items):
        self._items = list(items)
        self.pending: list[object] = []
        self.timeouts: list[float] = []

    def put(self, item) -> None:
        self._items.append(item)

    def take(self, timeout: float):
        self.timeouts.append(timeout)
        if not self._items:
            # A fake that returns instantly would spin the writer; a hair of
            # sleep keeps the test from burning a core while it waits.
            time.sleep(0.001)
            raise queue.Empty
        return self._items.pop(0)

    def stop(self) -> None:
        self._items.append(StopWriter())

    def finish(self) -> None:
        self._items.clear()


def _writer(source, *, clock, connection=None, heartbeat_s=15.0):
    return ConnectionWriter(
        connection=connection or FakeConnection(),
        source=source,
        clock=clock,
        heartbeat_s=heartbeat_s,
    )


def test_the_writer_writes_every_event_it_takes() -> None:
    connection = FakeConnection()
    source = _ScriptedSource([_frames(1, replayable=True), StopWriter()])
    _writer(source, clock=FakeClock(), connection=connection).run()
    assert connection.written.startswith(b"data: ")
    assert b"id: inst:1" in connection.written
    assert connection.closed is True


@pytest.mark.parametrize("phase", ["startup", "live"])
def test_the_writer_preserves_physical_frame_boundaries(phase: str) -> None:
    """A chunked logical event is written as its complete physical records."""
    prepared = frames.domain_event_frames(
        event_name="run.started",
        payload={"text": "x" * 2850},
        event_id="event-1",
        replayable=True,
        max_frame_bytes=512,
    ).with_checkpoint(1, "inst-test")
    expected = prepared.wire_frames()
    assert len(expected) == 9
    assert len(prepared.wire_bytes()) == 3912

    connection = FakeConnection()
    source = ConnectionQueue(budget=frames.FrameBudget(frames=9, encoded_bytes=1 << 20))
    startup = (prepared,) if phase == "startup" else ()
    if phase == "live":
        assert source.offer(prepared).kind == "accepted"
    source.stop()

    ConnectionWriter(
        connection=connection,
        source=source,
        clock=FakeClock(),
        startup=startup,
    ).run()

    assert connection.writes == list(expected)
    assert all(len(write) <= 512 for write in connection.writes)
    assert connection.written == prepared.wire_bytes()
    assert connection.closed is True


def test_a_heartbeat_is_written_only_when_the_interval_has_elapsed() -> None:
    connection = FakeConnection()
    clock = FakeClock(monotonic_value=0.0)
    source = _ScriptedSource([])
    writer = _writer(source, clock=clock, connection=connection)
    thread = threading.Thread(target=writer.run, daemon=True)
    thread.start()
    try:
        # Under the interval the wait produces nothing: a timeout that
        # returns early is not a heartbeat interval.
        clock.monotonic_value = 14.0
        _quiet_period()
        assert connection.writes == []
        # At the interval exactly one comment goes out, and it is not an
        # event -- no seq, no id, never replayed.
        clock.monotonic_value = 15.0
        _wait_until(lambda: len(connection.writes) == 1)
        assert connection.writes == [b":hb\n\n"]
        # The clock is still 15 s, so no second heartbeat follows.
        _quiet_period()
        assert len(connection.writes) == 1
        clock.monotonic_value = 30.0
        _wait_until(lambda: len(connection.writes) == 2)
    finally:
        writer.stop()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert source.timeouts and source.timeouts[0] == 15.0


def _quiet_period() -> None:
    time.sleep(0.05)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met before timeout")


def test_closing_for_a_replayable_overflow_raises_the_backoff_first() -> None:
    connection = FakeConnection()
    source = _ScriptedSource(
        [
            _frames(1, replayable=True),
            CloseConnection(retry_ms=frames.BACKOFF_RETRY_MS),
        ]
    )
    _writer(source, clock=FakeClock(), connection=connection).run()
    assert connection.written.endswith(b"retry: 3000\n\n")
    assert connection.closed is True


def test_a_plain_close_writes_no_retry_line() -> None:
    connection = FakeConnection()
    source = _ScriptedSource([CloseConnection()])
    _writer(source, clock=FakeClock(), connection=connection).run()
    assert connection.written == b""
    assert connection.closed is True


def test_the_writer_closes_the_connection_even_when_writing_raises() -> None:
    class Exploding(FakeConnection):
        def write(self, data: bytes) -> None:
            raise BrokenPipeError("peer is gone")

    connection = Exploding()
    source = _ScriptedSource([_frames(1, replayable=True)])
    writer = _writer(source, clock=FakeClock(), connection=connection)
    writer.run()
    assert connection.closed is True
    # Recorded, not swallowed and not raised.
    assert isinstance(writer.failure, BrokenPipeError)


def test_the_writer_thread_actually_exits() -> None:
    source = _ScriptedSource([])
    writer = _writer(source, clock=FakeClock())
    thread = threading.Thread(target=writer.run, daemon=True)
    thread.start()
    writer.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_fake_connection_records_and_is_idempotent_on_close() -> None:
    connection = FakeConnection()
    connection.write(b"a")
    connection.write(b"b")
    connection.close()
    connection.close()
    assert connection.writes == [b"a", b"b"]
    assert connection.closed is True


# --- the socket's one owner: closing must never wait for a wedged write -----


class _SocketWFile:
    """A wfile whose flush hands the bytes to a real socket.

    ``flush`` is the blocking point: it sets ``entered_flush`` -- so a test
    knows the writer is already inside the connection's write critical
    section -- and then sends for real, which is what makes a pre-filled
    send buffer block the writer exactly as a browser that stopped reading
    would.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._pending = b""
        self.entered_flush = threading.Event()

    def write(self, data: bytes) -> None:
        self._pending = data

    def flush(self) -> None:
        self.entered_flush.set()
        self._sock.sendall(self._pending)


def _fill_send_buffer(sock: socket.socket) -> None:
    """Fill one end of a socketpair until sends refuse, then restore it.

    After this returns, the *next* blocking send on ``sock`` blocks until the
    peer reads or the socket is shut down -- a fact about the kernel, not
    about thread scheduling, so the writer's block needs no sleep to be
    deterministic.
    """
    sock.setblocking(False)
    try:
        while True:
            sock.send(b"x" * 65536)
    except BlockingIOError:
        pass
    finally:
        sock.setblocking(True)


def test_close_does_not_wait_for_the_write_lock_a_blocked_writer_holds() -> None:
    """The close path must never queue behind the write it exists to break.

    A writer wedged inside ``flush`` holds the write lock; a close that takes
    that lock before shutting the socket down waits forever, and the bounded
    close above it turns into an unbounded one. The shutdown is what unblocks
    the writer -- so close marks itself closed first, shuts the socket down,
    and the blocked write fails with ``OSError`` instead of the close
    hanging.
    """
    sock, peer = socket.socketpair()
    _fill_send_buffer(sock)
    wfile = _SocketWFile(sock)
    connection = SocketConnection(sock, wfile)
    outcome: dict[str, BaseException | str] = {}

    def writer() -> None:
        try:
            connection.write(b"x" * 65536)
            outcome["write"] = "completed"
        except OSError as exc:  # unblocked by the close's shutdown
            outcome["write"] = exc

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert wfile.entered_flush.wait(2.0)
        close_done = threading.Event()

        def do_close() -> None:
            connection.close()
            close_done.set()

        closer = threading.Thread(target=do_close, daemon=True)
        closer.start()
        # The writer is holding the write lock and blocked in send; a close
        # that waits for that lock never sets this event.
        assert close_done.wait(2.0), "close waited for the wedged write's lock"
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "shutdown did not unblock the writer"
        # The unblocking is the shutdown itself: nobody ever read the peer.
        assert isinstance(outcome["write"], OSError)
        # Idempotent: the writer's own finally-close lands on an already
        # closed connection and must be a no-op, not a second shutdown.
        connection.close()
        closer.join(timeout=2.0)
        assert not closer.is_alive()
    finally:
        sock.close()
        peer.close()


class _RealThreads:
    """Runs every spawned thread for real, like the production broker."""

    def spawn(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread


def test_a_gated_flush_writer_does_not_wait_the_broker_close_open() -> None:
    """A broker whose writer is wedged mid-flush still finishes closing.

    The close call must return while the writer is still blocked -- False,
    because a thread it owns has not exited -- and a later close, once the
    writer is gone, reports True. The writer here is blocked the way a real
    one is: holding the connection's write lock inside a flush nobody has
    released, so any close that serialised behind that lock would hang.
    """
    harness = Harness(spawn=_RealThreads())
    harness.broker.start()
    sock, peer = socket.socketpair()
    gate = threading.Event()
    entered = threading.Event()

    class GatedWFile:
        def __init__(self) -> None:
            self._pending = b""

        def write(self, data: bytes) -> None:
            self._pending = data

        def flush(self) -> None:
            entered.set()
            gate.wait()
            sock.sendall(self._pending)

    connection = SocketConnection(sock, GatedWFile())
    handle = harness.broker.connect(connection=connection)
    close_result: list[bool] = []
    close_done = threading.Event()

    def do_close() -> None:
        close_result.append(harness.broker.close(timeout=0.3))
        close_done.set()

    closer = threading.Thread(target=do_close, daemon=True)
    closer.start()
    try:
        assert entered.wait(2.0), "writer never reached its first flush"
        # The writer holds the write lock, blocked in flush. A close that
        # queued behind that lock would never set this event.
        assert close_done.wait(5.0), "broker.close waited for the wedged writer"
        # The writer never confirmed its exit, so this close is an honest
        # "not drained yet" -- ask again.
        assert close_result == [False]
        gate.set()
        assert handle.finished.wait(5.0), "writer never exited after release"
        assert harness.broker.close(timeout=2.0) is True
        assert harness.broker.close(timeout=2.0) is True
    finally:
        gate.set()
        closer.join(timeout=5.0)
        harness.broker.close(timeout=2.0)
        sock.close()
        peer.close()
