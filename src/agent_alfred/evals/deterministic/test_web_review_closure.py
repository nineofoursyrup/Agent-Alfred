"""Issue #28 review closure: seven regressions the independent review found.

Each section corresponds to one finding and carries the tests that fail on
the #28 implementation (``1ff56df``) and pass afterwards. They are kept in
one file because they are one closure: seven independent proofs that the
skeleton's sharpest edges -- the loopback boundary, the mutation gate, the
start-up order, the bounded ingress, and the replay ring's two budgets --
are enforced rather than documented.
"""

from __future__ import annotations

import errno
import inspect
import io
import socket
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_web_broker import (
    Harness,
    _cursor_for,
    _drain,
    _ids,
    _snapshot,
)
from agent_alfred.evals.deterministic.test_web_runtime import (
    _api,
    _FailFinalizeWhen,
    _host,
    _SelectiveLatch,
    _wait_until,
)
from agent_alfred.events import RunStarted
from agent_alfred.gateway.web import frames, replay
from agent_alfred.gateway.web.api import DashboardApi, MutationGate
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_HOST,
    LOCK_NAME,
    DashboardService,
    PortUnavailable,
    ProcessLock,
    StateDirLocked,
    read_entry_descriptor,
    write_entry_descriptor,
)
from agent_alfred.gateway.web.replay import (
    ReplayRing,
    classify_cursor,
)
from agent_alfred.gateway.web.server import DashboardRuntime, HostFacade
from agent_alfred.gateway.web.state import apply_state_patch
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.snapshot import ActiveRunSummary
from agent_alfred.runtime.work import SubmitRequest
from agent_alfred.settings import Settings
from agent_alfred.wiring import build_dashboard

# --- one: the loopback boundary -------------------------------------------
#
# ADR-0014 puts "only listen on the loopback address" first in a list of four
# independent defences. It is the only one that shrinks the attack surface,
# and it is worthless if a caller can name another address.


def _service(tmp_path, *, port=17717, server_factory=None, **kwargs):
    return DashboardService(
        state_dir=tmp_path,
        handler=object(),
        instance_id="inst",
        port=port,
        server_factory=server_factory,
        **kwargs,
    )


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "localhost", "127.0.0.2", "192.168.1.10", "::1", "[::1]", ""],
)
def test_no_non_loopback_address_can_be_expressed_at_all(tmp_path, address) -> None:
    """The parameter is gone, not defaulted.

    A ``host`` knob that rejected everything but one value would still be a
    knob: it would invite the next change to widen it, and a caller reading
    the signature would reasonably assume the address is theirs to choose.
    There is no such parameter, so none of these values is reachable --
    including the empty one, which a socket would happily read as "all
    interfaces".
    """
    with pytest.raises(TypeError):
        _service(tmp_path, host=address)
    with pytest.raises(TypeError):
        _service(tmp_path, bind_host=address)
    with pytest.raises(TypeError):
        _service(tmp_path, address=address)


def test_the_decided_loopback_address_is_the_one_that_is_bound(tmp_path) -> None:
    service = _service(tmp_path, port=_free_port())
    assert service.bind_address == DEFAULT_HOST == "127.0.0.1"
    try:
        service.start()
        assert service.server.server_address[0] == "127.0.0.1"
    finally:
        service.close()


def test_an_injected_factory_is_given_the_loopback_and_nothing_else(tmp_path) -> None:
    """The test seam is not a way around the boundary.

    The factory is the seam every test uses to avoid a real socket, which
    makes it the one place a non-loopback address could have been smuggled
    in. It is handed the address rather than asked for one, so it cannot.
    """
    seen: list[tuple] = []

    def factory(address, handler):
        seen.append(address)
        return _FakeServer()

    service = _service(tmp_path, port=17717, server_factory=factory)
    try:
        service.start()
    finally:
        service.close()
    assert seen == [("127.0.0.1", 17717)]


def test_the_assembly_seams_cannot_name_an_address() -> None:
    for target in (build_dashboard, DashboardService.__init__):
        names = set(inspect.signature(target).parameters)
        assert not names & {"bind_host", "host", "bind_address", "address"}


def test_no_cli_flag_can_name_an_address() -> None:
    """Only the port is overridable from the command line (ADR-0014)."""
    from agent_alfred.gateway.cli import build_parser

    options = [
        option
        for action in build_parser()._actions
        for option in action.option_strings
    ]
    assert "--port" in options
    assert not [o for o in options if "host" in o or "bind" in o or "addr" in o]


class _FakeServer:
    """The least a server has to be for the lifecycle to own it."""

    daemon_threads = True
    block_on_close = False

    def __init__(self) -> None:
        self.server_address = ("127.0.0.1", 17717)

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        return None


# --- two: the mutation gate covers the whole admission lease --------------
#
# #23 §4: "locking chat alone and letting every other write through does not
# deliver serialisation." A lock held for the microseconds of one function
# call is not that gate: the lease a Run holds runs from ``accepted`` until
# its recording settles (ADR-0026), and every other write has to answer to
# the same authority for the same span.


def _session_rows(conn) -> list[str]:
    return [row[0] for row in conn.execute("SELECT session_id FROM sessions")]


def _start_run(host, *, message="hi"):
    session_id = host.create_session()
    result = host.submit(
        SubmitRequest(message=message, session_id=session_id, gateway="web")
    )
    assert result.kind == "accepted"
    return result, session_id


def _call_within(callable_, *, seconds: float) -> tuple[bool, object]:
    """Run ``callable_`` on a thread and wait a bounded time for it.

    The bound is the assertion: a gate that queued the write would still be
    sitting there when it expires, which is exactly the behaviour ADR-0016
    refuses to have.
    """
    box: dict = {}

    def run() -> None:
        box["value"] = callable_()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return (not thread.is_alive()), box.get("value")


def test_a_session_write_while_a_run_is_running_is_refused_at_once() -> None:
    gate = threading.Event()
    host, conn = _host(["pong"], gate=gate)
    host.start()
    try:
        result, _session_id = _start_run(host)
        _wait_until(lambda: host.snapshot().coordinator_state == "running")
        before = _session_rows(conn)
        returned, created = _call_within(_api(host).create_session, seconds=5.0)
        # Returned rather than waited: the Run is not going anywhere until
        # the gate opens, and it never does in this test.
        assert returned is True
        assert created.session_id is None
        assert created.code == "mutation_in_flight"
        assert created.status == 409
        # Nothing was written, and nothing was queued for later.
        assert _session_rows(conn) == before
        gate.set()
        host.wait(result.run_id)
    finally:
        host.close()


def test_a_session_write_while_the_recording_is_pending_is_refused() -> None:
    """``recording_pending`` still holds the lease (ADR-0026).

    The reply exists and the Run has been handed back; the one thing it is
    still holding -- the single unrecorded terminal projection -- is exactly
    what a second write would overwrite.
    """
    latch = _SelectiveLatch()
    latch.arm()
    host, conn = _host(["pong"], before_recording_commit=latch)
    host.start()
    try:
        result, _session_id = _start_run(host)
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        before = _session_rows(conn)
        returned, created = _call_within(_api(host).create_session, seconds=5.0)
        assert returned is True
        assert created.session_id is None
        assert created.code == "mutation_in_flight"
        assert created.status == 409
        assert _session_rows(conn) == before
        latch.release()
        host.wait(result.run_id)
    finally:
        host.close()


def test_a_session_write_is_accepted_once_the_lease_is_released() -> None:
    latch = _SelectiveLatch()
    latch.arm()
    host, conn = _host(["pong"], before_recording_commit=latch)
    host.start()
    try:
        result, _session_id = _start_run(host)
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        latch.release()
        host.wait(result.run_id)
        _wait_until(lambda: host.snapshot().coordinator_state == "idle")
        before = _session_rows(conn)
        created = _api(host).create_session()
        assert created.session_id is not None
        assert created.status == 201
        assert len(_session_rows(conn)) == len(before) + 1
    finally:
        host.close()


def test_a_session_write_is_refused_after_recording_failed() -> None:
    """Admission stays closed until recovery or restart (ADR-0026)."""
    flag = {"armed": False}
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    wrapped = _FailFinalizeWhen(database, flag, "finished_at")
    latch = _SelectiveLatch()
    host, _conn = _host(["pong"], conn=wrapped, before_recording_commit=latch)
    host.start()
    try:
        session_id = host.create_session()
        api = _api(host)
        latch.arm()
        first = api.submit({"message": "a-q", "session_id": session_id})
        assert first.status == 202
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        flag["armed"] = True
        latch.release()
        host.wait(first.run_id)
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_failed"
        )
        # A Run is still refused as a Run would be, and a plain write is
        # refused too: the lease never came back.
        failed = api.submit({"message": "again", "session_id": session_id})
        assert failed.status == 503
        assert failed.code == "recording_unavailable"
        # And a plain write is refused with the *same* word, not with
        # "busy": admission is closed until the process restarts (ADR-0026),
        # so telling the client to try again shortly would be a lie.
        created = api.create_session()
        assert created.session_id is None
        assert created.code == "recording_unavailable"
        assert created.status == 409
    finally:
        host.close()


def test_two_mutations_arriving_together_admit_exactly_one() -> None:
    """The gate's judgement is the Host's, so it is one judgement.

    Two writes arriving at the same microsecond cannot both see "free":
    whatever decides is the same lock that decides admission, so the second
    one is told no instead of being let through behind the first.
    """
    host, conn = _host(["pong"])
    host.start()
    try:
        entered = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        inner = HostFacade(host)

        class SlowFacade:
            def __init__(self) -> None:
                self._inner = inner

            def create_session(self) -> str:
                calls.append(1)
                entered.set()
                assert release.wait(5.0), "the test never released the write"
                return self._inner.create_session()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        slow = SlowFacade()
        gate = MutationGate(slow)
        first: dict = {}

        def run_first() -> None:
            first["value"] = gate.create_session()

        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        assert entered.wait(5.0), "the first write never entered"
        # The first write is still inside the door when the second arrives.
        returned, second = _call_within(gate.create_session, seconds=5.0)
        assert returned is True
        assert second == (None, "mutation_in_flight")
        release.set()
        thread.join(5.0)
        assert not thread.is_alive()
        assert len(calls) == 1
        # The first write got through and was given no refusal code.
        assert first["value"][0] is not None
        assert first["value"][1] is None
        # The lease is free again, so the refused write may now succeed.
        assert gate.create_session()[0] is not None
    finally:
        host.close()


def test_a_run_is_refused_while_a_plain_mutation_is_in_flight() -> None:
    """The same authority, the other direction: no queueing either way."""
    host, _conn = _host(["pong"])
    host.start()
    try:
        entered = threading.Event()
        release = threading.Event()
        inner = HostFacade(host)

        class SlowFacade:
            def __init__(self) -> None:
                self._inner = inner

            def create_session(self) -> str:
                entered.set()
                assert release.wait(5.0), "the test never released the write"
                return self._inner.create_session()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        api = DashboardApi(facade=SlowFacade())
        thread = threading.Thread(
            target=api.create_session, daemon=True
        )
        thread.start()
        assert entered.wait(5.0), "the write never entered"
        outcome = api.submit({"message": "hi"})
        # A conflict, not a queue and not a 503: nothing is unavailable,
        # something is merely busy (ADR-0016).
        assert outcome.status == 409
        assert outcome.code == "mutation_in_flight"
        assert outcome.run_id is None
        release.set()
        thread.join(5.0)
    finally:
        host.close()


def test_the_three_run_contracts_survive_the_gate() -> None:
    """202 / 409 run_in_progress / 503 recording_unavailable, unchanged."""
    gate = threading.Event()
    host, _conn = _host(["pong"], gate=gate)
    host.start()
    try:
        result, session_id = _start_run(host)
        assert result.kind == "accepted"
        api = _api(host)
        second = api.submit({"message": "second"})
        assert second.status == 409
        assert second.code == "run_in_progress"
        assert second.busy is not None
        # The same card the prevention path renders, from the same snapshot.
        assert second.busy.purpose == "chat"
        gate.set()
        host.wait(result.run_id)
        assert result.run_id is not None
        assert session_id is not None
    finally:
        host.close()


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    try:
        return int(probe.getsockname()[1])
    finally:
        probe.close()


# --- six and seven: the ring's two budgets and its checkpoints -------------
#
# The ring is the only replay source for an active Run, so two things about
# it have to be true: it may never issue a checkpoint it cannot honour, and
# its budget has to be counted in the unit the decision named -- physical
# frames, not logical events.


def _frames_in(ring) -> int:
    return sum(len(entry.frames) for entry in ring._entries)


def _entry(seq: int, size: int = 8, chunks: int = 1) -> PreparedFrames:
    """One logical event: ``chunks`` physical frames of ``size`` bytes."""
    head = b"data: " + b"x" * size
    return frames.measured_frames(
        seq=seq,
        frames=tuple(head for _ in range(chunks)),
        id_line=b"id: inst:%d\n" % seq,
        replayable=True,
    )


def test_a_forged_cursor_on_an_unrecoverable_event_is_a_gap() -> None:
    """An event the ring cannot produce is not a checkpoint.

    A single event over the byte budget is published and then dropped: the
    floor and the high-water mark both move past it, so a client that forged
    ``instance:seq`` for it was told "valid" -- letting it skip the one fact
    it was owed a gap notice for.
    """
    ring = ReplayRing(max_frames=100, max_bytes=1 << 10)
    ring.append(_entry(1))
    result = ring.append(_entry(2, size=(1 << 10) + 1))
    assert result.accepted is False
    assert result.ring_cleared is True
    assert ring.replay_floor_seq() == 2
    assert ring.high_water_seq() == 2
    # The boundary moved, but no checkpoint was issued for it.
    assert ring.classify_seq(2) != "valid"
    # Nor for anything before it: the ring was cleared, so every earlier seq
    # is now below the floor and unusable for an exact catch-up.
    assert ring.latest_complete_seq() is None
    assert ring.entries_after(1) is None
    forged = classify_cursor(
        replay.format_cursor("inst", 2), ring, process_instance_id="inst"
    )
    assert forged.kind == "gap"
    # A first connection is still given a boundary to stand on -- the one
    # this process issued before the loss. It is not replayable and the ring
    # will call it ``too_old`` when it comes back, which is the point: a
    # client holding nothing at all sends no ``Last-Event-ID``, and that is
    # the one shape that never gets a gap notice.
    first = classify_cursor(None, ring, process_instance_id="inst")
    assert first.reseed_seq == 1
    assert ring.reseed_boundary_seq() == 1
    assert ring.classify_seq(1) == "too_old"


def test_an_unrecoverable_seq_never_appears_in_an_id_line() -> None:
    """A checkpoint the ring cannot honour is not issued at all."""
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=64))
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # seq 1, too big
    harness.emit(RunStarted(purpose="chat"), run_id="r1")  # seq 2, fits
    ring = harness.broker._ring
    assert ring.high_water_seq() == 2
    # Nothing in the ring carries the unrecoverable event's id.
    assert all(entry.seq != 1 for entry in ring._entries)
    handle = harness.connect(cursor=_cursor_for(2))
    assert [seq for seq in _ids(_drain(handle)) if seq == 1] == []


def test_an_evicted_floor_is_still_a_usable_checkpoint() -> None:
    """Normal eviction and unrecoverable loss are different facts."""
    ring = ReplayRing(max_frames=2, max_bytes=1 << 20)
    for seq in (1, 2, 3, 4):
        ring.append(_entry(seq))
    assert ring.replay_floor_seq() == 2
    # 2 was issued and then evicted; everything past it is still held, so
    # resuming from it loses nothing.
    assert ring.classify_seq(2) == "valid"
    assert [entry.seq for entry in ring.entries_after(2) or ()] == [3, 4]
    # An unrecoverable loss is not the same: the boundary moves but the seq
    # was never issued, so it cannot be resumed from.
    ring.append(_entry(5, size=(1 << 20) + 1))
    assert ring.replay_floor_seq() == 5
    assert ring.classify_seq(5) != "valid"


def test_cursors_around_an_unrecoverable_event_leave_no_silent_hole() -> None:
    ring = ReplayRing(max_frames=100, max_bytes=1 << 10)
    ring.append(_entry(1))
    ring.append(_entry(2, size=(1 << 10) + 1))  # unrecoverable
    ring.append(_entry(3))
    # The cursor before it is now too old: the ring cannot prove continuity
    # across the lost event, and says so rather than resuming silently.
    assert ring.classify_seq(1) == "too_old"
    assert ring.entries_after(1) is None
    # The cursor after it is a real checkpoint and loses nothing.
    assert ring.classify_seq(3) == "valid"
    assert ring.entries_after(3) == ()
    # The unrecoverable seq itself is inside the range and still refused --
    # not valid, and not silently resumable.
    assert ring.classify_seq(2) == "malformed"
    # The four reasons stay closed.
    assert ring.classify_seq(9) == "ahead"


def test_a_run_spanning_an_unrecoverable_event_is_unrecoverable() -> None:
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=64))
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.emit(RunStarted(purpose="chat"), run_id="r1")
    harness.broker._run_start_seq["r1"] = 1
    harness.broker.publish_state_patch(
        _snapshot(
            coordinator_state="running",
            active_run=ActiveRunSummary(
                run_id="r1",
                purpose="chat",
                gateway="web",
                phase="running",
                session_id="s1",
                prompt_preview="hi",
                started_at=None,
                recording_state=None,
            ),
        )
    )
    handle = harness.connect(cursor=_cursor_for(1))
    notices = [
        item
        for item in _drain(handle)
        if isinstance(item, PreparedFrames)
        and any(b"replay_gap" in frame for frame in item.frames)
    ]
    assert notices
    assert b'"current_run_state":"unrecoverable"' in notices[0].frames[0]


def test_the_frame_budget_is_counted_in_physical_frames() -> None:
    """2048 means frames, not logical events.

    A logical event may be many frames, so counting entries let a handful of
    chunked events pin far more than the budget ever meant to allow.
    """
    ring = ReplayRing(max_frames=4, max_bytes=1 << 20)
    ring.append(_entry(1, chunks=3))
    assert _frames_in(ring) == 3
    # A second chunked event would take the ring to six frames, so the
    # oldest *whole* event goes: half an event is not expressible.
    ring.append(_entry(2, chunks=3))
    assert _frames_in(ring) == 3
    assert [entry.seq for entry in ring.entries_after(1) or ()] == [2]
    assert (ring.entries_after(1) or ())[0].frames == _entry(2, chunks=3).frames


def test_the_ring_never_holds_more_frames_than_its_budget() -> None:
    ring = ReplayRing(max_frames=5, max_bytes=1 << 20)
    for seq in range(1, 12):
        ring.append(_entry(seq, chunks=2))
        assert _frames_in(ring) <= 5
    assert _frames_in(ring) <= 5


def test_one_event_with_more_frames_than_the_budget_is_not_kept() -> None:
    """Same treatment as an event over the byte budget.

    It is not stored in part, the unrecoverable boundary advances, and no
    checkpoint is issued for a boundary the ring could never reproduce.
    """
    ring = ReplayRing(max_frames=3, max_bytes=1 << 20)
    ring.append(_entry(1))
    result = ring.append(_entry(2, chunks=4))
    assert result.accepted is False
    assert result.ring_cleared is True
    assert ring.oldest_seq() is None
    assert ring.replay_floor_seq() == 2
    assert ring.high_water_seq() == 2
    assert ring.classify_seq(2) != "valid"
    # The next event is unaffected.
    ring.append(_entry(3))
    assert ring.oldest_seq() == 3


def test_the_two_budgets_are_counted_independently() -> None:
    frames_first = ReplayRing(max_frames=2, max_bytes=1 << 20)
    for seq in (1, 2, 3):
        frames_first.append(_entry(seq, chunks=1))
    assert _frames_in(frames_first) == 2  # frames ran out, bytes nowhere near

    bytes_first = ReplayRing(max_frames=100, max_bytes=40)
    for seq in (1, 2, 3):
        bytes_first.append(_entry(seq, size=20))
    assert _frames_in(bytes_first) == 1  # bytes ran out, frames nowhere near


def test_a_chunked_event_is_replayed_whole_after_breaking_midway() -> None:
    """Breaking at chunk k resends the whole event, once.

    Held against the frame budget: a chunked event that all but fills the
    ring must still be replayed whole and evicted whole, which is only true
    if the ring's unit of removal is the logical event rather than the frame.
    """
    from agent_alfred.evals.deterministic.test_web_broker import (
        _user_message_with,
    )

    big = "é" * (200 * 1024)
    harness = Harness(
        max_frame_bytes=16 * 1024,
        ring=ReplayRing(max_frames=27, max_bytes=1 << 22),
    )
    first = harness.emit(RunStarted(purpose="chat"), run_id="r1")
    chunky = harness.emit(
        RunStarted(purpose="chat", user_message=_user_message_with(big)),
        run_id="chunky",
    )
    stored = harness.broker._ring.entries_after(first.seq)
    assert len(stored) == 1
    assert len(stored[0].frames) > 1
    # One single-frame event plus one many-frame event: the budget counts
    # physical frames, so these two logical events are already at the limit.
    assert _frames_in(harness.broker._ring) == 1 + len(stored[0].frames) == 27
    # A third event pushes it over, and what goes is a whole logical event --
    # never the tail of the chunked one.
    harness.emit(RunStarted(purpose="chat"), run_id="r3")
    assert _frames_in(harness.broker._ring) <= 27
    surviving = harness.broker._ring.entries_after(first.seq) or ()
    assert chunky.seq in [entry.seq for entry in surviving]
    assert all(
        entry.seq != chunky.seq or entry.frames == stored[0].frames
        for entry in surviving
    )
    # Reconnecting from before the chunked event re-sends every chunk of it,
    # exactly once, rather than the chunks after the break.
    handle = harness.connect(cursor=_cursor_for(first.seq))
    replayed = [item for item in _drain(handle) if item.seq == chunky.seq]
    assert len(replayed) == 1
    assert replayed[0].frames == stored[0].frames


# --- five: a state patch rides the bounded ingress -------------------------
#
# ADR-0025: the server updates the authoritative snapshot first and delivers
# the patch second; delivery that cannot be made reliably disconnects, so the
# client reconnects and takes an atomic snapshot. A patch that silently
# failed to arrive would leave a browser showing a lifetime state nothing
# will ever correct -- which is worse than a dropped transient by exactly the
# distance between "wrong for a moment" and "wrong until reload".


def _primed_patches(handle) -> list[dict]:
    """Every state patch already queued for a connection, decoded."""
    import json

    out: list[dict] = []
    while True:
        try:
            item = handle.queue.take(timeout=0)
        except Exception:  # noqa: BLE001 - queue.Empty, and nothing else
            return out
        if not isinstance(item, PreparedFrames):
            continue
        for frame in item.frames:
            if b"event: state_patch" in frame:
                raw = frame.split(b"data: ", 1)[1]
                out.append(json.loads(raw.decode("utf-8")))


def _fill_ingress(harness, count: int) -> None:
    """Put ``count`` replayable events in the ingress and leave them there.

    No dispatcher thread is running, so nothing drains it: the queue's
    fullness is a fact, not a race.
    """
    harness.emit_many(count)


def test_an_undeliverable_patch_still_moves_the_authoritative_snapshot() -> None:
    """Authority first, delivery second -- even when delivery fails."""
    harness = Harness(max_ingress_frames=2, max_ingress_bytes=1 << 20)
    handle = harness.connect(session_id="s1")
    _fill_ingress(harness, 2)
    assert harness.broker._ingress._frames == 2
    snapshot = _snapshot(
        state_revision=7,
        coordinator_state="running",
        active_run=None,
    )
    returned, accepted = _call_within(
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
    assert harness.broker._ingress._frames == 2
    # The one live connection is told to hang up and come back for a
    # snapshot, rather than left showing a state it will never be corrected
    # on.
    assert handle.queue.close_requested is True


def test_a_reconnect_after_a_failed_patch_gets_the_new_revision() -> None:
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    harness.connect(session_id="s1")
    _fill_ingress(harness, 1)
    harness.broker.publish_state_patch(_snapshot(state_revision=11))
    # A client reconnecting now is primed from ``_latest``, so the revision
    # the failed patch carried is the first thing it sees.
    reconnect = harness.connect(session_id="s1")
    patches = _primed_patches(reconnect)
    assert patches
    assert patches[0]["state_revision"] == 11


def test_the_frame_budget_alone_can_refuse_a_patch() -> None:
    harness = Harness(max_ingress_frames=1, max_ingress_bytes=1 << 20)
    handle = harness.connect(session_id="s1")
    _fill_ingress(harness, 1)
    assert harness.broker._ingress._frames == 1
    assert harness.broker._ingress._bytes < harness.broker._ingress.max_bytes
    assert harness.broker.publish_state_patch(_snapshot(state_revision=3)) is False
    assert handle.queue.close_requested is True


def test_the_byte_budget_alone_can_refuse_a_patch() -> None:
    harness = Harness(max_ingress_frames=4096, max_ingress_bytes=1)
    handle = harness.connect(session_id="s1")
    # One event already blows the byte budget, so the patch is refused on
    # bytes while the frame count is nowhere near its limit.
    _fill_ingress(harness, 1)
    assert harness.broker._ingress._frames == 0
    assert harness.broker.publish_state_patch(_snapshot(state_revision=3)) is False
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
    harness.broker.publish_state_patch(_snapshot(state_revision=5))
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
    from agent_alfred.evals.deterministic.test_web_runtime import (
        _patch,
        refused,
    )

    current = _patch(revision=5)
    assert refused(_patch(revision=4), current) == "revision_regression"
    assert refused(_patch(revision=6, instance="other"), current) == (
        "instance_mismatch"
    )
    assert refused(_patch(revision=6, pending=True), current) == (
        "pending_over_terminal"
    )
    # A repeat is idempotent: no movement, no error.
    assert apply_state_patch(current, _patch(revision=5)) == current


# --- three and four: one process, one Host, one ordered start-up ----------
#
# #23 §1 and §2: the default command starts HTTP on a daemon thread and then
# enters the CLI, on the same Host; and the state-directory lock is the first
# process-level ownership taken, before anything is written or bound. They
# are one finding here because the second is what makes the first safe: a CLI
# that opened the database before it owned the state directory would migrate
# and recover another instance's data on its way to being refused.


class _RecordingLock(ProcessLock):
    """A real flock that writes down when it was taken and when it went."""

    def __init__(self, path: Path, log: list[str]) -> None:
        super().__init__(path)
        self._log = log

    def acquire(self) -> None:
        self._log.append("lock")
        super().acquire()

    def release(self) -> None:
        self._log.append("unlock")
        super().release()


class _FakeHost:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.process_instance_id = "inst-steps"
        self.started = False
        self.closed = False

    def start(self) -> None:
        self._log.append("host.start")
        self.started = True

    def close(self, timeout: float | None = None) -> bool:
        self._log.append("host.close")
        self.closed = True
        return True

    def create_session(self) -> str:
        return "session-steps"


class _FakeBroker:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def start(self) -> None:
        self._log.append("broker.start")

    def close(self, timeout: float = 0.0) -> bool:
        self._log.append("broker.close")
        return True


class _FakeBoundServer:
    daemon_threads = True
    block_on_close = False

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.server_address = ("127.0.0.1", 17717)
        self.closed = False
        self.context = None

    def serve_forever(self) -> None:
        self._log.append("serve")

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self._log.append("socket.close")
        self.closed = True


def _step_runtime(tmp_path, *, log, bind_error=None, describe_error=None):
    """A runtime whose every process-level step is a line in ``log``."""

    def server_factory(address, handler):
        log.append("bind")
        if bind_error is not None:
            raise bind_error
        return _FakeBoundServer(log)

    def write_descriptor(directory, descriptor):
        log.append("describe")
        if describe_error is not None:
            raise describe_error
        return write_entry_descriptor(directory, descriptor)

    def open_database(directory):
        log.append("database")
        return sqlite3.connect(":memory:", check_same_thread=False)

    def assemble(conn, instance_id):
        log.append("assemble")
        return _FakeHost(log), _FakeBroker(log)

    return DashboardRuntime(
        state_dir=tmp_path,
        assemble=assemble,
        port=17717,
        instance_id="inst-steps",
        open_database=open_database,
        server_factory=server_factory,
        write_descriptor=write_descriptor,
        lock=_RecordingLock(tmp_path / LOCK_NAME, log),
    )


def _file_database(directory):
    """The production database seam, so the state directory is exercised."""
    from agent_alfred.wiring import open_database

    return open_database(directory)


def _scripted_factory(script=None):
    return ScriptedModelFactory(ScriptedModel(script or ["pong"]))


def test_a_lock_conflict_touches_nothing_at_all(tmp_path) -> None:
    """The lock is the first process-level ownership, or there is no start.

    A second instance that migrated the database, recovered a Run or wrote
    Run state before being refused would have altered the first instance's
    facts on its way out of the door -- which is the thing the lock exists
    to prevent, and it only holds if nothing precedes it.
    """
    holder = ProcessLock(tmp_path / LOCK_NAME)
    holder.acquire()
    log: list[str] = []
    runtime = _step_runtime(tmp_path, log=log)
    try:
        with pytest.raises(StateDirLocked):
            runtime.start()
    finally:
        holder.release()
    assert log == ["lock"]


def test_a_failed_bind_releases_the_lock_and_never_starts_the_host(tmp_path) -> None:
    log: list[str] = []
    runtime = _step_runtime(
        tmp_path, log=log, bind_error=OSError(errno.EADDRINUSE, "in use")
    )
    with pytest.raises(PortUnavailable):
        runtime.start()
    # Unwound completely: no descriptor, no database, no Host, lock back.
    assert log == ["lock", "bind", "unlock"]
    assert read_entry_descriptor(tmp_path) is None
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_a_failed_descriptor_closes_the_socket_and_releases_the_lock(
    tmp_path,
) -> None:
    log: list[str] = []
    runtime = _step_runtime(tmp_path, log=log, describe_error=OSError("disk full"))
    with pytest.raises(OSError):
        runtime.start()
    assert "socket.close" in log
    assert log[-1] == "unlock"
    assert "database" not in log
    assert "host.start" not in log
    assert read_entry_descriptor(tmp_path) is None
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_the_successful_path_runs_the_steps_in_one_order(tmp_path) -> None:
    log: list[str] = []
    runtime = _step_runtime(tmp_path, log=log)
    descriptor = runtime.start()
    try:
        assert log == [
            "lock",
            "bind",
            "describe",
            "database",
            "assemble",
            "broker.start",
            "host.start",
            "serve",
        ]
        assert descriptor.port == 17717
        assert read_entry_descriptor(tmp_path) == descriptor
    finally:
        started = len(log)
        runtime.close()
    # Closed in reverse and completely: the Host before the stream, the
    # socket and descriptor before the lock.
    undone = log[started:]
    assert "socket.close" in undone
    assert undone.index("host.close") < undone.index("broker.close")
    assert undone.index("broker.close") < undone.index("unlock")
    assert read_entry_descriptor(tmp_path) is None
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_a_second_instance_cannot_touch_the_first_instances_database(
    tmp_path,
) -> None:
    """Refused before it can migrate. Proven on a real database file."""
    first = build_dashboard(
        state_dir=tmp_path,
        factory=_scripted_factory(),
        clock=FakeClock(),
        port=_free_port(),
        open_database=_file_database,
    )
    first.start()
    try:
        before = (tmp_path / "db.sqlite3").read_bytes()
        log: list[str] = []
        second = _step_runtime(tmp_path, log=log)
        with pytest.raises(StateDirLocked):
            second.start()
        # Not one step: no bind, no database, no recovery, no Host.
        assert log == ["lock"]
        assert (tmp_path / "db.sqlite3").read_bytes() == before
        # The first instance is untouched: still described, still serving.
        assert read_entry_descriptor(tmp_path) is not None
    finally:
        first.close()


def test_the_cli_and_serve_paths_share_one_start_up_order(
    tmp_path, monkeypatch
) -> None:
    """One order, two surfaces: neither rediscovers it for itself."""
    from agent_alfred.gateway import cli as cli_module

    logs: dict[str, list[str]] = {}

    def build(**kwargs):
        assert "bind_host" not in kwargs
        log: list[str] = []
        runtime = _step_runtime(kwargs["state_dir"], log=log)
        logs["last"] = log
        return runtime

    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: (_ for _ in ()).throw(EOFError())
    )
    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            port=17717,
            out=io.StringIO(),
            stop=stop,
            build=build,
        )
        == 0
    )
    serve_log = list(logs["last"])
    # No ``-m``: the REPL's first read raises EOFError, which is how a
    # terminal exits. The order is what is under test, not the chat.
    assert (
        cli_module.main(
            ["--state-dir", str(tmp_path), "--port", "17717"],
            build=build,
        )
        == 0
    )
    cli_log = list(logs["last"])
    # Both took the lock first, bound second, described third, and only then
    # opened the database and built the Host.
    assert serve_log[:3] == ["lock", "bind", "describe"]
    assert cli_log[:3] == ["lock", "bind", "describe"]


# --- three: the CLI hosts the Dashboard -----------------------------------


class _CapturingRuntime:
    """Remembers what ``main`` built, after ``main`` has closed it."""

    def __init__(self, inner):
        self._inner = inner
        self.descriptor = None
        self.host = None
        self.broker = None
        self.closed = False

    def start(self):
        descriptor = self._inner.start()
        self.descriptor = descriptor
        self.host = self._inner.host
        self.broker = self._inner.broker
        return descriptor

    def close(self, *args, **kwargs):
        self.closed = True
        return self._inner.close(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _cli_build(**kwargs):
    """The ``main`` seam: a real Dashboard, wrapped so a test can see it."""
    return _CapturingRuntime(
        build_dashboard(**kwargs, open_database=_file_database)
    )


def test_the_default_cli_starts_the_dashboard(tmp_path, capsys) -> None:
    """#23 §1: the default command is not the REPL alone."""
    from agent_alfred.gateway import cli as cli_module

    port = _free_port()
    code = cli_module.main(
        ["--state-dir", str(tmp_path), "--port", str(port), "-m", "hi"],
        factory=_scripted_factory(),
        build=_cli_build,
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert f"dashboard on 127.0.0.1:{port}" in printed
    assert "pong" in printed


def test_cli_events_reach_the_same_broker(tmp_path) -> None:
    """One Broker, so the CLI's Run is observable in a browser at all.

    Its transient events are never written to the database, which is why a
    second process tailing the trace could never show them.
    """
    from agent_alfred.gateway import cli as cli_module

    captured: list[_CapturingRuntime] = []

    def build(**kwargs):
        runtime = _cli_build(**kwargs)
        captured.append(runtime)
        return runtime

    port = _free_port()
    assert (
        cli_module.main(
            ["--state-dir", str(tmp_path), "--port", str(port), "-m", "hi"],
            factory=_scripted_factory(),
            build=build,
        )
        == 0
    )
    runtime = captured[0]
    assert runtime.broker is not None
    # The CLI's Run went through the FanOut the browser reads.
    assert runtime.broker._ring.high_water_seq() >= 2
    assert runtime.broker._latest.state_revision >= 1


def test_the_cli_and_the_web_compete_for_one_coordinator(tmp_path) -> None:
    """Both surfaces call the same ``submit``; neither has its own Run."""
    gate = threading.Event()
    runtime = build_dashboard(
        state_dir=tmp_path,
        factory=ScriptedModelFactory(ScriptedModel(["pong"], gate=gate)),
        clock=FakeClock(),
        port=_free_port(),
        open_database=_file_database,
    )
    runtime.start()
    try:
        host = runtime.host
        api = DashboardApi(facade=HostFacade(host))
        session_id = host.create_session()
        web = host.submit(
            SubmitRequest(
                message="from the web", session_id=session_id, gateway="web"
            )
        )
        assert web.kind == "accepted"
        # A web Run holds the lease, so the CLI's Run is refused -- refused,
        # not queued, and not given its own coordinator.
        cli_attempt = host.submit(
            SubmitRequest(
                message="from the cli", session_id=session_id, gateway="cli"
            )
        )
        assert cli_attempt.kind == "run_in_progress"
        # ... and the same in the other direction.
        assert api.submit({"message": "again"}).status == 409
        gate.set()
        host.wait(web.run_id)
    finally:
        runtime.close()


def test_the_cli_releases_the_socket_the_descriptor_and_the_lock(tmp_path) -> None:
    """Exiting the CLI leaves the state directory free.

    Otherwise a second terminal would be refused for as long as the first is
    open -- the one thing a single-instance lock must not do to someone who
    simply started the assistant twice.
    """
    from agent_alfred.gateway import cli as cli_module

    port = _free_port()
    assert (
        cli_module.main(
            ["--state-dir", str(tmp_path), "--port", str(port), "-m", "hi"],
            factory=_scripted_factory(),
            build=_cli_build,
        )
        == 0
    )
    assert read_entry_descriptor(tmp_path) is None
    # The port answers again, so the same port can be bound immediately.
    probe = socket.socket()
    probe.bind(("127.0.0.1", port))
    probe.close()
    # And so does the lock.
    fresh = ProcessLock(tmp_path / LOCK_NAME)
    fresh.acquire()
    fresh.release()


def test_serve_never_enters_the_repl(tmp_path, monkeypatch) -> None:
    """``--serve`` is the Dashboard and nothing else."""
    from agent_alfred.gateway import cli as cli_module

    def refuse_input(*args, **kwargs):
        raise AssertionError("--serve entered the REPL")

    monkeypatch.setattr("builtins.input", refuse_input)
    stop = threading.Event()
    stop.set()
    assert (
        cli_module.serve_dashboard(
            state_dir=tmp_path,
            settings=Settings(),
            port=_free_port(),
            out=io.StringIO(),
            stop=stop,
            build=_cli_build,
        )
        == 0
    )


def test_the_serve_flag_runs_the_serve_path(tmp_path, monkeypatch) -> None:
    """And nothing else: no REPL, and no address to choose."""
    from agent_alfred.gateway import cli as cli_module

    seen: dict = {}

    def fake_serve(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli_module, "serve_dashboard", fake_serve)
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("entered REPL")),
    )
    assert cli_module.main(["--serve", "--port", "1234"]) == 0
    assert seen["port"] == 1234
    assert not [key for key in seen if "host" in key or "bind" in key]
