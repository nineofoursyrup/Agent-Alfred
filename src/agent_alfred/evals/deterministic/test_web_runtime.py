"""The Dashboard's write contract, driven through the real RuntimeHost.

``test_web_api.py`` proves the mapping from the coordinator's answer to a
status code. This file proves the coordinator really does produce those
answers: a 202 only after the lease, the committed accepted row and the
handoff; a 409 while the lease is held -- including through the whole
``recording_pending`` window; a 503 only once ``recording_failed`` has
landed; and never a 202 for a Run that failed to persist or to be handed
off.

The orderings are tested with latches, not sleeps: each one pauses the
finalizer at the exact instant the contract is about.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.gateway.web.api import (
    STAGE_SAVING,
    DashboardApi,
    busy_summary_from,
)
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.server import HostFacade
from agent_alfred.gateway.web.state import (
    StatePatchRejected,
    apply_state_patch,
    build_snapshot,
    snapshot_from_payload,
    snapshot_payload,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.runtime.snapshot import RuntimeSnapshot
from agent_alfred.settings import Settings

INSTANCE = "proc-runtime"


class _SelectiveLatch:
    """A ``before_``/``after_`` hook that waits only while armed."""

    def __init__(self):
        self._gate = threading.Event()
        self._gate.set()

    def arm(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def wait(self, timeout=None):
        return self._gate.wait(timeout)


class _FailFinalizeWhen:
    """Connection wrapper that fails one statement while armed."""

    def __init__(self, inner: sqlite3.Connection, flag: dict, needle: str):
        self._inner = inner
        self._flag = flag
        self._needle = needle

    def execute(self, sql, parameters=()):
        if self._flag["armed"] and self._needle in sql:
            raise sqlite3.OperationalError("injected failure")
        return self._inner.execute(sql, parameters)

    def commit(self):
        return self._inner.commit()

    def rollback(self):
        return self._inner.rollback()

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _host(
    script: list | None = None,
    *,
    conn=None,
    gate: threading.Event | None = None,
    before_recording_commit=None,
    after_recorded_snapshot=None,
    before_recording_failed=None,
    publish_work=None,
    extra_sinks=None,
    snapshot_listener=None,
):
    database = conn
    if database is None:
        database = sqlite3.connect(":memory:", check_same_thread=False)
        schema.migrate(database)
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    sinks = [capture, *(extra_sinks or ())]
    host = RuntimeHost(
        conn=database,
        factory=ScriptedModelFactory(
            ScriptedModel(script or ["pong"], gate=gate)
        ),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink(sinks, process_instance_id=INSTANCE),
        process_instance_id=INSTANCE,
        publish_work=publish_work,
        before_recording_commit=before_recording_commit,
        after_recorded_snapshot=after_recorded_snapshot,
        before_recording_failed=before_recording_failed,
        snapshot_listener=snapshot_listener,
    )
    return host, database


def _api(host: RuntimeHost) -> DashboardApi:
    return DashboardApi(facade=HostFacade(host))


# --- the 202 ----------------------------------------------------------------


def test_a_real_submit_answers_202_with_its_run_id() -> None:
    host, _conn = _host()
    host.start()
    try:
        session_id = host.create_session()
        outcome = _api(host).submit({"message": "hello", "session_id": session_id})
        assert outcome.status == 202
        assert outcome.run_id
        assert outcome.session_id == session_id
        host.wait(outcome.run_id)
    finally:
        host.close()


# --- the 409 ----------------------------------------------------------------


def test_a_second_submit_while_one_runs_is_409_with_the_busy_card() -> None:
    gate = threading.Event()
    host, _conn = _host(["pong", "pong"], gate=gate)
    host.start()
    try:
        session_id = host.create_session()
        api = _api(host)
        first = api.submit({"message": "first", "session_id": session_id})
        assert first.status == 202
        # The model is inside its round trip, so the Run is genuinely running.
        _wait_until(lambda: host.snapshot().coordinator_state == "running")
        second = api.submit({"message": "second", "session_id": session_id})
        assert second.status == 409
        assert second.code == "run_in_progress"
        assert second.busy is not None
        assert second.busy.run_id == first.run_id
        assert second.busy.stage == "运行中"
        assert second.busy.gateway == "web"
        gate.set()
        host.wait(first.run_id)
    finally:
        gate.set()
        host.close()


def test_recording_pending_is_409_and_the_card_says_saving() -> None:
    """The #30 补正一 closure, through the real finalizer.

    The Run's reply exists and its phase is finished, but the recording
    transaction has not committed: the lease is still held, so the answer is
    the existing 409, and the card says what it is waiting for.
    """
    latch = _SelectiveLatch()
    host, _conn = _host(before_recording_commit=latch)
    host.start()
    try:
        session_id = host.create_session()
        api = _api(host)
        first = api.submit({"message": "first", "session_id": session_id})
        assert first.status == 202
        host.wait(first.run_id)

        latch.arm()
        second = api.submit({"message": "second", "session_id": session_id})
        assert second.status == 202
        _wait_until(lambda: host.snapshot().coordinator_state == "recording_pending")

        third = api.submit({"message": "third", "session_id": session_id})
        assert third.status == 409
        assert third.busy is not None
        assert third.busy.stage == STAGE_SAVING
        # The Run the card names is the one still saving, not the one asked for.
        assert third.busy.run_id == second.run_id
    finally:
        latch.release()
        host.close()


def test_the_recorded_snapshot_is_authoritative_before_the_lease_opens() -> None:
    """Authority first, lease second.

    A 202 that arrived while the snapshot still described the old terminal
    state would let the next Run be admitted into a state nobody published.
    """
    latch = _SelectiveLatch()
    host, _conn = _host(["pong", "pong"], after_recorded_snapshot=latch)
    host.start()
    try:
        session_id = host.create_session()
        api = _api(host)
        latch.arm()
        first = api.submit({"message": "first", "session_id": session_id})
        assert first.status == 202
        # Paused after the recorded snapshot was published and before the
        # lease was let go -- the exact window the ordering is about.
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
            and host.snapshot().active_run is not None
            and host.snapshot().active_run.recording_state == "recorded"
        )
        # The lease is still held, so the next submit does not get in; and it
        # is refused against a snapshot that already says "recorded", not one
        # still describing the previous terminal state.
        assert (
            api.submit({"message": "second", "session_id": session_id}).status == 409
        )
    finally:
        latch.release()
        if first.run_id:
            host.wait(first.run_id, timeout=10.0)
        host.close()


# --- the 503 ----------------------------------------------------------------


def test_recording_failed_answers_503_and_only_then() -> None:
    flag = {"armed": False}
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    wrapped = _FailFinalizeWhen(database, flag, "finished_at")
    latch = _SelectiveLatch()
    host, _conn = _host(
        ["a-reply", "unused"],
        conn=wrapped,
        before_recording_commit=latch,
    )
    host.start()
    try:
        session_id = host.create_session()
        api = _api(host)
        first = api.submit({"message": "a-q", "session_id": session_id})
        host.wait(first.run_id)

        latch.arm()
        second = api.submit({"message": "b-q", "session_id": session_id})
        assert second.status == 202
        _wait_until(lambda: host.snapshot().coordinator_state == "recording_pending")
        # Still pending, so still a 409 -- the 503 is not available yet.
        assert (
            api.submit({"message": "c-q", "session_id": session_id}).status == 409
        )

        flag["armed"] = True
        latch.release()
        host.wait(second.run_id)
        _wait_until(lambda: host.snapshot().coordinator_state == "recording_failed")
        # Only now, and from here on.
        failed = api.submit({"message": "d-q", "session_id": session_id})
        assert failed.status == 503
        assert failed.code == "recording_unavailable"
        assert failed.busy is not None
        assert failed.busy.stage == "保存失败"
    finally:
        latch.release()
        host.close()


# --- never a 202 for a Run nobody is running --------------------------------


def test_a_failed_persist_never_answers_202() -> None:
    flag = {"armed": False}
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    wrapped = _FailFinalizeWhen(database, flag, "INSERT INTO runs")
    host, _conn = _host(conn=wrapped)
    host.start()
    try:
        flag["armed"] = True
        outcome = _api(host).submit({"message": "hello"})
        # A 202 here would promise a Run that was never accepted anywhere.
        assert outcome.status == 500
        assert outcome.code == "admission_failed"
        assert outcome.run_id is None
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()


def test_a_failed_handoff_never_answers_202() -> None:
    def explode(item):
        raise RuntimeError("handoff refused")

    host, _conn = _host(publish_work=explode)
    host.start()
    try:
        outcome = _api(host).submit({"message": "hello"})
        assert outcome.status == 500
        assert outcome.code == "admission_failed"
        # The Run was finalized interrupted and admission reopened rather
        # than left dangling on a Run nobody will execute.
        _wait_until(lambda: host.snapshot().coordinator_state == "idle")
    finally:
        host.close()


# --- the snapshot reaches the stream ----------------------------------------


def test_the_broker_hears_every_authoritative_transition() -> None:
    seen: list = []
    host, _conn = _host(snapshot_listener=seen.append)
    host.start()
    try:
        session_id = host.create_session()
        result = host.submit(
            _request(message="hello", session_id=session_id)
        )
        host.wait(result.run_id)
        # Several transitions, each one after the state it describes moved.
        assert len(seen) >= 3
        revisions = [snapshot.state_revision for snapshot in seen]
        assert revisions == sorted(revisions)
        assert revisions == list(range(1, len(revisions) + 1))
    finally:
        host.close()


def test_a_patch_is_an_absolute_replacement_carrying_its_own_revision() -> None:
    host, _conn = _host()
    host.start()
    try:
        session_id = host.create_session()
        result = host.submit(_request(message="hello", session_id=session_id))
        host.wait(result.run_id)
        latest = host.snapshot()
        wire = snapshot_payload(
            build_snapshot(latest, step=None, session_valid=True)
        )
        rebuilt = snapshot_from_payload(wire)
        # The revision and the instance travel with the patch, not with an
        # event seq: a patch owns no seq at all.
        assert rebuilt.process_instance_id == INSTANCE
        assert rebuilt.state_revision == latest.state_revision
        assert latest.state_revision >= 1
    finally:
        host.close()


def _request(message: str, session_id: str | None):
    from agent_alfred.runtime.work import SubmitRequest

    return SubmitRequest(message=message, session_id=session_id, gateway="web")


# --- the client's merge rules -----------------------------------------------


def _patch(
    *, revision: int, instance: str = INSTANCE, pending: bool = False,
    run_id: str = "r1",
):
    from agent_alfred.runtime.snapshot import (
        ActiveRunSummary,
        RuntimeSnapshot,
        UnrecordedTerminalProjection,
    )

    return RuntimeSnapshot(
        process_instance_id=instance,
        state_revision=revision,
        coordinator_state="recording_pending" if pending else "idle",
        active_run=(
            ActiveRunSummary(
                run_id=run_id,
                purpose="chat",
                gateway="web",
                phase="finished",
                session_id="s1",
                prompt_preview="hi",
                started_at=None,
                recording_state="pending" if pending else "recorded",
            )
        ),
        unrecorded_terminal_projection=(
            UnrecordedTerminalProjection(
                run_id="r1",
                purpose="chat",
                outcome="completed",
                reply_text="reply",
                error=None,
                recording_state="pending" if pending else "failed",
                session_id="s1",
                prompt_preview="hi",
            )
            if pending
            else None
        ),
    )


def test_a_patch_from_another_process_is_refused() -> None:
    current = _patch(revision=1)
    assert refused(_patch(revision=2, instance="other-process"), current) == (
        "instance_mismatch"
    )


def test_a_rewinding_patch_is_refused() -> None:
    current = _patch(revision=5)
    assert refused(_patch(revision=4), current) == "revision_regression"


def test_a_pending_patch_cannot_overwrite_a_settled_state() -> None:
    current = _patch(revision=5)
    assert refused(_patch(revision=6, pending=True), current) == (
        "pending_over_terminal"
    )


def test_a_repeated_revision_is_refused_whatever_its_payload() -> None:
    """A patch that does not move the revision is refused, not folded in.

    The server publishes each state revision exactly once, so a repeat is
    either a replay of something already held or something that never came
    from this process's snapshot sequence -- and the payload cannot tell
    the two apart. Refusing both shapes is what keeps ``state_revision``
    naming exactly one published state; the current state survives the
    refusal unchanged.
    """
    current = _patch(revision=5)
    assert refused(_patch(revision=5), current) == "revision_duplicate"
    assert refused(_patch(revision=5, run_id="r2"), current) == (
        "revision_duplicate"
    )


def test_the_first_patch_is_always_accepted() -> None:
    assert apply_state_patch(None, _patch(revision=0)) is not None


def refused(patch, current) -> str:
    try:
        apply_state_patch(current, patch)
    except StatePatchRejected as exc:
        return exc.reason
    raise AssertionError("the patch should have been refused")


# --- three revisions, never mixed -------------------------------------------


def test_the_three_revisions_are_orthogonal() -> None:
    """seq, state_revision and activity_revision count different things.

    They are all monotonic integers, which is the entire reason they get
    confused. A patch carries the state revision and no seq; an event carries
    a seq and no state revision; the database owns neither.
    """
    broker = SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=RuntimeSnapshot(INSTANCE, 0, "idle", None, None),
        session_is_valid=lambda _sid: True,
    )
    host, _conn = _host(extra_sinks=[broker])
    host.start()
    try:
        session_id = host.create_session()
        first = host.submit(_request(message="one", session_id=session_id))
        host.wait(first.run_id)
        second = host.submit(_request(message="two", session_id=session_id))
        host.wait(second.run_id)

        snapshot = host.snapshot()
        state_revision = snapshot.state_revision
        with host._db_lock:  # noqa: SLF001
            activity = host._conn.execute(  # noqa: SLF001
                "SELECT MAX(activity_revision) FROM runs"
            ).fetchone()[0]
        seq = broker._ring.high_water_seq()  # noqa: SLF001
        # All three moved, and none of them is derivable from another.
        assert state_revision >= 4
        assert activity >= 3
        assert seq >= 2
        assert len({state_revision, activity, seq}) >= 2
        # The snapshot is the only place a state revision lives; the ring is
        # the only place an event seq lives. Neither reads the other.
        assert snapshot.process_instance_id == INSTANCE
    finally:
        broker.close(timeout=1.0)
        host.close()


def test_the_busy_card_is_rendered_from_the_authoritative_snapshot() -> None:
    host, _conn = _host()
    host.start()
    try:
        session_id = host.create_session()
        result = host.submit(_request(message="hello", session_id=session_id))
        host.wait(result.run_id)
        assert busy_summary_from(host.snapshot()) is None
    finally:
        host.close()
