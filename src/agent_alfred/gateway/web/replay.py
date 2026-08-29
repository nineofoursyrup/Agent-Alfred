"""ReplayRing: the process-unique bounded store of replayable facts.

A pure data structure -- no threads, no IO, no time. Its element is a
:class:`~agent_alfred.gateway.web.frames.PreparedFrames` -- one **logical
event**, never a physical frame: eviction is therefore atomic and
"half an event" is not expressible, which removes the silent path where a
client is handed something that looks complete but is not.

Two independent budgets bound it, counted at once (frames *and* encoded
bytes): a frame count alone lets a few 1 MiB frames pin far more memory than
the byte budget ever meant to allow.

The ring also owns the cursor vocabulary. A cursor is ``{instance}:{seq}``
and must name a checkpoint this process actually issued -- the global
sequence carries transient and non-replayable positions, so the arithmetic
predecessor of a checkpoint is not itself a checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NewType

from agent_alfred.gateway.web.frames import PreparedFrames

# The decided capacity table. Constructor defaults on purpose: these are not
# user-facing knobs, and a test needs a two-frame ring to reach the overflow
# paths at all.
DEFAULT_MAX_ENTRIES = 2048
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
    evicted: tuple[int, ...] = ()
    ring_cleared: bool = False


@dataclass(frozen=True)
class CursorVerdict:
    """What a reconnecting client's cursor entitles it to.

    ``kind`` is closed: ``absent`` (the first connection, which is not a
    degradation), ``valid`` (exact catch-up), or ``gap`` (a transport notice
    plus the snapshot).

    ``reseed_seq`` is the checkpoint the response body must re-plant before
    any data frame. The SSE dispatch algorithm copies the id buffer into the
    last-event-id string even for a dataless frame, so without a re-seed the
    first id-less frame silently erases the client's cursor.
    """

    kind: CursorVerdictKind
    entries: tuple[PreparedFrames, ...] = ()
    reseed_seq: int | None = None
    reason: GapReason | None = None
    # The seq the client asked to resume from, so the notice can name it.
    # Absent for a first connection, which is not a gap.
    requested_seq: int | None = None


def format_cursor(process_instance_id: str, seq: int) -> CursorText:
    if ":" in process_instance_id:
        raise ValueError("process_instance_id may not contain ':'")
    if seq < 1:
        raise ValueError(f"cursor seq must be >= 1, got {seq}")
    return CursorText(f"{process_instance_id}:{seq}")


def parse_cursor(
    text: str, process_instance_id: str
) -> tuple[int | None, GapReason | None]:
    """Split a ``Last-Event-ID`` value into a seq, or say why it cannot be.

    Instance mismatch is reported ahead of any shape complaint about the
    seq: "this came from another process" and "this is unreadable" are
    different facts and the client shows different words for them.
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
    if seq < 1:
        return None, "malformed"
    return seq, None


class ReplayRing:
    """Bounded, process-unique, ordered by seq."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: list[PreparedFrames] = []
        self._issued: set[int] = set()
        self._bytes = 0
        self._floor = 0
        self._high_water = 0
        self._emitted_any = False

    # -- reads ------------------------------------------------------------

    def replay_floor_seq(self) -> int:
        """The highest seq that is no longer replayable. Monotonic.

        Zero means nothing has been dropped yet, which is how an empty ring
        tells "no event was ever produced" apart from "events were produced
        and then walked over".
        """
        return self._floor

    def high_water_seq(self) -> int:
        """The highest seq this ring has seen.

        Only replayable events are appended, so this is the newest replayable
        seq -- not the newest seq the process published. A transient event
        consumes a seq and never reaches here, which is exactly why the
        arithmetic predecessor of a checkpoint is not itself a checkpoint.
        """
        return self._high_water

    def latest_complete_seq(self) -> int | None:
        """The newest complete checkpoint, or None before the first event."""
        return self._high_water or None

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

        "In range but never issued" is ``malformed``: the closed table has no
        other slot for it, and a seq that only a transient event consumed is
        precisely the arithmetic predecessor that is not a checkpoint.
        """
        if not isinstance(seq, int) or isinstance(seq, bool):
            return "malformed"
        if seq > self._high_water:
            return "ahead"
        if seq < self._floor:
            return "too_old"
        if seq in self._issued:
            return "valid"
        # Two edges are usable even though the ring holds no entry under
        # them: the floor, because everything past it is still here, and the
        # high-water mark, because there is nothing past it to miss. A floor
        # of zero is neither -- it means nothing was ever dropped, so a zero
        # cursor is a client that has received nothing, not a checkpoint.
        if (seq == self._floor and self._floor > 0) or seq == self._high_water:
            return "valid"
        return "malformed"

    # -- writes -----------------------------------------------------------

    def append(self, entry: PreparedFrames) -> AppendResult:
        """Publish one logical event. Seq must be strictly increasing.

        An event larger than the whole byte budget cannot be carried at all:
        the ring is cleared and the floor jumps past it, so no client is
        told it can recover a fact this process no longer holds. The event
        still counts towards the high-water mark -- it was published, and a
        client that saw it must not read as "ahead".
        """
        if entry.seq <= self._high_water:
            raise ValueError(
                f"seq must increase: got {entry.seq} after {self._high_water}"
            )
        self._high_water = entry.seq
        self._emitted_any = True
        if entry.byte_size > self.max_bytes or not entry.frames:
            self._clear()
            self._floor = entry.seq
            return AppendResult(accepted=False, evicted=(), ring_cleared=True)
        self._entries.append(entry)
        self._issued.add(entry.seq)
        self._bytes += entry.byte_size
        evicted = self._evict_while_over_budget()
        return AppendResult(accepted=True, evicted=evicted)

    def _evict_while_over_budget(self) -> tuple[int, ...]:
        evicted: list[int] = []
        while self._entries and (
            len(self._entries) > self.max_entries or self._bytes > self.max_bytes
        ):
            dropped = self._entries.pop(0)
            self._issued.discard(dropped.seq)
            self._bytes -= dropped.byte_size
            # The floor is monotonic: it names the newest fact this ring can
            # no longer produce, so it only ever moves forward.
            if dropped.seq > self._floor:
                self._floor = dropped.seq
            evicted.append(dropped.seq)
        return tuple(evicted)

    def _clear(self) -> None:
        self._entries.clear()
        self._issued.clear()
        self._bytes = 0


def classify_cursor(
    cursor: CursorText | None,
    ring: ReplayRing,
    *,
    process_instance_id: str,
) -> CursorVerdict:
    """Decide what one reconnecting client gets. No cursor is not a gap."""
    reseed = ring.latest_complete_seq()
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
