"""Bounded ingress and authoritative patch contracts."""

from __future__ import annotations

import pytest

from agent_alfred.evals.deterministic._web_broker_test_helpers import (
    Harness,
    drain_dispatcher,
    runtime_snapshot,
)
from agent_alfred.evals.deterministic._web_runtime_test_helpers import call_within
from agent_alfred.events import RunStarted
from agent_alfred.gateway.web import frames
from agent_alfred.gateway.web.frames import PreparedFrames

# --- a state patch rides the bounded ingress -------------------------------
#
# ADR-0025: the server updates the authoritative snapshot first and delivers
# the patch second; delivery that cannot be made reliably disconnects, so the
# client reconnects and takes an atomic snapshot. A patch that silently
# failed to arrive would leave a browser showing a lifetime state nothing
# will ever correct -- which is worse than a dropped transient by exactly the
# distance between "wrong for a moment" and "wrong until reload".


def _primed_patches(handle) -> list[dict]:
    """Every state patch already queued for a connection, decoded.

    The opening stream travels on the handle -- a running writer would
    write it before the queue -- so both sources are drained: startup
    first, then whatever arrived after it.
    """
    import json

    out: list[dict] = []
    items = []
    if handle.writer is not None:
        handle.writer.deliver_startup(items.append)
    while True:
        try:
            items.append(handle.queue.take(timeout=0))
        except Exception:  # noqa: BLE001 - queue.Empty, and nothing else
            break
    for item in items:
        if not isinstance(item, PreparedFrames):
            continue
        for frame in item.frames:
            if b"event: state_patch" in frame:
                raw = frame.split(b"data: ", 1)[1]
                out.append(json.loads(raw.decode("utf-8")))
    return out


def _fill_ingress(harness, count: int) -> None:
    """Put ``count`` replayable events in the ingress and leave them there.

    No dispatcher thread is running, so nothing drains it: the queue's
    fullness is a fact, not a race.
    """
    harness.emit_many(count)


def test_an_undeliverable_patch_still_moves_the_authoritative_snapshot() -> None:
    """Authority first, delivery second -- even when delivery fails."""
    harness = Harness(ingress_budget=frames.FrameBudget(2, 1 << 20))
    handle = harness.connect(session_id="s1")
    _fill_ingress(harness, 2)
    assert harness.broker._ingress.current_cost.frames == 2
    snapshot = runtime_snapshot(
        state_revision=7,
        coordinator_state="running",
        active_run=None,
    )
    returned, accepted = call_within(
        lambda: harness.broker.publish_state_patch(snapshot), seconds=5.0
    )
    # The publishing thread came straight back: no blocking on a full queue
    # and no waiting for a dispatcher that is not running.
    assert returned is True
    assert accepted is False
    # The authoritative snapshot moved anyway, so a reconnect cannot be
    # answered with the state the patch was trying to correct.
    assert harness.broker._latest.state_revision == 7
    # Nothing was silently dropped and nothing was queued behind the limit.
    assert harness.broker._ingress.current_cost.frames == 2
    # The one live connection is told to hang up and come back for a
    # snapshot, rather than left showing a state it will never be corrected
    # on. The publisher only raised the disconnect generation; the closing
    # is the dispatcher's work, outside the publish path.
    drain_dispatcher(harness)
    assert handle.queue.close_requested is True


def test_a_reconnect_after_a_failed_patch_gets_the_new_revision() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    harness.connect(session_id="s1")
    _fill_ingress(harness, 1)
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=11))
    # A client reconnecting now is primed from ``_latest``, so the revision
    # the failed patch carried is the first thing it sees.
    reconnect = harness.connect(session_id="s1")
    patches = _primed_patches(reconnect)
    assert patches
    assert patches[0]["state_revision"] == 11


def test_the_frame_budget_alone_can_refuse_a_patch() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(1, 1 << 20))
    handle = harness.connect(session_id="s1")
    _fill_ingress(harness, 1)
    assert harness.broker._ingress.current_cost.frames == 1
    assert (
        harness.broker._ingress.current_cost.encoded_bytes
        < harness.broker._ingress.budget.encoded_bytes
    )
    assert (
        harness.broker.publish_state_patch(runtime_snapshot(state_revision=3)) is False
    )
    drain_dispatcher(harness)
    assert handle.queue.close_requested is True


def test_the_byte_budget_alone_can_refuse_a_patch() -> None:
    harness = Harness(ingress_budget=frames.FrameBudget(4096, 1))
    handle = harness.connect(session_id="s1")
    # One event already blows the byte budget, so the patch is refused on
    # bytes while the frame count is nowhere near its limit.
    _fill_ingress(harness, 1)
    assert harness.broker._ingress.current_cost.frames == 0
    assert (
        harness.broker.publish_state_patch(runtime_snapshot(state_revision=3)) is False
    )
    drain_dispatcher(harness)
    assert handle.queue.close_requested is True


def test_measuring_a_patch_never_refuses_to_measure() -> None:
    """The cost probe is total; the frame limit is the writer's problem.

    ``publish_state_patch`` runs inside the Host's state transition --
    the authoritative snapshot has already moved when it is called -- so a
    probe that could raise would leave the state machine half-finished.
    Measuring has to be bounded and total; refusing to build an
    over-limit frame belongs to the thread that writes, not the one that
    decides.
    """
    small = {"state_revision": 1}
    assert frames.payload_cost(frames.STATE_PATCH, small) == (
        frames.state_patch_frames(small).byte_size
    )
    huge = {"blob": "x" * (frames.MAX_FRAME_BYTES + 1)}
    # The frame itself refuses: an oversized frame is never written
    # silently.
    with pytest.raises(ValueError):
        frames.state_patch_frames(huge)
    # The measurement does not: it just says what the thing costs.
    assert frames.payload_cost(frames.STATE_PATCH, huge) > frames.MAX_FRAME_BYTES


def test_a_patch_that_fits_keeps_its_place_in_the_event_order() -> None:
    """Patches ride the ingress so they cannot overtake what they describe."""
    harness = Harness()
    handle = harness.connect(session_id="s1")
    _primed_patches(handle)  # drop the priming sequence from the queue
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.broker.publish_state_patch(runtime_snapshot(state_revision=5))
    harness.emit(RunStarted(purpose="chat"), run_id="r2")
    harness.deliver(3)
    items: list[str] = []
    while True:
        try:
            items.append(_kind(handle.queue.take(timeout=0)))
        except Exception:  # noqa: BLE001 - queue.Empty, and nothing else
            break
    assert items == ["domain_event", "state_patch", "domain_event"]


def _kind(item) -> str:
    if isinstance(item, PreparedFrames):
        if b"event: state_patch" in item.frames[0]:
            return "state_patch"
        return "domain_event"
    return type(item).__name__


def test_the_client_merge_rules_still_hold() -> None:
    """Duplicate, rewinding, cross-instance and pending-over-terminal.

    The patch's route to the client changed; the rules by which the client
    folds it into what it already has did not, so all four are re-checked
    here rather than left to a file that does not know about ingress.
    """
    from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
        refused,
        snapshot_patch,
    )

    current = snapshot_patch(revision=5)
    assert refused(snapshot_patch(revision=4), current) == "revision_regression"
    assert refused(snapshot_patch(revision=6, instance="other"), current) == (
        "instance_mismatch"
    )
    assert refused(snapshot_patch(revision=6, pending=True), current) == (
        "pending_over_terminal"
    )
    # A repeated revision is refused, not idempotently folded in: each
    # revision names exactly one published state.
    assert refused(snapshot_patch(revision=5), current) == "revision_duplicate"
