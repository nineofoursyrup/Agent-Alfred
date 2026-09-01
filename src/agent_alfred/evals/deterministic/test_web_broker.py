"""SSEBroker: reconnect without holes, per-connection backpressure, overflow."""

from __future__ import annotations

import gc
import json
import queue
import re
import threading
import time
import weakref
from dataclasses import replace
from typing import get_args

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_broker_test_helpers import (
    GatedThreads,
    GatedWriteConnection,
    Harness,
    RealThreadSpawner,
    cursor_for,
    drain_connection,
    drain_dispatcher,
    replay_ids,
    runtime_snapshot,
    user_message_with,
)
from agent_alfred.events import (
    AttemptCommitted,
    AttemptStarted,
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
    event_json_default,
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
    ConnectionWriter,
    FakeConnection,
    OfferOutcome,
)
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.progress import (
    AttemptTerminal,
    RunProgress,
    StepProjection,
)
from agent_alfred.gateway.web.replay import ReplayBatch, ReplayRing
from agent_alfred.gateway.web.state import SNAPSHOT_TEXT_LIMIT
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.session_validity import SessionValidity

INSTANCE = "inst-test"

# --- connect and reconnect -------------------------------------------------


def test_transport_notice_code_is_the_decided_two_value_closed_set() -> None:
    assert set(get_args(frames.TransportNoticeCode)) == {
        "replay_gap",
        "deltas_dropped",
    }


def test_a_preflight_proof_is_the_only_session_fact_used_to_open_the_stream() -> None:
    answers = iter(("valid", "unavailable"))
    queries: list[str | None] = []

    def validity(session_id: str | None):
        queries.append(session_id)
        return next(answers)

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=validity,
    )
    proof = broker.preflight_session("s1")

    connection = FakeConnection()
    handle = broker.connect(
        connection=connection,
        session_id="s1",
        admission=proof,
    )

    assert queries == ["s1"]
    assert handle.session_valid is True
    assert connection.writes[0] == b"retry: 1000\n\n"
    assert connection.writes[1].startswith(b"id: inst-test:")
    assert b"event: state_patch" in connection.writes[2]


def test_connect_closes_the_connection_when_session_validation_raises() -> None:
    """A handed-off HTTP socket cannot escape a failed Session lookup."""
    failure = RuntimeError("injected Session lookup failure")

    def fail_session_lookup(_session_id: str | None) -> bool:
        raise failure

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=fail_session_lookup,
    )
    connection = FakeConnection()

    with pytest.raises(RuntimeError) as raised:
        broker.connect(connection=connection, session_id="s1")

    assert raised.value is failure
    assert connection.closed is True
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0


def test_connect_cleans_the_unowned_handle_when_startup_encoding_raises(
    monkeypatch,
) -> None:
    """Startup frames and the socket die together when encoding cannot finish."""
    failure = ValueError("injected startup encoding failure")
    created = []
    real_handle = broker_module.ConnectionHandle

    def capture_handle(**kwargs):
        handle = real_handle(**kwargs)
        created.append(handle)
        return handle

    def fail_patch_encoding(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(broker_module, "ConnectionHandle", capture_handle)
    monkeypatch.setattr(broker_module, "_patch_frames", fail_patch_encoding)
    harness = Harness()
    connection = FakeConnection()

    with pytest.raises(ValueError) as raised:
        harness.connect(connection=connection, cursor=cursor_for(0))

    assert raised.value is failure
    assert len(created) == 1
    handle = created[0]
    assert connection.closed is True
    assert handle.finished.is_set()
    assert handle.thread is None
    assert handle.writer is None
    assert handle.queue.current_cost == frames.FrameCost(
        frames=0, encoded_bytes=0
    )
    assert harness.broker.connections == ()
    assert harness.broker.registrations_in_flight == 0
    assert harness.spawn.targets == []

    handle_ref = weakref.ref(handle)
    created.clear()
    del handle
    del raised
    failure.__traceback__ = None
    gc.collect()
    assert handle_ref() is None


def test_the_first_connection_gets_retry_a_reseed_and_the_snapshot() -> None:
    harness = Harness()
    harness.emit_many(3)
    handle = harness.connect()
    items = drain_connection(handle)
    assert items[0].wire_bytes() == b"retry: 1000\n\n"
    assert items[1].wire_bytes() == b"id: inst-test:3\n\n"
    # No gap: a first connection is not a degradation.
    assert not any(b"replay_gap" in item.wire_bytes() for item in items)
    assert any(b"event: state_patch" in item.wire_bytes() for item in items)


def test_reconnect_replays_exactly_the_missing_tail() -> None:
    harness = Harness()
    harness.emit_many(5)
    handle = harness.connect(cursor=cursor_for(2))
    items = drain_connection(handle)
    assert replay_ids(items) == [3, 4, 5]
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
        handle = harness.connect(cursor=cursor_for(cursor_seq))
        ids = replay_ids(drain_connection(handle))
        assert ids == list(range(cursor_seq + 1, 7))
        assert len(set(ids)) == len(ids), f"duplicates from {cursor_seq}: {ids}"


def test_a_rolled_out_ring_answers_a_gap_not_a_silent_resume() -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
        )
    )
    harness.emit_many(5)
    handle = harness.connect(cursor=cursor_for(1))
    items = drain_connection(handle)
    notice = next(item for item in items if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"too_old"' in notice.wire_bytes()
    assert b'"requested_seq":1' in notice.wire_bytes()
    # Nothing was replayed from a ring that no longer holds it.
    assert replay_ids(items) == []


def test_commit_does_not_walk_a_large_evicted_replay_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ring = ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20))
    retired_refs: list[weakref.ReferenceType[PreparedFrames]] = []
    for seq in range(1, 65):
        entry = frames.measured_frames(
            seq=seq,
            frames=(b"data: small",),
            id_line=b"id: inst-test:%d\n" % seq,
            replayable=True,
        )
        ring.append(entry)
        if seq <= 48:
            retired_refs.append(weakref.ref(entry))
    del entry
    harness = Harness(ring=ring)
    payload = RunStarted(purpose="chat")
    prepared = frames.measured_frames(
        frames=tuple(b"data: large" for _ in range(48)), replayable=True
    )
    harness.fanout._seq = 65
    monkeypatch.setattr(harness.broker, "prepare", lambda _event: prepared)
    cost_calls = 0
    original_ingress_cost = PreparedFrames.ingress_cost

    def counted_ingress_cost(item: PreparedFrames) -> frames.FrameCost:
        nonlocal cost_calls
        cost_calls += 1
        return original_ingress_cost(item)

    monkeypatch.setattr(PreparedFrames, "ingress_cost", counted_ingress_cost)
    original_release = ring._entries.release_retired
    released_outside_publish_lock = False

    def checked_release(start: int, count: int, through_seq: int) -> None:
        nonlocal released_outside_publish_lock
        acquired = harness.broker._lock.acquire(blocking=False)
        assert acquired, "references were released under the broker lock"
        fanout_acquired = harness.fanout._lock.acquire(blocking=False)
        assert fanout_acquired, "references were released under the FanOut lock"
        harness.fanout._lock.release()
        harness.broker._lock.release()
        released_outside_publish_lock = True
        original_release(start, count, through_seq)

    monkeypatch.setattr(ring._entries, "release_retired", checked_release)

    harness.emit(payload, run_id="r65")

    # One ring admission plus one ingress admission; none per displaced item.
    assert cost_calls == 2
    assert released_outside_publish_lock is True
    assert all(reference() is None for reference in retired_refs)
    retained_cells = [
        cell.entry
        for cell in ring._entries._entries
        if cell is not None and cell.entry is not None
    ]
    assert (
        frames.FrameCost(
            frames=sum(len(entry.frames) for entry in retained_cells),
            encoded_bytes=sum(entry.byte_size for entry in retained_cells),
        )
        == ring.current_cost
    )
    assert [entry.seq for entry in ring.entries_after(48) or ()] == list(range(49, 66))


def test_retirement_cleanup_failure_is_fatal_after_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "retirement-secret-must-not-be-published"
    ring = ReplayRing(budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20))
    harness = Harness(ring=ring)
    capture = CapturingSink(name="capture")
    fanout = FanOutSink(
        [harness.broker, capture], process_instance_id=INSTANCE
    )
    def fail_release(start: int, count: int, through_seq: int) -> None:
        del start, count, through_seq
        raise RuntimeError(secret)

    monkeypatch.setattr(
        ring._entries, "release_retired", fail_release
    )
    envelope = EventEnvelope(0.0, "r1", None, None, None, None)

    first = fanout.emit(RunStarted(purpose="chat"), envelope)
    committed = fanout.emit(RunStarted(purpose="chat"), envelope)
    fanout.emit(
        RunStarted(purpose="chat"),
        replace(envelope, run_id="r2"),
    )

    assert (first.seq, committed.seq) == (1, 2)
    assert any(event.seq == committed.seq for event in capture.events)
    assert harness.broker._fatal is not None  # noqa: SLF001
    assert harness.broker._stopping is True  # noqa: SLF001
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1, "process fatal is notified once across Runs"
    assert dict(notices[0].payload.detail) == {
        "sink": harness.broker.name,
        "stage": "post_commit",
    }
    assert secret not in json.dumps(capture.events, default=event_json_default)


def test_the_four_illegal_cursors_each_name_their_reason() -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
        )
    )
    harness.emit_many(4)
    cases = {
        "garbage": "malformed",
        "other-process:2": "instance_mismatch",
        cursor_for(1): "too_old",
        cursor_for(99): "ahead",
    }
    for cursor, reason in cases.items():
        handle = harness.connect(cursor=cursor)
        items = drain_connection(handle)
        notice = next(item for item in items if b"replay_gap" in item.wire_bytes())
        assert f'"gap_reason":"{reason}"' in notice.wire_bytes().decode()


def test_the_gap_notice_distinguishes_no_run_from_unrecoverable_run() -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
        )
    )
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit_many(3, start=10)
    # No active Run in the snapshot: absent.
    handle = harness.connect(cursor="garbage")
    assert b'"current_run_state":"absent"' in _wire_containing(handle, b"replay_gap")

    active = runtime_snapshot(
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
    for item in drain_connection(handle):
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
        runtime_snapshot(
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
        AttemptCommitted(attempt_id="a1", stop_reason="end_turn", duration_ms=5),
        run_id=run_id,
    )
    harness.emit(RunFinished(outcome="completed"), run_id=run_id)


def _recording_pending_snapshot(**kwargs) -> RuntimeSnapshot:
    return runtime_snapshot(
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
            current_step=2,
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
    progress.note_step_started("r1")
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
    frozen = progress.projection(2)
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
    assert progress.projection(None) is None
    progress.note_step_started("r2")
    assert progress.projection(0) == StepProjection(step_index=0, attempts=())


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
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=2))
    idle = _patch_payload(harness.connect(session_id="s1"))
    assert idle["active_run"] is None
    assert idle["step"] is None

    # A new Run admitted: the old Run's frozen summary must not show under it.
    harness.broker.publish_state_patch(
        runtime_snapshot(
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
    slow = harness.connect(budget=frames.FrameBudget(8, 8 * 1024 * 1024))
    other = harness.connect(budget=frames.FrameBudget(512, 8 * 1024 * 1024))
    started = time.monotonic()
    for _ in range(80):
        harness.emit(RunStarted(purpose="chat"), run_id="r1")
        harness.deliver()
    elapsed = time.monotonic() - started
    # Emitting is O(1) in connections: a wedged tab costs it nothing.
    assert elapsed < 2.0
    other_ids = replay_ids(drain_connection(other))
    assert other_ids == list(range(1, 81))
    # The slow one was closed rather than allowed to stall the process.
    assert slow.queue.close_requested is True


def test_dropped_transients_are_reported_once_the_connection_recovers() -> None:
    harness = Harness()
    # A queue with room for exactly two transient frames: the third is
    # backpressure, and transients are what gives way.
    handle = harness.connect(budget=frames.FrameBudget(2, 8 * 1024 * 1024))
    for _ in range(3):
        harness.emit(BlockDelta(text="x"), run_id="r1")
        harness.deliver()
    # Two were accepted, one was dropped. Drain past them until the queue
    # has room again.
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    items = drain_connection(handle)
    notice = next(
        (item for item in items if b"deltas_dropped" in item.wire_bytes()),
        None,
    )
    assert notice is not None
    assert b'"count":1' in notice.wire_bytes()
    # Reported once: the next delivery does not repeat the count.
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.deliver()
    assert not any(
        b"deltas_dropped" in item.wire_bytes() for item in drain_connection(handle)
    )


def test_drop_recovery_keeps_the_new_attempt_and_its_following_delta() -> None:
    harness = Harness()
    handle = harness.connect(budget=frames.FrameBudget(2, 8 * 1024 * 1024))
    drain_connection(handle)

    for text in ("queued-1", "queued-2", "missed-1", "missed-2"):
        harness.emit(BlockDelta(attempt_id="old", text=text), run_id="r1")
        harness.deliver()
    drain_connection(handle)

    harness.emit(
        AttemptStarted(attempt_id="new", streamed=True),
        run_id="r1",
    )
    harness.deliver()
    recovery = drain_connection(handle)
    assert len(recovery) == 2

    state: dict[str, str | None] = {"attempt_id": "old", "text": "stale"}

    def consume(item: PreparedFrames) -> None:
        payload = _decode_patch(item.wire_bytes())
        if payload.get("code") == "deltas_dropped":
            state.update(attempt_id=None, text="")
            return
        event = payload.get("event")
        event_payload = payload.get("payload", {}).get("payload", {})
        if event == "attempt.started":
            state.update(attempt_id=event_payload["attempt_id"], text="")
        elif (
            event == "block.delta"
            and event_payload.get("attempt_id") == state["attempt_id"]
        ):
            state["text"] = str(state["text"]) + event_payload["text"]

    for item in recovery:
        consume(item)
    assert state == {"attempt_id": "new", "text": ""}

    harness.emit(BlockDelta(attempt_id="new", text="kept"), run_id="r1")
    harness.deliver()
    for item in drain_connection(handle):
        consume(item)
    assert state == {"attempt_id": "new", "text": "kept"}


def test_replayable_recovery_that_cannot_fit_closes_and_remains_replayable() -> None:
    harness = Harness(connection_budget=frames.FrameBudget(2, 1 << 20))
    handle = harness.connect()
    drain_connection(handle)

    for text in ("queued-1", "queued-2", "missed"):
        harness.emit(BlockDelta(attempt_id="old", text=text), run_id="r1")
        harness.deliver()
    # Keep one queued physical frame, leaving room for either the notice or
    # the replayable candidate, but not the atomic pair.
    handle.queue.take(timeout=0)

    event = harness.emit(
        AttemptStarted(attempt_id="new", streamed=True),
        run_id="r1",
    )
    harness.deliver()

    assert handle.queue.close_requested is True
    remaining = drain_connection(handle)
    wire = b"".join(
        item.wire_bytes()
        for item in remaining
        if isinstance(item, PreparedFrames)
    )
    assert b"attempt.started" not in wire
    assert b"deltas_dropped" not in wire

    fresh = harness.connect(cursor=cursor_for(0))
    assert replay_ids(drain_connection(fresh)) == [event.seq]


def test_a_transient_missed_by_the_ingress_costs_liveness_only() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
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
    assert any(
        b"deltas_dropped" in item.wire_bytes() for item in drain_connection(handle)
    )


def test_a_replayable_that_misses_the_ingress_closes_live_connections() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
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
    fresh = harness.connect(cursor=cursor_for(event.seq - 1))
    assert replay_ids(drain_connection(fresh)) == [event.seq]


def test_a_transport_notice_owns_no_seq_and_never_enters_the_ring() -> None:
    harness = Harness()
    harness.emit_many(2)
    before = harness.broker._ring.high_water_seq()
    handle = harness.connect(cursor="garbage")
    items = drain_connection(handle)
    notice = next(item for item in items if b"replay_gap" in item.wire_bytes())
    assert b"\nid: " not in notice.wire_bytes()
    assert harness.broker._ring.high_water_seq() == before


# --- the opening stream is the writer's, never the queue's ------------------


def test_a_replay_tail_over_the_connection_budget_still_arrives_whole() -> None:
    """The opening sequence must not buy its way past the queue's budgets.

    A reconnecting client's exact replay tail can be larger than the
    connection's own frame budget; priming it into the queue made the queue
    hold more than it was ever allowed to the moment it existed. A bounded
    writer cursor still delivers the tail whole -- in order, with no hole --
    while every individual batch stays within the connection budget.
    """
    harness = Harness(
        ring=ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)),
        connection_budget=frames.FrameBudget(8, 1 << 20),
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=cursor_for(0))
    # The queue itself holds nothing: the opening stream belongs to the
    # writer, and the queue's budget has not been spent before it began.
    assert handle.queue.current_frames == 0
    assert handle.queue.current_bytes == 0
    # retry -> re-seed -> snapshot -> the exact tail, in order.
    opening = drain_connection(handle)
    assert opening[0].wire_bytes() == b"retry: 1000\n\n"
    assert opening[1].wire_bytes() == b"id: inst-test:0\n\n"
    assert any(b"event: state_patch" in frame for frame in opening[2].frames)
    assert replay_ids(opening) == list(range(1, 21))
    # The batches use the ring's immutable frames by reference -- not a
    # second, re-encoded copy of them. The deterministic writer seam has
    # already released each batch before returning this observation.
    stored = harness.broker._ring.entries_after(0)
    assert len(opening) == 3 + len(stored)
    assert all(a is b for a, b in zip(opening[3:], stored))


def test_startup_replay_uses_the_capacity_left_by_queued_live_traffic() -> None:
    """A frozen event makes progress in smaller slices instead of backing off."""
    harness = Harness(
        connection_budget=frames.FrameBudget(2, 1 << 20),
        max_frame_bytes=384,
    )
    event = harness.emit(RunStarted(purpose="x" * 100), run_id="r1")
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored[0].frames) == 3

    handle = harness.connect(cursor=cursor_for(0))
    live = frames.measured_frames(
        seq=event.seq + 1,
        frames=(b"data: live",),
    )
    assert handle.queue.offer(live).kind == "accepted"

    opening = drain_connection(handle)
    replay = [
        item
        for item in opening
        if isinstance(item, PreparedFrames) and item.seq == event.seq
    ]
    assert [len(item.frames) for item in replay] == [1, 1, 1]
    assert [item.id_line for item in replay] == [b"", b"", stored[0].id_line]
    assert b"".join(item.wire_bytes() for item in replay) == stored[0].wire_bytes()
    assert opening[-1] is live
    assert all(item.wire_bytes() != b"retry: 3000\n\n" for item in opening)
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_live_offers_cannot_steal_capacity_between_startup_slices() -> None:
    """Release-to-fetch handoff keeps one next record protected by bytes."""
    budget = frames.FrameBudget(4, 650)
    harness = Harness(connection_budget=budget, max_frame_bytes=384)
    event = harness.emit(RunStarted(purpose="x" * 100), run_id="r1")
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored[0].frames) == 3
    handle = harness.connect(cursor=cursor_for(0))

    first_live = frames.measured_frames(frames=(b"a" * 118,))
    inserted = tuple(
        frames.measured_frames(frames=(bytes([98 + index]) * 48,))
        for index in range(2)
    )
    assert handle.queue.offer(first_live).kind == "accepted"

    original_release = handle.queue.release_startup
    releases = 0
    outcomes: list[str] = []

    def release_then_offer(cost: frames.FrameCost) -> None:
        nonlocal releases
        original_release(cost)
        if releases < len(inserted):
            # This is the exact old race window: the current slice is gone,
            # but the next fetch has not started. No sleep or scheduler luck.
            outcomes.append(handle.queue.offer(inserted[releases]).kind)
        releases += 1

    handle.queue.release_startup = release_then_offer  # type: ignore[method-assign]
    replay: list[PreparedFrames] = []
    peak = frames.FrameCost(0, 0)

    def observe(item: PreparedFrames) -> None:
        nonlocal peak
        if item.seq == event.seq:
            replay.append(item)
        current = handle.queue.current_cost
        assert budget.fits(current)
        peak = frames.FrameCost(
            max(peak.frames, current.frames),
            max(peak.encoded_bytes, current.encoded_bytes),
        )

    assert handle.writer is not None
    assert handle.writer.deliver_startup(observe) is True
    assert outcomes == ["accepted", "accepted"]
    assert [len(item.frames) for item in replay] == [1, 1, 1]
    assert [item.id_line for item in replay] == [b"", b"", stored[0].id_line]
    assert budget.fits(peak)
    assert [handle.queue.take(0) for _ in range(3)] == [
        first_live,
        *inserted,
    ]
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_startup_finishes_its_partial_checkpoint_before_overflow_backoff() -> None:
    """A live overflow cannot strand a frozen logical event mid-checkpoint."""
    harness = Harness(
        connection_budget=frames.FrameBudget(2, 1 << 20),
        max_frame_bytes=384,
    )
    frozen = harness.emit(RunStarted(purpose="x" * 100), run_id="frozen")
    harness.deliver()
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored[0].frames) == 3

    class OverflowOnFirstReplay(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.handle = None
            self.live = None
            self.triggered = False

        def write(self, data: bytes) -> None:
            if b'"chunk_index":0' in data and not self.triggered:
                self.triggered = True
                self.live = harness.emit(
                    RunStarted(purpose="after-overflow"), run_id="live"
                )
                harness.deliver()
                assert self.handle is not None
                assert self.handle.queue.close_requested is True
            super().write(data)

    connection = OverflowOnFirstReplay()
    handle = harness.connect(connection=connection, cursor=cursor_for(0))
    connection.handle = handle
    queued_live = frames.measured_frames(frames=(b"data: queued-live",))
    assert handle.queue.offer(queued_live).kind == "accepted"

    # Drive the production _run_writer target synchronously: deterministic
    # socket callback, real unregister/final cleanup, no scheduling race.
    harness.spawn.targets[-1]()

    assert connection.triggered is True
    assert connection.live is not None
    frozen_wire = stored[0].wire_bytes()
    assert frozen_wire in connection.written
    assert connection.written.count(stored[0].id_line) == 1
    assert connection.written.endswith(b"retry: 3000\n\n")
    assert queued_live.wire_bytes() not in connection.written
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert handle.finished.is_set()

    fresh = harness.connect(cursor=cursor_for(frozen.seq))
    assert replay_ids(drain_connection(fresh)) == [connection.live.seq]


def test_the_no_thread_writer_releases_each_observed_startup_batch() -> None:
    """An observer never turns the writer back into a full-tail owner."""

    source = ConnectionQueue(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )

    class TwoBatchReplay:
        def __init__(self) -> None:
            self.next_seq = 1
            self.first_batch_refs: list = []
            self.released: list[frames.FrameCost] = []

        def take(self) -> ReplayBatch:
            if self.next_seq > 4:
                return ReplayBatch(kind="complete")
            if self.next_seq == 3:
                gc.collect()
                assert all(ref() is None for ref in self.first_batch_refs)
            entries = tuple(
                frames.measured_frames(
                    seq=seq,
                    frames=(f"data: {seq}".encode(),),
                    id_line=f"id: inst-test:{seq}\n".encode(),
                    replayable=True,
                )
                for seq in range(self.next_seq, self.next_seq + 2)
            )
            if self.next_seq == 1:
                self.first_batch_refs = [weakref.ref(item) for item in entries]
            self.next_seq += 2
            cost = frames.FrameCost(
                frames=sum(item.ingress_cost().frames for item in entries),
                encoded_bytes=sum(
                    item.ingress_cost().encoded_bytes for item in entries
                ),
            )
            assert source.reserve_startup(cost)
            return ReplayBatch(kind="batch", entries=entries, cost=cost)

        def release(self, batch: ReplayBatch) -> None:
            source.release_startup(batch.cost)
            self.released.append(batch.cost)

        def cancel(self, batch: ReplayBatch) -> None:
            source.release_startup(batch.cost)

    replay = TwoBatchReplay()
    writer = ConnectionWriter(
        connection=FakeConnection(),
        source=source,
        clock=FakeClock(),
        startup_replay=replay,
    )
    observed: list[int] = []
    peak_cost = frames.FrameCost(frames=0, encoded_bytes=0)

    def observe(item: PreparedFrames) -> None:
        nonlocal peak_cost
        assert item.seq is not None
        observed.append(item.seq)
        cost = source.current_cost
        peak_cost = frames.FrameCost(
            frames=max(peak_cost.frames, cost.frames),
            encoded_bytes=max(peak_cost.encoded_bytes, cost.encoded_bytes),
        )

    assert writer.deliver_startup(observe) is True
    assert observed == [1, 2, 3, 4]
    assert len(replay.released) == 2
    assert source.budget.fits(peak_cost)
    assert source.current_cost == frames.FrameCost(frames=0, encoded_bytes=0)


def test_a_blocked_startup_replay_never_holds_more_than_its_frame_budget() -> None:
    """A slow writer owns only one bounded replay batch at a time."""

    class BlockFirstReplay(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.blocked = threading.Event()
            self.release = threading.Event()

        def write(self, data: bytes) -> None:
            if b"id: inst-test:1\n" in data:
                self.blocked.set()
                assert self.release.wait(2.0), "test did not release replay write"
            super().write(data)

    budget = frames.FrameBudget(frames=4, encoded_bytes=2_000)
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)
        ),
        connection_budget=budget,
        spawn=RealThreadSpawner(),
    )
    harness.emit_many(20)
    assert harness.broker._ring.current_cost.frames > budget.frames
    assert harness.broker._ring.current_cost.encoded_bytes > budget.encoded_bytes
    connection = BlockFirstReplay()
    handle = harness.connect(connection=connection, cursor=cursor_for(0))

    try:
        assert connection.blocked.wait(2.0), "writer never reached replay"
        assert budget.fits(handle.queue.current_cost)
        assert 0 < handle.queue.current_cost.frames <= budget.frames
        assert not hasattr(handle, "startup")
    finally:
        connection.release.set()
        handle.queue.stop()
        assert handle.finished.wait(2.0)
        assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_a_failed_startup_write_releases_its_reserved_batch() -> None:
    """A dead peer cannot strand startup capacity on the closed handle."""

    class FailFirstReplay(FakeConnection):
        def write(self, data: bytes) -> None:
            if b"id: inst-test:1\n" in data:
                raise BrokenPipeError("injected replay write failure")
            super().write(data)

    harness = Harness(
        connection_budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20),
        spawn=RealThreadSpawner(),
    )
    harness.emit_many(4)
    connection = FailFirstReplay()
    handle = harness.connect(connection=connection, cursor=cursor_for(0))

    assert handle.finished.wait(2.0)
    assert handle.writer is not None
    assert isinstance(handle.writer.failure, BrokenPipeError)
    assert connection.closed is True
    assert handle.queue.current_cost == frames.FrameCost(0, 0)


def test_slow_startup_generations_release_written_and_unfetched_history() -> None:
    """Eviction cannot leave slow writers pinning complete old windows."""

    class BlockFirstReplay(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.blocked = threading.Event()
            self.release = threading.Event()

        def write(self, data: bytes) -> None:
            if b"id: inst-test:1\n" in data:
                self.blocked.set()
                assert self.release.wait(2.0), "test did not release replay write"
            super().write(data)

    ring = ReplayRing(
        budget=frames.FrameBudget(frames=6, encoded_bytes=1 << 20)
    )
    harness = Harness(
        ring=ring,
        connection_budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20),
        spawn=RealThreadSpawner(),
    )
    harness.emit_many(6)
    drain_dispatcher(harness)
    retained = ring.entries_after(0)
    assert retained is not None
    first_ref = weakref.ref(retained[0])
    unfetched_ref = weakref.ref(retained[2])
    del retained

    connections = [BlockFirstReplay() for _ in range(3)]
    handles = [
        harness.connect(connection=connection, cursor=cursor_for(0))
        for connection in connections
    ]
    assert all(connection.blocked.wait(2.0) for connection in connections)

    # Replace the complete old ring while three generations are parked in
    # their first two-entry batch. Entry 3 was never fetched by any writer,
    # so eviction must release it even though all connections remain slow.
    harness.emit_many(6, start=6)
    gc.collect()
    assert first_ref() is not None
    assert unfetched_ref() is None

    for connection in connections:
        connection.release.set()
    for handle in handles:
        assert handle.finished.wait(2.0)
        assert handle.queue.current_cost == frames.FrameCost(0, 0)
    gc.collect()
    assert first_ref() is None
    assert all(connection.closed for connection in connections)
    assert all(
        frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
        in connection.written
        for connection in connections
    )

    # A new connection from the last complete batch boundary observes the
    # gap explicitly and receives a fresh snapshot instead of a silent tail.
    fresh = FakeConnection()
    reconnected = harness.connect(
        connection=fresh,
        cursor=cursor_for(2),
    )
    deadline = time.monotonic() + 2.0
    while b"replay_gap" not in fresh.written and time.monotonic() < deadline:
        time.sleep(0.001)
    reconnected.queue.stop()
    assert reconnected.finished.wait(2.0)
    assert b"replay_gap" in fresh.written
    assert b"event: state_patch" in fresh.written


def test_startup_batches_keep_a_chunked_logical_event_whole() -> None:
    """The budget boundary is between logical events, never physical frames."""

    class BlockFirstChunk(FakeConnection):
        def __init__(self, marker: bytes) -> None:
            super().__init__()
            self._marker = marker
            self.blocked = threading.Event()
            self.release = threading.Event()
            self.complete = threading.Event()

        def write(self, data: bytes) -> None:
            if self._marker in data and not self.blocked.is_set():
                self.blocked.set()
                assert self.release.wait(2.0), "test did not release chunk write"
            super().write(data)
            if b"id: inst-test:2\n" in data:
                self.complete.set()

    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)
        ),
        max_frame_bytes=512,
        spawn=RealThreadSpawner(),
    )
    first = harness.emit(RunStarted(purpose="chat"), run_id="first")
    chunky = harness.emit(
        RunStarted(
            purpose="chat",
            user_message=user_message_with("x" * 2_850),
        ),
        run_id="chunky",
    )
    drain_dispatcher(harness)
    stored = harness.broker._ring.entries_after(first.seq)
    assert stored is not None and len(stored) == 1
    event = stored[0]
    assert event.seq == chunky.seq and len(event.frames) > 1
    budget = frames.FrameBudget(
        frames=event.ingress_cost().frames,
        encoded_bytes=event.ingress_cost().encoded_bytes,
    )
    marker = b'"run_id":"chunky"'
    connection = BlockFirstChunk(marker)
    handle = harness.connect(
        connection=connection,
        cursor=cursor_for(first.seq),
        budget=budget,
    )
    try:
        assert connection.blocked.wait(2.0), "writer never reached chunked event"
        assert handle.queue.current_cost == event.ingress_cost()
        connection.release.set()
        assert connection.complete.wait(2.0), "logical event never completed"
        handle.queue.stop()
        assert handle.finished.wait(2.0)
    finally:
        connection.release.set()
    expected = list(event.wire_frames())
    start = connection.writes.index(expected[0])
    assert connection.writes[start : start + len(expected)] == expected


def test_startup_replay_slices_one_large_event_without_advancing_its_id() -> None:
    """A ring-sized event may cross several connection-sized batches."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    previous = frames.measured_frames(
        seq=1,
        frames=(b"data: previous",),
        id_line=b"id: inst-test:1\n",
        replayable=True,
    )
    large = frames.measured_frames(
        seq=2,
        frames=(b"data: first", b"data: second", b"data: third"),
        id_line=b"id: inst-test:2\n",
        replayable=True,
    )
    ring.append(previous)
    ring.append(large)
    budget = frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    harness = Harness(ring=ring, connection_budget=budget)
    handle = harness.connect(cursor=cursor_for(1))
    observed: list[PreparedFrames] = []
    reservation_costs: list[frames.FrameCost] = []

    def observe(item: PreparedFrames) -> None:
        if item.seq == 2:
            observed.append(item)
            reservation_costs.append(handle.queue.current_cost)
            assert budget.fits(handle.queue.current_cost)
            assert len(item.frames) <= budget.frames

    assert handle.writer is not None
    assert handle.writer.deliver_startup(observe) is True

    assert [item.frames for item in observed] == [
        large.frames[:2],
        large.frames[2:],
    ]
    assert observed[0].id_line == b""
    assert observed[1].id_line == large.id_line
    assert b"".join(item.wire_bytes() for item in observed) == large.wire_bytes()
    assert all(budget.fits(cost) and cost.frames > 0 for cost in reservation_costs)
    assert handle.queue.current_cost == frames.FrameCost(0, 0)
    assert not any(
        item.wire_bytes() == frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
        for item in observed
    )


def test_live_large_event_overflow_reconnects_and_advances_once() -> None:
    """A live overflow is recovered in bounded slices, then stays checkpointed."""
    budget = frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
        ),
        connection_budget=budget,
        max_frame_bytes=512,
        spawn=RealThreadSpawner(),
    )
    old_connection = FakeConnection()
    old = harness.connect(connection=old_connection, cursor=cursor_for(0))
    deadline = time.monotonic() + 2.0
    while b"event: state_patch" not in old_connection.written:
        assert time.monotonic() < deadline, "opening stream did not finish"
        time.sleep(0.001)
    event = harness.emit(
        RunStarted(
            purpose="chat",
            user_message=user_message_with("x" * 300),
        ),
        run_id="large",
    )
    stored = harness.broker._ring.entries_after(0)
    assert stored is not None and len(stored) == 1
    assert len(stored[0].frames) == 3
    harness.deliver()

    assert old.finished.wait(2.0)
    assert old_connection.written.endswith(
        frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
    )
    assert old_connection.closed is True

    recovered_connection = FakeConnection()
    recovered = harness.connect(
        connection=recovered_connection, cursor=cursor_for(0)
    )
    deadline = time.monotonic() + 2.0
    while stored[0].id_line not in recovered_connection.written:
        assert time.monotonic() < deadline, "large replay did not finish"
        time.sleep(0.001)
    expected = stored[0].wire_bytes()
    start = recovered_connection.written.index(stored[0].wire_frames()[0])
    assert recovered_connection.written[start : start + len(expected)] == expected
    assert recovered_connection.written.count(stored[0].id_line) == 1
    assert not recovered_connection.written.endswith(
        frames.retry_frame(frames.BACKOFF_RETRY_MS).wire_bytes()
    )
    recovered.queue.stop()
    assert recovered.finished.wait(2.0)

    caught_up_connection = FakeConnection()
    caught_up = harness.connect(
        connection=caught_up_connection, cursor=cursor_for(event.seq)
    )
    deadline = time.monotonic() + 2.0
    while b"event: state_patch" not in caught_up_connection.written:
        assert time.monotonic() < deadline, "caught-up opening did not finish"
        time.sleep(0.001)
    assert b"event: domain_event" not in caught_up_connection.written
    caught_up.queue.stop()
    assert caught_up.finished.wait(2.0)


def test_eviction_between_large_event_slices_never_issues_its_checkpoint() -> None:
    """Losing a partial event closes now and reports a gap on reconnect."""
    ring = ReplayRing(
        budget=frames.FrameBudget(frames=4, encoded_bytes=1 << 20)
    )
    previous = frames.measured_frames(
        seq=1,
        frames=(b"data: previous",),
        id_line=b"id: inst-test:1\n",
        replayable=True,
    )
    large = frames.measured_frames(
        seq=2,
        frames=(b"data: first", b"data: second", b"data: third"),
        id_line=b"id: inst-test:2\n",
        replayable=True,
    )
    replacement = frames.measured_frames(
        seq=3,
        frames=(b"data: replacement-a", b"data: replacement-b"),
        id_line=b"id: inst-test:3\n",
        replayable=True,
    )
    ring.append(previous)
    ring.append(large)
    harness = Harness(
        ring=ring,
        connection_budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20),
    )
    handle = harness.connect(cursor=cursor_for(1))
    opening: list[PreparedFrames] = []

    def evict_after_first_slice(item: PreparedFrames) -> None:
        opening.append(item)
        if item.seq == 2 and not item.id_line:
            ring.append(replacement)

    assert handle.writer is not None
    assert handle.writer.deliver_startup(evict_after_first_slice) is False
    assert not any(item.id_line == large.id_line for item in opening)
    assert opening[-1].wire_bytes() == frames.retry_frame(
        frames.BACKOFF_RETRY_MS
    ).wire_bytes()
    assert handle.queue.current_cost == frames.FrameCost(0, 0)

    reconnected = harness.connect(cursor=cursor_for(1))
    next_opening = drain_connection(reconnected)
    gap = next(
        item for item in next_opening if b"replay_gap" in item.wire_bytes()
    )
    assert b'"gap_reason":"too_old"' in gap.wire_bytes()
    assert not any(item.id_line == large.id_line for item in next_opening)


def test_the_queue_budget_holds_during_startup_and_live() -> None:
    """A full ring behind it does not spend a small connection's budget.

    With the tail outside the queue, the first eight live events fit and the
    ninth -- a replayable that cannot fit -- closes the connection under the
    existing overflow rule. Priming the tail into the queue would have
    closed it on the very first live event instead.
    """
    harness = Harness(
        ring=ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)),
        connection_budget=frames.FrameBudget(8, 1 << 20),
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=cursor_for(0))
    drain_connection(handle)
    # The tail the opening stream carries makes the ingress backlog redundant
    # for this connection: everything committed before it registered is
    # skipped, so the dispatcher flushes it without the queue noticing.
    drain_dispatcher(harness)
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
        ring=ReplayRing(budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)),
        connection_budget=frames.FrameBudget(8, 1 << 20),
    )
    harness.emit_many(20)
    handle = harness.connect(cursor=cursor_for(0))
    opening = replay_ids(drain_connection(handle))
    # Flush the pre-registration backlog: the opening tail already covers
    # it, so the dispatcher skips every one of those items.
    drain_dispatcher(harness)
    for i in range(9):
        harness.emit(RunStarted(purpose="chat"), run_id=f"live{i}")
        harness.deliver()
    # What the client receives: the whole tail, then the live events that
    # fit -- every seq exactly once, no gap between the two.
    seen = opening + replay_ids(drain_connection(handle))
    assert seen == list(range(1, 29))
    # Reconnecting from the last delivered cursor recovers exactly the rest.
    fresh = harness.connect(cursor=cursor_for(28))
    assert replay_ids(drain_connection(fresh)) == [29]


# --- chunking and mid-event disconnection ----------------------------------


def test_prepare_is_deterministic_for_one_immutable_logical_event() -> None:
    harness = Harness(max_frame_bytes=16 * 1024)
    unsequenced = _unsequenced_chunky("é" * (200 * 1024), event_id="logical-event-1")

    first = harness.broker.prepare(unsequenced)
    second = harness.broker.prepare(unsequenced)

    assert first == second
    assert first.wire_bytes() == second.wire_bytes()
    assert len(first.frames) > 1
    assert {_chunk_meta(frame)["event_id"] for frame in first.frames} == {
        "logical-event-1"
    }


def test_a_chunked_event_is_replayed_whole_from_its_first_frame() -> None:
    # 16 KiB frames: a 400 KiB UTF-8 body cannot be one physical frame.
    harness = Harness(max_frame_bytes=16 * 1024)
    big = "é" * (200 * 1024)
    # One event ahead of it, so the chunked event has a checkpoint to resume
    # from that this process actually issued.
    first = harness.emit(RunStarted(purpose="chat"), run_id="r1")
    event = harness.emit(
        RunStarted(purpose="chat", user_message=user_message_with(big)),
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
    handle = harness.connect(cursor=cursor_for(first.seq))
    replayed = [item for item in drain_connection(handle) if item.id_line]
    assert len(replayed) == 1
    assert replayed[0].seq == event.seq
    assert replayed[0].frames == stored[0].frames


def _unsequenced_chunky(text: str, *, event_id: str = "chunky-event"):
    """The same logical event the test just published, unsequenced.

    Prepared independently of the emit so the frames under test are the ones
    a real reconnect would be re-sent, not a re-serialization of them.
    """
    from agent_alfred.events import UnsequencedEvent

    return UnsequencedEvent(
        event_id=event_id,
        envelope=EventEnvelope(
            ts=0.0,
            run_id="chunky",
            session_id=None,
            step_index=None,
            attempt_id=None,
            node_id=None,
        ),
        payload=RunStarted(purpose="chat", user_message=user_message_with(text)),
        trace_policy="persist",
        replayable=True,
    )


def _chunk_meta(frame: bytes) -> dict:
    raw = frame.split(b"data: ", 1)[1].decode("utf-8")
    head, _separator, _payload = raw.partition('"payload":')
    return json.loads(head.rstrip().rstrip(",") + "}")


# --- the concurrency race --------------------------------------------------


def test_snapshots_and_live_events_never_duplicate_or_gap_under_concurrency() -> None:
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
    )
    fanout = FanOutSink([broker], process_instance_id=INSTANCE)
    broker.start()
    connections: list[tuple[object, FakeConnection]] = []
    try:
        connections.append(
            (broker.connect(connection=FakeConnection()), _conn_of(broker, 0))
        )
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
    broker = SSEBroker(process_instance_id=INSTANCE, snapshot=runtime_snapshot())
    assert broker._ingress.budget == frames.FrameBudget(4096, 32 * 1024 * 1024)
    assert broker._ring.budget == frames.FrameBudget(2048, 32 * 1024 * 1024)


# --- the named cost of the ingress's accounting ------------------------------


def test_the_ingress_count_returns_exactly_to_zero() -> None:
    harness = Harness()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    cost = harness.broker._ingress.current_cost
    assert cost.frames == 2
    assert cost.encoded_bytes > 0
    drain_dispatcher(harness)
    assert harness.broker._ingress.current_cost == frames.FrameCost(0, 0)


def test_a_state_patch_pays_a_named_one_frame_cost() -> None:
    harness = Harness()
    snapshot = runtime_snapshot(state_revision=1)
    assert harness.broker.publish_state_patch(snapshot) is True
    expected = broker_module._patch_cost(
        snapshot, harness.broker._progress.projection(None)
    )
    assert harness.broker._ingress.current_cost == frames.FrameCost(
        frames=1, encoded_bytes=expected
    )


def test_close_is_idempotent_and_leaves_no_thread_running() -> None:
    threads: list[threading.Thread] = []

    def spawn(target):
        thread = threading.Thread(target=target, daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
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
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
    )
    broker.start()
    connection = Stuck()
    broker.connect(connection=connection)
    broker.publish_state_patch(runtime_snapshot(state_revision=1))
    started = time.monotonic()
    assert broker.close(timeout=0.3) is False
    # Bounded, and the descriptor is released rather than left behind.
    assert time.monotonic() - started < 2.0
    assert connection.closed is True


# --- close() is a completion report, not an intention -----------------------


def test_close_reports_false_until_every_thread_has_really_exited() -> None:
    """close() answers a question about threads, not about bookkeeping.

    While the dispatcher or any writer is still alive, close() must answer
    False -- on *every* attempt, not only the first. A True that comes from
    the first attempt's own state rather than from the threads having
    exited tells the caller to release what the broker is still draining:
    the database those frames were built from and the process lock the
    stream lives under.
    """
    gates = GatedThreads()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
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


@pytest.mark.parametrize("failure", [RuntimeError("spawn failed"), KeyboardInterrupt()])
def test_failed_writer_handoff_revokes_the_whole_registration(failure) -> None:
    """A failed spawn propagates unchanged and leaves no pre-writer owner."""
    target_ref = None

    def fail_spawn(target):
        nonlocal target_ref
        target_ref = weakref.ref(target)
        raise failure

    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=fail_spawn,
    )
    connection = FakeConnection()

    with pytest.raises(type(failure)) as raised:
        broker.connect(connection=connection)

    assert raised.value is failure
    assert connection.closed is True
    assert broker.connections == ()
    assert broker.registrations_in_flight == 0
    assert target_ref is not None
    del raised
    failure.__traceback__ = None
    gc.collect()
    assert target_ref() is None


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
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
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
    harness = Harness(spawn=RealThreadSpawner())
    held = GatedWriteConnection()
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
    harness = Harness(spawn=RealThreadSpawner())
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
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
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
    drain_connection(handle)
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=7))
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
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=9))
    # The refused patch raised the disconnect generation; the dispatcher
    # closes the connection that predates it.
    harness.deliver()
    assert handle.queue.close_requested is True
    assert any(isinstance(item, CloseConnection) for item in drain_connection(handle))


def test_a_broker_without_a_session_source_refuses_to_guess() -> None:
    """The snapshot's Session-validity field is a fact, not a default.

    The broker is built before the Host it asks, so there is a window in
    which it has no source. Answering "valid" there would publish a snapshot
    claiming a Session exists when nothing has been consulted.
    """
    broker = SSEBroker(process_instance_id=INSTANCE, snapshot=runtime_snapshot())
    with pytest.raises(RuntimeError):
        broker.connect(connection=FakeConnection())
    broker.bind_session_check(
        lambda session_id: "valid" if session_id is None else "invalid"
    )
    assert broker.connect(connection=FakeConnection()) is not None


def test_the_dispatcher_stops_on_the_sentinel() -> None:
    harness = Harness()
    harness.broker._ingress.put_stop()
    assert harness.broker.deliver_next(timeout=0.1) is False


def test_only_the_stop_sentinel_may_bypass_the_budget() -> None:
    """``put_stop`` takes no argument, so nothing else can ride it."""
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1))
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
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
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
    handle = harness.connect(cursor=cursor_for(0))
    assert replay_ids(drain_connection(handle)) == [first.seq, second.seq]
    assert not any(
        b"replay_gap" in item.wire_bytes() for item in drain_connection(handle)
    )
    # A checkpoint this process did issue replays exactly its own tail.
    handle = harness.connect(cursor=cursor_for(first.seq))
    assert replay_ids(drain_connection(handle)) == [second.seq]


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
    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    fatal_calls: list[BaseException] = []
    fatal_done = threading.Event()
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    broker.bind_fatal_handler(lambda exc: (fatal_calls.append(exc), fatal_done.set()))
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
    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
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
        event_id="fatal-event",
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    prepared = broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=1,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
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


def test_the_fatal_refusal_is_typed_as_process_fatal(monkeypatch) -> None:
    """The refusal behind a dead dispatcher carries the process-fatal type.

    A bare ``RuntimeError`` was enough to make the FanOut disable the sink
    -- but only for the Run that happened to be publishing, which is how a
    dead dispatcher got rediscovered and re-announced by every Run that
    followed. The refusal the FanOut needs in order to disable the sink for
    every Run and notify once must be recognizably the process-fatal class,
    on both paths a publish can touch.
    """
    from agent_alfred.events import ProcessFatalSinkError

    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    harness = Harness()
    harness.connect()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    with pytest.raises(RuntimeError):
        harness.broker._dispatch_loop()  # noqa: SLF001 - the loop under test

    payload = RunStarted(purpose="chat")
    envelope = EventEnvelope(
        ts=0.0,
        run_id="r2",
        session_id=None,
        step_index=None,
        attempt_id=None,
        node_id=None,
    )
    unsequenced = UnsequencedEvent(
        event_id="fatal-refusal-event",
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    # Prepare refuses too: framing an event nobody can deliver is work
    # wasted, and the refusal is the same typed signal.
    with pytest.raises(ProcessFatalSinkError):
        harness.broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=99,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
        envelope=envelope,
        payload=payload,
        trace_policy="persist",
        replayable=True,
    )
    with pytest.raises(ProcessFatalSinkError):
        harness.broker.commit(None, sequenced)


def test_process_level_sink_disabled_notification_is_persistent_and_once(
    monkeypatch,
) -> None:
    """The process-level ``sink_disabled`` notice is one envelope, ever.

    A dispatcher that died in r1 used to be rediscovered by every later
    Run: r2's first publish failed the fatal check, was booked as that
    Run's sink failure, and published its own ``sink_disabled`` notice --
    and so did r3, and r4. The notice a process-level fact owes is one
    envelope, and the Runs after it must find the sink already gone instead
    of re-announcing it.
    """
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    monkeypatch.setattr(broker_module, "ConnectionQueue", _ExplodingConnectionQueue)
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=runtime_snapshot(),
        session_is_valid=lambda _sid: "valid",
        spawn=RealThreadSpawner().spawn,
    )
    dead = threading.Event()
    broker.bind_fatal_handler(lambda exc: dead.set())
    broker.start()
    handle = broker.connect(connection=FakeConnection())
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([broker, capture], process_instance_id=INSTANCE)

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
    assert dead.wait(5.0), "fatal handler never ran"
    assert handle.finished.wait(5.0), "connection was never asked to close"
    fanout.flush_barrier("r1")
    emit("r2")
    fanout.flush_barrier("r2")
    emit("r3")

    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1, "one process, one process-level notice"
    # The refusal persists: the broker stopped taking work and stays stopped
    # across every Run that followed the death.
    assert broker._stopping is True  # noqa: SLF001
    refused = broker.connect(connection=FakeConnection())
    assert refused.finished.is_set()
    assert refused not in broker.connections
    # The healthy sink lost nothing: every Run's event still reached it.
    runs = [
        event.envelope.run_id
        for event in capture.events
        if event.payload.name == "run.started"
    ]
    assert runs == ["r1", "r2", "r3"]


class _BrokenRing(ReplayRing):
    """A ring that cannot record anything.

    The ring is the process's only replay source for an active Run; one
    that raises is the dispatcher-dying class of failure, not one Run's
    transient. Subclassing the real ring keeps every other ring behaviour
    intact -- only the recording step is broken.
    """

    def observe_published(self, seq, entry):
        del seq, entry
        raise RuntimeError("ring is broken")


def test_a_broken_ring_is_a_process_fatal_not_a_run_local_error() -> None:
    """A ring failure inside commit escalates to the fatal state.

    Before the typed signal, a ring exception was just another sink error:
    the FanOut disabled the broker for the Run, the broker stayed up, kept
    accepting connections and kept handing out cursors a ring that cannot
    record could never honour -- and the next Run drew the same exception
    and published the same notice again. The ring failing is the dispatcher
    failing (#23 §9): the broker publishes the fatal state, refuses
    everything that would pour work into a broken recovery source, and the
    notice is said once.
    """
    harness = Harness(ring=_BrokenRing())
    broker = harness.broker
    capture = CapturingSink(name="capture")
    fanout = FanOutSink([broker, capture], process_instance_id=INSTANCE)

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
    # The broker published the fatal state instead of limping on.
    assert broker._fatal is not None  # noqa: SLF001
    assert broker._stopping is True  # noqa: SLF001
    # New connections are refused, and so are state patches: both would
    # describe a world whose recovery source is gone.
    connection = FakeConnection()
    refused = broker.connect(connection=connection)
    assert refused.finished.is_set()
    assert refused.thread is None
    assert connection.closed is True
    assert broker.publish_state_patch(runtime_snapshot(state_revision=1)) is False
    # Said once: the next Run must not rediscover the broken ring and
    # publish the same notice again.
    fanout.flush_barrier("r1")
    emit("r2")
    notices = [
        event
        for event in capture.events
        if getattr(event.payload, "code", None) == "sink_disabled"
    ]
    assert len(notices) == 1
    runs = [
        event.envelope.run_id
        for event in capture.events
        if event.payload.name == "run.started"
    ]
    assert runs == ["r1", "r2"], "the healthy sink keeps receiving events"


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
    harness = Harness(
        ingress_budget=frames.FrameBudget(2, 32 * 1024 * 1024)
    )
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


def test_a_transient_published_before_a_connection_registers_is_not_delivered() -> None:
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
    wire = b"".join(item.wire_bytes() for item in drain_connection(handle))
    assert b'"event":"block.delta"' not in wire
    # A transient published after the registration is live delivery.
    harness.emit(BlockDelta(attempt_id="a1", index=0, text="still going"))
    _deliver_ingress(harness)
    items = drain_connection(handle)
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
    items = drain_connection(handle)
    assert replay_ids(items) == []
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' not in wire
    harness.emit(BlockDelta(attempt_id="a1"))  # seq 4, transient
    harness.emit(RunStarted(purpose="chat"))  # seq 5, replayable
    _deliver_ingress(harness)
    items = drain_connection(handle)
    # Only the replayable event carries a checkpoint.
    assert replay_ids(items) == [5]
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
    handle = harness.connect(cursor=cursor_for(1))
    _deliver_ingress(harness)
    items = drain_connection(handle)
    # The replay tail, exactly once, and no preregistration transient.
    assert replay_ids(items) == [2]
    wire = b"".join(item.wire_bytes() for item in items)
    assert b'"event":"block.delta"' not in wire


# --- fan-out asks for Session truth only when a patch needs it --------------


def test_a_domain_event_fan_out_never_queries_session_validity() -> None:
    """A domain event has one wire view, independent of its Session.

    Session validity is database-backed in production. Asking it here would
    put one SQL query per connection on the sole dispatcher even though the
    answer cannot change the domain event that any connection receives.
    """
    harness = Harness()
    handle = harness.connect(session_id="s1")
    drain_connection(handle)

    def unexpected_query(_session_id: str | None) -> bool:
        raise AssertionError("domain event fan-out queried Session validity")

    harness.broker.bind_session_check(unexpected_query)
    harness.emit(RunStarted(purpose="chat"))
    harness.deliver()

    wire = b"".join(item.wire_bytes() for item in drain_connection(handle))
    assert b'"event":"run.started"' in wire


def test_a_patch_fan_out_keeps_each_sessions_validity_view() -> None:
    """A patch still carries the database-backed fact for its connection."""
    harness = Harness()
    valid = harness.connect(session_id="s1")
    invalid = harness.connect(session_id="gone")
    drain_connection(valid)
    drain_connection(invalid)

    harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
    harness.deliver()

    assert _patch_payload(valid)["session_valid"] is True
    assert _patch_payload(invalid)["session_valid"] is False


def test_a_patch_queries_each_distinct_session_at_most_once() -> None:
    """Tabs sharing one Session share its validity answer for this patch."""
    harness = Harness()
    first = harness.connect(session_id="s1")
    second = harness.connect(session_id="s1")
    other = harness.connect(session_id="gone")
    for handle in (first, second, other):
        drain_connection(handle)
    queried: list[str | None] = []

    def session_exists(session_id: str | None) -> SessionValidity:
        queried.append(session_id)
        return "valid" if session_id == "s1" else "invalid"

    harness.broker.bind_session_check(session_exists)
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
    harness.deliver()

    assert queried == ["s1", "gone"]


def test_a_patch_keeps_each_connections_last_proven_validity_when_unavailable(
) -> None:
    """Storage loss cannot rewrite or erase a fact established at connect."""
    answer = {"value": "valid"}
    harness = Harness()
    harness.broker.bind_session_check(lambda _session_id: answer["value"])
    handle = harness.connect(session_id="s1")
    drain_connection(handle)

    answer["value"] = "unavailable"
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
    harness.deliver()

    assert _patch_payload(handle)["session_valid"] is True
    assert handle in harness.broker.connections


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
    harness = Harness(spawn=RealThreadSpawner())
    handle = harness.connect()
    drain_connection(handle)
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=1))
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
    for item in drain_connection(handle):
        if not isinstance(item, PreparedFrames):
            # A sweep's close sentinel is a mark, not a frame the wire saw.
            continue
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
    drain_connection(handle)
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
        current_step=0,
    )
    overtaken = runtime_snapshot(
        state_revision=1, coordinator_state="running", active_run=active
    )
    newer = runtime_snapshot(
        state_revision=2,
        coordinator_state="running",
        active_run=replace(active, current_step=1),
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
    drain_dispatcher(harness)
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
    drain_connection(handle)
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
        current_step=0,
    )
    overtaken = runtime_snapshot(
        state_revision=1, coordinator_state="running", active_run=active
    )
    newer = runtime_snapshot(
        state_revision=2,
        coordinator_state="running",
        active_run=replace(active, current_step=1),
    )
    barrier, thread, _answers = _park_a_publisher(harness, monkeypatch, overtaken)
    try:
        harness.emit(StepStarted(step_index=1), run_id="r1")
        assert harness.broker.publish_state_patch(newer)
        barrier.wait(timeout=5.0)
    except BaseException:
        barrier.abort()
        raise
    thread.join(timeout=5.0)
    drain_dispatcher(harness)
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
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=2))
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
    harness = Harness(spawn=RealThreadSpawner())
    connect_done = threading.Event()
    descriptor: list[bytes] = []

    def do_connect() -> None:
        connection = FakeConnection()
        handle = harness.broker.connect(connection=connection)
        deadline = time.monotonic() + 2.0
        while len(connection.writes) < 3 and time.monotonic() < deadline:
            time.sleep(0.001)
        descriptor.append(connection.written)
        handle.queue.stop()
        connect_done.set()

    connector = threading.Thread(target=do_connect, daemon=True)
    connector.start()
    try:
        assert encoder.entered.wait(2.0), "startup encoding never started"
        # While the startup is parked mid-encoding, the world moves: the
        # authoritative snapshot advances. A connect holding the lock here
        # would deadlock the publish; a connect that merely encoded outside
        # but registered the stale frame would ship revision 0.
        assert harness.broker.publish_state_patch(runtime_snapshot(state_revision=1)), (
            "publish waited for the startup encoding"
        )
    finally:
        encoder.release.set()
        assert connect_done.wait(5.0), "connect never finished"
        connector.join(timeout=5.0)
    wire = descriptor[0]
    assert b'"state_revision":1' in wire
    assert b'"state_revision":0' not in wire


def test_connect_refuses_after_bounded_startup_recaptures(monkeypatch) -> None:
    """A perpetually moving world cannot make connect spin forever.

    Every opening-patch encoding below advances one real authoritative state
    patch before registration can re-check its epoch. The old unbounded loop
    reaches a fifth encoding and has to be stopped externally; the bounded
    path instead returns the ordinary explicit refusal after exactly the
    shared capture budget, without ever registering or starting a writer.
    """
    harness = Harness()
    connection = FakeConnection()
    real_encoder = broker_module._patch_frames
    runaway = threading.Event()
    release_runaway = threading.Event()
    connect_done = threading.Event()
    signals: queue.Queue[str] = queue.Queue()
    captures = 0
    answers = []
    errors: list[BaseException] = []

    def advancing_encoder(snapshot, step, session_valid):
        nonlocal captures
        captures += 1
        if captures > broker_module._MAX_PATCH_CAPTURES:
            runaway.set()
            signals.put("runaway")
            release_runaway.wait(5.0)  # anti-hang only; cleanup releases it
        encoded = real_encoder(snapshot, step, session_valid)
        harness.broker.publish_state_patch(
            runtime_snapshot(state_revision=captures)
        )
        return encoded

    monkeypatch.setattr(broker_module, "_patch_frames", advancing_encoder)

    def connect() -> None:
        try:
            answers.append(harness.broker.connect(connection=connection))
        except BaseException as exc:  # preserve the worker's testimony
            errors.append(exc)
        finally:
            connect_done.set()
            signals.put("done")

    connector = threading.Thread(target=connect, daemon=True)
    connector.start()
    # Fixed code returns promptly. Old code deterministically announces that
    # it crossed the budget and parks, proving the pre-fix non-return without
    # leaving an infinite daemon consuming snapshots in the test process.
    signal = signals.get(timeout=5.0)
    bounded = signal == "done"
    if not bounded:
        assert runaway.is_set(), "connect neither returned nor crossed its budget"
    try:
        assert bounded, "connect exceeded the fixed startup capture budget"
    finally:
        if not bounded:
            # The old implementation needs an external close to escape its
            # unbounded loop. Close first, then release the parked encoder so
            # its next registration check observes the refusal.
            assert harness.broker.close(timeout=0.2) is True
            release_runaway.set()
        connector.join(timeout=5.0)

    assert not connector.is_alive()
    assert errors == []
    assert captures == broker_module._MAX_PATCH_CAPTURES
    assert len(answers) == 1
    refused = answers[0]
    assert refused.finished.is_set()
    assert refused.thread is None
    assert refused.writer is None
    assert refused not in harness.broker.connections
    assert harness.broker.registrations_in_flight == 0
    assert harness.spawn.targets == []
    assert connection.written == b""
    assert connection.closed is True
    assert refused.queue.current_cost == frames.FrameCost(
        frames=0, encoded_bytes=0
    )
    assert refused.queue.close_requested is False

    # The rejected handle and its queue are not retained by the broker or a
    # never-started writer target after the caller releases its own answer.
    refused_ref = weakref.ref(refused)
    answers.clear()
    del refused
    gc.collect()
    assert refused_ref() is None


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
        event_id=f"direct-{seq}",
        envelope=envelope,
        payload=RunStarted(purpose="chat"),
        trace_policy="persist",
        replayable=True,
    )
    prepared = harness.broker.prepare(unsequenced)
    sequenced = SequencedEvent(
        seq=seq,
        process_instance_id=INSTANCE,
        event_id=unsequenced.event_id,
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
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
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
    drain_dispatcher(harness)
    assert all(probe.close_requests == 1 for probe in probes)


def test_a_connection_registered_after_the_overflow_is_not_closed() -> None:
    """The generation answers "who was there when it happened".

    A connection that registers after the overflow recovered its starting
    state from the snapshot and the ring; closing it would punish the one
    client that cannot be missing anything.
    """
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    handle = harness.connect()
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    _commit_direct(harness, seq=2, run_id="r2")  # overflows the ingress
    late = harness.connect(cursor=cursor_for(0))
    drain_dispatcher(harness)
    assert handle.queue.close_requested is True
    assert late.queue.close_requested is False


def test_patch_overflow_never_walks_the_connections_from_the_publisher() -> None:
    """The state-patch overflow rides the same mechanism, not a second path.

    A refused patch moves the authoritative snapshot and raises the
    generation; the publisher itself touches no queue, and the dispatcher
    closes exactly the connections that predate the incident.
    """
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    probes: list[_ProbingQueue] = []
    for _ in range(4):
        handle = harness.connect()
        handle.queue = _ProbingQueue()
        probes.append(handle.queue)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # occupies ingress
    accepted = harness.broker.publish_state_patch(runtime_snapshot(state_revision=5))
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
    drain_dispatcher(harness)
    assert all(probe.close_requests == 1 for probe in probes)
    assert late.queue.close_requested is False


# --- capture exhaustion keeps the authority and owes the reconnect -----------


class _PerLapCost:
    """The cost probe that parks the publisher once per capture lap.

    Where :class:`_GatedCost` parks a single encode so the world can move
    around it, this one parks *every* lap up to a budget, so a test can
    lose the recapture race exactly ``_MAX_PATCH_CAPTURES`` times in a row
    and walk the publisher into its exhaustion path for certain. Each lap
    gets its own rendezvous: the probe raises that lap's ``arrived`` once
    the publisher is parked mid-measurement -- outside the broker lock,
    where the world can move -- and the test sets that lap's release after
    committing its interleave. The scheduler decides nothing.
    """

    def __init__(self, real, laps: int) -> None:
        self._real = real
        self._arrived = [threading.Event() for _ in range(laps)]
        self._releases = [threading.Event() for _ in range(laps)]
        self._next = 0
        self._aborted = False

    def __call__(self, snapshot, step) -> int:
        if self._aborted or self._next >= len(self._arrived):
            return self._real(snapshot, step)
        lap = self._next
        self._next += 1
        self._arrived[lap].set()
        self._releases[lap].wait(5.0)  # anti-hang only; the test releases
        return self._real(snapshot, step)

    def wait_parked(self, lap: int) -> bool:
        return self._arrived[lap].wait(5.0)

    def release_lap(self, lap: int) -> None:
        self._releases[lap].set()

    def abort(self) -> None:
        """Free a publisher a failing test would otherwise park forever."""
        self._aborted = True
        for release in self._releases:
            release.set()


def _park_a_publisher_every_lap(
    harness, monkeypatch, snapshot
) -> tuple[_PerLapCost, threading.Thread, list[bool]]:
    """Start one publisher and hold it at every lap's cost measurement.

    Like :func:`_park_a_publisher`, but the rendezvous repeats once per
    capture lap: the test commits one real transition between each lap's
    capture and its commit, so the bounded recapture is exhausted for
    certain. The publisher's answer lands in the returned list.
    """
    gate = _PerLapCost(broker_module._patch_cost, broker_module._MAX_PATCH_CAPTURES)
    monkeypatch.setattr(broker_module, "_patch_cost", gate)
    answers: list[bool] = []

    def publisher() -> None:
        answers.append(harness.broker.publish_state_patch(snapshot))

    thread = threading.Thread(target=publisher, daemon=True)
    thread.start()
    assert gate.wait_parked(0), "the publisher never reached its first encoding"
    return gate, thread, answers


def _opening_revision(wire: bytes) -> int | None:
    """The state revision an opening stream's snapshot names, if any."""
    match = re.search(rb'"state_revision":(\d+)', wire)
    return None if match is None else int(match.group(1))


def test_capture_exhaustion_still_moves_the_authority_and_disconnects(
    monkeypatch,
) -> None:
    """Losing every recapture lap must not leave the authority behind.

    A world that moves during every single encode lap -- here one real
    ``StepStarted`` per lap, ``_MAX_PATCH_CAPTURES`` times -- exhausts the
    bounded recapture, and the captured Step belongs to a world that is
    already gone. A bare ``False`` that did nothing would leave the
    broker's ``_latest`` behind the authoritative store's snapshot for
    good: no patch, no disconnect, and every reconnect re-seeding the
    stale revision. The exhaustion path must still adopt the snapshot as
    the authority, raise the disconnect generation so the dispatcher asks
    the pre-incident connections to come back for an atomic snapshot, and
    answer False -- the patch genuinely never entered the ingress.
    """
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    requested = runtime_snapshot(state_revision=5)
    gate, thread, answers = _park_a_publisher_every_lap(harness, monkeypatch, requested)
    try:
        for lap in range(broker_module._MAX_PATCH_CAPTURES):
            assert gate.wait_parked(lap), f"lap {lap} never reached its encoding"
            # One real transition per lap, committed while the publisher
            # sits parked mid-measurement, holding no broker lock.
            harness.emit(StepStarted(step_index=lap), run_id="r1")
            gate.release_lap(lap)
    except BaseException:
        gate.abort()
        raise
    thread.join(timeout=5.0)
    # A connection registering after the exhaustion re-seeds from whatever
    # the broker now holds -- and registers at the current generation.
    late = harness.connect()
    late_wire = b"".join(item.wire_bytes() for item in drain_connection(late))
    drain_dispatcher(harness)
    # Observed before the first assert, so a red run reports the whole
    # failure, not just its first casualty.
    observed = {
        "answer": answers,
        "latest_revision": harness.broker._latest.state_revision,
        "disconnect_generation": harness.broker._disconnect_generation,
        "old_connection_close_requested": handle.queue.close_requested,
        "late_connection_close_requested": late.queue.close_requested,
        "late_opening_revision": _opening_revision(late_wire),
        "patches_delivered": _patches_received(handle),
    }
    assert answers == [False], observed
    assert observed["latest_revision"] == requested.state_revision, observed
    assert observed["disconnect_generation"] == 1, observed
    assert observed["old_connection_close_requested"] is True, observed
    assert observed["late_connection_close_requested"] is False, observed
    assert observed["late_opening_revision"] == requested.state_revision, observed
    assert observed["patches_delivered"] == [], observed


def test_exhausted_publisher_never_regresses_an_overtaken_snapshot(
    monkeypatch,
) -> None:
    """Exhaustion adopts the snapshot only while it is still the newest.

    The closing move of a fully lost recapture race must re-check the
    authority it is about to move: if a newer absolute replacement was
    published while the laps ran, its own patch is already in flight, and
    adopting the older snapshot would walk ``_latest`` -- and every
    reconnect -- backwards. The refusal then owes nobody a reconnect.
    """
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    requested = runtime_snapshot(state_revision=5)
    gate, thread, answers = _park_a_publisher_every_lap(harness, monkeypatch, requested)
    newer = None
    try:
        last = broker_module._MAX_PATCH_CAPTURES - 1
        for lap in range(broker_module._MAX_PATCH_CAPTURES):
            assert gate.wait_parked(lap), f"lap {lap} never reached its encoding"
            harness.emit(StepStarted(step_index=lap), run_id="r1")
            if lap == last:
                # The final lap loses to a newer *snapshot*, not only to an
                # event: by the time the exhausted publisher reaches its
                # closing move, the authority has already moved past it.
                newer = runtime_snapshot(state_revision=6)
                assert harness.broker.publish_state_patch(newer), (
                    "the newer publish must not wait behind the parked one"
                )
            gate.release_lap(lap)
    except BaseException:
        gate.abort()
        raise
    thread.join(timeout=5.0)
    drain_dispatcher(harness)
    assert answers == [False], "the exhausted publisher must still refuse"
    assert harness.broker._latest is newer, "_latest moved backwards"
    assert harness.broker._disconnect_generation == 0, (
        "the overtaking patch was delivered; nobody owes a reconnect"
    )
    assert handle.queue.close_requested is False, (
        "an overtaken refusal must not disconnect a connection that is "
        "about to receive the newer patch"
    )
    patches = _patches_received(handle)
    assert patches and patches[-1]["state_revision"] == 6, (
        "the overtaking patch never reached the connection"
    )


# --- a trailing transient is published, but never a checkpoint --------------


def _gap_payload(harness, cursor: str) -> dict:
    handle = harness.connect(cursor=cursor)
    notice = _wire_containing(handle, b"replay_gap")
    return _decode_patch(notice)


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
    assert _gap_payload(harness, cursor_for(2)) == {
        "code": "replay_gap",
        "gap_reason": "malformed",
        "requested_seq": 2,
        "oldest_seq": 1,
        "high_water_seq": 2,
        "current_run_state": "absent",
    }
    assert _gap_payload(harness, cursor_for(3)) == {
        "code": "replay_gap",
        "gap_reason": "ahead",
        "requested_seq": 3,
        "oldest_seq": 1,
        "high_water_seq": 2,
        "current_run_state": "absent",
    }
    # The next replayable event lands on the transient's successor and is a
    # checkpoint like any other.
    third = harness.emit(RunStarted(purpose="chat"), run_id="r2")
    assert third.seq == 3
    handle = harness.connect(cursor=cursor_for(1))
    assert replay_ids(drain_connection(handle)) == [3]
    # Resuming *from* 3 has nothing to catch up, as any checkpoint.
    handle = harness.connect(cursor=cursor_for(3))
    assert replay_ids(drain_connection(handle)) == []
    # The transient never entered the ring and never got an id:.
    assert len(harness.broker._ring) == 2
