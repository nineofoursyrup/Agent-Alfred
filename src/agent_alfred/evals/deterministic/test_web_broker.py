"""SSEBroker: reconnect without holes, per-connection backpressure, overflow."""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    BestEffortFlushResult,
    BlockDelta,
    EventEnvelope,
    FanOutSink,
    RunStarted,
    SequencedEvent,
)
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web.broker import SSEBroker, _IngressStop
from agent_alfred.gateway.web.connection import (
    CloseConnection,
    FakeConnection,
    StopWriter,
)
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.replay import CursorText, ReplayRing
from agent_alfred.gateway.web.state import SNAPSHOT_TEXT_LIMIT
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)

INSTANCE = "inst-test"


def _snapshot(**kwargs) -> RuntimeSnapshot:
    base = {
        "process_instance_id": INSTANCE,
        "state_revision": 0,
        "coordinator_state": "idle",
        "active_run": None,
        "unrecorded_terminal_projection": None,
    }
    base.update(kwargs)
    return RuntimeSnapshot(**base)


class Harness:
    """A broker fed by a real FanOutSink, with threads under test control."""

    def __init__(
        self,
        *,
        ring=None,
        max_ingress_frames=4096,
        max_ingress_bytes=32 * 1024 * 1024,
        connection_frames=512,
        connection_bytes=8 * 1024 * 1024,
        max_frame_bytes=frames.MAX_FRAME_BYTES,
        spawn=None,
    ):
        self.spawn = spawn if spawn is not None else _NoThreads()
        self.connection_frames = connection_frames
        self.connection_bytes = connection_bytes
        self.broker = SSEBroker(
            process_instance_id=INSTANCE,
            snapshot=_snapshot(),
            session_is_valid=lambda session_id: session_id in (None, "s1"),
            ring=ring if ring is not None else ReplayRing(),
            max_ingress_frames=max_ingress_frames,
            max_ingress_bytes=max_ingress_bytes,
            max_connection_frames=connection_frames,
            max_connection_bytes=connection_bytes,
            max_frame_bytes=max_frame_bytes,
            spawn=self.spawn.spawn,
        )
        self.fanout = FanOutSink([self.broker], process_instance_id=INSTANCE)
        self.seq = 0

    def emit(self, payload, *, run_id="r1", session_id=None):
        envelope = EventEnvelope(
            ts=float(self.seq),
            run_id=run_id,
            session_id=session_id,
            step_index=None,
            attempt_id=None,
            node_id=None,
        )
        self.seq += 1
        return self.fanout.emit(payload, envelope)

    def emit_many(self, count, *, start=0):
        return [
            self.emit(RunStarted(purpose="chat"), run_id=f"r{start + i}")
            for i in range(count)
        ]

    def connect(self, *, cursor=None, session_id=None, connection=None, **limits):
        return self.broker.connect(
            connection=connection or FakeConnection(),
            cursor=None if cursor is None else CursorText(cursor),
            session_id=session_id,
            **limits,
        )

    def deliver(self, count=1) -> None:
        for _ in range(count):
            assert self.broker.deliver_next(timeout=0.2), "nothing to deliver"


class _NoThreads:
    """Keeps every writer unstarted so a test drives the fan-out by hand."""

    def __init__(self):
        self.targets: list = []

    def spawn(self, target):
        self.targets.append(target)
        return _FakeThread(target)


class _FakeThread:
    def __init__(self, target):
        self._target = target
        self._alive = False

    def start(self):
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False

    def run(self):
        return self._target()


def _drain(handle) -> list:
    items = []
    while True:
        try:
            items.append(handle.queue.take(timeout=0))
        except queue.Empty:
            return items


def _ids(items) -> list[int]:
    out = []
    for item in items:
        if isinstance(item, PreparedFrames) and item.id_line:
            out.append(int(item.id_line.split(b":")[-1].strip()))
    return out


def _cursor_for(seq: int) -> str:
    return f"{INSTANCE}:{seq}"


# --- connect and reconnect -------------------------------------------------


def test_the_first_connection_gets_retry_a_reseed_and_the_snapshot() -> None:
    harness = Harness()
    harness.emit_many(3)
    handle = harness.connect()
    items = _drain(handle)
    assert items[0].wire_bytes() == b"retry: 1000\n\n"
    assert items[1].wire_bytes() == b"id: inst-test:3\n\n"
    # No gap: a first connection is not a degradation.
    assert not any(b"replay_gap" in item.wire_bytes() for item in items)
    assert any(b"event: state_patch" in item.wire_bytes() for item in items)


def test_reconnect_replays_exactly_the_missing_tail() -> None:
    harness = Harness()
    harness.emit_many(5)
    handle = harness.connect(cursor=_cursor_for(2))
    items = _drain(handle)
    assert _ids(items) == [3, 4, 5]
    # Re-seeded at the cursor so an id-less frame cannot erase it.
    assert items[1].wire_bytes() == b"id: inst-test:2\n\n"
    assert not any(b"replay_gap" in item.wire_bytes() for item in items)


def test_reconnect_never_duplicates_and_never_leaves_a_hole() -> None:
    harness = Harness()
    harness.emit_many(6)
    # Each connection resumes from *its own* cursor, so the acceptance is
    # per connection: exactly the missing tail, in order, once. Accumulating
    # across different cursors would ask for the union of three overlapping
    # tails, which is not a property any connection is promised.
    for cursor_seq in (1, 3, 5):
        handle = harness.connect(cursor=_cursor_for(cursor_seq))
        ids = _ids(_drain(handle))
        assert ids == list(range(cursor_seq + 1, 7))
        assert len(set(ids)) == len(ids), f"duplicates from {cursor_seq}: {ids}"


def test_a_rolled_out_ring_answers_a_gap_not_a_silent_resume() -> None:
    harness = Harness(ring=ReplayRing(max_frames=2, max_bytes=1 << 20))
    harness.emit_many(5)
    handle = harness.connect(cursor=_cursor_for(1))
    items = _drain(handle)
    notice = next(
        item for item in items if b"replay_gap" in item.wire_bytes()
    )
    assert b'"gap_reason":"too_old"' in notice.wire_bytes()
    assert b'"requested_seq":1' in notice.wire_bytes()
    # Nothing was replayed from a ring that no longer holds it.
    assert _ids(items) == []


def test_the_four_illegal_cursors_each_name_their_reason() -> None:
    harness = Harness(ring=ReplayRing(max_frames=2, max_bytes=1 << 20))
    harness.emit_many(4)
    cases = {
        "garbage": "malformed",
        "other-process:2": "instance_mismatch",
        _cursor_for(1): "too_old",
        _cursor_for(99): "ahead",
    }
    for cursor, reason in cases.items():
        handle = harness.connect(cursor=cursor)
        items = _drain(handle)
        notice = next(
            item for item in items if b"replay_gap" in item.wire_bytes()
        )
        assert f'"gap_reason":"{reason}"' in notice.wire_bytes().decode()


def test_the_gap_notice_distinguishes_no_run_from_unrecoverable_run() -> None:
    harness = Harness(ring=ReplayRing(max_frames=1, max_bytes=1 << 20))
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit_many(3, start=10)
    # No active Run in the snapshot: absent.
    handle = harness.connect(cursor="garbage")
    assert b'"current_run_state":"absent"' in _wire_containing(handle, b"replay_gap")

    active = _snapshot(
        state_revision=2,
        coordinator_state="running",
        active_run=ActiveRunSummary(
            run_id="r1",
            purpose="chat",
            gateway="web",
            phase="running",
            session_id=None,
            prompt_preview="hi",
            started_at=None,
            recording_state=None,
        ),
    )
    harness.broker.publish_state_patch(active)
    handle = harness.connect(cursor="garbage")
    assert b'"current_run_state":"unrecoverable"' in _wire_containing(
        handle, b"replay_gap"
    )


def _wire_containing(handle, needle: bytes) -> bytes:
    for item in _drain(handle):
        if needle in item.wire_bytes():
            return item.wire_bytes()
    raise AssertionError(f"no frame containing {needle!r}")


def _decode_patch(wire: bytes) -> dict:
    """The state_patch document exactly as it crossed the wire."""
    body = b"".join(
        line[len(b"data: ") :]
        for line in wire.split(b"\n")
        if line.startswith(b"data: ")
    )
    return json.loads(body.decode("utf-8"))


def _patch_payload(handle) -> dict:
    return _decode_patch(_wire_containing(handle, b"event: state_patch"))


def test_the_snapshot_names_the_unrecorded_terminal_projection_bounded() -> None:
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.broker.publish_state_patch(
        _snapshot(
            state_revision=4,
            coordinator_state="recording_pending",
            unrecorded_terminal_projection=UnrecordedTerminalProjection(
                run_id="r1",
                purpose="chat",
                outcome="completed",
                reply_text="y" * 5000,
                error=None,
                recording_state="pending",
                session_id="s1",
                prompt_preview="hi",
            ),
        )
    )
    handle = harness.connect(session_id="s1")
    wire = _wire_containing(handle, b"event: state_patch")
    payload = _decode_patch(wire)
    projection = payload["unrecorded_terminal_projection"]
    assert payload["recording_state"] == "pending"
    assert payload["session_valid"] is True
    assert projection["run_id"] == "r1"
    assert projection["outcome"] == "completed"
    # Bounded, and the cut is marked rather than silent: the snapshot is the
    # one place the full reply text could escape to a browser.
    preview = projection["reply_preview"]
    assert len(preview) == SNAPSHOT_TEXT_LIMIT
    assert preview.endswith("…")
    assert preview.startswith("y" * 100)
    assert len(wire) < frames.MAX_FRAME_BYTES


def test_session_validity_is_this_connections_own_fact() -> None:
    harness = Harness()
    harness.connect(session_id="gone")
    handle = harness.connect(session_id="gone")
    patch = _wire_containing(handle, b"event: state_patch")
    assert b'"session_valid":false' in patch


# --- backpressure ----------------------------------------------------------


def test_a_connection_that_never_consumes_does_not_block_the_run_or_others() -> None:
    harness = Harness()
    # One tab that never reads a single frame from its queue, with a queue
    # small enough to actually fill; the other one keeps up. Backpressure is
    # per connection, so only the first is allowed to suffer for it.
    slow = harness.connect(max_frames=8)
    other = harness.connect(max_frames=512)
    started = time.monotonic()
    for _ in range(80):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    elapsed = time.monotonic() - started
    # Emitting is O(1) in connections: a wedged tab costs it nothing.
    assert elapsed < 2.0
    other_ids = _ids(_drain(other))
    assert other_ids == list(range(1, 81))
    # The slow one was closed rather than allowed to stall the process.
    assert slow.queue.close_requested is True


def test_dropped_transients_are_reported_once_the_connection_recovers() -> None:
    harness = Harness()
    # A queue whose only room is the opening sequence: every transient after
    # it is backpressure, and transients are what gives way.
    handle = harness.connect(max_frames=2)
    for _ in range(3):
        harness.emit(BlockDelta(text="x"), run_id="r1")
        harness.deliver()
    # Drain past the opening frames until the queue has room again.
    _drain(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    items = _drain(handle)
    notice = next(
        (
            item
            for item in items
            if b"deltas_dropped" in item.wire_bytes()
        ),
        None,
    )
    assert notice is not None
    assert b'"count":3' in notice.wire_bytes()
    # Reported once: the next delivery does not repeat the count.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    assert not any(
        b"deltas_dropped" in item.wire_bytes() for item in _drain(handle)
    )


def test_a_transient_missed_by_the_ingress_costs_liveness_only() -> None:
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    handle = harness.connect()
    # The first transient occupies the ingress; the second finds it full.
    harness.emit(BlockDelta(text="x"), run_id="r1")
    harness.emit(BlockDelta(text="y"), run_id="r1")
    # Not queued, and nothing was closed: transients are presentation.
    assert handle.queue.close_requested is False
    assert harness.broker._ingress_dropped == 1
    # The next replayable event carries the notice for the missed transient.
    harness.deliver()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    assert any(b"deltas_dropped" in item.wire_bytes() for item in _drain(handle))


def test_a_replayable_that_misses_the_ingress_closes_live_connections() -> None:
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    handle = harness.connect()
    # One replayable event occupies the ingress; the next cannot fit.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    event = harness.emit(RunStarted(purpose="chat"), run_id="r2")
    # The fact survived in the ring, which is the whole point of ordering
    # ring-before-ingress.
    assert harness.broker._ring.high_water_seq() == event.seq
    assert handle.queue.close_requested is True
    # A reconnect with the cursor recovers it exactly.
    fresh = harness.connect(cursor=_cursor_for(event.seq - 1))
    assert _ids(_drain(fresh)) == [event.seq]


def test_a_transport_notice_owns_no_seq_and_never_enters_the_ring() -> None:
    harness = Harness()
    harness.emit_many(2)
    before = harness.broker._ring.high_water_seq()
    handle = harness.connect(cursor="garbage")
    items = _drain(handle)
    notice = next(item for item in items if b"replay_gap" in item.wire_bytes())
    assert b"\nid: " not in notice.wire_bytes()
    assert harness.broker._ring.high_water_seq() == before


# --- chunking and mid-event disconnection ----------------------------------


def test_a_chunked_event_is_replayed_whole_from_its_first_frame() -> None:
    # 16 KiB frames: a 400 KiB UTF-8 body cannot be one physical frame.
    harness = Harness(max_frame_bytes=16 * 1024)
    big = "é" * (200 * 1024)
    # One event ahead of it, so the chunked event has a checkpoint to resume
    # from that this process actually issued.
    first = harness.emit(RunStarted(purpose="chat"), run_id="r1")
    event = harness.emit(
        RunStarted(purpose="chat", user_message=_user_message_with(big)),
        run_id="chunky",
    )
    # Preparing an event of the same size really does chunk it: this is the
    # transport fact the replay assertion below depends on.
    assert len(harness.broker.prepare(_unsequenced_chunky(big)).frames) > 1
    # What the broker actually committed is the replay source -- not a
    # re-serialization of it, which would carry a different event id and
    # prove nothing about the bytes a reconnect is sent.
    stored = harness.broker._ring.entries_after(first.seq)
    assert len(stored) == 1
    assert stored[0].seq == event.seq
    assert len(stored[0].frames) > 1
    wire = stored[0].wire_frames()
    # Only the last frame of a logical event advances the cursor.
    assert all(b"\nid: " not in frame for frame in wire[:-1])
    assert wire[-1].endswith(b"id: %s:%d\n\n" % (INSTANCE.encode(), event.seq))
    # Reconnecting from before the event re-sends every chunk, not a tail.
    handle = harness.connect(cursor=_cursor_for(first.seq))
    replayed = [item for item in _drain(handle) if item.id_line]
    assert len(replayed) == 1
    assert replayed[0].seq == event.seq
    assert replayed[0].frames == stored[0].frames


def _user_message_with(text: str):
    from agent_alfred.messages import text_message

    return text_message("user", text)


def _unsequenced_chunky(text: str):
    """The same logical event the test just published, unsequenced.

    Prepared independently of the emit so the frames under test are the ones
    a real reconnect would be re-sent, not a re-serialization of them.
    """
    from agent_alfred.events import UnsequencedEvent

    return UnsequencedEvent(
        envelope=EventEnvelope(
            ts=0.0,
            run_id="chunky",
            session_id=None,
            step_index=None,
            attempt_id=None,
            node_id=None,
        ),
        payload=RunStarted(purpose="chat", user_message=_user_message_with(text)),
        trace_policy="persist",
        replayable=True,
    )


# --- the concurrency race --------------------------------------------------


def test_snapshots_and_live_events_never_duplicate_or_gap_under_concurrency() -> None:
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
    )
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    broker.start()
    connections: list[tuple[object, FakeConnection]] = []
    try:
        connections.append((broker.connect(connection=FakeConnection()),
                            _conn_of(broker, 0)))
        stop = threading.Event()

        def emit_forever() -> None:
            i = 0
            while not stop.is_set():
                i += 1
                fanout.emit(
                    RunStarted(purpose="chat"),
                    EventEnvelope(
                        ts=float(i),
                        run_id=f"r{i}",
                        session_id=None,
                        step_index=None,
                        attempt_id=None,
                        node_id=None,
                    ),
                )

        worker = threading.Thread(target=emit_forever, daemon=True)
        worker.start()
        # Connect three more times while events are being published: each
        # one's snapshot, high-water mark and registration must land together.
        for _ in range(3):
            connection = FakeConnection()
            handle = broker.connect(connection=connection)
            connections.append((handle, connection))
            time.sleep(0.01)
        time.sleep(0.15)
        stop.set()
        worker.join(timeout=2)
        time.sleep(0.1)
    finally:
        broker.close(timeout=2.0)

    for _handle, connection in connections:
        ids = _ids_from_wire(connection.written)
        assert ids, "a connection received no event at all"
        # No duplicate, no hole: strictly increasing by exactly one from the
        # first id this connection was given.
        assert ids == sorted(set(ids)), f"duplicates: {ids[:20]}"
        assert ids == list(range(ids[0], ids[0] + len(ids))), f"hole: {ids[:20]}"


def _conn_of(broker, index: int) -> FakeConnection:
    return broker.connections[index].connection


def _ids_from_wire(wire: bytes) -> list[int]:
    """Parse ``id: <process_instance_id>:<seq>`` back into the bare seq."""
    ids = []
    for line in wire.split(b"\n"):
        if not line.startswith(b"id: "):
            continue
        _prefix, _, seq = line.decode().rpartition(":")
        ids.append(int(seq))
    return ids


# --- flush, close, and failure isolation -----------------------------------


def test_flush_cannot_claim_to_have_flushed() -> None:
    harness = Harness()
    result = harness.broker.flush("r1")
    assert isinstance(result, BestEffortFlushResult)
    assert result.outcome == "best_effort"
    assert harness.broker.flush_at_run_end is False


def test_capacity_defaults_match_the_decided_table() -> None:
    broker = SSEBroker(
        process_instance_id=INSTANCE, snapshot=_snapshot()
    )
    assert broker._ingress.max_frames == 4096
    assert broker._ingress.max_bytes == 32 * 1024 * 1024
    assert broker._ring.max_frames == 2048
    assert broker._ring.max_bytes == 32 * 1024 * 1024


def test_close_is_idempotent_and_leaves_no_thread_running() -> None:
    threads: list[threading.Thread] = []

    def spawn(target):
        thread = threading.Thread(target=target, daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
        spawn=spawn,
    )
    broker.start()
    connection = FakeConnection()
    broker.connect(connection=connection)
    assert broker.close(timeout=2.0) is True
    assert broker.close(timeout=2.0) is True
    for thread in threads:
        assert not thread.is_alive()
    assert connection.closed is True


def test_a_wedged_connection_does_not_hold_close_open() -> None:
    class Stuck(FakeConnection):
        def write(self, data: bytes) -> None:
            del data
            time.sleep(5)

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
    )
    broker.start()
    connection = Stuck()
    broker.connect(connection=connection)
    broker.publish_state_patch(_snapshot(state_revision=1))
    started = time.monotonic()
    assert broker.close(timeout=0.3) is False
    # Bounded, and the descriptor is released rather than left behind.
    assert time.monotonic() - started < 2.0
    assert connection.closed is True


def test_one_connections_failure_does_not_touch_the_others() -> None:
    class Exploding(FakeConnection):
        def write(self, data: bytes) -> None:
            raise BrokenPipeError("gone")

    broken = Exploding()
    healthy = FakeConnection()
    threads: list[threading.Thread] = []

    def spawn(target):
        thread = threading.Thread(target=target, daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
        spawn=spawn,
    )
    broker.start()
    broker.connect(connection=broken)
    broker.connect(connection=healthy)
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    for i in range(5):
        fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=float(i),
                run_id=f"r{i}",
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )
    time.sleep(0.3)
    broker.close(timeout=2.0)
    # The healthy tab saw every event even though its neighbour died. The 0
    # is the re-seed: both connections connected to an empty ring, so both
    # were planted the reserved startup boundary before any data.
    assert sorted(set(_ids_from_wire(healthy.written))) == [0, 1, 2, 3, 4, 5]
    assert broken.closed is True


def test_a_patch_is_broadcast_after_the_authoritative_snapshot_moves() -> None:
    harness = Harness()
    handle = harness.connect()
    _drain(handle)
    harness.broker.publish_state_patch(_snapshot(state_revision=7))
    harness.deliver()
    patch = _wire_containing(handle, b"event: state_patch")
    assert b'"state_revision":7' in patch
    # A connection that arrives afterwards is primed with the same revision.
    late = harness.connect()
    assert b'"state_revision":7' in _wire_containing(late, b"event: state_patch")


def test_an_undeliverable_patch_closes_the_connection() -> None:
    harness = Harness()
    handle = harness.connect()
    # Fill the queue to the frame limit with the opening frames plus events.
    for _ in range(600):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    harness.broker.publish_state_patch(_snapshot(state_revision=9))
    harness.deliver()
    assert handle.queue.close_requested is True
    assert any(
        isinstance(item, CloseConnection) for item in _drain(handle)
    )


def test_a_broker_without_a_session_source_refuses_to_guess() -> None:
    """The snapshot's Session-validity field is a fact, not a default.

    The broker is built before the Host it asks, so there is a window in
    which it has no source. Answering "valid" there would publish a snapshot
    claiming a Session exists when nothing has been consulted.
    """
    broker = SSEBroker(process_instance_id=INSTANCE, snapshot=_snapshot())
    with pytest.raises(RuntimeError):
        broker.connect(connection=FakeConnection())
    broker.bind_session_check(lambda session_id: session_id is None)
    assert broker.connect(connection=FakeConnection()) is not None


def test_the_dispatcher_stops_on_the_sentinel() -> None:
    harness = Harness()
    harness.broker._ingress.put_stop()
    assert harness.broker.deliver_next(timeout=0.1) is False


def test_only_the_stop_sentinel_may_bypass_the_budget() -> None:
    """``put_stop`` takes no argument, so nothing else can ride it."""
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1)
    # A full ingress still accepts the sentinel: the dispatcher's own end
    # cannot be refused for want of room.
    harness.emit_many(1)
    harness.broker._ingress.put_stop()
    assert harness.broker._ingress.take(timeout=0.1) is not None
    with pytest.raises(TypeError):
        harness.broker._ingress.put_stop(_IngressStop())


def test_commit_is_quantitative_and_never_writes_io() -> None:
    """The commit half must not touch a socket: closing a connection does IO
    and is therefore the writer thread's job, never the emitting thread's."""
    harness = Harness()
    handle = harness.connect()
    for _ in range(600):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    connection = handle.connection
    assert isinstance(connection, FakeConnection)
    before = len(connection.writes)
    # The queue is over its frame budget from here on.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    # Still nothing written by the emitting thread: the close is a flag the
    # writer thread acts on.
    assert len(connection.writes) == before
    assert handle.queue.close_requested is True


@pytest.mark.parametrize("seq", [1, 2, 3])
def test_a_sequenced_event_enters_the_ring_before_the_ingress(seq: int) -> None:
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    event = harness.emit(RunStarted(purpose="chat"), run_id=f"r{seq}")
    harness.deliver()
    # The very next event cannot fit the ingress, yet the ring already has
    # the previous one: recovery never depends on liveness.
    assert harness.broker._ring.latest_complete_seq() == event.seq
    harness.emit(RunStarted(purpose="chat"), run_id=f"r{seq + 10}")
    assert harness.broker._ring.latest_complete_seq() == event.seq + 1


def test_sequenced_events_pass_through_unchanged() -> None:
    harness = Harness()
    first = harness.emit(RunStarted(purpose="chat"), run_id="r1")
    assert isinstance(first, SequencedEvent)
    assert first.process_instance_id == INSTANCE
    second = harness.emit(RunStarted(purpose="chat"), run_id="r2")
    # Zero is the reserved boundary before the first event, not an event
    # position: on a ring that has lost nothing it is a real starting point,
    # and resuming from it replays everything.
    handle = harness.connect(cursor=_cursor_for(0))
    assert _ids(_drain(handle)) == [first.seq, second.seq]
    assert not any(b"replay_gap" in item.wire_bytes() for item in _drain(handle))
    # A checkpoint this process did issue replays exactly its own tail.
    handle = harness.connect(cursor=_cursor_for(first.seq))
    assert _ids(_drain(handle)) == [second.seq]


def _clock() -> FakeClock:
    return FakeClock()


# --- failure escalation ----------------------------------------------------


class _ExplodingQueue:
    """A connection queue that cannot take anything."""

    def offer(self, item):
        del item
        raise RuntimeError("dispatcher boom")


def test_a_dispatcher_that_dies_is_reported_not_swallowed() -> None:
    """#23 §9: only the dispatcher or the ring escalates to sink_disabled.

    A dead dispatcher is not one connection's problem -- from that moment
    nothing published reaches any browser and the ring stops being the
    recovery source it claims to be. Swallowing it here would leave every
    client looking at a stream that died minutes ago and still says it is
    live.
    """
    harness = Harness()
    reported: list[BaseException] = []
    harness.broker.bind_fatal_handler(reported.append)
    handle = harness.connect()
    handle.queue = _ExplodingQueue()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    with pytest.raises(RuntimeError):
        harness.broker._dispatch_loop()  # noqa: SLF001 - the loop under test
    # Reported upward -- the broker does not decide what a process-level
    # fact means, it only refuses to keep quiet about one.
    assert [str(exc) for exc in reported] == ["dispatcher boom"]
    # And the broker stopped taking new work rather than limping on.
    assert harness.broker._stopping is True  # noqa: SLF001


def test_a_connection_registered_while_closing_is_stopped_at_once() -> None:
    """The HTTP handler waits on ``finished``.

    A connection registered after ``close()`` would never be told to stop,
    so its handler thread would block until the process exited -- one leaked
    thread per reconnect after shutdown.
    """
    harness = Harness()
    harness.broker.close(timeout=0.1)
    handle = harness.connect()
    # Told to stop as part of being registered, not left waiting for a
    # sentinel that is never coming.
    assert any(isinstance(item, StopWriter) for item in _drain(handle))
