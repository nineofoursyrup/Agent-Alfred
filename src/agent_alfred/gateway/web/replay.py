"""ReplayRing: the process-unique bounded store of replayable facts.

A pure data structure -- no threads, no IO, no time. Its element is a
:class:`~agent_alfred.gateway.web.frames.PreparedFrames` -- one **logical
event**, never a physical frame: eviction is therefore atomic and
"half an event" is not expressible, which removes the silent path where a
client is handed something that looks complete but is not.

Two independent budgets bound it, counted at once (frames *and* encoded
bytes): a frame count alone lets a few 1 MiB frames pin far more memory than
the byte budget ever meant to allow. Both count **physical frames**, not
logical events -- a logical event may be many frames, so counting entries
would let a handful of chunked events pin far more than the budget allows.
Eviction still removes whole logical events, because half an event is not
expressible.

The ring also owns the cursor vocabulary. A cursor is ``{instance}:{seq}``
and, for every positive seq, must name a checkpoint this process actually
issued -- the global sequence carries transient and non-replayable
positions, so the arithmetic predecessor of a checkpoint is not itself a
checkpoint. (The one non-positive seq, reserved and argued below, is not a
checkpoint of any event; it is the boundary in front of them all.)

"Checkpoint" is three different things and the ring keeps them apart,
because conflating any two of them is how a client is handed a cursor that
lies:

- :meth:`ReplayRing.reseed_boundary_seq` is the **re-seed boundary** -- the
  newest checkpoint this process has ever issued, whether or not the ring
  can still reproduce it. It is what a stream re-plants in front of its
  first data frame. It is allowed to be stale: a client that comes back
  holding it is *classified*, and a stale one is classified ``too_old`` and
  gets a ``replay_gap``. Planting nothing instead is the one answer a stream
  may not give -- see the point of the reserved boundary below.
- :meth:`ReplayRing.latest_complete_seq` is the **currently replayable
  checkpoint** -- the newest one the ring can actually hand back. It is not
  what a re-seed has to be; it is only what an exact catch-up is built from.
- :meth:`ReplayRing.replay_floor_seq` is the **unrecoverable boundary** --
  the newest fact this ring can no longer produce, for any reason. It only
  ever moves forward, including past events that were never stored at all.
  The eviction floor is the subset of it this process *did* issue and later
  dropped; everything past an issued checkpoint is still held, so resuming
  from an evicted one loses nothing.

Below all three sits :data:`~agent_alfred.gateway.web.frames.STARTUP_CHECKPOINT_SEQ`,
the reserved boundary before the first domain event. It names no event, so
it is never "issued" and never replayable -- but a clean ring accepts it,
because a ring that has lost nothing can prove continuity from the very
beginning, and planting nothing is not an option because a browser with an
empty id buffer sends no ``Last-Event-ID`` -- which is indistinguishable
from a first connection, and a first connection is the one shape that never
gets a gap notice. Formally defined (ADR-0013, 修订 2026-08-30) as a
**transport startup boundary, not an event checkpoint**: it consumes no
domain seq, and it is the one non-positive value the cursor vocabulary
writes.

Conflating the replayable checkpoint with the unrecoverable boundary is how
a forged cursor for an event that was too large to store would be answered
"valid", skipping the one fact the client was owed a ``replay_gap`` for.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, NewType

from agent_alfred.gateway.web.frames import (
    STARTUP_CHECKPOINT_SEQ,
    FrameCost,
    PreparedFrames,
)

# The decided capacity table. Constructor defaults on purpose: these are not
# user-facing knobs, and a test needs a two-frame ring to reach the overflow
# paths at all. ``max_frames`` counts *physical* frames; calling it
# ``max_entries`` would invite the reader to assume one entry is one frame.
DEFAULT_MAX_FRAMES = 2048
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

GapReason = Literal["malformed", "instance_mismatch", "too_old", "ahead"]
CursorVerdictKind = Literal["absent", "valid", "gap"]
SeqVerdict = Literal["valid", "too_old", "ahead", "malformed"]

# The wire shape of a cursor. NewType so a bare string is not quietly
# accepted where a parsed cursor is meant.
CursorText = NewType("CursorText", str)


@dataclass(frozen=True)
class AppendResult:
    accepted: bool
    ring_cleared: bool = False
    _retired: _RetiredPrefix | None = field(
        default=None, repr=False, compare=False
    )

    def release_retired(self) -> None:
        """Release displaced frame references after the publish lock."""
        if self._retired is not None:
            self._retired.release()


@dataclass(frozen=True)
class CursorVerdict:
    """What a reconnecting client's cursor entitles it to.

    ``kind`` is closed: ``absent`` (the first connection, which is not a
    degradation), ``valid`` (exact catch-up), or ``gap`` (a transport notice
    plus the snapshot).

    ``reseed_seq`` is the boundary the response body must re-plant before any
    data frame, and it is always present -- why it cannot be conditional is
    argued once, at
    :data:`~agent_alfred.gateway.web.frames.STARTUP_CHECKPOINT_SEQ`.

    It is a boundary, not a promise. What the ring can reproduce is decided
    when the client brings it back.
    """

    kind: CursorVerdictKind
    reseed_seq: int
    entries: tuple[PreparedFrames, ...] = ()
    reason: GapReason | None = None
    # The seq the client asked to resume from, so the notice can name it.
    # Absent for a first connection, which is not a gap.
    requested_seq: int | None = None


@dataclass
class _EntryCell:
    entry: PreparedFrames | None


class _RetiredPrefix:
    """A constant-size description of slots to release outside publication."""

    def __init__(
        self, owner: _IndexedEntries, start: int, count: int, through_seq: int
    ):
        self._owner = owner
        self._start = start
        self._count = count
        self._through_seq = through_seq
        self._released = False
        self._overwritten: _EntryCell | None = None

    def hold_overwritten(self, cell: _EntryCell) -> None:
        """Keep a reused slot's old cell alive until outside publication."""
        if self._overwritten is not None:
            raise RuntimeError("retired prefix already holds an overwritten cell")
        self._overwritten = cell

    def release(self) -> None:
        if self._released:
            return
        self._owner.release_retired(
            self._start, self._count, self._through_seq
        )
        if self._overwritten is not None:
            self._overwritten.entry = None
        self._released = True


class _IndexedEntries:
    """A fixed-slot logical ring with cumulative cost indexes.

    One accepted logical event consumes at least one physical frame, so a
    replay ring can retain at most ``max_frames`` entries. The extra slot is
    the just-appended event before its displaced prefix is retired.
    """

    def __init__(self, max_entries: int):
        capacity = max_entries + 1
        self._entries: list[_EntryCell | None] = [None] * capacity
        self._frame_totals = [0] * capacity
        self._byte_totals = [0] * capacity
        self._head = 0
        self._size = 0

    def __bool__(self) -> bool:
        return self._size > 0

    def __len__(self) -> int:
        return self._size

    def __iter__(self) -> Iterator[PreparedFrames]:
        for index in range(self._size):
            yield self[index]

    def __getitem__(self, index: int) -> PreparedFrames:
        if index < 0:
            index += self._size
        if index < 0 or index >= self._size:
            raise IndexError(index)
        cell = self._entries[self._slot(index)]
        assert cell is not None and cell.entry is not None
        return cell.entry

    def append(
        self, entry: PreparedFrames, cumulative: FrameCost
    ) -> _EntryCell | None:
        if self._size >= len(self._entries):
            raise RuntimeError("replay ring exceeded its physical frame bound")
        slot = self._slot(self._size)
        overwritten = self._entries[slot]
        self._entries[slot] = _EntryCell(entry)
        self._frame_totals[slot] = cumulative.frames
        self._byte_totals[slot] = cumulative.encoded_bytes
        self._size += 1
        if overwritten is not None and overwritten.entry is not None:
            return overwritten
        return None

    def cumulative_cost_at(self, index: int) -> FrameCost:
        slot = self._slot(index)
        return FrameCost(
            frames=self._frame_totals[slot],
            encoded_bytes=self._byte_totals[slot],
        )

    def prefix_through_cost(
        self, target_frames: int, target_bytes: int, base: FrameCost
    ) -> int:
        """Number of leading entries needed to reach both target totals."""
        return max(
            self._lower_bound(self._frame_totals, target_frames)
            if target_frames > base.frames
            else 0,
            self._lower_bound(self._byte_totals, target_bytes)
            if target_bytes > base.encoded_bytes
            else 0,
        )

    def contains_seq(self, seq: int) -> bool:
        low = 0
        high = self._size
        while low < high:
            middle = (low + high) // 2
            if self[middle].seq < seq:
                low = middle + 1
            else:
                high = middle
        return low < self._size and self[low].seq == seq

    def drop_prefix(self, count: int) -> _RetiredPrefix | None:
        if count < 0 or count > self._size:
            raise ValueError(f"invalid replay prefix length: {count}")
        if count == 0:
            return None
        start = self._head
        through_seq = self[count - 1].seq
        self._head = self._slot(count)
        self._size -= count
        return _RetiredPrefix(self, start, count, through_seq)

    def clear(self) -> _RetiredPrefix | None:
        retired = self.drop_prefix(self._size)
        self._head = 0
        return retired

    def release_retired(
        self, start: int, count: int, through_seq: int
    ) -> None:
        """Clear old cells; a reused slot's newer cell is never touched.

        Appends replace the whole cell rather than mutating it. If another
        publisher reuses a retired physical slot before this cleanup runs,
        its newer seq is above ``through_seq`` and remains live.
        """
        for offset in range(count):
            cell = self._entries[(start + offset) % len(self._entries)]
            if (
                cell is not None
                and cell.entry is not None
                and cell.entry.seq is not None
                and cell.entry.seq <= through_seq
            ):
                cell.entry = None

    def _lower_bound(self, totals: list[int], target: int) -> int:
        low = 0
        high = self._size
        while low < high:
            middle = (low + high) // 2
            if totals[self._slot(middle)] < target:
                low = middle + 1
            else:
                high = middle
        # The caller only asks while the newest cumulative total reaches the
        # target, so ``low`` names an entry and the prefix includes it.
        if low >= self._size:
            raise RuntimeError("replay cumulative index is inconsistent")
        return low + 1

    def _slot(self, logical_index: int) -> int:
        return (self._head + logical_index) % len(self._entries)


def format_cursor(process_instance_id: str, seq: int) -> CursorText:
    if ":" in process_instance_id:
        raise ValueError("process_instance_id may not contain ':'")
    # Zero is the reserved startup boundary, not an event position, so it is
    # the one non-positive-of-events value this will write.
    if seq < STARTUP_CHECKPOINT_SEQ:
        raise ValueError(
            f"cursor seq must be >= {STARTUP_CHECKPOINT_SEQ}, got {seq}"
        )
    return CursorText(f"{process_instance_id}:{seq}")


def parse_cursor(
    text: str, process_instance_id: str
) -> tuple[int | None, GapReason | None]:
    """Split a ``Last-Event-ID`` value into a seq, or say why it cannot be.

    Instance mismatch is reported ahead of any shape complaint about the
    seq: "this came from another process" and "this is unreadable" are
    different facts and the client shows different words for them.

    ``0`` parses. It is the reserved startup boundary this process mints for
    a clean ring, and reading it back as "malformed" would turn every client
    that connected before the first event into a client whose history cannot
    be read.
    """
    if not isinstance(text, str) or not text:
        return None, "malformed"
    parts = text.split(":", 1)
    if len(parts) != 2:
        return None, "malformed"
    instance, raw = parts
    if instance != process_instance_id:
        return None, "instance_mismatch"
    if not raw.isdigit() or (len(raw) > 1 and raw[0] == "0"):
        return None, "malformed"
    seq = int(raw)
    if seq < STARTUP_CHECKPOINT_SEQ:
        return None, "malformed"
    return seq, None


class ReplayRing:
    """Bounded, process-unique, ordered by seq."""

    def __init__(
        self,
        *,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self._entries = _IndexedEntries(max_frames)
        self._usage = FrameCost(frames=0, encoded_bytes=0)
        # Monotonic totals make a displaced prefix discoverable by two
        # bounded index searches. ``_retained_base`` is the cumulative cost
        # immediately before the logical head.
        self._cumulative_cost = FrameCost(frames=0, encoded_bytes=0)
        self._retained_base = FrameCost(frames=0, encoded_bytes=0)
        # The unrecoverable boundary: the newest seq this ring can no longer
        # produce, whatever the reason. Monotonic.
        self._unrecoverable_floor = 0
        # The subset of that boundary this process actually issued and then
        # evicted -- the only floor a cursor may stand on.
        self._evicted_floor = 0
        self._last_issued: int | None = None
        # The re-seed boundary: the newest checkpoint this process *ever*
        # issued. A clear takes away the ring's ability to reproduce a
        # checkpoint, not the fact that it was issued, so this one never
        # goes back.
        self._reseed_boundary: int | None = None
        self._high_water = 0
        # The newest domain-event seq this process has **published**, whether
        # or not the event was replayable. A transient consumes a seq and can
        # be sitting in a client's screen when it reconnects, so "ahead"
        # means "past everything published" -- not "past the newest
        # replayable one". O(1) metadata only: no transient is stored, and
        # the components below (issued checkpoints, replayable entries,
        # floors, the re-seed boundary) stay independent of it.
        self._published_high_water = 0
        self._emitted_any = False

    # -- reads ------------------------------------------------------------

    @property
    def current_cost(self) -> FrameCost:
        """What the ring is holding right now, in both counted dimensions."""
        return self._usage

    def replay_floor_seq(self) -> int:
        """The highest seq that is no longer replayable. Monotonic.

        Zero means nothing has been dropped yet, which is how an empty ring
        tells "no event was ever produced" apart from "events were produced
        and then walked over".
        """
        return self._unrecoverable_floor

    def high_water_seq(self) -> int:
        """The highest seq this ring has seen.

        Only replayable events are appended, so this is the newest replayable
        seq -- not the newest seq the process published. A transient event
        consumes a seq and never reaches here, which is exactly why the
        arithmetic predecessor of a checkpoint is not itself a checkpoint.
        For "newest published, transient included", see
        :meth:`published_high_water_seq`.
        """
        return self._high_water

    def published_high_water_seq(self) -> int:
        """The newest domain-event seq this process has published.

        Transients included: every published domain event advances this by
        one O(1) metadata step, and nothing else -- no entry, no checkpoint,
        no floor. It is the boundary the cursor classification's ``ahead``
        answer is measured against, because a client holding a transient's
        seq on screen must not be told the process never sent it.
        """
        return self._published_high_water

    def latest_complete_seq(self) -> int | None:
        """The newest checkpoint this ring can actually reproduce.

        Not the high-water mark: an event too large to store still advances
        it, and handing a client a cursor for a fact this ring does not hold
        would be issuing a checkpoint it cannot honour.

        This is the checkpoint an **exact catch-up** is measured from. It is
        not what a stream has to re-plant -- see
        :meth:`reseed_boundary_seq`, which is allowed to name a checkpoint
        this one has forgotten.
        """
        return self._last_issued

    def reseed_boundary_seq(self) -> int | None:
        """The newest checkpoint this process has ever issued. Monotonic.

        Deliberately *not* the same as :meth:`latest_complete_seq`: what was
        issued is not undone by what can no longer be produced. Planting a
        boundary the ring will call ``too_old`` is not a contradiction, it is
        how the client finds out -- on every reconnect, until the ring has
        something it can actually prove. Why "nothing" is not an answer is
        argued on
        :data:`~agent_alfred.gateway.web.frames.STARTUP_CHECKPOINT_SEQ`.

        ``None`` means this process has never issued one, which is the only
        case in which the reserved
        :data:`~agent_alfred.gateway.web.frames.STARTUP_CHECKPOINT_SEQ` is
        planted instead.
        """
        return self._reseed_boundary

    def oldest_seq(self) -> int | None:
        return self._entries[0].seq if self._entries else None

    def emitted_any(self) -> bool:
        return self._emitted_any

    def __len__(self) -> int:
        return len(self._entries)

    def entries_after(self, cursor_seq: int) -> tuple[PreparedFrames, ...] | None:
        """Every replayable entry in ``(cursor_seq, high_water]``.

        ``None`` means the cursor is unusable -- never a shorter list, which
        is what keeps a gap from degrading into a silent hole.
        """
        if self.classify_seq(cursor_seq) != "valid":
            return None
        return tuple(entry for entry in self._entries if entry.seq > cursor_seq)

    def classify_seq(self, seq: int) -> SeqVerdict:
        """Close a cursor's position into one of the four decided reasons.

        The classification is ordered so that each answer is the strongest
        one available:

        1. past everything **published** (transients included) -> ``ahead``;
        2. below the unrecoverable floor -> ``too_old``;
        3. the reserved startup boundary, exactly while nothing has been
           lost -> ``valid``;
        4. an issued checkpoint -- or the one edge usable with nothing under
           it, a checkpoint issued and then evicted whose whole tail is
           still here -> ``valid``;
        5. anything else is a positive seq that **was** published but never
           issued as a checkpoint -- a transient's seq, or an event too
           large to store -> ``malformed``.

        The two floors are not interchangeable. A seq that was issued and
        later evicted names a real boundary -- everything past it is still
        held, so resuming from it loses nothing. A seq the ring never stored
        (an event too large for either budget) also moves the floor, but it
        was never issued and so it is not a checkpoint: answering "valid"
        for it would let a client skip the one fact it was owed a gap for.

        "In range but never issued" is therefore ``malformed`` -- the closed
        table has no other slot for it, and a seq that only a transient
        event consumed is precisely the arithmetic predecessor that is not a
        checkpoint.
        """
        if not isinstance(seq, int) or isinstance(seq, bool):
            return "malformed"
        if seq > self._published_high_water:
            return "ahead"
        if seq < self._unrecoverable_floor:
            return "too_old"
        if seq == STARTUP_CHECKPOINT_SEQ:
            # The transport startup boundary before the first domain event
            # (ADR-0013, 修订 2026-08-30: a boundary, not an event
            # checkpoint). Valid exactly while the ring has lost nothing --
            # a floor of zero is a ring that can prove continuity from the
            # beginning. Once anything is unrecoverable, the check above has
            # already called it ``too_old``, which is the honest answer:
            # this process cannot prove what happened between there and
            # here.
            return "valid"
        if self._entries.contains_seq(seq):
            return "valid"
        # The one edge that is usable with nothing under it: a checkpoint
        # this process issued and then dropped, whose whole tail is still
        # here. A floor of zero is not it -- it means nothing was ever
        # dropped, so a zero cursor is a client that has received nothing.
        if seq == self._evicted_floor and self._evicted_floor > 0:
            return "valid"
        return "malformed"

    # -- writes ------------------------------------------------------------

    def append(self, entry: PreparedFrames) -> AppendResult:
        """Publish one replayable event. Seq must strictly increase.

        The same step as :meth:`observe_published` with the entry given; see
        there for the refused-entry and monotonicity rules.
        """
        return self.observe_published(entry.seq, entry)

    def observe_published(
        self,
        seq: int,
        entry: PreparedFrames | None,
        *,
        defer_retired_release: bool = False,
    ) -> AppendResult:
        """Record one published domain event, atomically.

        Every published domain event goes through here exactly once: the
        published high water advances to ``seq`` -- one O(1) metadata step,
        transient or not -- and, when the event was replayable, ``entry``
        (the same logical event with its checkpoint attached) is appended to
        the ring **in the same step**. There is no state in between: an
        observer never sees a published seq whose entry is missing, which is
        exactly the middle state a "advance, then append" pair would expose.

        An entry the ring cannot carry at all -- one with no frames, more
        physical frames than the whole frame budget, or more encoded bytes
        than the whole byte budget -- is refused whole: the ring is cleared
        and the unrecoverable boundary jumps past it, so no client is told
        it can recover a fact this process does not hold. The event still
        counts as published -- it was, and a client that saw it must not read
        as "ahead". What it does not get is a checkpoint: the caller is told
        the append was refused precisely so it can withhold the ``id:`` line.

        A transient passes ``entry=None``: the published high water moves
        and nothing is stored -- no entry, no checkpoint, no floor.
        """
        if seq <= self._published_high_water:
            raise ValueError(
                "seq must increase: got "
                f"{seq} after {self._published_high_water}"
            )
        if entry is not None and entry.seq != seq:
            raise ValueError(
                f"entry seq {entry.seq} does not match published seq {seq}"
            )
        self._published_high_water = seq
        if entry is None:
            return AppendResult(accepted=False)
        self._high_water = entry.seq
        self._emitted_any = True
        cost = entry.ingress_cost()
        if (
            not entry.frames
            or cost.frames > self.max_frames
            or cost.encoded_bytes > self.max_bytes
        ):
            retired = self._clear()
            self._unrecoverable_floor = entry.seq
            return self._finish_append(
                AppendResult(
                    accepted=False, ring_cleared=True, _retired=retired
                ),
                defer_retired_release,
            )
        self._cumulative_cost = self._cumulative_cost + cost
        overwritten = self._entries.append(entry, self._cumulative_cost)
        self._last_issued = entry.seq
        self._reseed_boundary = entry.seq
        self._usage = self._usage + cost
        retired = self._evict_while_over_budget()
        if overwritten is not None:
            if retired is None:
                raise RuntimeError("replay slot reused without retiring a prefix")
            retired.hold_overwritten(overwritten)
        return self._finish_append(
            AppendResult(accepted=True, _retired=retired),
            defer_retired_release,
        )

    def _evict_while_over_budget(self) -> _RetiredPrefix | None:
        if (
            self._usage.frames <= self.max_frames
            and self._usage.encoded_bytes <= self.max_bytes
        ):
            return None

        # Prefix totals are monotonic. Two binary searches find the shortest
        # whole-event prefix satisfying both budgets, then one logical-head
        # update retires it. This is O(log(max_frames)) in the configurable
        # capacity (at the decided 2048-frame production cap, at most 12
        # probes per dimension), not a claim of parameterized O(1). Crucially
        # it is independent of the number of displaced entries: none is
        # visited, moved, or released in the publish critical section.
        drop_count = self._entries.prefix_through_cost(
            self._cumulative_cost.frames - self.max_frames,
            self._cumulative_cost.encoded_bytes - self.max_bytes,
            self._retained_base,
        )
        dropped = self._entries[drop_count - 1]
        self._retained_base = self._entries.cumulative_cost_at(drop_count - 1)
        retired = self._entries.drop_prefix(drop_count)
        self._usage = self._cumulative_cost - self._retained_base

        # Both boundaries are monotonic: the last removed logical event is
        # the newest fact this ring can no longer reproduce.
        if dropped.seq > self._unrecoverable_floor:
            self._unrecoverable_floor = dropped.seq
        if dropped.seq > self._evicted_floor:
            self._evicted_floor = dropped.seq
        return retired

    def _clear(self) -> _RetiredPrefix | None:
        """Drop everything the ring was holding. Only the unrecoverable
        path calls this.

        ``_last_issued`` goes, and that is the point: every checkpoint left
        behind it is now below the unrecoverable floor, so the ring cannot
        reproduce it and must not offer one for an exact catch-up.

        ``_reseed_boundary`` stays, and that is the whole point of it: what
        was issued is not undone by what can no longer be produced. The
        client keeps a real boundary, this same ring calls it ``too_old``,
        and the gap is reported every time it comes back.
        """
        retired = self._entries.clear()
        self._last_issued = None
        self._usage = FrameCost(frames=0, encoded_bytes=0)
        self._retained_base = self._cumulative_cost
        return retired

    @staticmethod
    def _finish_append(
        result: AppendResult, defer_retired_release: bool
    ) -> AppendResult:
        if not defer_retired_release:
            result.release_retired()
        return result


def classify_cursor(
    cursor: CursorText | None,
    ring: ReplayRing,
    *,
    process_instance_id: str,
) -> CursorVerdict:
    """Decide what one reconnecting client gets. No cursor is not a gap.

    Every answer carries a ``reseed_seq``: the newest boundary this process
    issued, or the reserved startup boundary when it has never issued any.
    It is not required to be replayable -- a re-seed that comes back
    ``too_old`` is the mechanism by which a gap is reported at all, and the
    only thing worse than a stale boundary is no boundary.
    """
    reseed = ring.reseed_boundary_seq()
    if reseed is None:
        reseed = STARTUP_CHECKPOINT_SEQ
    if cursor is None:
        return CursorVerdict(kind="absent", reseed_seq=reseed)
    seq, reason = parse_cursor(cursor, process_instance_id)
    if reason is not None:
        return CursorVerdict(
            kind="gap", reseed_seq=reseed, reason=reason, requested_seq=seq
        )
    assert seq is not None  # parse_cursor returns both or neither
    if ring.classify_seq(seq) != "valid":
        verdict = ring.classify_seq(seq)
        return CursorVerdict(
            kind="gap",
            reseed_seq=reseed,
            reason=verdict if verdict != "valid" else "malformed",
            requested_seq=seq,
        )
    entries = ring.entries_after(seq)
    # The nearest complete checkpoint strictly before the first replayed
    # frame is the cursor itself: it named a complete event boundary, which
    # is exactly what "checkpoint" means.
    return CursorVerdict(
        kind="valid", entries=entries or (), reseed_seq=seq, requested_seq=seq
    )
