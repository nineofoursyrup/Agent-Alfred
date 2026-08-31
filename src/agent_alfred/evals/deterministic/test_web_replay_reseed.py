"""SSE reconnect re-seed and gap contracts."""

from __future__ import annotations

import pytest

from agent_alfred.evals.deterministic._web_broker_test_helpers import (
    Harness,
    cursor_for,
    drain_connection,
)
from agent_alfred.events import RunStarted
from agent_alfred.gateway.web import frames, replay
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.replay import ReplayRing, classify_cursor

# A RunStarted frame is ~467 bytes, so 512 admits one and refuses a padded
# one -- the two events a test needs in order to reach the clear path with
# a checkpoint already issued in front of it.
_TIGHT_BYTES = 512


def _entry(seq: int, size: int = 8, chunks: int = 1) -> PreparedFrames:
    head = b"data: " + b"x" * size
    return frames.measured_frames(
        seq=seq,
        frames=tuple(head for _ in range(chunks)),
        id_line=b"id: inst:%d\n" % seq,
        replayable=True,
    )


# --- the re-seed every stream owes -----------------------------------------


def _wire_ids(items) -> list[int]:
    """Every ``id:`` line a browser would see, in order.

    The re-seed carries its id *in the frame body*, not in ``id_line``, so
    reading the wire is the only way to see what the client's ``lastEventId``
    becomes.
    """
    out: list[int] = []
    for item in items:
        if not isinstance(item, PreparedFrames):
            continue
        for line in item.wire_bytes().split(b"\n"):
            if line.startswith(b"id: "):
                out.append(int(line.split(b":")[-1]))
    return out


def _browser_cursor(items) -> str | None:
    """The ``Last-Event-ID`` the browser would send on the next connection.

    An SSE client sends the header only when its id buffer is non-empty, so
    "no id was ever written" and "the header is absent" are the same fact --
    and both make the next connection look like a first one.
    """
    ids = _wire_ids(items)
    return cursor_for(ids[-1]) if ids else None


def test_the_reserved_startup_checkpoint_is_a_real_cursor() -> None:
    """``instance:0`` names the boundary before the first event.

    It is not an event position, so it consumes no seq; it is a transport
    boundary this process owns, valid as a starting point on a ring that has
    never lost anything.
    """
    assert replay.STARTUP_CHECKPOINT_SEQ == 0
    assert replay.format_cursor("inst", 0) == "inst:0"
    assert replay.parse_cursor("inst:0", "inst") == (0, None)
    # It is process-specific like every other cursor.
    assert replay.parse_cursor("inst:0", "other")[1] == "instance_mismatch"
    # And it is the only zero: a padded or negative position is still junk.
    assert replay.parse_cursor("inst:00", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:-1", "inst")[1] == "malformed"


def test_a_clean_empty_ring_accepts_the_startup_checkpoint() -> None:
    ring = ReplayRing()
    assert ring.classify_seq(0) == "valid"
    assert ring.entries_after(0) == ()
    # The ring has issued nothing, so there is no *event* checkpoint yet --
    # the reserved boundary is not one.
    assert ring.latest_complete_seq() is None
    assert ring.reseed_boundary_seq() is None


def test_the_first_frame_of_an_empty_ring_is_the_reseed() -> None:
    """Before any data, always -- even when there is nothing to replay.

    The dispatch algorithm copies the id buffer even for a dataless frame,
    so a stream that opens with a snapshot and no ``id:`` leaves the browser
    holding an empty buffer -- and an empty buffer means no ``Last-Event-ID``
    on the next connection, which is indistinguishable from a client that
    never connected.
    """
    harness = Harness()
    items = drain_connection(harness.connect())
    assert items[0].wire_bytes() == b"retry: 1000\n\n"
    assert items[1].wire_bytes() == b"id: inst-test:0\n\n"
    # The snapshot follows the re-seed, never precedes it.
    assert any(b"event: state_patch" in item.wire_bytes() for item in items[2:])
    assert _browser_cursor(items) == cursor_for(0)


def test_the_startup_checkpoint_gives_way_to_a_real_one() -> None:
    """The reserved boundary is a start, not a permanent answer."""
    harness = Harness()
    assert _wire_ids(drain_connection(harness.connect())) == [0]

    harness.emit_many(3)
    ring = harness.broker._ring  # noqa: SLF001
    # Real checkpoints advanced; the reserved one is still valid underneath
    # them because nothing has been lost.
    assert ring.latest_complete_seq() == 3
    assert ring.reseed_boundary_seq() == 3
    assert ring.classify_seq(0) == "valid"

    again = drain_connection(harness.connect(cursor=cursor_for(0)))
    assert _wire_ids(again) == [0, 1, 2, 3]
    # And a real checkpoint resumes exactly, as before.
    assert _wire_ids(drain_connection(harness.connect(cursor=cursor_for(1)))) == [
        1,
        2,
        3,
    ]


def test_a_first_event_over_budget_still_plants_a_cursor() -> None:
    """An unrecoverable loss is reported, never swallowed.

    The very first event does not fit, so the ring is cleared and the floor
    moves past it. There is no issued checkpoint to re-plant -- but planting
    nothing would erase the client's cursor, and a client with no cursor
    cannot be told it has a gap. The reserved boundary is planted instead,
    and it classifies as ``too_old`` for exactly the reason the client needs
    to hear.
    """
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=8))
    harness.emit_many(1)
    ring = harness.broker._ring  # noqa: SLF001
    assert ring.replay_floor_seq() == 1
    assert ring.latest_complete_seq() is None

    items = drain_connection(harness.connect())
    assert _wire_ids(items) == [0]
    assert ring.classify_seq(0) == "too_old"

    # The browser's cursor is not empty, so it comes back and is told again.
    cursor = _browser_cursor(items)
    assert cursor == cursor_for(0)
    again = drain_connection(harness.connect(cursor=cursor))
    notice = next(item for item in again if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"too_old"' in notice.wire_bytes()
    # And it is re-seeded again, so the gap survives as many reconnects as
    # it takes rather than vanishing on the second one.
    assert _browser_cursor(again) == cursor_for(0)


def test_a_cleared_ring_keeps_reporting_the_gap() -> None:
    """A last issued checkpoint is a re-seed boundary, not a debt.

    After an unrecoverable clear the ring cannot reproduce the checkpoint it
    issued. Keeping it as the re-seed is deliberate: the alternative is an
    empty browser cursor, and an empty cursor is a client that looks like it
    never connected -- which is the one shape that never gets a gap notice.
    """
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=_TIGHT_BYTES))
    harness.emit_many(1)  # seq 1, small enough to be issued
    # seq 2, padded past the byte budget: the ring is cleared.
    harness.emit(RunStarted(purpose="chat"), run_id="r" + "x" * 400)
    ring = harness.broker._ring  # noqa: SLF001
    assert ring.high_water_seq() == 2
    assert ring.replay_floor_seq() == 2
    assert ring.latest_complete_seq() is None
    assert ring.reseed_boundary_seq() == 1

    items = drain_connection(harness.connect())
    assert _wire_ids(items) == [1]
    cursor = _browser_cursor(items)
    assert cursor == cursor_for(1)

    again = drain_connection(harness.connect(cursor=cursor))
    notice = next(item for item in again if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"too_old"' in notice.wire_bytes()
    assert b'"requested_seq":1' in notice.wire_bytes()
    # Re-planted, so the next reconnect reports the gap again.
    assert _browser_cursor(again) == cursor_for(1)


def test_a_reconnect_after_an_empty_ring_is_not_a_first_connection() -> None:
    """The cursor a client keeps lets the next connection be classified.

    A stream that opened on an empty ring has nothing to replay, which is
    exactly why it owes a re-seed: without one the browser's id buffer ends
    up empty, it sends no ``Last-Event-ID``, and the server reads a second
    connection from the same client as a first connection from a new one.
    """
    harness = Harness()
    first = drain_connection(harness.connect())
    cursor = _browser_cursor(first)
    assert cursor == cursor_for(0)

    again = drain_connection(harness.connect(cursor=cursor))
    # Not a gap: the client is resuming, and it resumed from a boundary the
    # server recognises as its own.
    assert not any(b"replay_gap" in item.wire_bytes() for item in again)
    assert _wire_ids(again) == [0]


def test_a_gap_that_survives_a_disconnect_does_not_go_quiet() -> None:
    """A client that drops out mid-notice is still owed the notice.

    The notice and the snapshot carry no id, so the browser's cursor after
    reading them is whatever the re-seed planted. What matters is that it is
    a value the server can classify -- never an empty buffer that turns the
    next connection into a first one.
    """
    harness = Harness()
    harness.emit_many(3)
    interrupted = drain_connection(harness.connect(cursor="garbage"))
    assert any(b"replay_gap" in item.wire_bytes() for item in interrupted)
    cursor = _browser_cursor(interrupted)
    assert cursor == cursor_for(3)
    # Reconnecting with it resumes exactly: the gap was about a cursor that
    # could not be read, not about lost facts.
    resumed = drain_connection(harness.connect(cursor=cursor))
    assert not any(b"replay_gap" in item.wire_bytes() for item in resumed)
    assert _wire_ids(resumed) == [3]

    # A cursor the ring cannot honour, by contrast, reports a gap every time
    # it comes back -- and still leaves the client holding a real boundary.
    lost = drain_connection(harness.connect(cursor=cursor_for(9)))
    notice = next(item for item in lost if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"ahead"' in notice.wire_bytes()
    assert _browser_cursor(lost) == cursor_for(3)


def test_the_replay_order_is_retry_reseed_gap_patch_then_events() -> None:
    """The decided order appears on the wire in one place."""
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=_TIGHT_BYTES))
    harness.emit_many(1)
    harness.emit(RunStarted(purpose="chat"), run_id="r" + "x" * 400)
    items = drain_connection(harness.connect(cursor=cursor_for(1)))
    kinds = []
    for item in items:
        wire = item.wire_bytes()
        if wire.startswith(b"retry: "):
            kinds.append("retry")
        elif wire.startswith(b"id: "):
            kinds.append("reseed")
        elif b"replay_gap" in wire:
            kinds.append("gap")
        elif b"event: state_patch" in wire:
            kinds.append("patch")
        elif b"event: domain_event" in wire:
            kinds.append("event")
    assert kinds == ["retry", "reseed", "gap", "patch"]


def test_a_forged_positive_seq_is_still_refused() -> None:
    """The reserved boundary is 0, and only 0.

    A plain positive integer the server never issued is not a checkpoint and
    cannot be faked into one -- not even one sitting inside the ring's range.
    """
    ring = ReplayRing(max_frames=100, max_bytes=1 << 10)
    ring.append(_entry(1))
    ring.append(_entry(2, size=(1 << 10) + 1))  # unrecoverable
    ring.append(_entry(3))
    assert ring.classify_seq(2) == "malformed"
    forged = classify_cursor(
        replay.format_cursor("inst", 2), ring, process_instance_id="inst"
    )
    assert forged.reason == "malformed"
    # And the reserved boundary is not a checkpoint for events.
    with pytest.raises(ValueError):
        frames.reseed_frame("inst", -1)
    with pytest.raises(ValueError):
        _entry(1).with_checkpoint(0, "inst")
