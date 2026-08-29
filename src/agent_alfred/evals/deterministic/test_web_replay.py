"""ReplayRing: the process-unique bounded store of replayable facts."""

from __future__ import annotations

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
    ring = replay.ReplayRing(max_entries=2, max_bytes=1 << 20)
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    assert ring.oldest_seq() == 2
    assert [entry.seq for entry in ring.entries_after(1) or ()] == [2, 3]
    # No half event exists: every entry still carries all of its frames.
    for entry in ring.entries_after(1) or ():
        assert entry.frames


def test_a_multi_frame_event_keeps_every_frame_together() -> None:
    ring = replay.ReplayRing(max_entries=3, max_bytes=1 << 20)
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


def test_replay_floor_advances_monotonically_and_never_goes_back() -> None:
    ring = replay.ReplayRing(max_entries=2, max_bytes=1 << 20)
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
    fresh = replay.ReplayRing(max_entries=4, max_bytes=1 << 20)
    assert fresh.replay_floor_seq() == 0
    assert fresh.oldest_seq() is None
    assert fresh.emitted_any() is False

    cleared = replay.ReplayRing(max_entries=4, max_bytes=1 << 20)
    for seq in (1, 2, 3):
        cleared.append(_entry(seq))
    cleared.append(_entry(4, size=(1 << 20) + 1))
    assert cleared.oldest_seq() is None
    assert cleared.replay_floor_seq() == 4
    assert cleared.emitted_any() is True


# --- byte budget -----------------------------------------------------------


def test_byte_budget_evicts_before_the_frame_limit_is_reached() -> None:
    ring = replay.ReplayRing(max_entries=100, max_bytes=100)
    for seq in (1, 2, 3):
        ring.append(_entry(seq, size=40))
    # The frame limit is nowhere near reached; the byte budget did the work.
    assert len(ring) == 1
    assert [entry.seq for entry in ring.entries_after(2) or ()] == [3]


def test_one_event_over_the_byte_budget_clears_the_ring() -> None:
    ring = replay.ReplayRing(max_entries=100, max_bytes=1 << 10)
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
    ring = replay.ReplayRing(max_entries=100, max_bytes=1 << 10)
    ring.append(_entry(1, size=(1 << 10) + 1))
    ring.append(_entry(2, size=8))
    assert [entry.seq for entry in ring.entries_after(1) or ()] == [2]


# --- cursor classification -------------------------------------------------


def test_cursors_that_were_never_issued_are_not_valid_checkpoints() -> None:
    ring = replay.ReplayRing(max_entries=10, max_bytes=1 << 20)
    ring.append(_entry(1))
    ring.append(_entry(2))
    # seq 3 has not been produced yet; seq 0 is not a checkpoint; a seq inside
    # the range that only a transient event consumed is not one either.
    assert ring.classify_seq(0) == "malformed"
    assert ring.classify_seq(3) == "ahead"
    # 1 and 2 were issued and are still in the ring.
    assert ring.classify_seq(1) == "valid"
    assert ring.classify_seq(2) == "valid"


def test_a_transient_seq_in_the_middle_is_not_a_checkpoint() -> None:
    ring = replay.ReplayRing(max_entries=10, max_bytes=1 << 20)
    ring.append(_entry(1))
    ring.append(_entry(3))
    assert ring.classify_seq(2) == "malformed"


def test_a_cursor_on_the_floor_is_still_usable() -> None:
    ring = replay.ReplayRing(max_entries=2, max_bytes=1 << 20)
    for seq in (1, 2, 3, 4):
        ring.append(_entry(seq))
    assert ring.replay_floor_seq() == 2
    assert ring.classify_seq(2) == "valid"
    assert [entry.seq for entry in ring.entries_after(2) or ()] == [3, 4]


def test_entries_after_a_gap_cursor_is_none_not_a_silent_partial() -> None:
    ring = replay.ReplayRing(max_entries=2, max_bytes=1 << 20)
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    assert ring.entries_after(0) is None
    assert ring.entries_after(99) is None


def test_high_water_tracks_every_appended_event() -> None:
    ring = replay.ReplayRing(max_entries=2, max_bytes=1 << 20)
    for seq in (1, 2, 3, 4, 5):
        ring.append(_entry(seq))
    assert ring.high_water_seq() == 5
    assert ring.latest_complete_seq() == 5


# --- the Last-Event-ID text ------------------------------------------------


def test_cursor_text_parses_only_the_issued_shape() -> None:
    assert replay.parse_cursor("inst:7", "inst") == (7, None)
    assert replay.parse_cursor("inst:7", "other")[1] == "instance_mismatch"
    assert replay.parse_cursor("nonsense", "inst")[1] == "malformed"
    assert replay.parse_cursor("", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:0", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:-1", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:7:8", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:x", "inst")[1] == "malformed"


def test_format_cursor_is_the_inverse_of_parse() -> None:
    assert replay.format_cursor("inst-1", 42) == "inst-1:42"
    assert replay.parse_cursor(replay.format_cursor("inst-1", 42), "inst-1") == (
        42,
        None,
    )


def test_a_well_formed_cursor_beyond_the_ring_is_ahead() -> None:
    ring = replay.ReplayRing(max_entries=10, max_bytes=1 << 20)
    ring.append(_entry(1))
    verdict = replay.classify_cursor(
        replay.CursorText("inst:9"), ring, process_instance_id="inst"
    )
    assert verdict.kind == "gap"
    assert verdict.reason == "ahead"


def test_a_valid_cursor_replays_exactly_the_missing_tail() -> None:
    ring = replay.ReplayRing(max_entries=10, max_bytes=1 << 20)
    for seq in (1, 2, 3):
        ring.append(_entry(seq))
    verdict = replay.classify_cursor(
        replay.CursorText("inst:1"), ring, process_instance_id="inst"
    )
    assert verdict.kind == "valid"
    assert [entry.seq for entry in verdict.entries] == [2, 3]
    assert verdict.reseed_seq == 1


def test_the_first_connection_has_no_cursor_and_is_not_a_gap() -> None:
    ring = replay.ReplayRing(max_entries=10, max_bytes=1 << 20)
    ring.append(_entry(1))
    verdict = replay.classify_cursor(None, ring, process_instance_id="inst")
    assert verdict.kind == "absent"
    assert verdict.reason is None


def test_a_cleared_ring_answers_too_old_for_surviving_cursors() -> None:
    ring = replay.ReplayRing(max_entries=100, max_bytes=1 << 10)
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
    assert ring.max_entries == 2048
    assert ring.max_bytes == 32 * 1024 * 1024


def test_a_ring_rejects_a_non_positive_capacity() -> None:
    with pytest.raises(ValueError):
        replay.ReplayRing(max_entries=0)
    with pytest.raises(ValueError):
        replay.ReplayRing(max_bytes=0)
