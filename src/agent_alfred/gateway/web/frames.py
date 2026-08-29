"""The SSE wire format: physical frames, chunking, and the three payloads.

Everything here is a pure function. Serialization, UTF-8-safe splitting and
frame construction all belong to the *prepare* half of ADR-0015: they are
allowed to be slow, they do no IO, and they touch nothing shared. The only
thing left for the commit half is :meth:`PreparedFrames.with_checkpoint`,
which adds a bounded id line and shares the already-built frames by
reference -- a commit that copies the payload would not be a short critical
section.

The wire shape in one place:

- ``retry:`` then the **re-seed** (a dataless ``id:`` frame) come first,
  always, before any data. The SSE dispatch algorithm copies the id buffer
  into the last-event-id string even for a dataless frame, so without the
  re-seed the first id-less frame silently erases the client's cursor.
- ``id:`` appears only at a replayable *and* complete boundary: the last
  frame of a replayable logical event, and nowhere else.
- A logical event that does not fit one frame is split into consecutive
  frames carrying ``event_id`` / ``chunk_index`` / ``chunk_count``; the
  client concatenates the ``payload`` fragments and only then parses. One
  logical event still consumes exactly one ``seq``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Literal

from agent_alfred.events import event_json_default

# 1 MiB is a hard ceiling on a physical frame, not a soft target that may be
# exceeded by "just one more" event.
MAX_FRAME_BYTES = 1024 * 1024
# The smallest frame budget worth accepting. Below this the fixed SSE field
# lines leave no room for a payload at all.
MIN_FRAME_BYTES = 256

# Local reconnect is nearly free; a replayable overflow, on the other hand,
# closes the connection deliberately and must not turn into a reconnect
# storm, so it raises the backoff before hanging up.
DEFAULT_RETRY_MS = 1000
BACKOFF_RETRY_MS = 3000

DOMAIN_EVENT = "domain_event"
TRANSPORT_NOTICE = "transport_notice"
STATE_PATCH = "state_patch"

TransportNoticeCode = Literal["replay_gap", "deltas_dropped"]
CurrentRunState = Literal["recoverable", "unrecoverable", "absent"]

# Room reserved in every chunk's budget for the id line that commit appends.
# commit cannot know ``seq`` when the frames are prepared, so the budget
# assumes the worst legal id line rather than discovering it later.
_ID_LINE_RESERVE = 64
# Longest UTF-8 encoding of one code point.
_MAX_CHAR_BYTES = 4

_EMPTY = b""
_NEWLINE = b"\n"
_DATA_PREFIX = b"data: "


@dataclass(frozen=True)
class PreparedFrames:
    """One logical thing to write: the frames, and the cursor it advances.

    ``seq`` is ``None`` for anything that is not a domain event -- transport
    notices and state patches deliberately own no ``seq`` (CONTEXT.md: a
    connection-local notice that consumed a global seq would leave a hole in
    every *other* connection's sequence, and a hole is exactly the signal the
    client uses to notice it missed something).

    ``id_line`` is empty when this thing carries no checkpoint. When it is
    set it is written after the last frame only.
    """

    seq: int | None
    frames: tuple[bytes, ...]
    id_line: bytes = b""
    byte_size: int = 0
    replayable: bool = False
    # Cannot be dropped for want of room: a state patch that goes missing
    # leaves the client holding a lifetime state it will never be corrected
    # on. Undeliverable means the connection closes and the client reconnects
    # for an atomic snapshot instead.
    must_deliver: bool = False

    def wire_frames(self) -> tuple[bytes, ...]:
        """The exact bytes, in order. Only the last frame carries the id.

        Every frame is a complete SSE record, so each one ends in a blank
        line; the id line belongs *before* that blank line, which is the only
        reason it is not simply appended.
        """
        last = len(self.frames) - 1
        return tuple(
            frame + _NEWLINE + (self.id_line if i == last else b"") + _NEWLINE
            for i, frame in enumerate(self.frames)
        )

    def wire_bytes(self) -> bytes:
        return b"".join(self.wire_frames())

    def with_checkpoint(
        self, seq: int, process_instance_id: str
    ) -> "PreparedFrames":
        """Attach the checkpoint. O(1): the frames are shared, not copied."""
        if seq < 1:
            raise ValueError(f"checkpoint seq must be >= 1, got {seq}")
        id_line = b"id: %s:%d\n" % (process_instance_id.encode("utf-8"), seq)
        if len(id_line) > _ID_LINE_RESERVE:
            raise ValueError(
                f"id line is {len(id_line)} bytes, over the reserved "
                f"{_ID_LINE_RESERVE}"
            )
        return replace(
            self,
            seq=seq,
            id_line=id_line,
            byte_size=self.byte_size + len(id_line),
            must_deliver=True,
        )


def measured_frames(
    *,
    frames,
    seq: int | None = None,
    id_line: bytes = b"",
    replayable: bool = False,
    must_deliver: bool = False,
) -> PreparedFrames:
    """Assemble frames with the encoded-byte cost derived from them.

    The two budgets that matter downstream (the ring's and each connection
    queue's) count encoded bytes, so the number has to come from the frames
    themselves rather than from whatever the caller believes it built.
    """
    built = tuple(frames)
    return PreparedFrames(
        seq=seq,
        frames=built,
        id_line=id_line,
        byte_size=sum(len(frame) for frame in built)
        + 2 * len(built)
        + len(id_line),
        replayable=replayable,
        must_deliver=must_deliver,
    )


# --- control frames --------------------------------------------------------


def _control(frame: bytes) -> PreparedFrames:
    return measured_frames(frames=(frame,))


def retry_frame(milliseconds: int) -> PreparedFrames:
    """``retry: N`` -- no data, so it cannot dispatch an event."""
    return _control(b"retry: %d" % milliseconds)


def reseed_frame(process_instance_id: str, seq: int) -> PreparedFrames:
    """The dataless ``id:`` frame that re-plants the cursor.

    It must precede every data frame: a new stream starts with an empty id
    buffer, so the first dataless dispatch would otherwise assign an empty
    string to the last-event-id.
    """
    if seq < 1:
        raise ValueError(f"reseed seq must be >= 1, got {seq}")
    return _control(b"id: %s:%d" % (process_instance_id.encode("utf-8"), seq))


def heartbeat_frame() -> PreparedFrames:
    """An SSE comment. Not an event: no ``seq``, no ``id``, never replayed.

    Its only jobs are to make the socket actually get written to -- which is
    how a connection thread learns the peer is gone instead of blocking on
    ``get()`` forever -- and to cross middleboxes that drop idle sockets.
    """
    return _control(b":hb")


# --- the three payloads ----------------------------------------------------


# Compact separators: every byte here is a byte on the wire, and the frame
# budget is a hard limit rather than a target.
_dump_json = partial(
    json.dumps,
    ensure_ascii=False,
    separators=(",", ":"),
    default=event_json_default,
)


def _dump(body: Any) -> bytes:
    return _dump_json(body).encode("utf-8")


def _utf8_safe_split(data: bytes, limit: int) -> tuple[bytes, ...]:
    """Cut ``data`` into pieces of at most ``limit`` bytes, never mid-character.

    A cut that lands inside a multi-byte sequence produces bytes that are not
    UTF-8 at all -- the client cannot recover from that by concatenation, so
    the boundary retreats past continuation bytes (``10xxxxxx``) instead.
    """
    if limit < _MAX_CHAR_BYTES:
        raise ValueError(f"frame budget {limit} leaves no room for one character")
    pieces: list[bytes] = []
    start = 0
    total = len(data)
    while start < total:
        end = min(start + limit, total)
        if end < total:
            while end > start and (data[end] & 0xC0) == 0x80:
                end -= 1
            if end == start:
                raise ValueError(
                    f"frame budget {limit} cannot hold one character here"
                )
        pieces.append(data[start:end])
        start = end
    return tuple(pieces)


def _body_budget(
    *,
    sse_event: str,
    event_name: str,
    event_id: str,
    chunk_count: int,
    max_frame_bytes: int,
) -> int:
    """How many payload bytes one frame may carry.

    ``chunk_count`` enters because the header it appears in is part of the
    frame: a three-digit count costs two bytes more than a one-digit one.
    """
    head = '{"event":%s,"event_id":%s,"chunk_index":%d,"chunk_count":%d,"payload":'
    longest = head % (
        _dump_json(event_name),
        _dump_json(event_id),
        chunk_count,
        chunk_count,
    )
    fixed = len(b"event: ") + len(sse_event) + len(_NEWLINE)
    fixed += len(_DATA_PREFIX) + len(longest.encode("utf-8")) + len(b"}")
    fixed += len(_NEWLINE) + _ID_LINE_RESERVE
    return max_frame_bytes - fixed


def domain_event_frames(
    *,
    event_name: str,
    payload: Any,
    event_id: str,
    replayable: bool,
    max_frame_bytes: int = MAX_FRAME_BYTES,
) -> PreparedFrames:
    """Prepare one domain event. Pure: no IO, no shared state, may be slow.

    ``seq`` does not appear in the prepared bytes -- it does not exist yet
    (ADR-0015 allocates it in the commit critical section). That is precisely
    why the checkpoint rides in the ``id:`` line: it is the one part of the
    frame commit can add without touching the payload. Transient frames
    therefore carry no ``seq``; their order is the order they are written,
    which is the publication order the ``seq`` records.
    """
    if max_frame_bytes < MIN_FRAME_BYTES:
        raise ValueError(
            f"max_frame_bytes must be >= {MIN_FRAME_BYTES}, got {max_frame_bytes}"
        )
    body = _dump(payload)
    count = 1
    chunks: tuple[bytes, ...] = ()
    for _ in range(16):
        budget = _body_budget(
            sse_event=DOMAIN_EVENT,
            event_name=event_name,
            event_id=event_id,
            chunk_count=count,
            max_frame_bytes=max_frame_bytes,
        )
        chunks = _utf8_safe_split(body, budget)
        if len(chunks) == count:
            break
        count = len(chunks)
    else:  # pragma: no cover - bounded by the digit count of the frame count
        budget = _body_budget(
            sse_event=DOMAIN_EVENT,
            event_name=event_name,
            event_id=event_id,
            chunk_count=len(chunks),
            max_frame_bytes=max_frame_bytes,
        )
        chunks = _utf8_safe_split(body, budget)
    frames = tuple(
        b"event: "
        + DOMAIN_EVENT.encode()
        + _NEWLINE
        + _DATA_PREFIX
        + (
            '{"event":%s,"event_id":%s,"chunk_index":%d,"chunk_count":%d,"payload":'
            % (
                _dump_json(event_name),
                _dump_json(event_id),
                index,
                len(chunks),
            )
        ).encode("utf-8")
        + chunk
        + b"}"
        for index, chunk in enumerate(chunks)
    )
    return measured_frames(
        frames=frames, replayable=replayable, must_deliver=replayable
    )


def _single_payload_frame(sse_event: str, body: dict) -> PreparedFrames:
    """One frame for a bounded payload. Over the hard limit is a refusal,
    never a silently oversized frame."""
    frame = b"event: " + sse_event.encode() + _NEWLINE + _DATA_PREFIX + _dump(body)
    if len(frame) + 2 > MAX_FRAME_BYTES:
        raise ValueError(f"{sse_event} payload exceeds {MAX_FRAME_BYTES} bytes")
    return measured_frames(frames=(frame,))


def replay_gap_notice(
    *,
    reason: str,
    requested_seq: int | None,
    oldest_seq: int | None,
    high_water_seq: int,
    current_run_state: CurrentRunState,
) -> PreparedFrames:
    """A connection-local fact: this browser cannot resume where it was.

    ``current_run_state`` is three-valued on purpose -- a boolean cannot tell
    "the current Run is unrecoverable" apart from "there is no current Run",
    and those are two different sentences on screen.
    """
    return _single_payload_frame(
        TRANSPORT_NOTICE,
        {
            "code": "replay_gap",
            "gap_reason": reason,
            "requested_seq": requested_seq,
            "oldest_seq": oldest_seq,
            "high_water_seq": high_water_seq,
            "current_run_state": current_run_state,
        },
    )


def deltas_dropped_notice(count: int) -> PreparedFrames:
    """Transient frames this connection missed. Not a fact about the Run."""
    return _single_payload_frame(
        TRANSPORT_NOTICE, {"code": "deltas_dropped", "count": count}
    )


def state_patch_frames(body: dict) -> PreparedFrames:
    """An absolute lifecycle replacement, idempotent, owning no event seq.

    It carries ``process_instance_id`` and ``state_revision`` itself so a
    client can refuse a patch from another process or one that rewinds. It is
    also undroppable: a patch that silently failed to arrive would leave the
    client showing a lifetime state nothing will ever correct, so the
    connection is closed instead and the client reconnects for the snapshot.
    """
    return replace(_single_payload_frame(STATE_PATCH, body), must_deliver=True)
