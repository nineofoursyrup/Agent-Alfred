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


def test_heartbeat_is_a_comment_not_an_event() -> None:
    wire = _wire(frames.heartbeat_frame())
    assert wire == b":hb\n\n"
    # A comment is ignored by the dispatch algorithm, so it cannot dispatch
    # an event and cannot touch the last event id buffer.
    assert not wire.startswith(b"data:")
    assert not wire.startswith(b"event:")


# --- domain events ---------------------------------------------------------


def _domain(*, text: str = "hello", **kwargs) -> frames.PreparedFrames:
    kwargs.setdefault("replayable", True)
    return frames.domain_event_frames(
        event_name="block.delta",
        payload={"text": text},
        event_id="eid-1",
        **kwargs,
    )


def test_a_small_event_is_one_frame_carrying_the_checkpoint() -> None:
    prepared = _domain().with_checkpoint(9, "inst")
    wire = _wire(prepared)
    assert wire.startswith(b"event: domain_event\ndata: ")
    assert wire.endswith(b"\nid: inst:9\n\n")
    assert wire.count(b"id: inst:9") == 1


def test_small_event_wire_digest_and_checkpoint_bytes_are_stable() -> None:
    wire = _domain().with_checkpoint(9, "inst").wire_bytes()
    assert wire == (
        b"event: domain_event\n"
        b'data: {"event":"block.delta","event_id":"eid-1",'
        b'"chunk_index":0,"chunk_count":1,"payload":{"text":"hello"}}\n'
        b"id: inst:9\n\n"
    )
    assert hashlib.sha256(wire).hexdigest() == (
        "074b30013f402c34de364d5247cb89e14760053ab730096c27ba2f75b6c39aed"
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
        max_frame_bytes=frames.MIN_FRAME_BYTES,
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


def test_byte_size_matches_the_bytes_actually_written() -> None:
    prepared = _domain(text="z" * 4096, max_frame_bytes=1024).with_checkpoint(
        4, "inst"
    )
    assert prepared.byte_size == len(prepared.wire_bytes())


def test_adding_the_checkpoint_shares_the_frames_and_stays_constant_cost() -> None:
    prepared = _domain(text="q" * 4096, max_frame_bytes=1024)
    identified = prepared.with_checkpoint(5, "inst")
    # The commit step must not copy the payload: the prepared frames are
    # shared by reference and only the small id line is added.
    assert identified.frames is prepared.frames
    assert identified.seq == 5


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
