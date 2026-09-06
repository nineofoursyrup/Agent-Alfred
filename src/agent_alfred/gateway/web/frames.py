"""The SSE wire format: physical frames, chunking, and the three payloads.

Everything here is a pure function. Serialization, UTF-8-safe splitting and
frame construction all belong to the *prepare* half of ADR-0015: they are
allowed to be slow, they do no IO, and they touch nothing shared. The commit
half only binds bounded metadata through :meth:`PreparedFrames.with_sequence`
and :meth:`PreparedFrames.with_checkpoint`; both share the already-built
frames by reference -- a commit that copied the payload would not be a short
critical section.

The wire shape in one place:

- ``retry:`` then the **re-seed** (a dataless ``id:`` frame) come first,
  always, before any data -- including when there is no data yet. The re-seed
  is a **boundary this process once stood at**, not a promise that everything
  past it is still replayable; on an empty ring it is the reserved
  :data:`STARTUP_CHECKPOINT_SEQ`, which costs no event seq. Why it cannot be
  conditional is argued once, on that constant, and the ring's half of the
  vocabulary in :mod:`agent_alfred.gateway.web.replay`.
- ``id:`` appears only at a replayable *and* complete boundary: the last
  frame of a replayable logical event, and nowhere else -- with the one
  exception of the reserved :data:`STARTUP_CHECKPOINT_SEQ` re-seed below,
  which is a boundary with no event under it.
- A logical event that does not fit one frame is split into consecutive
  frames carrying ``event_id`` / ``chunk_index`` / ``chunk_count``; the
  client concatenates the ``payload`` fragments and only then parses. One
  logical event still consumes exactly one ``seq``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Literal, Sequence

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

# The reserved starting cursor: ``{process_instance_id}:0``.
#
# It is the transport boundary *before* the first domain event, formally
# defined (ADR-0013, 修订 2026-08-30) as **not an event checkpoint**: no
# event ever owns seq 0, nothing consumes it, and it never appears as the
# ``id:`` of a data frame. It is a boundary this process owns, planted
# because a stream has to plant *something* before any data: the SSE
# dispatch algorithm copies the id buffer even for a dataless frame, so a
# stream with no ``id:`` leaves the browser holding an empty buffer, an
# empty buffer sends no ``Last-Event-ID`` on the next connection, and a
# client that sends no cursor is indistinguishable from one that never
# connected -- the one shape that never receives a ``replay_gap``. On a
# ring that has never issued a checkpoint, the boundary in front of them
# all is the only thing there is to plant.
#
# It is a boundary, not a promise: the ring accepts it as a starting
# position exactly while it has lost nothing, and classifies it ``too_old``
# the moment anything is unrecoverable. Every positive seq the ring never
# issued is still refused outright -- the property the ADR protects.
#
# It is defined here, beside the ``id:`` line's shape, because that is where
# both producers of the line live: :func:`reseed_frame` and
# :meth:`PreparedFrames.with_checkpoint`.
STARTUP_CHECKPOINT_SEQ = 0

DOMAIN_EVENT = "domain_event"
TRANSPORT_NOTICE = "transport_notice"
STATE_PATCH = "state_patch"

TransportNoticeCode = Literal["replay_gap", "deltas_dropped"]
CurrentRunState = Literal["recoverable", "unrecoverable", "absent"]

# An instance id is embedded verbatim in an SSE ``id:`` field and separated
# from its sequence by ``:``. Keep that token to an explicit ASCII alphabet:
# merely banning the delimiter would still admit line breaks and control
# bytes that can mint extra SSE fields.
_WIRE_PROCESS_INSTANCE_ID = re.compile(r"[A-Za-z0-9._~-]+")


def validate_process_instance_id(value: object) -> str:
    """Return one instance token, or refuse a value unsafe for an SSE field."""
    if (
        type(value) is not str
        or _WIRE_PROCESS_INSTANCE_ID.fullmatch(value) is None
    ):
        raise ValueError("process_instance_id is outside its ASCII wire syntax")
    return value

# Room reserved in every chunk's budget for the id line that commit appends.
# commit cannot know ``seq`` when the frames are prepared, so the budget
# assumes the worst legal id line rather than discovering it later.
_ID_LINE_RESERVE = 64
# Normal room for ``"seq":<integer>,`` in every physical domain-event frame.
# The public 256-byte test boundary cannot hold all of it beside the frozen
# digest/checkpoint metadata, so preparation scales this down only at that
# deliberately constrained boundary and records the exact limit on its
# immutable result. Commit fails closed instead of producing an oversized
# record if the eventual sequence token does not fit.
_SEQ_FIELD_RESERVE = 32
_MIN_SEQ_FIELD_RESERVE = len(b'"seq":1,')
# A live broker must reserve the full normal token while retaining the same
# payload room the 256-byte codec boundary had before seq became wire data.
# The smaller boundary remains useful for direct hard-limit/fail-closed tests,
# but is not a sustainable stream configuration once publication advances.
MIN_BROKER_FRAME_BYTES = MIN_FRAME_BYTES + _SEQ_FIELD_RESERVE

_EMPTY = b""
_NEWLINE = b"\n"
_DATA_PREFIX = b"data: "
_DOMAIN_FRAME_PREFIX = b"event: domain_event\ndata: {"


@dataclass(frozen=True)
class FrameCost:
    """What one logical item costs a queue that counts frames *and* bytes.

    Named rather than a bare ``(int, int)`` pair so the two dimensions cannot
    be swapped, half-updated, or drifted negative: every queue in the
    Dashboard budgets both, and an item admitted on frames alone would be the
    one that quietly breaks the byte promise.

    Arithmetic is closed over this type and refuses to produce a negative
    count -- a queue that released more than it held is a bug worth an
    exception, not a negative balance to carry.
    """

    frames: int
    encoded_bytes: int

    def __post_init__(self) -> None:
        if self.frames < 0:
            raise ValueError(f"frames must be >= 0, got {self.frames}")
        if self.encoded_bytes < 0:
            raise ValueError(
                f"encoded_bytes must be >= 0, got {self.encoded_bytes}"
            )

    def __add__(self, other: FrameCost) -> FrameCost:
        if not isinstance(other, FrameCost):
            return NotImplemented
        return FrameCost(
            frames=self.frames + other.frames,
            encoded_bytes=self.encoded_bytes + other.encoded_bytes,
        )

    def __sub__(self, other: FrameCost) -> FrameCost:
        if not isinstance(other, FrameCost):
            return NotImplemented
        return FrameCost(
            frames=self.frames - other.frames,
            encoded_bytes=self.encoded_bytes - other.encoded_bytes,
        )


@dataclass(frozen=True)
class FrameBudget:
    """Capacity for physical frames and their encoded wire bytes."""

    frames: int
    encoded_bytes: int

    def __post_init__(self) -> None:
        if self.frames < 1:
            raise ValueError(f"frames must be >= 1, got {self.frames}")
        if self.encoded_bytes < 1:
            raise ValueError(
                f"encoded_bytes must be >= 1, got {self.encoded_bytes}"
            )

    def fits(self, cost: FrameCost) -> bool:
        """Whether ``cost`` stays within both capacity dimensions."""
        return (
            cost.frames <= self.frames
            and cost.encoded_bytes <= self.encoded_bytes
        )


class WireFrameError(ValueError):
    """A candidate domain event is not one complete valid wire event."""


@dataclass(frozen=True)
class AssembledEvent:
    seq: int
    event: str
    event_id: str
    payload: object
    event_sha256: str
    checkpoint: str | None


@dataclass(frozen=True)
class PreparedFrames:
    """One logical thing to write: the frames, and the cursor it advances.

    ``seq`` is temporarily ``None`` for an unbound domain-event template and
    permanently ``None`` for anything that is not a domain event. Transport
    notices and state patches deliberately own no ``seq`` (CONTEXT.md: a
    connection-local notice that consumed a global seq would leave a hole in
    every *other* connection's sequence, and a hole is exactly the signal the
    client uses to notice it missed something).

    ``id_line`` is empty when this thing carries no checkpoint. When it is
    set it is written after the last frame only.
    """

    seq: int | None
    frames: tuple[bytes, ...]
    sequence_offset: int | None = None
    sequence_field_limit: int = 0
    sequence_field: bytes = b""
    id_line: bytes = b""
    byte_size: int = 0
    replayable: bool = False
    # Cannot be dropped for want of room: a state patch that goes missing
    # leaves the client holding a lifetime state it will never be corrected
    # on. Undeliverable means the connection closes and the client reconnects
    # for an atomic snapshot instead.
    must_deliver: bool = False

    def ingress_cost(self) -> FrameCost:
        """Compute this prepared item's cost from its frame count and byte size."""
        return FrameCost(frames=len(self.frames), encoded_bytes=self.byte_size)

    def wire_frames(self) -> tuple[bytes, ...]:
        """The exact bytes, in order. Only the last frame carries the id.

        Every frame is a complete SSE record, so each one ends in a blank
        line; the id line belongs *before* that blank line, which is the only
        reason it is not simply appended.
        """
        last = len(self.frames) - 1
        return tuple(
            self._wire_frame_body(frame)
            + _NEWLINE
            + (self.id_line if i == last else b"")
            + _NEWLINE
            for i, frame in enumerate(self.frames)
        )

    def wire_bytes(self) -> bytes:
        return b"".join(self.wire_frames())

    def wire_frame_cost(self, index: int) -> FrameCost:
        """The exact encoded cost of one physical record."""
        if index < 0 or index >= len(self.frames):
            raise IndexError(index)
        is_final = index + 1 == len(self.frames)
        return FrameCost(
            frames=1,
            encoded_bytes=(
                len(self.frames[index])
                + len(self.sequence_field)
                + 2
                + (len(self.id_line) if is_final else 0)
            ),
        )

    def _wire_frame_body(self, frame: bytes) -> bytes:
        if not self.sequence_field:
            return frame
        offset = self.sequence_offset
        if offset is None:
            raise ValueError("sequence field has no insertion point")
        return frame[:offset] + self.sequence_field + frame[offset:]

    def with_sequence(self, seq: int) -> PreparedFrames:
        """Bind publication order without copying any prepared payload."""
        if seq < 1:
            raise ValueError(f"domain-event seq must be >= 1, got {seq}")
        if self.sequence_offset is None:
            raise ValueError("only a domain-event frame can carry an event seq")
        field = b'"seq":%d,' % seq
        if len(field) > self.sequence_field_limit:
            raise ValueError(
                f"seq field is {len(field)} bytes, over the reserved "
                f"{self.sequence_field_limit}"
            )
        return replace(
            self,
            seq=seq,
            sequence_field=field,
            byte_size=(
                self.byte_size
                + (len(field) - len(self.sequence_field)) * len(self.frames)
            ),
        )

    def with_checkpoint(
        self, seq: int, process_instance_id: str
    ) -> PreparedFrames:
        """Attach the checkpoint. O(1): the frames are shared, not copied."""
        process_instance_id = validate_process_instance_id(process_instance_id)
        if seq < 1:
            raise ValueError(f"checkpoint seq must be >= 1, got {seq}")
        sequenced = self.with_sequence(seq)
        id_line = b"id: %s:%d\n" % (process_instance_id.encode("utf-8"), seq)
        if len(id_line) > _ID_LINE_RESERVE:
            raise ValueError(
                f"id line is {len(id_line)} bytes, over the reserved "
                f"{_ID_LINE_RESERVE}"
            )
        return replace(
            sequenced,
            id_line=id_line,
            byte_size=sequenced.byte_size - len(sequenced.id_line) + len(id_line),
            must_deliver=True,
        )


def measured_frames(
    *,
    frames,
    seq: int | None = None,
    sequence_offset: int | None = None,
    sequence_field_limit: int = 0,
    sequence_field: bytes = b"",
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
        sequence_offset=sequence_offset,
        sequence_field_limit=sequence_field_limit,
        sequence_field=sequence_field,
        id_line=id_line,
        byte_size=sum(len(frame) for frame in built)
        + 2 * len(built)
        + len(sequence_field) * len(built)
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

    It must precede every data frame, and it is unconditional -- the argument
    is on :data:`STARTUP_CHECKPOINT_SEQ`.

    What it plants is a **re-seed boundary**, not a promise: the seq names
    where this process last stood, and the ring decides whether that
    position is resumable or too old. The ring's half of the vocabulary is
    in :mod:`agent_alfred.gateway.web.replay`.

    ``STARTUP_CHECKPOINT_SEQ`` is the one seq allowed here that no event
    ever owned; anything below it is not a boundary of any kind. It is also
    the one value for which the trailing-line rule is relaxed, because it is
    a boundary with no event under it rather than an event boundary.
    """
    process_instance_id = validate_process_instance_id(process_instance_id)
    if seq < STARTUP_CHECKPOINT_SEQ:
        raise ValueError(
            f"reseed seq must be >= {STARTUP_CHECKPOINT_SEQ}, got {seq}"
        )
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
    if limit < 1:
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


def _domain_header(
    *,
    event_name: str,
    event_id: str,
    chunk_index: int,
    chunk_count: int,
    event_sha256: str,
) -> bytes:
    """The exact metadata bytes used by both measurement and encoding."""
    return (
        '{"event":%s,"event_id":%s,"chunk_index":%d,"chunk_count":%d,'
        '"event_sha256":%s,"payload":'
        % (
            _dump_json(event_name),
            _dump_json(event_id),
            chunk_index,
            chunk_count,
            _dump_json(event_sha256),
        )
    ).encode("utf-8")


def _body_budget(
    *,
    sse_event: str,
    event_name: str,
    event_id: str,
    event_sha256: str,
    chunk_count: int,
    max_frame_bytes: int,
    sequence_field_reserve: int,
) -> int:
    """How many payload bytes one frame may carry.

    ``chunk_count`` enters because the header it appears in is part of the
    frame: a three-digit count costs two bytes more than a one-digit one.
    """
    longest = _domain_header(
        event_name=event_name,
        event_id=event_id,
        chunk_index=chunk_count,
        chunk_count=chunk_count,
        event_sha256=event_sha256,
    )
    fixed = len(b"event: ") + len(sse_event) + len(_NEWLINE)
    fixed += len(_DATA_PREFIX) + len(longest) + len(b"}")
    fixed += 2 * len(_NEWLINE) + _ID_LINE_RESERVE + sequence_field_reserve
    return max_frame_bytes - fixed


def _sequence_field_reserve(max_frame_bytes: int) -> int:
    """Reserve normal seq room while preserving the frozen 256-byte edge."""
    return min(
        _SEQ_FIELD_RESERVE,
        _MIN_SEQ_FIELD_RESERVE + max_frame_bytes - MIN_FRAME_BYTES,
    )


def validate_max_frame_bytes(value: int, *, minimum: int = MIN_FRAME_BYTES) -> None:
    """Keep caller-specific sequence room inside the physical frame cap."""
    if value < minimum:
        raise ValueError(f"max_frame_bytes must be >= {minimum}, got {value}")
    if value > MAX_FRAME_BYTES:
        raise ValueError(
            f"max_frame_bytes must be <= {MAX_FRAME_BYTES}, got {value}"
        )


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
    (ADR-0015 allocates it in the commit critical section). Each frame instead
    carries the same insertion point; commit binds the small sequence token
    while sharing every prepared payload byte by reference.
    """
    validate_max_frame_bytes(max_frame_bytes)
    sequence_field_reserve = _sequence_field_reserve(max_frame_bytes)
    body = _dump(payload)
    event_sha256 = hashlib.sha256(
        _dump({"event": event_name, "event_id": event_id, "payload": payload})
    ).hexdigest()
    count = 1
    chunks: tuple[bytes, ...] = ()
    for _ in range(16):
        budget = _body_budget(
            sse_event=DOMAIN_EVENT,
            event_name=event_name,
            event_id=event_id,
            event_sha256=event_sha256,
            chunk_count=count,
            max_frame_bytes=max_frame_bytes,
            sequence_field_reserve=sequence_field_reserve,
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
            event_sha256=event_sha256,
            chunk_count=len(chunks),
            max_frame_bytes=max_frame_bytes,
            sequence_field_reserve=sequence_field_reserve,
        )
        chunks = _utf8_safe_split(body, budget)
    frames = tuple(
        b"event: "
        + DOMAIN_EVENT.encode()
        + _NEWLINE
        + _DATA_PREFIX
        + _domain_header(
            event_name=event_name,
            event_id=event_id,
            chunk_index=index,
            chunk_count=len(chunks),
            event_sha256=event_sha256,
        )
        + chunk
        + b"}"
        for index, chunk in enumerate(chunks)
    )
    return measured_frames(
        frames=frames,
        sequence_offset=len(_DOMAIN_FRAME_PREFIX),
        sequence_field_limit=sequence_field_reserve,
        replayable=replayable,
        must_deliver=replayable,
    )


_WIRE_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)")
_WIRE_DIGEST = re.compile(r"[0-9a-f]{64}")
_WIRE_CHECKPOINT = re.compile(rb"id: [^:\r\n]+:[1-9][0-9]*")


def _take_json_string(text: str, position: int) -> tuple[str, bytes, int]:
    value, end = json.JSONDecoder().raw_decode(text, position)
    if not isinstance(value, str):
        raise WireFrameError("domain event metadata must be strings")
    raw_token = text[position:end].encode("utf-8")
    try:
        canonical_token = _dump_json(value).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WireFrameError("domain event metadata is not canonical UTF-8") from exc
    if raw_token != canonical_token:
        raise WireFrameError("domain event metadata is not canonically encoded")
    return value, raw_token, end


def _take_wire_integer(text: str, position: int) -> tuple[int, int]:
    matched = _WIRE_INTEGER.match(text, position)
    if matched is None:
        raise WireFrameError("chunk metadata must be non-negative integers")
    return int(matched.group()), matched.end()


@dataclass(frozen=True, slots=True)
class _DomainIdentity:
    seq: int
    event: str
    event_token: bytes
    event_id: str
    event_id_token: bytes
    chunk_count: int
    event_sha256: str
    digest_token: bytes


@dataclass(frozen=True, slots=True)
class _DomainChunk:
    identity: _DomainIdentity
    index: int
    fragment: bytes


def _parse_domain_data(data: bytes) -> _DomainChunk:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WireFrameError("domain event data must be UTF-8") from exc
    position = 0

    def consume(literal: str) -> None:
        nonlocal position
        if not text.startswith(literal, position):
            raise WireFrameError("domain event data has an invalid shape")
        position += len(literal)

    if not text.startswith('{"seq":'):
        raise WireFrameError("domain event seq is missing")
    consume('{"seq":')
    seq, position = _take_wire_integer(text, position)
    if seq < 1:
        raise WireFrameError("domain event seq must be a positive integer")
    consume(',"event":')
    event, event_token, position = _take_json_string(text, position)
    consume(',"event_id":')
    event_id, event_id_token, position = _take_json_string(text, position)
    consume(',"chunk_index":')
    chunk_index, position = _take_wire_integer(text, position)
    consume(',"chunk_count":')
    chunk_count, position = _take_wire_integer(text, position)
    consume(',"event_sha256":')
    event_sha256, digest_token, position = _take_json_string(text, position)
    if _WIRE_DIGEST.fullmatch(event_sha256) is None:
        raise WireFrameError("event_sha256 must be 64 lowercase hexadecimal characters")
    consume(',"payload":')
    if not text.endswith("}"):
        raise WireFrameError("domain event data has an invalid payload fragment")
    payload_fragment = text[position:-1].encode("utf-8")
    return _DomainChunk(
        identity=_DomainIdentity(
            seq=seq,
            event=event,
            event_token=event_token,
            event_id=event_id,
            event_id_token=event_id_token,
            chunk_count=chunk_count,
            event_sha256=event_sha256,
            digest_token=digest_token,
        ),
        index=chunk_index,
        fragment=payload_fragment,
    )


def assemble_domain_event_wire_frames(
    wire_records: Sequence[bytes],
    *,
    budget: FrameBudget,
) -> AssembledEvent:
    """Validate and assemble exactly one complete domain event.

    The input is a finite candidate boundary rather than a stream: absence of
    the final chunk is therefore an error, not an ambiguous "wait for more".
    """
    if not wire_records:
        raise WireFrameError("a domain event requires at least one wire record")
    if len(wire_records) > budget.frames:
        raise WireFrameError("domain event exceeds the frame budget")

    total_bytes = 0
    identity: _DomainIdentity | None = None
    payload_fragments: list[bytes] = []
    checkpoint: str | None = None
    for expected_index, record in enumerate(wire_records):
        if not isinstance(record, bytes):
            raise WireFrameError("wire records must be bytes")
        total_bytes += len(record)
        if len(record) > MAX_FRAME_BYTES:
            raise WireFrameError("wire record exceeds the hard frame limit")
        if total_bytes > budget.encoded_bytes:
            raise WireFrameError("domain event exceeds the encoded-byte budget")
        if not record.endswith(b"\n\n"):
            raise WireFrameError("wire record must end in exactly one blank line")
        content = record[:-2]
        lines = content.split(b"\n")
        if len(lines) not in (2, 3) or lines[0] != b"event: domain_event":
            raise WireFrameError("wire record is not exactly one domain_event record")
        if not lines[1].startswith(_DATA_PREFIX):
            raise WireFrameError("wire record must contain exactly one data field")
        if len(lines) == 3:
            if expected_index != len(wire_records) - 1:
                raise WireFrameError("checkpoint is only valid on the final chunk")
            if (
                len(lines[2]) + len(_NEWLINE) > _ID_LINE_RESERVE
                or _WIRE_CHECKPOINT.fullmatch(lines[2]) is None
            ):
                raise WireFrameError("checkpoint has an invalid wire shape")
            try:
                checkpoint = lines[2][len(b"id: ") :].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WireFrameError("checkpoint must be UTF-8") from exc
            instance, _separator, _seq = checkpoint.partition(":")
            try:
                validate_process_instance_id(instance)
            except ValueError as exc:
                raise WireFrameError("checkpoint has an invalid wire shape") from exc

        chunk = _parse_domain_data(lines[1][len(_DATA_PREFIX) :])
        current_identity = chunk.identity
        count = current_identity.chunk_count
        if count < 1 or count > budget.frames:
            raise WireFrameError("chunk_count is outside the frame budget")
        if chunk.index != expected_index or chunk.index >= count:
            raise WireFrameError("chunk indexes must be consecutive and in range")
        if identity is None:
            identity = current_identity
        elif current_identity != identity:
            raise WireFrameError("domain event metadata changed between chunks")
        payload_fragments.append(chunk.fragment)

    assert identity is not None
    if len(wire_records) != identity.chunk_count:
        raise WireFrameError("domain event is missing or has extra chunks")
    try:
        payload = json.loads(b"".join(payload_fragments).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireFrameError("assembled payload is not valid JSON") from exc
    try:
        canonical_event = _dump(
            {"event": identity.event, "event_id": identity.event_id, "payload": payload}
        )
    except UnicodeEncodeError as exc:
        raise WireFrameError("assembled event is not canonical UTF-8") from exc
    actual_digest = hashlib.sha256(canonical_event).hexdigest()
    if actual_digest != identity.event_sha256:
        raise WireFrameError("event_sha256 does not match the assembled event")
    if checkpoint is not None:
        _instance, _separator, checkpoint_seq = checkpoint.rpartition(":")
        if int(checkpoint_seq) != identity.seq:
            raise WireFrameError("checkpoint seq does not match domain event seq")
    return AssembledEvent(
        seq=identity.seq,
        event=identity.event,
        event_id=identity.event_id,
        payload=payload,
        event_sha256=identity.event_sha256,
        checkpoint=checkpoint,
    )


def _payload_frame(sse_event: str, body: dict) -> bytes:
    return b"event: " + sse_event.encode() + _NEWLINE + _DATA_PREFIX + _dump(body)


def _single_payload_frame(sse_event: str, body: dict) -> PreparedFrames:
    """One frame for a bounded payload. Over the hard limit is a refusal,
    never a silently oversized frame."""
    frame = _payload_frame(sse_event, body)
    if len(frame) + 2 > MAX_FRAME_BYTES:
        raise ValueError(f"{sse_event} payload exceeds {MAX_FRAME_BYTES} bytes")
    return measured_frames(frames=(frame,))


def payload_cost(sse_event: str, body: dict) -> int:
    """Exactly what this payload would cost a queue counting encoded bytes.

    The same number the frame would carry, arrived at without building the
    frame -- and without the hard frame limit being a reason to fail. That
    second property is the point: this is called from a state transition,
    on the thread that owns the Run, while the authoritative snapshot has
    already moved, and an exception raised here would leave the Host's
    state machine half-finished. Measuring must be total and bounded; the
    limit's refusal belongs to the thread that writes, not the one that
    decides.
    """
    return len(_payload_frame(sse_event, body)) + 2


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
