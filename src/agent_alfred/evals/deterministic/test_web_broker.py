"""SSEBroker: reconnect without holes, per-connection backpressure, overflow."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import replace

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.events import (
    AttemptCommitted,
    BestEffortFlushResult,
    BlockDelta,
    CapturingSink,
    EventEnvelope,
    FanOutSink,
    RunFinished,
    RunStarted,
    SequencedEvent,
    StepStarted,
    UnsequencedEvent,
)
from agent_alfred.gateway.web import broker as broker_module
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web.broker import (
    SSEBroker,
    _IngressKick,
    _IngressStop,
    _patch_frames,
)
from agent_alfred.gateway.web.connection import (
    CloseConnection,
    ConnectionQueue,
    FakeConnection,
    OfferOutcome,
)
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.progress import (
    AttemptTerminal,
    RunProgress,
    StepProjection,
)
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


class _RealSpawn:
    """Runs every spawned thread for real, like the production broker."""

    def spawn(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread


class _GatedWriteConnection(FakeConnection):
    """A connection whose first write parks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.entered_write = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes) -> None:
        self.entered_write.set()
        self.release.wait()
        super().write(data)


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
    """Everything this connection would receive now -- and consume it.

    The opening stream travels on the handle -- the writer writes it before
    touching the queue -- so a drain returns startup then queue items, and
    marks the startup as written: a second drain returns only what arrived
    since, exactly what a running writer would leave behind.
    """
    items = list(handle.startup)
    handle.startup = ()
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


# --- the terminal Step summary survives the settlement window ---------------


def _terminal_stream(harness: Harness, run_id: str) -> None:
    """One Step's real event vocabulary, through the broker's own commit."""
    harness.emit(RunStarted(purpose="chat"), run_id=run_id)
    harness.emit(StepStarted(step_index=2), run_id=run_id)
    harness.emit(
        AttemptCommitted(
            attempt_id="a1", stop_reason="end_turn", duration_ms=5
        ),
        run_id=run_id,
    )
    harness.emit(RunFinished(outcome="completed"), run_id=run_id)


def _recording_pending_snapshot(**kwargs) -> RuntimeSnapshot:
    return _snapshot(
        state_revision=1,
        coordinator_state="recording_pending",
        active_run=ActiveRunSummary(
            run_id="r1",
            purpose="chat",
            gateway="web",
            phase="finished",
            session_id="s1",
            prompt_preview="hi",
            started_at=None,
            recording_state="pending",
            outcome="completed",
        ),
        unrecorded_terminal_projection=UnrecordedTerminalProjection(
            run_id="r1",
            purpose="chat",
            outcome="completed",
            reply_text="pong",
            error=None,
            recording_state="pending",
            session_id="s1",
            prompt_preview="hi",
        ),
        **kwargs,
    )


def test_run_finished_freezes_the_last_step_until_the_next_run_started() -> None:
    """``run.finished`` freezes the view; it does not erase it.

    The admission lease is held until the recording settles (ADR-0026), and
    in that window this view is the only in-process record of what the Run
    produced -- a same-process reconnect reads exactly it. The summary stops
    when the state moves past the Run: the next ``run.started`` replaces it.
    """
    progress = RunProgress()
    progress.note_run_started("r1")
    progress.note_step_started("r1", 2)
    progress.note_attempt_terminal(
        "r1",
        attempt_id="a1",
        outcome="committed",
        stop_reason="end_turn",
        error_code=None,
        duration_ms=5,
    )
    # run.finished: the reply exists, the recording transaction does not yet.
    progress.note_run_finished("r1")
    frozen = progress.projection()
    assert frozen == StepProjection(
        step_index=2,
        attempts=(
            AttemptTerminal(
                attempt_id="a1",
                outcome="committed",
                stop_reason="end_turn",
                error_code=None,
                duration_ms=5,
            ),
        ),
    )
    # The next Run's run.started replaces the frozen summary, wholesale.
    progress.note_run_started("r2")
    assert progress.projection() is None
    progress.note_step_started("r2", 0)
    assert progress.projection() == StepProjection(step_index=0, attempts=())


def test_the_startup_patch_keeps_the_frozen_summary_while_authoritative() -> None:
    """Same-process reconnect during the settlement window.

    The pending patch is authoritative, its active Run is the finished one,
    so the opening stream must carry the frozen terminal summary -- the last
    StepProjection and its Attempt terminals, exactly as the event stream
    produced them. After recorded→idle (or a new active Run) the old Run's
    summary is cleaned: it may not masquerade as the new state's progress.
    """
    harness = Harness()
    _terminal_stream(harness, "r1")
    harness.broker.publish_state_patch(_recording_pending_snapshot())
    handle = harness.connect(session_id="s1")
    step = _patch_payload(handle)["step"]
    assert step == {
        "step_index": 2,
        "attempts": [
            {
                "attempt_id": "a1",
                "outcome": "committed",
                "stop_reason": "end_turn",
                "error_code": None,
                "duration_ms": 5,
            }
        ],
        "attempts_truncated": False,
    }

    # The lease released: idle, and the old Run's summary goes with it.
    harness.broker.publish_state_patch(_snapshot(state_revision=2))
    idle = _patch_payload(harness.connect(session_id="s1"))
    assert idle["active_run"] is None
    assert idle["step"] is None

    # A new Run admitted: the old Run's frozen summary must not show under it.
    harness.broker.publish_state_patch(
        _snapshot(
            state_revision=3,
            coordinator_state="running",
            active_run=ActiveRunSummary(
                run_id="r2",
                purpose="chat",
                gateway="web",
                phase="running",
                session_id="s1",
                prompt_preview="next",
                started_at=None,
                recording_state=None,
            ),
        )
    )
    next_run = _patch_payload(harness.connect(session_id="s1"))
    assert next_run["active_run"]["run_id"] == "r2"
    assert next_run["step"] is None


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
    # A queue with room for exactly two transient frames: the third is
    # backpressure, and transients are what gives way.
    handle = harness.connect(max_frames=2)
    for _ in range(3):
        harness.emit(BlockDelta(text="x"), run_id="r1")
        harness.deliver()
    # Two were accepted, one was dropped. Drain past them until the queue
    # has room again.
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
    assert b'"count":1' in notice.wire_bytes()
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
    # The overflow raised the disconnect generation; the dispatcher does the
    # closing, outside the publish path.
    harness.deliver()
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


# --- the opening stream is the writer's, never the queue's ------------------


def test_a_replay_tail_over_the_connection_budget_still_arrives_whole() -> None:
    """The opening sequence must not buy its way past the queue's budgets.

    A reconnecting client's exact replay tail can be larger than the
    connection's own frame budget; priming it into the queue made the queue
    hold more than it was ever allowed to the moment it existed. Held by the
    handle for the writer instead, the tail still arrives whole -- in order,
    with no hole -- while the queue starts (and stays) within its budget.
    """
    harness = Harness(
        ring=ReplayRing(max_frames=64, max_bytes=1 << 20),
        connection_frames=8,
        connection_bytes=1 << 20,
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=_cursor_for(0))
    # The queue itself holds nothing: the opening stream belongs to the
    # writer, and the queue's budget has not been spent before it began.
    assert handle.queue.current_frames == 0
    assert handle.queue.current_bytes == 0
    # retry -> re-seed -> snapshot -> the exact tail, in order.
    opening = handle.startup
    assert opening[0].wire_bytes() == b"retry: 1000\n\n"
    assert opening[1].wire_bytes() == b"id: inst-test:0\n\n"
    assert any(
        b"event: state_patch" in frame
        for frame in opening[2].frames
    )
    assert _ids(opening) == list(range(1, 21))
    # The tail is the ring's own frames, reused by reference -- not a
    # second, re-encoded copy of them.
    stored = harness.broker._ring.entries_after(0)
    assert len(opening) == 3 + len(stored)
    assert all(a is b for a, b in zip(opening[3:], stored))


def test_the_queue_budget_holds_during_startup_and_live() -> None:
    """A full ring behind it does not spend a small connection's budget.

    With the tail outside the queue, the first eight live events fit and the
    ninth -- a replayable that cannot fit -- closes the connection under the
    existing overflow rule. Priming the tail into the queue would have
    closed it on the very first live event instead.
    """
    harness = Harness(
        ring=ReplayRing(max_frames=64, max_bytes=1 << 20),
        connection_frames=8,
        connection_bytes=1 << 20,
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=_cursor_for(0))
    # The tail the opening stream carries makes the ingress backlog redundant
    # for this connection: everything committed before it registered is
    # skipped, so the dispatcher flushes it without the queue noticing.
    _run_dispatcher(harness)
    assert handle.queue.current_frames == 0
    for i in range(8):
        harness.emit(RunStarted(purpose="chat"), run_id=f"live{i}")
        harness.deliver()
    assert handle.queue.close_requested is False
    # The ninth live replayable does not fit: the connection is closed, and
    # a silent hole is never produced in its place.
    harness.emit(RunStarted(purpose="chat"), run_id="live8")
    harness.deliver()
    assert handle.queue.close_requested is True


def test_startup_then_live_has_no_hole_and_reconnect_recovers_exactly() -> None:
    """Line order, integrity, budget and recovery, on a tail over budget."""
    harness = Harness(
        ring=ReplayRing(max_frames=64, max_bytes=1 << 20),
        connection_frames=8,
        connection_bytes=1 << 20,
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=_cursor_for(0))
    # Flush the pre-registration backlog: the opening tail already covers
    # it, so the dispatcher skips every one of those items.
    _run_dispatcher(harness)
    for i in range(9):
        harness.emit(RunStarted(purpose="chat"), run_id=f"live{i}")
        harness.deliver()
    # What the client receives: the whole tail, then the live events that
    # fit -- every seq exactly once, no gap between the two.
    seen = _ids(_drain(handle))
    assert seen == list(range(1, 29))
    # Reconnecting from the last delivered cursor recovers exactly the rest.
    fresh = harness.connect(cursor=_cursor_for(28))
    assert _ids(list(fresh.startup)) == [29]


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


# --- close() is a completion report, not an intention -----------------------


class _GatedThreads:
    """Thread stand-ins a test can hold at the starting line.

    Every spawned thread runs its real target only when the gate named for
    that target opens, so a test decides exactly which part of the broker
    is still alive -- without a sleep and without guessing at scheduling.
    The dispatcher's target is the bound method ``_dispatch_loop``; each
    writer's target is the ``<lambda>`` that wraps ``_run_writer``.
    """

    def __init__(self):
        self.gates: dict[str, threading.Event] = {}
        self.by_name: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def spawn(self, target):
        name = getattr(target, "__name__", "<lambda>")
        with self._lock:
            gate = self.gates.setdefault(name, threading.Event())

        def run():
            gate.wait()
            target()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.by_name.setdefault(name, thread)
        return thread

    def open(self, name: str) -> None:
        self.gates[name].set()


def test_close_reports_false_until_every_thread_has_really_exited() -> None:
    """close() answers a question about threads, not about bookkeeping.

    While the dispatcher or any writer is still alive, close() must answer
    False -- on *every* attempt, not only the first. A True that comes from
    the first attempt's own state rather than from the threads having
    exited tells the caller to release what the broker is still draining:
    the database those frames were built from and the process lock the
    stream lives under.
    """
    gates = _GatedThreads()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
        spawn=gates.spawn,
    )
    broker.start()
    broker.connect(connection=FakeConnection())
    dispatcher = gates.by_name["_dispatch_loop"]
    writer = gates.by_name["<lambda>"]

    # First attempt: both threads alive. Stopping has begun; closing has not.
    assert broker.close(timeout=0.05) is False
    assert dispatcher.is_alive()
    assert writer.is_alive()

    # The dispatcher drains as soon as it is allowed to run, but the writer
    # is still alive. A repeated close must keep joining and keep saying
    # False rather than flip to True on the strength of the first attempt.
    gates.open("_dispatch_loop")
    dispatcher.join(timeout=5.0)
    assert not dispatcher.is_alive()
    assert broker.close(timeout=0.05) is False
    assert writer.is_alive()

    # The last thread exits; the next close completes, and stays complete.
    gates.open("<lambda>")
    writer.join(timeout=5.0)
    assert not writer.is_alive()
    assert broker.close(timeout=0.05) is True
    assert broker.close(timeout=0.05) is True


class _GatedSpawn:
    """Holds one spawned thread at the registration's last step.

    The connect path registers a handle and then installs its writer; a
    close that runs while the writer is not installed yet must refuse to
    complete. The gate parks the spawn itself -- the step between "the
    handle is in the registry" and "the writer thread exists".
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []

    def spawn(self, target):
        self.entered.set()
        self.release.wait()
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread


def test_close_cannot_complete_behind_an_in_flight_registration() -> None:
    """A close must not answer True while a connection is mid-registration.

    A handle that is registered but whose writer is not installed yet is a
    writer this close has not seen. Answering True here sends the caller
    off to close the database and release the process lock, and the writer
    starts afterwards -- a live writer on a broker the caller believes is
    closed. The registration must finish (or be revoked) before ``closed``
    is published, and when the close does answer True there must be no
    registered handle, no registration in flight, and no live thread.
    """
    gated = _GatedSpawn()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
        spawn=gated.spawn,
    )
    connect_done = threading.Event()

    def do_connect() -> None:
        broker.connect(connection=FakeConnection())
        connect_done.set()

    connector = threading.Thread(target=do_connect, daemon=True)
    connector.start()
    try:
        assert gated.entered.wait(2.0), "connect never reached the writer step"
        # The handle is registered; the writer is not installed. This close
        # must not claim completion.
        assert broker.close(timeout=0.1) is False
        assert broker.registrations_in_flight == 1
        gated.release.set()
        assert connect_done.wait(5.0), "connect never finished its registration"
        assert broker.close(timeout=2.0) is True
        # A True answer is a fact about the registry and the threads.
        assert broker.connections == ()
        assert broker.registrations_in_flight == 0
    finally:
        gated.release.set()
        connector.join(timeout=5.0)


def test_a_closing_broker_refuses_new_connections_without_a_writer() -> None:
    """Once closing has begun, a connect registers nothing and starts none.

    The refused connection is closed by the broker -- outside the broker
    lock -- and its ``finished`` is observable, because the HTTP handler
    holding the stream open waits on exactly that event. Nothing is written:
    a startup sequence describes a stream the broker is taking away.
    """
    harness = Harness(spawn=_RealSpawn())
    held = _GatedWriteConnection()
    harness.broker.connect(connection=held)
    assert held.entered_write.wait(2.0), "writer never reached its first write"
    assert harness.broker.close(timeout=0.2) is False  # stopping, not closed
    connection = FakeConnection()
    refused = harness.broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused not in harness.broker.connections
    assert connection.written == b""
    assert connection.closed is True
    held.release.set()
    assert harness.broker.close(timeout=2.0) is True


def test_a_closed_broker_refuses_new_connections_idempotently() -> None:
    """After close() has answered True, a connect gets the same refusal.

    Idempotence is the point: the first refusal and the one after a fully
    completed close are the same answer, for the same reason -- nothing may
    register, nothing may write, and the caller is told by ``finished``
    rather than left waiting on a stream that will never carry a frame.
    """
    harness = Harness(spawn=_RealSpawn())
    harness.connect()
    assert harness.broker.close(timeout=2.0) is True
    connection = FakeConnection()
    refused = harness.broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused not in harness.broker.connections
    assert connection.written == b""
    assert connection.closed is True
    # Asking again changes nothing.
    again = harness.broker.connect(connection=FakeConnection())
    assert again.finished.is_set()
    assert harness.broker.connections == ()


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
    # Fill the queue to the frame limit with events.
    for _ in range(600):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    harness.broker.publish_state_patch(_snapshot(state_revision=9))
    # The refused patch raised the disconnect generation; the dispatcher
    # closes the connection that predates it.
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
    # The event cannot fit a one-byte ingress, so the overflow queues the
    # free kick -- and a full ingress still accepts the end sentinel: the
    # dispatcher's own end cannot be refused for want of room.
    harness.emit_many(1)
    harness.broker._ingress.put_stop()
    assert isinstance(harness.broker._ingress.take(timeout=0.1), _IngressKick)
    assert isinstance(harness.broker._ingress.take(timeout=0.1), _IngressStop)
    with pytest.raises(TypeError):
        harness.broker._ingress.put_stop(_IngressStop())
    # The kick wakes the dispatcher through its own free door -- also with
    # no argument, so nothing can ride past the budgets on it either.
    harness.broker._ingress.put_kick()
    with pytest.raises(TypeError):
        harness.broker._ingress.put_kick(_IngressKick())
    # A kick is not an end: the dispatcher sweeps and keeps going.
    assert harness.broker.deliver_next(timeout=0.1) is True
    # ...and the stop sentinel still ends it.
    assert harness.broker.deliver_next(timeout=0.1) is False


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

    def __init__(self) -> None:
        self.close_requests = 0

    def offer(self, item):
        del item
        raise RuntimeError("dispatcher boom")

    def request_close(self) -> None:
        # The real queue marks itself and hands the writer a sentinel; the
        # stub records the ask, which is the part the fatal path exercises.
        self.close_requests += 1


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

    A connection registered after ``close()`` used to get a writer whose
    opening stream was written before any stop could reach it. The refusal
    is now earlier and stronger: no registry entry, no writer, no opening
    stream -- the broker closes the connection itself and ``finished`` is
    set, so the handler thread holding the stream open returns instead of
    waiting on a sentinel that is never coming.
    """
    harness = Harness()
    harness.broker.close(timeout=0.1)
    connection = FakeConnection()
    handle = harness.broker.connect(connection=connection)
    assert handle.finished.is_set()
    assert handle.thread is None
    assert connection.written == b""
    assert connection.closed is True
    assert handle not in harness.broker.connections


# --- a dead dispatcher is a process fact, not one connection's --------------


class _ExplodingConnectionQueue(ConnectionQueue):
    """A per-connection queue whose offer fails every time, for real.

    Unlike the plain ``_ExplodingQueue`` above, this one stands in for the
    queue a *connect* builds, so its constructor takes the budgets the
    broker passes -- and the dispatcher's fan-out dies on its first offer,
    deterministically, with no timing.
    """

    def offer(self, item):
        raise RuntimeError("fan-out is broken")


def _capture_fanout(broker):
    """A FanOut whose second sink records what the broker refuses to say."""
    capture = CapturingSink(name="capture")
    return capture, FanOutSink([broker, capture], process_instance_id=INSTANCE)


def test_a_fatal_dispatcher_closes_connections_and_refuses_the_rest(
    monkeypatch,
) -> None:
    """When the dispatcher dies, everything behind it stops honestly.

    A dispatcher that dies mid-fan-out leaves every existing connection
    open and every later publish pouring into an ingress nobody drains.
    The death is a process-level fact: the existing connections are asked
    to hang up, new connections are refused, later commits fail loudly (so
    the FanOut can disable this sink and tell every other sink why), and
    nothing accumulates behind a dead consumer.
    """
    # The re-raise after the fatal report is the dispatcher's own testimony;
    # it is silenced here because the report, not the traceback, is what
    # this test reads.
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(
        broker_module, "ConnectionQueue", _ExplodingConnectionQueue
    )
    fatal_calls: list[BaseException] = []
    fatal_done = threading.Event()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
        spawn=_RealSpawn().spawn,
    )
    broker.bind_fatal_handler(
        lambda exc: (fatal_calls.append(exc), fatal_done.set())
    )
    broker.start()
    capture, fanout = _capture_fanout(broker)
    handle = broker.connect(connection=FakeConnection())

    def emit(run_id: str) -> None:
        fanout.emit(
            RunStarted(purpose="chat"),
            EventEnvelope(
                ts=0.0,
                run_id=run_id,
                session_id=None,
                step_index=None,
                attempt_id=None,
                node_id=None,
            ),
        )

    emit("r1")
    assert fatal_done.wait(5.0), "fatal handler never ran"
    assert len(fatal_calls) == 1
    assert isinstance(fatal_calls[0], RuntimeError)

    # Every existing connection is asked to hang up, and its writer leaves.
    assert handle.queue.close_requested is True
    assert handle.finished.wait(5.0), "connection was never asked to close"

    # No new connections behind a dead dispatcher.
    connection = FakeConnection()
    refused = broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused not in broker.connections
    assert connection.written == b""
    assert connection.closed is True

    # Later events neither accumulate nor pretend to succeed: the commit
    # fails so the FanOut disables this sink, the ingress stays empty, and
    # the failure is told to the other sinks exactly once -- which is the
    # no-recursion property too, because publishing that notice must not
    # kill the fan-out again.
    stalled = broker._ingress._items.qsize()  # noqa: SLF001
    assert stalled == 0  # the item that killed the fan-out was already taken
    for _ in range(3):
        emit("r1")
    assert broker._ingress._items.qsize() == stalled  # noqa: SLF001
    disabled = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(disabled) == 1


def test_a_fatal_commit_fails_instead_of_delivering_to_no_one(monkeypatch) -> None:
    """The sink says it cannot deliver; it does not silently swallow.

    After the dispatcher has died, a direct commit must raise -- that is
    what lets the FanOutSink disable this sink and record ``sink_disabled``
    for the Run -- and the refusal is stable: it does not depend on which
    thread asks or how many times.
    """
    monkeypatch.setattr(
        broker_module, "ConnectionQueue", _ExplodingConnectionQueue
    )
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=_snapshot(),
        session_is_valid=lambda _sid: True,
    )
    broker.connect(connection=FakeConnection())
    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(
        ts=0.0,
        run_id="r1",
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    prepared = broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=1,
        process_instance_id=INSTANCE,
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    # Queue the item first, so the hand-driven dispatch loop has something
    # to take: the failing fan-out is what publishes the fatal state,
    # exactly as a live dispatcher's death would.
    broker.commit(prepared, sequenced)
    with pytest.raises(RuntimeError):
        broker._dispatch_loop()  # noqa: SLF001
    # The refusal is stable across repeats, so the later commits use their
    # own seqs: what must fail is the fatal check, not the ring's
    # monotonicity.
    for later_seq in (2, 3):
        with pytest.raises(RuntimeError):
            broker.commit(
                prepared,
                replace(sequenced, seq=later_seq),
            )


# --- overflow wakes the dispatcher once, not once per overflow --------------


def test_overflow_kicks_merge_and_stay_bounded() -> None:
    """A pending kick is a bit, not a queue item per overflow.

    Every must-deliver overflow that cannot fit the ingress raises the
    disconnect generation and needs the dispatcher woken -- once. One
    unwoken kick is a liveness bug; one kick *per* overflow is the one
    item that grows without bound behind a stalled dispatcher, and it is
    the control item that would delay the close's own stop sentinel.
    The generation may grow without limit; the number of pending kicks
    may not.
    """
    harness = Harness(max_ingress_frames=2)
    broker = harness.broker
    for _ in range(2):
        harness.emit(RunStarted(purpose="chat"))
    assert broker._ingress._items.qsize() == 2  # noqa: SLF001 - billed data
    for _ in range(1000):
        harness.emit(RunStarted(purpose="chat"))
    assert broker._disconnect_generation == 1000
    # Two billed data items and ONE pending kick, whatever the overflow
    # count.
    assert broker._ingress._items.qsize() == 3  # noqa: SLF001
    # Consuming the kick re-arms it: the next overflow may produce the next
    # kick, still one at a time.
    for _ in range(2):
        assert broker.deliver_next(timeout=0.2) is True  # the data
    assert broker.deliver_next(timeout=0.2) is True  # the kick
    for _ in range(2):
        harness.emit(RunStarted(purpose="chat"))  # the two frames fit again
    for _ in range(10):
        harness.emit(RunStarted(purpose="chat"))  # overflows again
    assert broker._disconnect_generation == 1010
    assert broker._ingress._items.qsize() == 3  # noqa: SLF001 - 2 data + 1 kick
    # And the close's stop sentinel is never stuck behind unbounded control
    # items: the whole queue is bounded by billed data plus the two
    # sentinels.
    assert broker.close(timeout=1.0) is True
    assert broker._ingress._items.qsize() <= 3  # noqa: SLF001



def _deliver_ingress(harness) -> None:
    """Drive the fan-out until the ingress is empty, like a dispatcher would.

    Delivering exactly one item would leave the boundary question unasked:
    the ingress can hold several events behind a registration, and every
    one of them is measured against the connection's boundary.
    """
    while harness.broker.deliver_next(timeout=0.2):
        pass


# --- a transient's seq names its publication, not its delivery --------------


def test_a_transient_published_before_a_connection_registers_is_not_delivered() -> (
    None
):
    """Reconnect does not resurrect a half-finished attempt.

    A transient published before a connection registers is exactly the
    in-flight delta ADR-0013 says a reconnect must not receive: the client
    dropped it on purpose and will be handed the attempt's terminal
    snapshot instead. The registration boundary is the newest *published*
    seq -- transients included -- so a preregistration transient is skipped
    like any other event the replay already covers, while a transient
    published after registration is delivered live, still without an
    ``id:`` on the wire.
    """
    harness = Harness()
    # The delta is in the ingress, but the dispatcher has not run: the
    # registration below happens after the publication.
    harness.emit(BlockDelta(attempt_id="a1", index=0, text="half an attempt"))
    handle = harness.connect()
    _deliver_ingress(harness)
    wire = b"".join(item.wire_bytes() for item in _drain(handle))
    assert b'"event":"block.delta"' not in wire
    # A transient published after the registration is live delivery.
    harness.emit(BlockDelta(attempt_id="a1", index=0, text="still going"))
    _deliver_ingress(harness)
    items = _drain(handle)
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' in wire
    # And a transient never carries a checkpoint: no ``id:``, then or now.
    assert all(not item.id_line for item in items)


def test_mixed_backlogs_respect_the_registration_boundary() -> None:
    """First connection: backlog events are the snapshot's job, not live.

    A first connection primed with the replay ring holds nothing, so every
    backlog event -- replayable or transient -- is behind its published
    boundary and none is delivered live. What arrives afterwards is
    delivered, replayables with their checkpoints, transients without.
    """
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"))  # seq 1, replayable
    harness.emit(RunStarted(purpose="chat"))  # seq 2, replayable
    harness.emit(BlockDelta(attempt_id="a1"))  # seq 3, transient
    handle = harness.connect()
    _deliver_ingress(harness)
    items = _drain(handle)
    assert _ids(items) == []
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' not in wire
    harness.emit(BlockDelta(attempt_id="a1"))  # seq 4, transient
    harness.emit(RunStarted(purpose="chat"))  # seq 5, replayable
    _deliver_ingress(harness)
    items = _drain(handle)
    # Only the replayable event carries a checkpoint.
    assert _ids(items) == [5]
    wire = b"".join(item.wire_bytes() for item in items)
    assert wire.count(b'"event":"block.delta"') == 1


def test_a_reconnect_skips_preregistration_transients_too() -> None:
    """The boundary is the same on a reconnect from a valid checkpoint.

    The replay tail in the opening stream covers the replayable backlog;
    the published boundary covers the transient sitting behind it in the
    ingress. Delivering the ingress after the connection registered must
    therefore produce neither a duplicate replayable nor a resurrected
    delta.
    """
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"))  # seq 1
    harness.emit(RunStarted(purpose="chat"))  # seq 2
    harness.emit(BlockDelta(attempt_id="a1"))  # seq 3, still in ingress
    handle = harness.connect(cursor=_cursor_for(1))
    _deliver_ingress(harness)
    items = _drain(handle)
    # The replay tail, exactly once, and no preregistration transient.
    assert _ids(items) == [2]
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' not in wire


# --- encoding never happens under the broker lock ---------------------------


class _GatedEncoder:
    """A patch encoder the test can park mid-JSON.

    It stands in for the pure, lock-free encoding half of ADR-0015: slow is
    legal, holding the broker lock while slow is not. Every call records
    the ``session_valid`` answer it encoded for.
    """

    def __init__(self, real):
        self._real = real
        self.lock = threading.Lock()
        self.encodings: list[bool] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, snapshot, step, session_valid):
        with self.lock:
            self.encodings.append(session_valid)
        self.entered.set()
        self.release.wait()
        return self._real(snapshot, step, session_valid)

    def calls_for(self, session_valid: bool) -> int:
        with self.lock:
            return self.encodings.count(session_valid)


def test_patch_encoding_does_not_block_event_commits(monkeypatch) -> None:
    """A slow patch is the dispatcher's problem, not the publish path's.

    The dispatcher holds the broker lock while it fans out; encoding a
    patch *inside* that critical section therefore charges every event
    commit for one connection's serialization -- and a Run's own commit
    waits behind a browser tab's payload size. Encoding happens before the
    fan-out critical section, so a commit issued while an encoder is
    parked goes straight through.
    """
    encoder = _GatedEncoder(_patch_frames)
    # Open while the connection registers -- its opening stream goes through
    # the same encoder -- and cleared again so the patch fan-out below parks
    # mid-encode.
    encoder.release.set()
    monkeypatch.setattr(broker_module, "_patch_frames", encoder)
    harness = Harness(spawn=_RealSpawn())
    handle = harness.connect()
    _drain(handle)
    harness.broker.publish_state_patch(_snapshot(state_revision=1))
    encoder.release.clear()
    dispatcher_done = threading.Event()

    def drive() -> None:
        harness.broker.deliver_next(timeout=2.0)
        dispatcher_done.set()

    dispatcher = threading.Thread(target=drive, daemon=True)
    dispatcher.start()
    try:
        assert encoder.entered.wait(2.0), "patch encoding never started"
        commit_done = threading.Event()

        def commit() -> None:
            harness.emit(RunStarted(purpose="chat"))
            commit_done.set()

        emitter = threading.Thread(target=commit, daemon=True)
        emitter.start()
        # The encoder is still parked; the commit must not be queued behind
        # it.
        assert commit_done.wait(5.0), "commit waited for the patch encoding"
    finally:
        encoder.release.set()
        dispatcher.join(timeout=5.0)
        dispatcher_done.wait(2.0)
    assert dispatcher_done.is_set()


class _GatedCost:
    """The publisher's lock-free cost probe, parked by the test.

    It stands in at the exact point the publish path measures a patch's
    ingress cost: the Step is already captured when it runs, so parking here
    holds the whole capture-to-commit window open while the rest of the
    world moves. The barrier is the rendezvous -- the parked publisher is
    the first party, the test that has finished moving the world is the
    second -- so the interleaving is decided by the test, not the scheduler.
    """

    def __init__(self, real, barrier: threading.Barrier) -> None:
        self._real = real
        self._barrier = barrier
        self.parked = threading.Event()

    def __call__(self, snapshot, step) -> int:
        if self.parked.is_set():
            # Only the first publish parks: the one the test started before
            # moving the world. Any later cost measurement -- the test's own
            # newer publish, or a recapture lap -- passes straight through.
            return self._real(snapshot, step)
        self.parked.set()
        self._barrier.wait()
        return self._real(snapshot, step)


def _patches_received(handle) -> list[dict]:
    """Every state_patch frame this connection has been handed, in order."""
    patches = []
    for item in _drain(handle):
        wire = item.wire_bytes()
        if b"event: state_patch" in wire:
            patches.append(_decode_patch(wire))
    return patches


def _park_a_publisher(
    harness, monkeypatch, stale_snapshot
) -> tuple[threading.Barrier, threading.Thread, list[bool]]:
    """Start one publisher and hold it mid-encode, after its capture.

    Returns the gate's barrier, the publisher thread, and the list its
    return answer lands in. The test moves the world while it is parked and
    then joins the barrier as the second party; a failing test aborts the
    barrier instead, so the parked thread can never outlive the test that
    parked it.
    """
    barrier = threading.Barrier(2)
    gate = _GatedCost(broker_module._patch_cost, barrier)
    monkeypatch.setattr(broker_module, "_patch_cost", gate)
    answers: list[bool] = []

    def publisher() -> None:
        answers.append(harness.broker.publish_state_patch(stale_snapshot))

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    assert gate.parked.wait(5.0), "the publisher never reached its encoding"
    return barrier, thread, answers


def test_stale_step_never_publishes_after_newer_revision(monkeypatch) -> None:
    """A patch encoded while the world moved must not ship the old Step.

    The publisher captures its Step under the lock and measures the patch's
    cost outside it. A progress-advancing domain event and a newer
    authoritative snapshot both land inside that window; a publisher that
    commits unconditionally on return would publish the captured-old Step
    under the new revision, *after* the newer facts, and move ``_latest``
    backwards past them. The captured facts must still hold at the
    linearization point, and a snapshot the authority has already overtaken
    must be refused -- patches are absolute replacements, so publishing one
    is how an old domain fact ends up behind a newer one.
    """
    harness = Harness()
    handle = harness.connect()
    _drain(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(StepStarted(step_index=0), run_id="r1")
    active = ActiveRunSummary(
        run_id="r1",
        purpose="chat",
        gateway="web",
        phase="running",
        session_id=None,
        prompt_preview="hi",
        started_at=None,
        recording_state=None,
    )
    overtaken = _snapshot(
        state_revision=1, coordinator_state="running", active_run=active
    )
    newer = _snapshot(
        state_revision=2, coordinator_state="running", active_run=active
    )
    barrier, thread, answers = _park_a_publisher(harness, monkeypatch, overtaken)
    try:
        harness.emit(StepStarted(step_index=1), run_id="r1")
        assert harness.broker.publish_state_patch(newer), (
            "the newer publish must not wait behind the parked one"
        )
        barrier.wait(timeout=5.0)  # the interleave is done: release it
    except BaseException:
        barrier.abort()
        raise
    thread.join(timeout=5.0)
    assert answers == [False], (
        "a snapshot the authority had overtaken was published anyway"
    )
    assert harness.broker._latest is newer, "_latest moved backwards"
    _run_dispatcher(harness)
    patches = _patches_received(handle)
    assert patches, "no state patch was published at all"
    revisions = [patch["state_revision"] for patch in patches]
    assert revisions == sorted(revisions), f"patch order regressed: {revisions}"
    seen_newer = False
    for patch in patches:
        if patch["state_revision"] == newer.state_revision:
            seen_newer = True
        if seen_newer:
            step = patch["step"]
            assert step is not None and step["step_index"] == 1, (
                f"a stale Step shipped after the newer facts: {patch}"
            )
    assert seen_newer


def test_connection_never_regresses_to_stale_absolute_replacement(
    monkeypatch,
) -> None:
    """Applying the patches a connection receives, in order, never goes back.

    A patch is an absolute replacement (ADR-0025): whatever it carries
    becomes the connection's whole view of the state. A stale Step published
    after newer facts would walk the connection backwards -- the exact
    outcome the revision exists to prevent -- so the patch stream a
    connection reads must advance monotonically and end on the latest
    authoritative facts, whatever the publisher's encoding window held.
    """
    harness = Harness()
    handle = harness.connect()
    _drain(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(StepStarted(step_index=0), run_id="r1")
    active = ActiveRunSummary(
        run_id="r1",
        purpose="chat",
        gateway="web",
        phase="running",
        session_id=None,
        prompt_preview="hi",
        started_at=None,
        recording_state=None,
    )
    overtaken = _snapshot(
        state_revision=1, coordinator_state="running", active_run=active
    )
    newer = _snapshot(
        state_revision=2, coordinator_state="running", active_run=active
    )
    barrier, thread, _answers = _park_a_publisher(
        harness, monkeypatch, overtaken
    )
    try:
        harness.emit(StepStarted(step_index=1), run_id="r1")
        assert harness.broker.publish_state_patch(newer)
        barrier.wait(timeout=5.0)
    except BaseException:
        barrier.abort()
        raise
    thread.join(timeout=5.0)
    _run_dispatcher(harness)
    patches = _patches_received(handle)
    assert patches, "the connection saw no state patch"
    revisions = [patch["state_revision"] for patch in patches]
    assert revisions == sorted(revisions), (
        f"absolute replacements walked the connection backwards: {revisions}"
    )
    final = patches[-1]
    assert final["state_revision"] == harness.broker._latest.state_revision, (
        "the connection was left on a state that is no longer authoritative"
    )
    assert final["step"] is not None and final["step"]["step_index"] == 1, (
        f"the connection was left on a stale Step: {final}"
    )


def test_a_patch_is_encoded_once_per_session_validity(monkeypatch) -> None:
    """One state, one encoding per distinct answer -- not per connection.

    A patch is absolute: what two valid connections receive differs in
    nothing, and what a valid and an invalid connection receive differs in
    exactly one field. Encoding per connection would charge N-1 redundant
    serializations -- with the broker lock held, under the old shape -- so
    the fan-out encodes at most one frame per distinct session-validity
    result and hands out references.
    """
    encoder = _GatedEncoder(_patch_frames)
    # Released throughout: connect's opening stream goes through the same
    # encoder now, and the counting -- not the parking -- is what this test
    # pins.
    encoder.release.set()
    monkeypatch.setattr(broker_module, "_patch_frames", encoder)
    harness = Harness()
    harness.connect(session_id="s1")
    harness.connect(session_id="s1")
    harness.connect(session_id="not-s1")  # the invalid answer
    encoder.encodings.clear()
    harness.broker.publish_state_patch(_snapshot(state_revision=2))
    _deliver_ingress(harness)
    assert encoder.calls_for(True) == 1
    assert encoder.calls_for(False) == 1


def test_connect_encodes_its_startup_outside_the_lock(monkeypatch) -> None:
    """The opening stream is encoded lock-free, and honestly re-captured.

    A connect that encoded its startup patch inside the broker lock would
    block every commit for the length of its serialization. Encoding runs
    outside; the registration critical section then verifies the captured
    snapshot is still current and, if events moved the world meanwhile,
    recaptures and re-encodes -- so the stream the client finally gets
    names the state that was authoritative *at registration*.
    """
    encoder = _GatedEncoder(_patch_frames)
    monkeypatch.setattr(broker_module, "_patch_frames", encoder)
    harness = Harness(spawn=_RealSpawn())
    connect_done = threading.Event()
    descriptor: list[bytes] = []

    def do_connect() -> None:
        handle = harness.broker.connect(connection=FakeConnection())
        descriptor.append(
            b"".join(item.wire_bytes() for item in handle.startup)
        )
        connect_done.set()

    connector = threading.Thread(target=do_connect, daemon=True)
    connector.start()
    try:
        assert encoder.entered.wait(2.0), "startup encoding never started"
        # While the startup is parked mid-encoding, the world moves: the
        # authoritative snapshot advances. A connect holding the lock here
        # would deadlock the publish; a connect that merely encoded outside
        # but registered the stale frame would ship revision 0.
        assert harness.broker.publish_state_patch(
            _snapshot(state_revision=1)
        ), "publish waited for the startup encoding"
    finally:
        encoder.release.set()
        assert connect_done.wait(5.0), "connect never finished"
        connector.join(timeout=5.0)
    wire = descriptor[0]
    assert b'"state_revision":1' in wire
    assert b'"state_revision":0' not in wire


# --- the publish path never touches a connection ----------------------------

class _ProbingQueue:
    """A queue stand-in that refuses to be touched while it is armed.

    The publish path -- ``commit`` and ``publish_state_patch`` -- must cost
    the same whether one connection exists or a thousand: any traversal of
    the connection registry from the emitting thread, which is what closing
    a connection from there requires, trips this stub instead of silently
    charging the Run for the crowd.
    """

    def __init__(self) -> None:
        self.armed = True
        self.close_requests = 0

    def request_close(self, retry_ms: int | None = None) -> None:
        if self.armed:
            raise AssertionError("a connection was touched from the publish path")
        self.close_requests += 1

    def offer(self, item):
        del item
        return OfferOutcome(kind="accepted")

    def stop(self) -> None:
        return None


def _run_dispatcher(harness) -> None:
    """Drive the dispatcher until the ingress is empty, kick included."""
    while harness.broker.deliver_next(timeout=0.05):
        pass


def _commit_direct(harness, seq: int, run_id: str):
    """One replayable event through ``commit`` alone, bypassing the fan-out.

    The fan-out catches a sink's commit failure and disables the sink; the
    behaviour under test is ``commit`` itself, so the event is committed by
    hand and anything it raises reaches the test.
    """
    envelope = EventEnvelope(
        ts=0.0,
        run_id=run_id,
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        envelope=envelope,
        payload=RunStarted(purpose="chat"),
        trace_policy="persist",
        replayable=True,
    )
    prepared = harness.broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=seq,
        process_instance_id=INSTANCE,
        envelope=envelope,
        payload=unsequenced.payload,
        trace_policy="persist",
        replayable=True,
    )
    harness.broker.commit(prepared, sequenced)
    return sequenced


def test_ingress_overflow_never_walks_the_connections_from_commit() -> None:
    """commit stays O(1) in connections even when it must refuse liveness.

    Six connections hold queues that throw the moment the publish path
    touches them. Overflowing the ingress with an undroppable event from
    the emitting thread must therefore return without touching any of them;
    asking them to hang up is the dispatcher's work, outside the publish
    critical section.
    """
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    probes: list[_ProbingQueue] = []
    for _ in range(6):
        handle = harness.connect()
        handle.queue = _ProbingQueue()
        probes.append(handle.queue)
    # The ingress is occupied by a first event, so the next one overflows.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    overflow = _commit_direct(harness, seq=2, run_id="r2")
    # Ring first: the fact is recoverable even though liveness was refused.
    assert harness.broker._ring.high_water_seq() == overflow.seq
    # commit returned without touching a single queue -- the O(1) claim.
    assert all(probe.close_requests == 0 for probe in probes)
    # The dispatcher closes every connection that was registered before the
    # overflow, and no queue is touched until it runs.
    for probe in probes:
        probe.armed = False
    _run_dispatcher(harness)
    assert all(probe.close_requests == 1 for probe in probes)


def test_a_connection_registered_after_the_overflow_is_not_closed() -> None:
    """The generation answers "who was there when it happened".

    A connection that registers after the overflow recovered its starting
    state from the snapshot and the ring; closing it would punish the one
    client that cannot be missing anything.
    """
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    handle = harness.connect()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    _commit_direct(harness, seq=2, run_id="r2")  # overflows the ingress
    late = harness.connect(cursor=_cursor_for(0))
    _run_dispatcher(harness)
    assert handle.queue.close_requested is True
    assert late.queue.close_requested is False


def test_patch_overflow_never_walks_the_connections_from_the_publisher() -> None:
    """The state-patch overflow rides the same mechanism, not a second path.

    A refused patch moves the authoritative snapshot and raises the
    generation; the publisher itself touches no queue, and the dispatcher
    closes exactly the connections that predate the incident.
    """
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    probes: list[_ProbingQueue] = []
    for _ in range(4):
        handle = harness.connect()
        handle.queue = _ProbingQueue()
        probes.append(handle.queue)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # occupies ingress
    accepted = harness.broker.publish_state_patch(_snapshot(state_revision=5))
    assert accepted is False
    # The authoritative snapshot moved anyway (a reconnect sees revision 5),
    # and no connection was touched from the publishing thread.
    assert harness.broker._latest.state_revision == 5
    assert all(probe.close_requests == 0 for probe in probes)
    # A connection registered after the refused patch already holds the new
    # state, so the sweep must leave it alone.
    late = harness.connect()
    for probe in probes:
        probe.armed = False
    _run_dispatcher(harness)
    assert all(probe.close_requests == 1 for probe in probes)
    assert late.queue.close_requested is False


# --- a trailing transient is published, but never a checkpoint --------------


def _gap_reason(harness, cursor: str) -> str:
    handle = harness.connect(cursor=cursor)
    notice = _wire_containing(handle, b"replay_gap")
    match = re.search(rb'"gap_reason":"([a-z_]+)"', notice)
    assert match is not None
    return match.group(1).decode()


def test_a_trailing_transient_seq_is_malformed_not_ahead() -> None:
    """A transient consumes a seq without minting a checkpoint.

    The published high water knows the transient was published, so a cursor
    naming it is refused as ``malformed`` (published, never issued) -- not
    ``ahead``, which would claim this process never sent something the
    client may well be holding. One seq past the published high water is
    still ``ahead``, and the next real checkpoint is still ``valid``.
    """
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # seq 1, replayable
    harness.emit(BlockDelta(text="x"), run_id="r1")  # seq 2, transient
    assert _gap_reason(harness, _cursor_for(2)) == "malformed"
    assert _gap_reason(harness, _cursor_for(3)) == "ahead"
    # The next replayable event lands on the transient's successor and is a
    # checkpoint like any other.
    third = harness.emit(RunStarted(purpose="chat"), run_id="r2")
    assert third.seq == 3
    handle = harness.connect(cursor=_cursor_for(1))
    assert _ids(_drain(handle)) == [3]
    # Resuming *from* 3 has nothing to catch up, as any checkpoint.
    handle = harness.connect(cursor=_cursor_for(3))
    assert _ids(_drain(handle)) == []
    # The transient never entered the ring and never got an id:.
    assert len(harness.broker._ring) == 2
