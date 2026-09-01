"""ReplayRing: the process-unique bounded store of replayable facts."""

from __future__ import annotations

import weakref

import pytest

from agent_alfred.gateway.web import frames, replay


def _entry(seq: int, size: int = 8) -> frames.PreparedFrames:
    """One logical event of one frame, `size` payload bytes."""
    head = b"data: " + b"x" * size
    return frames.measured_frames(
        seq=seq, frames=(head,), id_line=b"id: inst:%d\n" % seq, replayable=True
    )


# --- atomic logical events -------------------------------------------------


def test_an_event_is_evicted_whole_never_half() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    assert ring.oldest_seq() == 2
    assert [entry.seq for entry in ring.entries_after(1) or ()] == [2, 3]
    # No half event exists: every entry still carries all of its frames.
    for entry in ring.entries_after(1) or ():
        assert entry.frames


def test_a_multi_frame_event_keeps_every_frame_together() -> None:
    """Eviction is in physical frames, removal is in logical events.

    The budget that runs out first is the frame count -- three frames for
    the chunked event plus one each for its neighbours -- and what it costs
    is a whole logical event, never the tail of one.
    """
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=5, encoded_bytes=1 << 20))
    chunked = frames.measured_frames(
        seq=7,
        frames=(b"data: a", b"data: b", b"data: c"),
        id_line=b"id: inst:7\n",
        replayable=True,
    )
    ring.append(_entry(6))
    ring.append(chunked)
    ring.append(_entry(8))
    replayed = ring.entries_after(6)
    assert replayed is not None
    assert [entry.seq for entry in replayed] == [7, 8]
    assert replayed[0].frames == (b"data: a", b"data: b", b"data: c")
    # One more single-frame event takes the ring to five frames, so the
    # chunked event goes whole rather than losing its first frame.
    ring.append(_entry(9))
    assert [entry.seq for entry in ring.entries_after(7) or ()] == [8, 9]
    assert all(
        entry.seq != 7 or entry.frames == chunked.frames
        for entry in ring.entries_after(7) or ()
    )


def test_replay_floor_advances_monotonically_and_never_goes_back() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    assert ring.replay_floor_seq() == 0
    for seq in (1, 2, 3, 4):
        ring.append(_entry(seq))
    assert ring.replay_floor_seq() == 2
    # Seq is a publication order, so a re-used or reordered seq is refused
    # outright rather than silently rewriting history behind the floor.
    with pytest.raises(ValueError):
        ring.append(_entry(1))
    assert ring.replay_floor_seq() == 2


def test_an_empty_ring_distinguishes_never_used_from_run_over() -> None:
    fresh = replay.ReplayRing(
        budget=frames.FrameBudget(frames=4, encoded_bytes=1 << 20)
    )
    assert fresh.replay_floor_seq() == 0
    assert fresh.oldest_seq() is None
    assert fresh.emitted_any() is False

    cleared = replay.ReplayRing(
        budget=frames.FrameBudget(frames=4, encoded_bytes=1 << 20)
    )
    for seq in (1, 2, 3):
        cleared.append(_entry(seq))
    cleared.append(_entry(4, size=(1 << 20) + 1))
    assert cleared.oldest_seq() is None
    assert cleared.replay_floor_seq() == 4
    assert cleared.emitted_any() is True


# --- byte budget -----------------------------------------------------------


def test_a_startup_batch_stops_at_the_byte_budget_before_the_frame_budget() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=8, encoded_bytes=1 << 20)
    )
    entries = tuple(_entry(seq, size=700) for seq in (1, 2, 3))
    for entry in entries:
        ring.append(entry)
    first_cost = entries[0].ingress_cost()
    budget = frames.FrameBudget(
        frames=8,
        encoded_bytes=first_cost.encoded_bytes + 1,
    )

    batch = ring.bounded_entries_after(0, 3, budget)

    assert batch.kind == "batch"
    assert batch.entries == (entries[0],)
    assert batch.cost.frames < budget.frames
    assert budget.fits(batch.cost)
    assert not budget.fits(batch.cost + entries[1].ingress_cost())


def test_one_logical_event_is_sliced_by_bytes_without_moving_its_checkpoint() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    entry = frames.measured_frames(
        seq=1,
        frames=(b"data: " + b"a" * 200,) * 3,
        id_line=b"id: inst:1\n",
        replayable=True,
    )
    ring.append(entry)
    one_final_frame = len(entry.frames[-1]) + 2 + len(entry.id_line)
    budget = frames.FrameBudget(frames=3, encoded_bytes=one_final_frame)
    progress = replay.ReplayProgress(completed_seq=0)
    slices = []

    while True:
        batch = ring.bounded_entries_after(progress, 1, budget)
        if batch.kind == "complete":
            break
        assert batch.kind == "batch"
        assert budget.fits(batch.cost)
        assert batch.cost.frames == 1
        slices.extend(batch.entries)
        assert batch.next_progress is not None
        progress = batch.next_progress

    assert [item.id_line for item in slices] == [b"", b"", entry.id_line]
    assert b"".join(item.wire_bytes() for item in slices) == entry.wire_bytes()


def test_byte_budget_evicts_before_the_frame_limit_is_reached() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=100, encoded_bytes=100))
    for seq in (1, 2, 3):
        ring.append(_entry(seq, size=40))
    # The frame limit is nowhere near reached; the byte budget did the work.
    assert len(ring) == 1
    assert [entry.seq for entry in ring.entries_after(2) or ()] == [3]


def test_one_event_over_the_byte_budget_clears_the_ring() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=100, encoded_bytes=1 << 10)
    )
    for seq in (1, 2, 3):
        ring.append(_entry(seq, size=128))
    assert [entry.seq for entry in ring.entries_after(1) or ()] == [2, 3]

    result = ring.append(_entry(4, size=(1 << 10) + 1))
    assert result.ring_cleared is True
    assert result.accepted is False
    assert ring.oldest_seq() is None
    # The floor jumped past everything, so any earlier cursor is now too old.
    assert ring.replay_floor_seq() == 4
    verdict = ring.classify_seq(3)
    assert verdict == "too_old"


def test_an_oversized_event_cannot_be_replayed_from_the_ring() -> None:
    """It advances the boundary without ever becoming a checkpoint.

    The ring moved past it, but it never issued an ``id:`` for it, so a
    cursor naming it is a client asking for a fact this process does not
    hold -- and the answer has to be a gap, not a silent resume.
    """
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=100, encoded_bytes=1 << 10)
    )
    ring.append(_entry(1, size=(1 << 10) + 1))
    ring.append(_entry(2, size=8))
    assert ring.replay_floor_seq() == 1
    assert ring.classify_seq(1) != "valid"
    assert ring.entries_after(1) is None
    # The next event is a real checkpoint and is still reachable.
    assert ring.classify_seq(2) == "valid"
    assert ring.oldest_seq() == 2


# --- cursor classification -------------------------------------------------


def test_cursors_that_were_never_issued_are_not_valid_checkpoints() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    ring.append(_entry(2))
    # seq 3 has not been produced yet; a seq inside the range that only a
    # transient event consumed is not a checkpoint either.
    assert ring.classify_seq(3) == "ahead"
    # seq 0 is the one exception, and it is not an event position: it is the
    # reserved boundary before the first event, and a ring that has lost
    # nothing can prove continuity from there.
    assert ring.classify_seq(0) == "valid"
    assert ring.entries_after(0) == tuple(ring._entries)
    # 1 and 2 were issued and are still in the ring.
    assert ring.classify_seq(1) == "valid"
    assert ring.classify_seq(2) == "valid"


def test_the_startup_boundary_dies_with_the_first_unrecoverable_loss() -> None:
    """Zero is a starting point, not a free pass.

    Once the ring has dropped anything it cannot prove what happened between
    the beginning and here, so the reserved boundary stops being a position
    a client may stand on -- and says so, rather than replaying a hole.
    """
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 10)
    )
    ring.append(_entry(1))
    ring.append(_entry(2, size=(1 << 10) + 1))  # unrecoverable
    assert ring.replay_floor_seq() == 2
    assert ring.classify_seq(0) == "too_old"
    assert ring.entries_after(0) is None


def test_a_transient_seq_in_the_middle_is_not_a_checkpoint() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    ring.append(_entry(3))
    assert ring.classify_seq(2) == "malformed"


def test_a_cursor_on_the_floor_is_still_usable() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    for seq in (1, 2, 3, 4):
        ring.append(_entry(seq))
    assert ring.replay_floor_seq() == 2
    assert ring.classify_seq(2) == "valid"
    assert [entry.seq for entry in ring.entries_after(2) or ()] == [3, 4]


def test_entries_after_a_gap_cursor_is_none_not_a_silent_partial() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    assert ring.entries_after(0) is None
    assert ring.entries_after(99) is None


def test_high_water_tracks_every_appended_event() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    for seq in (1, 2, 3, 4, 5):
        ring.append(_entry(seq))
    assert ring.high_water_seq() == 5
    assert ring.latest_complete_seq() == 5


# --- the published high water ----------------------------------------------


def test_a_published_transient_advances_only_the_published_high_water() -> None:
    """A transient consumes a seq without minting a checkpoint.

    The high water the classification answers with is the one the process
    actually published -- transient included -- so a cursor naming the
    transient's seq is refused as *published but never issued*
    (``malformed``), not ``ahead`` (which would claim it was never sent).
    The transient itself never enters the ring.
    """
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    ring.observe_published(2, None)
    assert ring.published_high_water_seq() == 2
    assert ring.high_water_seq() == 1
    assert len(ring) == 1
    assert ring.latest_complete_seq() == 1
    assert ring.reseed_boundary_seq() == 1
    # Published but never issued: malformed, not ahead.
    assert ring.classify_seq(2) == "malformed"
    # One past everything published: ahead, as before.
    assert ring.classify_seq(3) == "ahead"
    # The next replayable event lands on the transient's successor and is a
    # checkpoint like any other.
    ring.append(_entry(3))
    assert ring.classify_seq(3) == "valid"
    assert [entry.seq for entry in ring.entries_after(1) or ()] == [3]


def test_published_high_water_is_monotonic_across_both_kinds() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    ring.observe_published(1, None)
    with pytest.raises(ValueError):
        ring.observe_published(1, None)
    with pytest.raises(ValueError):
        ring.observe_published(1, _entry(2))
    # The one-call shape: advancing and appending are the same step, so no
    # observer can see a published seq whose entry is not there yet.
    result = ring.observe_published(2, _entry(2))
    assert result.accepted is True
    assert ring.published_high_water_seq() == 2
    assert ring.classify_seq(2) == "valid"


def test_append_is_observe_published_with_the_entry_given() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    assert ring.published_high_water_seq() == 3
    assert ring.high_water_seq() == 3
    assert ring.classify_seq(3) == "valid"
    # A seq at or below the published high water is refused, transient or
    # not: publication is a linear order.
    with pytest.raises(ValueError):
        ring.observe_published(3, _entry(4))


# --- the Last-Event-ID text ------------------------------------------------


def test_cursor_text_parses_only_the_issued_shape() -> None:
    assert replay.parse_cursor("inst:7", "inst") == (7, None)
    assert replay.parse_cursor("inst:7", "other")[1] == "instance_mismatch"
    assert replay.parse_cursor("nonsense", "inst")[1] == "malformed"
    assert replay.parse_cursor("", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:-1", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:7:8", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:x", "inst")[1] == "malformed"


def test_the_reserved_startup_cursor_parses_and_formats() -> None:
    """``instance:0`` is a real cursor, and the only non-positive one.

    It names the boundary before the first domain event. Refusing to read it
    back would turn every client that connected before the first event into
    a client whose history cannot be read.
    """
    assert replay.parse_cursor("inst:0", "inst") == (0, None)
    assert replay.format_cursor("inst", 0) == "inst:0"
    # Padded zeros are still junk: the reserved boundary is one spelling.
    assert replay.parse_cursor("inst:00", "inst")[1] == "malformed"
    with pytest.raises(ValueError):
        replay.format_cursor("inst", -1)


def test_format_cursor_is_the_inverse_of_parse() -> None:
    assert replay.format_cursor("inst-1", 42) == "inst-1:42"
    assert replay.parse_cursor(replay.format_cursor("inst-1", 42), "inst-1") == (
        42,
        None,
    )


def test_a_well_formed_cursor_beyond_the_ring_is_ahead() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    verdict = replay.classify_cursor(
        replay.CursorText("inst:9"), ring, process_instance_id="inst"
    )
    assert verdict.kind == "gap"
    assert verdict.reason == "ahead"


def test_a_valid_cursor_replays_exactly_the_missing_tail() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    verdict = replay.classify_cursor(
        replay.CursorText("inst:1"), ring, process_instance_id="inst"
    )
    assert verdict.kind == "valid"
    assert [entry.seq for entry in verdict.entries] == [2, 3]
    assert verdict.reseed_seq == 1


def test_the_first_connection_has_no_cursor_and_is_not_a_gap() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=10, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    verdict = replay.classify_cursor(None, ring, process_instance_id="inst")
    assert verdict.kind == "absent"
    assert verdict.reason is None


def test_a_cleared_ring_answers_too_old_for_surviving_cursors() -> None:
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=100, encoded_bytes=1 << 10)
    )
    for seq in (1, 2):
        ring.append(_entry(seq, size=8))
    ring.append(_entry(3, size=(1 << 10) + 1))
    verdict = replay.classify_cursor(
        replay.CursorText("inst:2"), ring, process_instance_id="inst"
    )
    assert verdict.kind == "gap"
    assert verdict.reason == "too_old"


def test_capacity_defaults_match_the_decided_table() -> None:
    ring = replay.ReplayRing()
    # Physical frames, not logical events: the name is the assertion.
    assert ring.budget == frames.FrameBudget(2048, 32 * 1024 * 1024)


# --- the named cost of the ring's accounting --------------------------------


def test_the_ring_capacity_contract_holds_per_dimension() -> None:
    capacity = frames.FrameBudget(frames=2048, encoded_bytes=32 * 1024 * 1024)
    ring = replay.ReplayRing()
    assert ring.budget == capacity


def test_the_ring_judges_each_dimension_of_the_cost_independently() -> None:
    # The frame dimension binds while the bytes sit far under their budget.
    frame_bound = replay.ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    for seq in (1, 2, 3):
        frame_bound.append(_entry(seq))
    assert len(frame_bound) == 2
    # The byte dimension binds while the frame count sits far under its
    # budget.
    byte_bound = replay.ReplayRing(
        budget=frames.FrameBudget(frames=100, encoded_bytes=100)
    )
    for seq in (1, 2, 3):
        byte_bound.append(_entry(seq, size=40))
    assert len(byte_bound) == 1
    survivors = byte_bound.entries_after(2) or ()
    assert byte_bound.current_cost == survivors[0].ingress_cost()


def test_eviction_leaves_exactly_the_survivors_cost() -> None:
    """Eviction subtracts in the unit it added: whole logical events."""
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    assert ring.current_cost == (
        _entry(2).ingress_cost() + _entry(3).ingress_cost()
    )


def test_one_append_does_not_walk_the_evicted_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legal large event cannot charge commit for every displaced event."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=64, encoded_bytes=1 << 20)
    )
    for seq in range(1, 65):
        ring.append(_entry(seq))

    cost_calls = 0
    original_ingress_cost = frames.PreparedFrames.ingress_cost

    def counted_ingress_cost(entry: frames.PreparedFrames) -> frames.FrameCost:
        nonlocal cost_calls
        cost_calls += 1
        return original_ingress_cost(entry)

    monkeypatch.setattr(frames.PreparedFrames, "ingress_cost", counted_ingress_cost)
    large = frames.measured_frames(
        seq=65,
        frames=tuple(b"data: large" for _ in range(48)),
        id_line=b"id: inst:65\n",
        replayable=True,
    )

    result = ring.append(large)

    assert result.accepted is True
    assert cost_calls == 1
    assert [entry.seq for entry in ring.entries_after(48) or ()] == list(
        range(49, 66)
    )


def test_delayed_cleanup_never_releases_a_reused_live_slot() -> None:
    """A later publisher may reuse a slot before an earlier cleanup runs."""
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20))
    first = _entry(1)
    first_ref = weakref.ref(first)
    ring.append(first)
    ring.append(_entry(2))
    del first

    earlier = ring.observe_published(
        3, _entry(3), defer_retired_release=True
    )
    later = ring.observe_published(4, _entry(4), defer_retired_release=True)
    # Reusing seq 1's physical slot under the later publication does not
    # decrement its final reference there: the later cleanup carries it out.
    assert first_ref() is not None

    later.release_retired()
    assert first_ref() is None
    earlier.release_retired()
    assert [entry.seq for entry in ring.entries_after(2) or ()] == [3, 4]


def test_failed_retirement_release_is_retryable_then_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20))
    ring.append(_entry(1))
    result = ring.observe_published(
        2, _entry(2), defer_retired_release=True
    )
    original_release = ring._entries.release_retired
    attempts = 0

    def fail_once(start: int, count: int, through_seq: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("release failed")
        original_release(start, count, through_seq)

    monkeypatch.setattr(ring._entries, "release_retired", fail_once)

    with pytest.raises(RuntimeError, match="release failed"):
        result.release_retired()
    result.release_retired()
    result.release_retired()

    assert attempts == 2


def test_ring_accounting_returns_exactly_to_zero() -> None:
    ring = replay.ReplayRing(budget=frames.FrameBudget(frames=4, encoded_bytes=200))
    first, second = _entry(1), _entry(2)
    ring.append(first)
    ring.append(second)
    assert ring.current_cost == first.ingress_cost() + second.ingress_cost()
    # The whole-count discard leaves nothing behind: not one frame, not one
    # byte.
    ring.append(_entry(3, size=200))
    assert ring.current_cost == frames.FrameCost(frames=0, encoded_bytes=0)
    assert ring.oldest_seq() is None
    assert ring.replay_floor_seq() == 3
