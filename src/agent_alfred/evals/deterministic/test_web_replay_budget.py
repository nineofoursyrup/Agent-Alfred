"""Replay cursor and physical frame budget contracts."""

from __future__ import annotations

from agent_alfred.evals.deterministic._web_broker_test_helpers import (
    Harness,
    cursor_for,
    drain_connection,
    replay_ids,
    runtime_snapshot,
)
from agent_alfred.events import RunStarted
from agent_alfred.gateway.web import frames, replay
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.replay import (
    ReplayRing,
    classify_cursor,
)
from agent_alfred.runtime.snapshot import ActiveRunSummary

# --- the ring's two budgets and its checkpoints ----------------------------
#
# The ring is the only replay source for an active Run, so two things about
# it have to be true: it may never issue a checkpoint it cannot honour, and
# its budget has to be counted in the unit the decision named -- physical
# frames, not logical events.


def _frames_in(ring) -> int:
    return sum(len(entry.frames) for entry in ring._entries)


def _entry(seq: int, size: int = 8, chunks: int = 1) -> PreparedFrames:
    """One logical event: ``chunks`` physical frames of ``size`` bytes."""
    head = b"data: " + b"x" * size
    return frames.measured_frames(
        seq=seq,
        frames=tuple(head for _ in range(chunks)),
        id_line=b"id: inst:%d\n" % seq,
        replayable=True,
    )


def test_a_forged_cursor_on_an_unrecoverable_event_is_a_gap() -> None:
    """An event the ring cannot produce is not a checkpoint.

    A single event over the byte budget is published and then dropped: the
    floor and the high-water mark both move past it, so a client that forged
    ``instance:seq`` for it was told "valid" -- letting it skip the one fact
    it was owed a gap notice for.
    """
    ring = ReplayRing(budget=frames.FrameBudget(frames=100, encoded_bytes=1 << 10))
    ring.append(_entry(1))
    result = ring.append(_entry(2, size=(1 << 10) + 1))
    assert result.accepted is False
    assert result.ring_cleared is True
    assert ring.replay_floor_seq() == 2
    assert ring.high_water_seq() == 2
    # The boundary moved, but no checkpoint was issued for it.
    assert ring.classify_seq(2) != "valid"
    # Nor for anything before it: the ring was cleared, so every earlier seq
    # is now below the floor and unusable for an exact catch-up.
    assert ring.latest_complete_seq() is None
    assert ring.entries_after(1) is None
    forged = classify_cursor(
        replay.format_cursor("inst", 2), ring, process_instance_id="inst"
    )
    assert forged.kind == "gap"
    # A first connection is still given a boundary to stand on -- the one
    # this process issued before the loss. It is not replayable and the ring
    # will call it ``too_old`` when it comes back, which is the point: a
    # client holding nothing at all sends no ``Last-Event-ID``, and that is
    # the one shape that never gets a gap notice.
    first = classify_cursor(None, ring, process_instance_id="inst")
    assert first.reseed_seq == 1
    assert ring.reseed_boundary_seq() == 1
    assert ring.classify_seq(1) == "too_old"


def test_an_unrecoverable_seq_never_appears_in_an_id_line() -> None:
    """A checkpoint the ring cannot honour is not issued at all."""
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=100, encoded_bytes=64)
        )
    )
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # seq 1, too big
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # seq 2, fits
    ring = harness.broker._ring
    assert ring.high_water_seq() == 2
    # Nothing in the ring carries the unrecoverable event's id.
    assert all(entry.seq != 1 for entry in ring._entries)
    handle = harness.connect(cursor=cursor_for(2))
    assert [seq for seq in replay_ids(drain_connection(handle)) if seq == 1] == []


def test_an_evicted_floor_is_still_a_usable_checkpoint() -> None:
    """Normal eviction and unrecoverable loss are different facts."""
    ring = ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    for seq in (1, 2, 3, 4):
        ring.append(_entry(seq))
    assert ring.replay_floor_seq() == 2
    # 2 was issued and then evicted; everything past it is still held, so
    # resuming from it loses nothing.
    assert ring.classify_seq(2) == "valid"
    assert [entry.seq for entry in ring.entries_after(2) or ()] == [3, 4]
    # An unrecoverable loss is not the same: the boundary moves but the seq
    # was never issued, so it cannot be resumed from.
    ring.append(_entry(5, size=(1 << 20) + 1))
    assert ring.replay_floor_seq() == 5
    assert ring.classify_seq(5) != "valid"


def test_cursors_around_an_unrecoverable_event_leave_no_silent_hole() -> None:
    ring = ReplayRing(budget=frames.FrameBudget(frames=100, encoded_bytes=1 << 10))
    ring.append(_entry(1))
    ring.append(_entry(2, size=(1 << 10) + 1))  # unrecoverable
    ring.append(_entry(3))
    # The cursor before it is now too old: the ring cannot prove continuity
    # across the lost event, and says so rather than resuming silently.
    assert ring.classify_seq(1) == "too_old"
    assert ring.entries_after(1) is None
    # The cursor after it is a real checkpoint and loses nothing.
    assert ring.classify_seq(3) == "valid"
    assert ring.entries_after(3) == ()
    # The unrecoverable seq itself is inside the range and still refused --
    # not valid, and not silently resumable.
    assert ring.classify_seq(2) == "malformed"
    # The four reasons stay closed.
    assert ring.classify_seq(9) == "ahead"


def test_a_run_spanning_an_unrecoverable_event_is_unrecoverable() -> None:
    harness = Harness(
        ring=ReplayRing(
            budget=frames.FrameBudget(frames=100, encoded_bytes=64)
        )
    )
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.broker._run_start_seq["r1"] = 1
    harness.broker.publish_state_patch(
        runtime_snapshot(
            coordinator_state="running",
            active_run=ActiveRunSummary(
                run_id="r1",
                purpose="chat",
                gateway="web",
                phase="running",
                session_id="s1",
                prompt_preview="hi",
                started_at=None,
                recording_state=None,
            ),
        )
    )
    handle = harness.connect(cursor=cursor_for(1))
    notices = [
        item
        for item in drain_connection(handle)
        if isinstance(item, PreparedFrames)
        and any(b"replay_gap" in frame for frame in item.frames)
    ]
    assert notices
    assert b'"current_run_state":"unrecoverable"' in notices[0].frames[0]


def test_the_frame_budget_is_counted_in_physical_frames() -> None:
    """2048 means frames, not logical events.

    A logical event may be many frames, so counting entries let a handful of
    chunked events pin far more than the budget ever meant to allow.
    """
    ring = ReplayRing(budget=frames.FrameBudget(frames=4, encoded_bytes=1 << 20))
    ring.append(_entry(1, chunks=3))
    assert _frames_in(ring) == 3
    # A second chunked event would take the ring to six frames, so the
    # oldest *whole* event goes: half an event is not expressible.
    ring.append(_entry(2, chunks=3))
    assert _frames_in(ring) == 3
    assert [entry.seq for entry in ring.entries_after(1) or ()] == [2]
    assert (ring.entries_after(1) or ())[0].frames == _entry(2, chunks=3).frames


def test_the_ring_never_holds_more_frames_than_its_budget() -> None:
    ring = ReplayRing(budget=frames.FrameBudget(frames=5, encoded_bytes=1 << 20))
    for seq in range(1, 12):
        ring.append(_entry(seq, chunks=2))
        assert _frames_in(ring) <= 5
    assert _frames_in(ring) <= 5


def test_one_event_with_more_frames_than_the_budget_is_not_kept() -> None:
    """Same treatment as an event over the byte budget.

    It is not stored in part, the unrecoverable boundary advances, and no
    checkpoint is issued for a boundary the ring could never reproduce.
    """
    ring = ReplayRing(budget=frames.FrameBudget(frames=3, encoded_bytes=1 << 20))
    ring.append(_entry(1))
    result = ring.append(_entry(2, chunks=4))
    assert result.accepted is False
    assert result.ring_cleared is True
    assert ring.oldest_seq() is None
    assert ring.replay_floor_seq() == 2
    assert ring.high_water_seq() == 2
    assert ring.classify_seq(2) != "valid"
    # The next event is unaffected.
    ring.append(_entry(3))
    assert ring.oldest_seq() == 3


def test_the_two_budgets_are_counted_independently() -> None:
    frames_first = ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    for seq in (1, 2, 3):
        frames_first.append(_entry(seq, chunks=1))
    assert _frames_in(frames_first) == 2  # frames ran out, bytes nowhere near

    bytes_first = ReplayRing(budget=frames.FrameBudget(frames=100, encoded_bytes=40))
    for seq in (1, 2, 3):
        bytes_first.append(_entry(seq, size=20))
    assert _frames_in(bytes_first) == 1  # bytes ran out, frames nowhere near


def test_a_chunked_event_is_replayed_whole_after_breaking_midway() -> None:
    """Breaking at chunk k resends the whole event, once.

    Held against the frame budget: a chunked event that all but fills the
    ring must still be replayed whole and evicted whole, which is only true
    if the ring's unit of removal is the logical event rather than the frame.
    """
    from agent_alfred.evals.deterministic._web_broker_test_helpers import (
        user_message_with,
    )

    big = "é" * (200 * 1024)
    harness = Harness(
        max_frame_bytes=16 * 1024,
        ring=ReplayRing(budget=frames.FrameBudget(frames=27, encoded_bytes=1 << 22)),
    )
    first = harness.emit(RunStarted(purpose="chat"), run_id="r1")
    chunky = harness.emit(
        RunStarted(purpose="chat", user_message=user_message_with(big)),
        run_id="chunky",
    )
    stored = harness.broker._ring.entries_after(first.seq)
    assert len(stored) == 1
    assert len(stored[0].frames) > 1
    # One single-frame event plus one many-frame event: the budget counts
    # physical frames, so these two logical events are already at the limit.
    assert _frames_in(harness.broker._ring) == 1 + len(stored[0].frames) == 27
    # A third event pushes it over, and what goes is a whole logical event --
    # never the tail of the chunked one.
    harness.emit(RunStarted(purpose="chat"), run_id="r3")
    assert _frames_in(harness.broker._ring) <= 27
    surviving = harness.broker._ring.entries_after(first.seq) or ()
    assert chunky.seq in [entry.seq for entry in surviving]
    assert all(
        entry.seq != chunky.seq or entry.frames == stored[0].frames
        for entry in surviving
    )
    # Reconnecting from before the chunked event re-sends every chunk of it,
    # exactly once, rather than the chunks after the break.
    handle = harness.connect(cursor=cursor_for(first.seq))
    replayed = [item for item in drain_connection(handle) if item.seq == chunky.seq]
    assert len(replayed) == 1
    assert replayed[0].frames == stored[0].frames
