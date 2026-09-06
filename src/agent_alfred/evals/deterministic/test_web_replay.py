"""ReplayRing: the process-unique bounded store of replayable facts."""

from __future__ import annotations

import dis
import gc
import os
import subprocess
import sys
import threading
import weakref

import pytest

from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    interrupt_instruction_once,
)
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


def test_replay_selection_rejects_a_mid_publication_eviction_token(
    monkeypatch,
) -> None:
    """A moved head is not stable until its floor is published too."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    moved = threading.Event()
    release = threading.Event()
    real_drop = replay._IndexedEntries.drop_prefix

    def paused_drop(entries, count):
        retired = real_drop(entries, count)
        moved.set()
        assert release.wait(3.0)
        return retired

    monkeypatch.setattr(replay._IndexedEntries, "drop_prefix", paused_drop)
    writer = threading.Thread(target=lambda: ring.append(_entry(2)))
    writer.start()
    assert moved.wait(3.0)
    try:
        batch = ring.bounded_entries_after(
            0,
            2,
            frames.FrameBudget(frames=1, encoded_bytes=1 << 20),
        )
        assert batch.kind == "unavailable" or not ring.replay_batch_is_current(batch)
    finally:
        release.set()
        writer.join()


def test_publication_control_exit_leaves_an_odd_poison_not_a_stable_half_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook interruption cannot publish a stable-looking partial ring."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    cost_entered = threading.Event()
    release_cost = threading.Event()
    failure = KeyboardInterrupt("ring cost interrupted")
    real_cost = frames.PreparedFrames.ingress_cost

    def gated_cost(entry: frames.PreparedFrames) -> frames.FrameCost:
        if entry.seq == 1:
            cost_entered.set()
            assert release_cost.wait(3.0), "test did not release ring cost"
            raise failure
        return real_cost(entry)

    monkeypatch.setattr(frames.PreparedFrames, "ingress_cost", gated_cost)
    caught: list[BaseException] = []

    def publish() -> None:
        try:
            ring.observe_published(1, _entry(1))
        except BaseException as exc:  # noqa: BLE001 - identity asserted below
            caught.append(exc)

    publisher = threading.Thread(target=publish, name="poisoned-ring-writer")
    publisher.start()
    assert cost_entered.wait(3.0), "writer never entered cost binding"
    try:
        with pytest.raises(RuntimeError, match="interrupted publication"):
            ring.published_high_water_seq()
        assert (
            ring.bounded_entries_after(
                0,
                1,
                frames.FrameBudget(frames=1, encoded_bytes=1 << 20),
            ).kind
            == "unavailable"
        )
    finally:
        release_cost.set()
        publisher.join(3.0)
    assert not publisher.is_alive()

    assert caught == [failure]
    assert ring._generation % 2 == 1  # noqa: SLF001
    for read in (
        ring.published_high_water_seq,
        ring.high_water_seq,
        lambda: ring.classify_seq(1),
        lambda: ring.entries_after(0),
    ):
        with pytest.raises(RuntimeError, match="interrupted publication"):
            read()
    assert (
        ring.bounded_entries_after(
            0,
            1,
            frames.FrameBudget(frames=1, encoded_bytes=1 << 20),
        ).kind
        == "unavailable"
    )
    with pytest.raises(RuntimeError, match="interrupted publication") as raised:
        ring.observe_published(2, _entry(2))
    assert raised.value.__cause__ is failure


@pytest.mark.parametrize(
    "read_name",
    [
        "current_cost",
        "published_high_water",
        "high_water",
        "oldest",
        "classify",
        "entries_after",
        "startup_guard",
    ],
)
def test_public_read_never_returns_across_an_active_odd_generation(
    monkeypatch: pytest.MonkeyPatch,
    read_name: str,
) -> None:
    """A reader that began on even state must reject a concurrent half-write."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    read_checked_even = threading.Event()
    release_read = threading.Event()
    writer_is_odd = threading.Event()
    release_writer = threading.Event()
    real_require = ring._require_stable  # noqa: SLF001
    real_cost = frames.PreparedFrames.ingress_cost
    pause_once = True

    def gated_require() -> int:
        nonlocal pause_once
        generation = real_require()
        if threading.current_thread().name == "generation-reader" and pause_once:
            pause_once = False
            read_checked_even.set()
            assert release_read.wait(3.0), "test did not release reader"
        return generation

    def gated_cost(entry: frames.PreparedFrames) -> frames.FrameCost:
        if entry.seq == 2:
            writer_is_odd.set()
            assert release_writer.wait(3.0), "test did not release writer"
        return real_cost(entry)

    reads = {
        "current_cost": lambda: ring.current_cost,
        "published_high_water": ring.published_high_water_seq,
        "high_water": ring.high_water_seq,
        "oldest": ring.oldest_seq,
        "classify": lambda: ring.classify_seq(1),
        "entries_after": lambda: ring.entries_after(0),
        "startup_guard": lambda: ring.startup_guard_cost(0, 1),
    }
    monkeypatch.setattr(ring, "_require_stable", gated_require)
    monkeypatch.setattr(frames.PreparedFrames, "ingress_cost", gated_cost)
    outcomes: list[tuple[str, object]] = []

    def read() -> None:
        try:
            outcomes.append(("value", reads[read_name]()))
        except BaseException as exc:  # noqa: BLE001 - classified below
            outcomes.append(("error", exc))

    reader = threading.Thread(target=read, name="generation-reader")
    writer = threading.Thread(
        target=lambda: ring.observe_published(2, _entry(2)),
        name="generation-writer",
    )
    reader.start()
    assert read_checked_even.wait(3.0), "reader never checked even generation"
    writer.start()
    assert writer_is_odd.wait(3.0), "writer never published odd generation"
    release_read.set()
    reader.join(3.0)
    try:
        assert not reader.is_alive()
        assert writer.is_alive(), "writer did not remain parked in odd state"
        assert len(outcomes) == 1
        assert outcomes[0][0] == "error", outcomes
        assert isinstance(outcomes[0][1], RuntimeError)
    finally:
        release_writer.set()
        writer.join(3.0)
    assert not writer.is_alive()


def test_bounded_read_maps_mid_read_generation_drift_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal writer overlap is retryable unavailability, not ring failure."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    classify_entered = threading.Event()
    release_classify = threading.Event()
    writer_is_odd = threading.Event()
    release_writer = threading.Event()
    real_classify = ring.classify_seq
    real_cost = frames.PreparedFrames.ingress_cost

    def gated_classify(seq: int):
        classify_entered.set()
        assert release_classify.wait(3.0), "test did not release classification"
        return real_classify(seq)

    def gated_cost(entry: frames.PreparedFrames) -> frames.FrameCost:
        if entry.seq == 2:
            writer_is_odd.set()
            assert release_writer.wait(3.0), "test did not release writer"
        return real_cost(entry)

    monkeypatch.setattr(ring, "classify_seq", gated_classify)
    monkeypatch.setattr(frames.PreparedFrames, "ingress_cost", gated_cost)
    outcomes: list[tuple[str, object]] = []

    def read() -> None:
        try:
            outcomes.append(
                (
                    "value",
                    ring.bounded_entries_after(
                        0,
                        1,
                        frames.FrameBudget(frames=1, encoded_bytes=1 << 20),
                    ),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - classified below
            outcomes.append(("error", exc))

    reader = threading.Thread(target=read, name="bounded-generation-reader")
    writer = threading.Thread(
        target=lambda: ring.observe_published(2, _entry(2)),
        name="bounded-generation-writer",
    )
    reader.start()
    assert classify_entered.wait(3.0), "bounded reader never began classification"
    writer.start()
    assert writer_is_odd.wait(3.0), "writer never published odd generation"
    release_classify.set()
    reader.join(3.0)
    try:
        assert outcomes and outcomes[0][0] == "value", outcomes
        batch = outcomes[0][1]
        assert isinstance(batch, replay.ReplayBatch)
        assert batch.kind == "unavailable"
        assert writer.is_alive(), "writer did not remain parked in odd state"
    finally:
        release_writer.set()
        writer.join(3.0)
    assert not writer.is_alive()


def test_cursor_verdict_and_entries_share_one_ring_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid verdict cannot be paired with a later generation's empty tail."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    classified = threading.Event()
    release_reader = threading.Event()
    real_classify = ring.classify_seq
    pause_once = True

    def classify_then_pause(seq: int):
        nonlocal pause_once
        verdict = real_classify(seq)
        if threading.current_thread().name == "cursor-reader" and pause_once:
            pause_once = False
            assert verdict == "valid"
            classified.set()
            assert release_reader.wait(3.0), "test did not release cursor read"
        return verdict

    monkeypatch.setattr(ring, "classify_seq", classify_then_pause)
    outcomes: list[tuple[str, object]] = []

    def read() -> None:
        try:
            outcomes.append(
                (
                    "value",
                    replay.classify_cursor(
                        replay.format_cursor("inst", 0),
                        ring,
                        process_instance_id="inst",
                    ),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - classified below
            outcomes.append(("error", exc))

    reader = threading.Thread(target=read, name="cursor-reader")
    reader.start()
    assert classified.wait(3.0), "cursor reader never classified its old interval"
    ring.append(_entry(2))
    release_reader.set()
    reader.join(3.0)

    assert not reader.is_alive()
    assert len(outcomes) == 1
    assert outcomes[0][0] == "error", outcomes
    assert isinstance(outcomes[0][1], RuntimeError)
    assert ring.classify_seq(0) == "too_old"


def test_overwritten_cell_is_owned_before_append_can_interrupt_after_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a stale physical slot cannot orphan its previous cell."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    first = _entry(1)
    first_ref = weakref.ref(first)
    ring.append(first)
    del first
    prior_retirement = ring.observe_published(
        2, _entry(2), defer_retired_release=True
    )
    prior_retirement.acknowledge_retired_transfer()

    failure = SystemExit("indexed append interrupted after slot replacement")
    real_append = ring._entries.append  # noqa: SLF001

    def interrupt_after_append(
        entry: frames.PreparedFrames, cumulative: frames.FrameCost
    ):
        real_append(entry, cumulative)
        raise failure

    monkeypatch.setattr(ring._entries, "append", interrupt_after_append)  # noqa: SLF001

    with pytest.raises(SystemExit) as raised:
        ring.observe_published(3, _entry(3), defer_retired_release=True)
    assert raised.value is failure
    cleanup = ring.poisoned_cleanup()
    assert cleanup is not None
    cleanup()
    prior_retirement.release_retired()
    failure.__traceback__ = None
    gc.collect()
    assert first_ref() is None


def test_deferred_owner_is_recoverable_before_latest_token_publication() -> None:
    """The authoritative deferred registry is fully drainable without its hint."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    first = _entry(1)
    first_ref = weakref.ref(first)
    ring.append(first)
    del first
    failure = KeyboardInterrupt("deferred owner stored before token hint")

    class InterruptAfterFirstStore(dict[object, object]):
        armed = True

        def __setitem__(self, key: object, value: object) -> None:
            super().__setitem__(key, value)
            if self.armed:
                self.armed = False
                raise failure

    ring._deferred_retired = InterruptAfterFirstStore()  # noqa: SLF001

    with pytest.raises(KeyboardInterrupt) as raised:
        ring.observe_published(2, _entry(2), defer_retired_release=True)
    assert raised.value is failure
    assert ring.has_pending_cleanup() is True

    cleanup = ring.poisoned_cleanup()
    assert cleanup is not None
    cleanup()
    failure.__traceback__ = None
    del raised
    gc.collect()
    assert first_ref() is None
    assert ring.has_pending_cleanup() is False


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


@pytest.mark.parametrize("raw", ("9" * 5_000, "²"))
def test_cursor_text_rejects_unconvertible_digit_shapes(raw: str) -> None:
    assert replay.parse_cursor(f"inst:{raw}", "inst") == (None, "malformed")


def test_cursor_text_owns_its_decimal_length_boundary() -> None:
    largest_text = "1" + "0" * (replay.MAX_CURSOR_SEQ_DIGITS - 1)
    assert replay.parse_cursor(f"inst:{largest_text}", "inst") == (
        10 ** (replay.MAX_CURSOR_SEQ_DIGITS - 1),
        None,
    )
    assert replay.parse_cursor(f"inst:{largest_text}0", "inst") == (
        None,
        "malformed",
    )


@pytest.mark.parametrize(
    ("configured_limit", "expected_limit"),
    (
        (None, sys.int_info.default_max_str_digits),
        (0, 0),
        (10_000, 10_000),
    ),
)
def test_overlong_cursor_is_malformed_under_every_interpreter_digit_limit(
    configured_limit: int | None, expected_limit: int
) -> None:
    """The wire verdict belongs to the application, not Python startup."""
    environment = os.environ.copy()
    if configured_limit is None:
        environment.pop("PYTHONINTMAXSTRDIGITS", None)
    else:
        environment["PYTHONINTMAXSTRDIGITS"] = str(configured_limit)
    script = """
import sys
from agent_alfred.gateway.web.replay import parse_cursor

reason = parse_cursor("inst:" + "9" * 5_000, "inst")[1]
print(f"{sys.get_int_max_str_digits()}:{reason or 'accepted'}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.stdout.strip() == f"{expected_limit}:malformed"


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


@pytest.mark.parametrize(
    "process_instance_id",
    ("line\nfeed", "carriage\rreturn", "nul\0byte", "contains:colon", "snowman-☃"),
)
def test_format_cursor_refuses_an_instance_outside_its_ascii_wire_syntax(
    process_instance_id: str,
) -> None:
    with pytest.raises(ValueError, match="process_instance_id"):
        replay.format_cursor(process_instance_id, 42)


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


@pytest.mark.parametrize(
    "release_order",
    (
        ("clear", "fourth", "fifth"),
        ("fourth", "fifth", "clear"),
        ("fourth", "clear", "fifth"),
        ("fifth", "fourth", "clear"),
    ),
)
def test_deferred_clear_and_reused_stale_slots_release_in_any_order(
    release_order: tuple[str, str, str],
) -> None:
    """A post-clear append owns an overwritten cell without a new eviction."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=200)
    )
    first = _entry(1)
    second = _entry(2)
    first_ref = weakref.ref(first)
    second_ref = weakref.ref(second)
    ring.append(first)
    ring.append(second)
    del first, second

    cleared = ring.observe_published(
        3, _entry(3, size=200), defer_retired_release=True
    )
    assert cleared.accepted is False
    assert cleared.ring_cleared is True
    assert first_ref() is not None and second_ref() is not None

    fourth = ring.observe_published(
        4, _entry(4), defer_retired_release=True
    )
    fifth = ring.observe_published(
        5, _entry(5), defer_retired_release=True
    )

    assert fourth.accepted is True
    assert fifth.accepted is True
    assert len(ring) == 2
    assert ring.oldest_seq() == 4
    assert [entry.seq for entry in ring.entries_after(4) or ()] == [5]
    assert first_ref() is not None and second_ref() is not None

    releases = {
        "clear": cleared.release_retired,
        "fourth": fourth.release_retired,
        "fifth": fifth.release_retired,
    }
    for owner in release_order:
        releases[owner]()

    assert first_ref() is None and second_ref() is None
    assert len(ring) == 2
    assert ring.oldest_seq() == 4
    assert [entry.seq for entry in ring.entries_after(4) or ()] == [5]


def test_immediate_append_releases_only_its_post_clear_overwritten_cell() -> None:
    """The ordinary non-deferred append keeps the same cleanup ownership."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=200)
    )
    first = _entry(1)
    second = _entry(2)
    first_ref = weakref.ref(first)
    second_ref = weakref.ref(second)
    ring.append(first)
    ring.append(second)
    del first, second
    cleared = ring.observe_published(
        3, _entry(3, size=200), defer_retired_release=True
    )

    fourth = ring.append(_entry(4))

    assert fourth.accepted is True
    assert first_ref() is None
    assert second_ref() is not None
    assert len(ring) == 1
    assert ring.oldest_seq() == 4
    cleared.release_retired()
    assert second_ref() is None
    assert len(ring) == 1
    assert ring.oldest_seq() == 4


def test_concurrent_cleanup_survives_an_overwritten_cell_being_released() -> None:
    """One cleanup owns its loaded entry even if another clears the cell.

    The older cleanup captures the retired slot's cell, then a later publish
    reuses that slot and carries the same cell as ``overwritten``. Releasing
    the later publish clears the old cell while the first cleanup is paused
    after its first ``entry`` read. The first cleanup must neither dereference
    ``None`` nor clear the live cell installed by the later publisher.
    """

    class PausingCell:
        def __init__(self, entry: frames.PreparedFrames) -> None:
            self._entry: frames.PreparedFrames | None = entry
            self._paused = False

        @property
        def entry(self) -> frames.PreparedFrames | None:
            captured = self._entry
            if not self._paused:
                self._paused = True
                entry_captured.set()
                assert resume_old_cleanup.wait(3.0)
            return captured

        @entry.setter
        def entry(self, value: frames.PreparedFrames | None) -> None:
            self._entry = value

    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=2, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    ring.append(_entry(2))
    earlier = ring.observe_published(
        3, _entry(3), defer_retired_release=True
    )
    assert earlier._retired is not None
    retired_slot = earlier._retired._start
    original_cell = ring._entries._entries[retired_slot]
    assert original_cell is not None and original_cell.entry is not None
    entry_captured = threading.Event()
    resume_old_cleanup = threading.Event()
    old_cleanup_finished = threading.Event()
    errors: list[BaseException] = []
    pausing_cell = PausingCell(original_cell.entry)
    ring._entries._entries[retired_slot] = pausing_cell

    def release_earlier() -> None:
        try:
            earlier.release_retired()
        except BaseException as exc:
            errors.append(exc)
        finally:
            old_cleanup_finished.set()

    old_cleanup = threading.Thread(target=release_earlier)
    old_cleanup.start()
    try:
        assert entry_captured.wait(3.0)
        later = ring.observe_published(
            4, _entry(4), defer_retired_release=True
        )
        assert later._retired is not None
        assert later._retired._overwritten is pausing_cell
        later.release_retired()
    finally:
        resume_old_cleanup.set()
    assert old_cleanup_finished.wait(3.0)
    old_cleanup.join()

    assert errors == []
    assert pausing_cell.entry is None
    live_cell = ring._entries._entries[retired_slot]
    assert live_cell is not None and live_cell.entry is not None
    assert live_cell.entry.seq == 4
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


@pytest.mark.parametrize("boundary", ["claimed", "callback_returned"])
def test_retirement_instruction_exit_never_leaves_an_immortal_claim(
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup interruption always leaves the same owner retryable."""
    ring = replay.ReplayRing(
        budget=frames.FrameBudget(frames=1, encoded_bytes=1 << 20)
    )
    ring.append(_entry(1))
    result = ring.observe_published(
        2, _entry(2), defer_retired_release=True
    )
    retired = result._retired
    assert retired is not None
    real_release = ring._entries.release_retired
    release_calls = 0

    def counted_release(start: int, count: int, through_seq: int) -> None:
        nonlocal release_calls
        release_calls += 1
        real_release(start, count, through_seq)

    monkeypatch.setattr(ring._entries, "release_retired", counted_release)
    instructions = tuple(dis.get_instructions(replay._RetiredPrefix.release))
    if boundary == "claimed":
        claim_call = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "CALL"
            and any(
                prior.opname == "LOAD_ATTR"
                and prior.argval == "setdefault"
                for prior in instructions[max(0, index - 4) : index]
            )
        )
        target = instructions[claim_call + 1].offset
    else:
        released_store = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "STORE_ATTR"
            and instruction.argval == "_released"
        )
        target = instructions[released_store - 1].offset
    failure = KeyboardInterrupt(f"retirement {boundary} interrupted")

    with interrupt_instruction_once(
        replay._RetiredPrefix.release.__code__, target, failure
    ) as armed:
        with pytest.raises(KeyboardInterrupt) as raised:
            result.release_retired()

    assert armed == [False]
    assert raised.value is failure
    result.release_retired()
    result.release_retired()
    assert retired._released is True
    assert retired._release_in_progress is False
    assert release_calls == (1 if boundary == "claimed" else 2)


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
