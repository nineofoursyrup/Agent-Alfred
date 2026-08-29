"""Per-connection queueing, the writer thread, and the closing discipline."""

from __future__ import annotations

import queue
import threading
import time

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web.connection import (
    CloseConnection,
    ConnectionQueue,
    ConnectionWriter,
    FakeConnection,
    StopWriter,
)


def _frames(seq: int = 1, *, size: int = 8, replayable: bool = False):
    return frames.measured_frames(
        seq=seq,
        frames=(b"data: " + b"x" * size,),
        id_line=b"id: inst:%d\n" % seq if replayable else b"",
        replayable=replayable,
    )


# --- the per-connection queue ----------------------------------------------


def test_a_transient_frame_that_fits_is_accepted() -> None:
    conn = ConnectionQueue(max_frames=4, max_bytes=1 << 20)
    assert conn.offer(_frames(replayable=False)).kind == "accepted"


def test_a_full_queue_drops_transients_and_counts_them() -> None:
    conn = ConnectionQueue(max_frames=2, max_bytes=1 << 20)
    assert conn.offer(_frames(1, replayable=False)).kind == "accepted"
    assert conn.offer(_frames(2, replayable=False)).kind == "accepted"
    outcome = conn.offer(_frames(3, replayable=False))
    assert outcome.kind == "dropped"
    # The connection stays open: a slow tab loses presentation, not facts.
    assert conn.close_requested is False


def test_the_dropped_count_is_reported_exactly_once_on_recovery() -> None:
    conn = ConnectionQueue(max_frames=1, max_bytes=1 << 20)
    conn.offer(_frames(1, replayable=False))
    for seq in (2, 3, 4):
        assert conn.offer(_frames(seq, replayable=False)).kind == "dropped"
    # Drain one, then make room: the first accepted offer carries the count.
    conn.take(timeout=0)
    outcome = conn.offer(_frames(5, replayable=False))
    assert outcome.kind == "accepted"
    assert outcome.recovered_dropped == 3
    assert conn.offer(_frames(6, replayable=False)).recovered_dropped == 0


def test_a_replayable_frame_that_does_not_fit_closes_the_connection() -> None:
    conn = ConnectionQueue(max_frames=1, max_bytes=1 << 20)
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
    conn = ConnectionQueue(max_frames=100, max_bytes=64)
    assert conn.offer(_frames(1, replayable=False, size=40)).kind == "accepted"
    assert conn.offer(_frames(2, replayable=False, size=40)).kind == "dropped"


def test_capacity_defaults_match_the_decided_table() -> None:
    conn = ConnectionQueue()
    assert conn.max_frames == 512
    assert conn.max_bytes == 8 * 1024 * 1024


def test_a_closed_queue_refuses_further_offers() -> None:
    conn = ConnectionQueue(max_frames=4, max_bytes=1 << 20)
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
