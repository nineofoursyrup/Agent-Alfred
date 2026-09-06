"""The SSE wire format: frames, chunking, and the three payload kinds."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from agent_alfred.gateway.web import frames


def _wire(prepared: frames.PreparedFrames) -> bytes:
    return prepared.wire_bytes()


# --- control frames --------------------------------------------------------


def test_retry_frame_is_bare_and_carries_no_data() -> None:
    assert _wire(frames.retry_frame(1000)) == b"retry: 1000\n\n"
    assert frames.DEFAULT_RETRY_MS == 1000
    assert frames.BACKOFF_RETRY_MS == 3000


def test_the_reseed_frame_is_a_dataless_id_frame() -> None:
    wire = _wire(frames.reseed_frame("inst", 7))
    assert wire == b"id: inst:7\n\n"
    assert b"data" not in wire
    assert b"event" not in wire


@pytest.mark.parametrize(
    "process_instance_id",
    ("line\nfeed", "carriage\rreturn", "nul\0byte", "contains:colon", "snowman-☃"),
)
@pytest.mark.parametrize("producer", ("reseed", "checkpoint"))
def test_an_sse_id_refuses_an_instance_outside_its_ascii_wire_syntax(
    process_instance_id: str, producer: str
) -> None:
    with pytest.raises(ValueError, match="process_instance_id"):
        if producer == "reseed":
            frames.reseed_frame(process_instance_id, 7)
        else:
            _domain().with_checkpoint(7, process_instance_id)


def test_heartbeat_is_a_comment_not_an_event() -> None:
    wire = _wire(frames.heartbeat_frame())
    assert wire == b":hb\n\n"
    # A comment is ignored by the dispatch algorithm, so it cannot dispatch
    # an event and cannot touch the last event id buffer.
    assert not wire.startswith(b"data:")
    assert not wire.startswith(b"event:")


# --- domain events ---------------------------------------------------------


def _domain(*, text: str = "hello", seq: int = 1, **kwargs) -> frames.PreparedFrames:
    kwargs.setdefault("replayable", True)
    return frames.domain_event_frames(
        event_name="block.delta",
        payload={"text": text},
        event_id="eid-1",
        **kwargs,
    ).with_sequence(seq)


def test_a_small_event_is_one_frame_carrying_the_checkpoint() -> None:
    prepared = _domain().with_checkpoint(9, "inst")
    wire = _wire(prepared)
    assert wire.startswith(b"event: domain_event\ndata: ")
    assert wire.endswith(b"\nid: inst:9\n\n")
    assert wire.count(b"id: inst:9") == 1


def test_event_sha256_has_fixed_literal_for_known_event() -> None:
    prepared = _domain()
    [physical] = prepared.frames

    assert b'"event_sha256":"' in physical
    assert (
        b'"event_sha256":"'
        b"22da99b10adb62dfa04c2b163118496bab1f366f8787cfed02c40e1ecb025515"
        b'"' in physical
    )


def test_maximum_checkpoint_reserve_keeps_every_wire_record_within_limit() -> None:
    prepared = frames.domain_event_frames(
        event_name="e",
        payload="x" * 92,
        event_id="i",
        replayable=True,
        max_frame_bytes=256,
    ).with_checkpoint(1, "p" * 57)

    assert max(map(len, prepared.wire_frames())) <= 256


def test_minimum_frame_limit_refuses_a_seq_token_that_no_longer_fits() -> None:
    prepared = frames.domain_event_frames(
        event_name="e",
        payload="x" * 92,
        event_id="i",
        replayable=True,
        max_frame_bytes=256,
    )

    assert max(map(len, prepared.with_sequence(9).wire_frames())) <= 256
    with pytest.raises(ValueError, match="over the reserved 8"):
        prepared.with_sequence(10)


def test_small_event_wire_digest_and_checkpoint_bytes_are_stable() -> None:
    wire = _domain().with_checkpoint(9, "inst").wire_bytes()
    assert wire == (
        b"event: domain_event\n"
        b'data: {"seq":9,"event":"block.delta","event_id":"eid-1",'
        b'"chunk_index":0,"chunk_count":1,'
        b'"event_sha256":"22da99b10adb62dfa04c2b163118496bab1f366f8787cfed02c40e1ecb025515",'
        b'"payload":{"text":"hello"}}\n'
        b"id: inst:9\n\n"
    )
    assert hashlib.sha256(wire).hexdigest() == (
        "1e244626595949fbbff9c2e0af903c64f33646363717891eecdb07a79e92f6a5"
    )


def test_only_the_last_frame_carries_the_id() -> None:
    big = "x" * (300 * 1024)
    prepared = _domain(text=big, max_frame_bytes=64 * 1024).with_checkpoint(3, "inst")
    wire_frames = prepared.wire_frames()
    assert len(wire_frames) > 2
    for frame in wire_frames[:-1]:
        assert b"\nid: " not in frame
    assert wire_frames[-1].endswith(b"id: inst:3\n\n")


def test_every_frame_stays_under_the_hard_limit() -> None:
    prepared = _domain(
        text="y" * (900 * 1024), max_frame_bytes=64 * 1024
    ).with_checkpoint(11, "inst")
    assert len(prepared.frames) > 1
    for frame in prepared.wire_frames():
        assert len(frame) <= 64 * 1024


def test_chunk_metadata_lets_a_client_reassemble_one_logical_event() -> None:
    body = {"text": "é" * (200 * 1024)}
    prepared = frames.domain_event_frames(
        event_name="block.delta",
        payload=body,
        event_id="eid-9",
        replayable=True,
        max_frame_bytes=64 * 1024,
    )
    assert len(prepared.frames) > 1
    heads = [_chunk_meta(frame) for frame in prepared.frames]
    assert [head["chunk_index"] for head in heads] == list(range(len(heads)))
    assert {head["chunk_count"] for head in heads} == {len(heads)}
    assert {head["event_id"] for head in heads} == {"eid-9"}
    assert {head["event"] for head in heads} == {"block.delta"}


def _chunk_meta(frame: bytes) -> dict:
    """Read one frame back the way a browser would: strip the SSE field
    lines, then take the header's own fields off the partial JSON."""
    lines = frame.split(b"\n")
    data_lines = [line[len(b"data: ") :] for line in lines if line.startswith(b"data:")]
    raw = b"".join(data_lines).decode("utf-8")
    head, _sep, _body = raw.partition('"payload":')
    return json.loads(head.rstrip().rstrip(",") + "}")


def test_utf8_splitting_never_tears_a_character() -> None:
    # A four-byte character straddling every plausible boundary: if a chunk
    # is cut mid-character the reassembled bytes are not UTF-8 at all.
    body = {"text": ("a" * 3 + "\U0001f600" + "b" * 3) * 60}
    prepared = frames.domain_event_frames(
        event_name="block.delta",
        payload=body,
        event_id="e",
        replayable=False,
        max_frame_bytes=512,
    )
    assert len(prepared.frames) > 1
    for frame in prepared.frames:
        frame.decode("utf-8")  # raises if a boundary was torn
    assert _reassemble(prepared) == body


def test_reassembly_is_exact_for_a_multi_chunk_event() -> None:
    body = {"text": "中文é\U0001f600" * 5000, "n": 3}
    prepared = frames.domain_event_frames(
        event_name="step.finished",
        payload=body,
        event_id="e-2",
        replayable=True,
        max_frame_bytes=8 * 1024,
    )
    assert _reassemble(prepared) == body


def test_reference_assembler_commits_complete_digest_verified_event_once() -> None:
    prepared = _domain(text="中文é\U0001f600" * 500, max_frame_bytes=512)
    identified = prepared.with_checkpoint(9, "inst")

    assembled = frames.assemble_domain_event_wire_frames(
        identified.wire_frames(),
        budget=frames.FrameBudget(
            frames=len(identified.frames),
            encoded_bytes=identified.byte_size,
        ),
    )

    assert assembled == frames.AssembledEvent(
        seq=9,
        event="block.delta",
        event_id="eid-1",
        payload={"text": "中文é\U0001f600" * 500},
        event_sha256=(
            "687b41dc0294e093a96d36e8e907b42e92b0a482007b7d56e1a029b919498e57"
        ),
        checkpoint="inst:9",
    )


def test_reference_assembler_requires_a_positive_domain_event_seq() -> None:
    [record] = _domain().wire_frames()
    missing = (record.replace(b'{"seq":1,', b"{", 1),)
    zero = (record.replace(b'"seq":1', b'"seq":0', 1),)

    with pytest.raises(frames.WireFrameError, match="seq is missing"):
        frames.assemble_domain_event_wire_frames(
            missing, budget=_assembly_budget(missing)
        )
    with pytest.raises(frames.WireFrameError, match="positive integer"):
        frames.assemble_domain_event_wire_frames(zero, budget=_assembly_budget(zero))


def test_reference_assembler_rejects_seq_drift_between_chunks() -> None:
    records = _chunked_wire()
    changed = records[1].replace(b'"seq":8', b'"seq":9', 1)
    candidate = (records[0], changed, *records[2:])

    with pytest.raises(frames.WireFrameError, match="metadata changed"):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_checkpoint_seq_mismatch() -> None:
    records = _domain().with_checkpoint(2, "inst").wire_frames()
    candidate = (records[0].replace(b"id: inst:2", b"id: inst:3", 1),)

    with pytest.raises(frames.WireFrameError, match="checkpoint seq"):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_normalizes_non_utf8_checkpoint_to_wire_error() -> None:
    [record] = _domain().with_checkpoint(2, "inst").wire_frames()
    candidate = (record.replace(b"id: inst:2", b"id: \xff:2", 1),)

    with pytest.raises(frames.WireFrameError) as caught:
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def _assembly_budget(
    records: tuple[bytes, ...], *, extra_frames: int = 0
) -> frames.FrameBudget:
    return frames.FrameBudget(
        frames=len(records) + extra_frames,
        encoded_bytes=sum(map(len, records)),
    )


def _assemble(prepared: frames.PreparedFrames) -> frames.AssembledEvent:
    if prepared.sequence_offset is not None and prepared.seq is None:
        prepared = prepared.with_sequence(1)
    records = prepared.wire_frames()
    return frames.assemble_domain_event_wire_frames(
        records, budget=_assembly_budget(records)
    )


def _chunked_wire(*, event_id: str = "eid-1") -> tuple[bytes, ...]:
    return frames.domain_event_frames(
        event_name="block.delta",
        payload={"text": "é\U0001f600" * 300},
        event_id=event_id,
        replayable=True,
        max_frame_bytes=512,
    ).with_checkpoint(8, "inst").wire_frames()


def test_every_single_and_chunked_frame_carries_same_event_sha256() -> None:
    single = _domain()
    chunked_small = _domain(text="x" * 6000, max_frame_bytes=512)
    chunked_large = _domain(text="x" * 6000, max_frame_bytes=1024)

    single_digest = _chunk_meta(single.frames[0])["event_sha256"]
    small_digests = {
        _chunk_meta(frame)["event_sha256"] for frame in chunked_small.frames
    }
    large_digests = {
        _chunk_meta(frame)["event_sha256"] for frame in chunked_large.frames
    }
    assert small_digests == large_digests
    assert len(single_digest) == 64
    assert all("a" <= char <= "f" or char.isdigit() for char in single_digest)


def test_reference_assembler_rejects_malformed_sse_and_nonterminal_checkpoint() -> None:
    records = _chunked_wire()
    malformed = (records[0].replace(b"event: domain_event", b"event: state_patch"),)
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            malformed, budget=_assembly_budget(malformed)
        )

    nonterminal = records[0][:-1] + b"id: inst:8\n\n"
    candidate = (nonterminal, *records[1:])
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_missing_chunk() -> None:
    records = _chunked_wire()
    candidate = records[:-1]
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate,
            budget=frames.FrameBudget(
                frames=len(records), encoded_bytes=sum(map(len, records))
            ),
        )


def test_reference_assembler_rejects_duplicate_chunk() -> None:
    records = _chunked_wire()
    candidate = (records[0], records[0], *records[1:])
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_out_of_order_chunk() -> None:
    records = _chunked_wire()
    candidate = (records[1], records[0], *records[2:])
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_chunk_count_drift() -> None:
    records = _chunked_wire()
    candidate = (
        records[0],
        records[1].replace(
            f'"chunk_count":{len(records)}'.encode(),
            f'"chunk_count":{len(records) + 1}'.encode(),
        ),
        *records[2:],
    )
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_digest_drift() -> None:
    records = _chunked_wire()
    digest = _chunk_meta(records[1])["event_sha256"].encode()
    replacement = (b"0" if digest[:1] != b"0" else b"1") + digest[1:]
    candidate = (records[0], records[1].replace(digest, replacement), *records[2:])
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_payload_tampering() -> None:
    prepared = _domain()
    [record] = prepared.wire_frames()
    candidate = (record.replace(b"hello", b"jello"),)
    with pytest.raises(frames.WireFrameError, match="does not match"):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_interleaved_event_frames() -> None:
    first = _chunked_wire(event_id="first")
    second = _chunked_wire(event_id="second")
    candidate = (first[0], second[1], *first[2:])
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


@pytest.mark.parametrize(
    "literal,escaped",
    [
        (b'"event":"block.delta"', b'"event":"\\u0062lock.delta"'),
        (b'"event_id":"eid-1"', b'"event_id":"\\u0065id-1"'),
    ],
)
def test_reference_assembler_rejects_metadata_escape_drift_between_chunks(
    literal: bytes,
    escaped: bytes,
) -> None:
    records = _chunked_wire()
    candidate = (records[0], records[1].replace(literal, escaped), *records[2:])

    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


def test_reference_assembler_rejects_noncanonical_digest_escape() -> None:
    [record] = _domain().wire_frames()
    digest = _chunk_meta(record)["event_sha256"].encode()
    assert digest.startswith(b"2")
    escaped = b"\\u0032" + digest[1:]
    candidate = (record.replace(digest, escaped, 1),)

    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


@pytest.mark.parametrize(
    "old,new",
    [
        (b'"event":"block.delta"', b'"event":"\\ud800"'),
        (b'"event_id":"eid-1"', b'"event_id":"\\ud800"'),
        (b'"payload":{"text":"hello"}', b'"payload":{"text":"\\ud800"}'),
    ],
)
def test_reference_assembler_normalizes_lone_surrogate_to_wire_error(
    old: bytes,
    new: bytes,
) -> None:
    [record] = _domain().wire_frames()
    candidate = (record.replace(old, new, 1),)

    with pytest.raises(frames.WireFrameError) as caught:
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )
    assert isinstance(caught.value.__cause__, UnicodeEncodeError)


def test_reference_assembler_round_trips_canonical_escaped_metadata() -> None:
    prepared = frames.domain_event_frames(
        event_name='quoted "event" \\ 中文',
        event_id='quoted "id" \\ é',
        payload={"nested": ['quoted "value"', "反斜杠\\", "😀"]},
        replayable=False,
        max_frame_bytes=512,
    )

    assembled = _assemble(prepared)
    assert assembled.event == 'quoted "event" \\ 中文'
    assert assembled.event_id == 'quoted "id" \\ é'
    assert assembled.payload == {"nested": ['quoted "value"', "反斜杠\\", "😀"]}


def test_digest_overhead_is_included_in_frame_hard_limit() -> None:
    prepared = frames.domain_event_frames(
        event_name="e",
        payload="x" * 8000,
        event_id="i",
        replayable=True,
        max_frame_bytes=288,
    ).with_checkpoint(1, "p" * 57)
    assert len(prepared.frames) > 1
    assert max(map(len, prepared.wire_frames())) <= 288

    production = frames.domain_event_frames(
        event_name="e",
        payload="x" * (2 * frames.MAX_FRAME_BYTES),
        event_id="i",
        replayable=True,
    ).with_checkpoint(1, "p" * 57)
    assert len(production.frames) > 1
    assert max(map(len, production.wire_frames())) <= frames.MAX_FRAME_BYTES
    assert _assemble(production).payload == "x" * (2 * frames.MAX_FRAME_BYTES)


def test_digest_does_not_move_checkpoint_off_last_wire_record() -> None:
    records = frames.domain_event_frames(
        event_name="e",
        payload="x" * 8000,
        event_id="i",
        replayable=True,
        max_frame_bytes=288,
    ).with_checkpoint(1, "inst").wire_frames()
    assert all(b"\nid: " not in record for record in records[:-1])
    assert records[-1].endswith(b"\nid: inst:1\n\n")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        "",
        True,
        17,
        {"z": 1, "a": "中文é\U0001f600" * 100},
    ],
)
def test_digest_budget_boundary_round_trips_through_reference_assembler(
    payload: object,
) -> None:
    prepared = frames.domain_event_frames(
        event_name="boundary",
        payload=payload,
        event_id="event",
        replayable=False,
        max_frame_bytes=512,
    )
    assert max(map(len, prepared.wire_frames())) <= 512
    assert _assemble(prepared).payload == payload


@pytest.mark.parametrize(
    "old,new",
    [
        (b'"chunk_index":0', b'"chunk_index":-1'),
        (b'"chunk_index":0', b'"chunk_index":true'),
        (b'"chunk_count":1', b'"chunk_count":0'),
        (b'"chunk_count":1', b'"chunk_count":true'),
        (b'"event_sha256":"', b'"event_sha256":"A'),
    ],
)
def test_reference_assembler_rejects_invalid_central_metadata(
    old: bytes, new: bytes
) -> None:
    [record] = _domain().wire_frames()
    candidate = (record.replace(old, new, 1),)
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate, budget=_assembly_budget(candidate)
        )


@pytest.mark.parametrize(
    "candidate",
    [
        (),
        (b"event: domain_event\ndata: {}",),
        (b"event: domain_event\ndata: {}\n\nextra",),
        (b"event: domain_event\ndata: {}\n\n\n",),
        (b"event: domain_event\ndata: {}\nid: bad\n\n",),
        (b"event: domain_event\ndata: {}\nid: inst:1\nid: inst:2\n\n",),
    ],
)
def test_reference_assembler_rejects_invalid_record_boundaries(
    candidate: tuple[bytes, ...],
) -> None:
    with pytest.raises(frames.WireFrameError):
        frames.assemble_domain_event_wire_frames(
            candidate,
            budget=frames.FrameBudget(frames=8, encoded_bytes=2 * 1024 * 1024),
        )


def test_reference_assembler_enforces_frame_and_encoded_byte_budgets() -> None:
    records = _chunked_wire()
    with pytest.raises(frames.WireFrameError, match="frame budget"):
        frames.assemble_domain_event_wire_frames(
            records,
            budget=frames.FrameBudget(
                frames=len(records) - 1,
                encoded_bytes=sum(map(len, records)),
            ),
        )
    with pytest.raises(frames.WireFrameError, match="encoded-byte budget"):
        frames.assemble_domain_event_wire_frames(
            records,
            budget=frames.FrameBudget(
                frames=len(records),
                encoded_bytes=sum(map(len, records)) - 1,
            ),
        )


def test_reference_assembler_returns_none_for_transient_checkpoint_and_is_frozen(
) -> None:
    assembled = _assemble(_domain(replayable=False))
    assert assembled.checkpoint is None
    with pytest.raises(FrozenInstanceError):
        assembled.payload = {}


def _reassemble(prepared: frames.PreparedFrames) -> object:
    """Join the payload fragments exactly as a browser would, then parse."""
    joined = b"".join(_payload_fragment(frame) for frame in prepared.frames)
    return json.loads(joined.decode("utf-8"))


def _payload_fragment(frame: bytes) -> bytes:
    head, sep, body = frame.partition(b'"payload":')
    assert sep
    # One data line per frame: everything after the header key is the
    # fragment, minus the trailing "}" and the record separator.
    return body.rstrip(b"\n")[:-1]


def test_a_frame_that_cannot_hold_one_character_is_refused() -> None:
    with pytest.raises(ValueError):
        frames.domain_event_frames(
            event_name="block.delta",
            payload={"text": "\U0001f600"},
            event_id="e",
            replayable=False,
            max_frame_bytes=1,
        )


def test_domain_frames_reject_a_budget_above_the_physical_frame_limit() -> None:
    with pytest.raises(ValueError, match="max_frame_bytes must be <= 1048576"):
        frames.domain_event_frames(
            event_name="run.started",
            payload={"purpose": "chat"},
            event_id="e",
            replayable=True,
            max_frame_bytes=1_048_577,
        )


def test_byte_size_matches_the_bytes_actually_written() -> None:
    prepared = _domain(text="z" * 4096, max_frame_bytes=1024).with_checkpoint(
        4, "inst"
    )
    assert prepared.byte_size == len(prepared.wire_bytes())


def test_binding_sequence_and_checkpoint_shares_the_prepared_frames() -> None:
    prepared = frames.domain_event_frames(
        event_name="block.delta",
        payload={"text": "q" * 4096},
        event_id="eid-1",
        replayable=True,
        max_frame_bytes=1024,
    )
    sequenced = prepared.with_sequence(5)
    identified = sequenced.with_checkpoint(5, "inst")
    # The commit step must not copy the payload: the prepared frames are
    # shared by reference and only bounded sequence/checkpoint metadata moves.
    assert sequenced.frames is prepared.frames
    assert identified.frames is prepared.frames
    assert sequenced.seq == 5
    assert identified.seq == 5
    assert sequenced.byte_size == len(sequenced.wire_bytes())


def test_a_checkpoint_longer_than_the_reserved_room_is_refused() -> None:
    with pytest.raises(ValueError):
        _domain().with_checkpoint(1, "i" * 200)


# --- the other two payload kinds -------------------------------------------


def test_a_transport_notice_carries_no_id_and_no_seq() -> None:
    prepared = frames.replay_gap_notice(
        reason="too_old",
        requested_seq=3,
        oldest_seq=10,
        high_water_seq=20,
        current_run_state="unrecoverable",
    )
    wire = _wire(prepared)
    assert wire.startswith(b"event: transport_notice\ndata: ")
    assert b"\nid: " not in wire
    assert prepared.seq is None
    payload = json.loads(wire.split(b"data: ", 1)[1].split(b"\n", 1)[0])
    assert payload == {
        "code": "replay_gap",
        "gap_reason": "too_old",
        "requested_seq": 3,
        "oldest_seq": 10,
        "high_water_seq": 20,
        "current_run_state": "unrecoverable",
    }


def test_deltas_dropped_is_a_notice_about_this_connection_only() -> None:
    prepared = frames.deltas_dropped_notice(7)
    wire = _wire(prepared)
    assert b"event: transport_notice" in wire
    payload = json.loads(wire.split(b"data: ", 1)[1].split(b"\n", 1)[0])
    assert payload == {"code": "deltas_dropped", "count": 7}


def test_a_state_patch_carries_its_own_instance_and_revision() -> None:
    prepared = frames.state_patch_frames(
        {"process_instance_id": "inst", "state_revision": 4}
    )
    wire = _wire(prepared)
    assert wire.startswith(b"event: state_patch\ndata: ")
    assert b"\nid: " not in wire
    assert prepared.seq is None
    assert prepared.replayable is False


def test_transport_notices_and_patches_are_never_replayable() -> None:
    assert frames.deltas_dropped_notice(1).replayable is False
    assert frames.state_patch_frames({}).replayable is False
    assert frames.heartbeat_frame().replayable is False
    # A transient domain event is prepared with replayable False; the broker
    # is the one that says so, from the event's own replayable predicate.
    assert frames.domain_event_frames(
        event_name="block.delta", payload={}, event_id="e", replayable=False
    ).replayable is False
    assert frames.domain_event_frames(
        event_name="run.finished", payload={}, event_id="e", replayable=True
    ).replayable is True


# --- the named cost of one logical item -------------------------------------


def test_frame_cost_carries_two_named_dimensions() -> None:
    cost = frames.FrameCost(frames=2, encoded_bytes=300)
    assert cost.frames == 2
    assert cost.encoded_bytes == 300


def test_frame_cost_is_frozen() -> None:
    cost = frames.FrameCost(frames=1, encoded_bytes=10)
    with pytest.raises(FrozenInstanceError):
        cost.frames = 5
    with pytest.raises(FrozenInstanceError):
        cost.encoded_bytes = 50


def test_frame_cost_dimensions_cannot_be_swapped() -> None:
    cost = frames.FrameCost(frames=2, encoded_bytes=300)
    swapped = frames.FrameCost(frames=300, encoded_bytes=2)
    assert cost != swapped
    assert cost == frames.FrameCost(frames=2, encoded_bytes=300)


def test_frame_cost_addition_sums_each_dimension() -> None:
    assert (
        frames.FrameCost(frames=1, encoded_bytes=10)
        + frames.FrameCost(frames=2, encoded_bytes=20)
    ) == frames.FrameCost(frames=3, encoded_bytes=30)


def test_frame_cost_subtraction_removes_each_dimension() -> None:
    assert (
        frames.FrameCost(frames=3, encoded_bytes=30)
        - frames.FrameCost(frames=1, encoded_bytes=10)
    ) == frames.FrameCost(frames=2, encoded_bytes=20)


def test_frame_cost_refuses_negative_counts() -> None:
    with pytest.raises(ValueError):
        frames.FrameCost(frames=-1, encoded_bytes=0)
    with pytest.raises(ValueError):
        frames.FrameCost(frames=0, encoded_bytes=-1)
    # A subtraction that would take either dimension below zero is refused
    # outright: a queue that released more than it held is a bug to surface,
    # not a negative balance to carry.
    with pytest.raises(ValueError):
        frames.FrameCost(frames=1, encoded_bytes=10) - frames.FrameCost(
            frames=2, encoded_bytes=0
        )
    with pytest.raises(ValueError):
        frames.FrameCost(frames=1, encoded_bytes=10) - frames.FrameCost(
            frames=0, encoded_bytes=20
        )


def test_frame_budget_requires_positive_dimensions() -> None:
    with pytest.raises(ValueError, match="frames must be >= 1"):
        frames.FrameBudget(frames=0, encoded_bytes=1)
    with pytest.raises(ValueError, match="encoded_bytes must be >= 1"):
        frames.FrameBudget(frames=1, encoded_bytes=0)


def test_frame_budget_fits_only_when_both_dimensions_fit() -> None:
    budget = frames.FrameBudget(frames=2, encoded_bytes=300)

    assert budget.fits(frames.FrameCost(frames=2, encoded_bytes=300)) is True
    assert budget.fits(frames.FrameCost(frames=3, encoded_bytes=299)) is False
    assert budget.fits(frames.FrameCost(frames=1, encoded_bytes=301)) is False
    with pytest.raises(FrozenInstanceError):
        budget.frames = 3


def test_ingress_cost_is_a_frame_cost_with_both_dimensions() -> None:
    prepared = _domain(text="z" * 4096, max_frame_bytes=1024).with_checkpoint(
        4, "inst"
    )
    cost = prepared.ingress_cost()
    assert isinstance(cost, frames.FrameCost)
    assert cost.frames == len(prepared.frames)
    assert cost.encoded_bytes == prepared.byte_size
    # The same holds for a logical event split into many physical frames.
    chunked = _domain(text="y" * (900 * 1024), max_frame_bytes=64 * 1024)
    assert chunked.ingress_cost().frames == len(chunked.frames) > 1
    assert chunked.ingress_cost().encoded_bytes == chunked.byte_size
