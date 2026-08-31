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

import json
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
from agent_alfred.gateway.web.connection import FakeConnection
from agent_alfred.gateway.web.state import (
    StatePatchRejected,
    apply_state_patch,
    build_snapshot,
    snapshot_from_payload,
    snapshot_payload,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import SettingsBackedSnapshotProvider
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


class _ArmableCaptureFailure:
    def __init__(self):
        self._delegate = SettingsBackedSnapshotProvider(Settings())
        self.fail = False
        self.calls = 0

    def capture(self, *, stream: bool = False):
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected capture failure")
        return self._delegate.capture(stream=stream)


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
    snapshot_provider=None,
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
        snapshot_provider=snapshot_provider,
    )
    return host, database


def _api(host: RuntimeHost) -> DashboardApi:
    # The Host satisfies the DashboardFacade protocol itself: the API is
    # driven by direct injection, with no object between them.
    return DashboardApi(facade=host)


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


def test_busy_web_submit_keeps_authoritative_409_when_capture_would_fail() -> None:
    gate = threading.Event()
    provider = _ArmableCaptureFailure()
    host, _conn = _host(["pong"], gate=gate, snapshot_provider=provider)
    host.start()
    try:
        session_id = host.create_session()
        api = _api(host)
        first = api.submit({"message": "first", "session_id": session_id})
        assert first.status == 202
        _wait_until(lambda: host.snapshot().coordinator_state == "running")
        provider.fail = True

        second = api.submit({"message": "second", "session_id": session_id})

        assert second.status == 409
        assert second.code == "run_in_progress"
        assert second.busy is not None
        assert second.busy.run_id == first.run_id
        assert second.busy.stage == "运行中"
        assert provider.calls == 1
    finally:
        gate.set()
        if first.run_id:
            host.wait(first.run_id)
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


def test_recording_failed_web_submit_keeps_503_when_capture_would_fail() -> None:
    flag = {"armed": False}
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    wrapped = _FailFinalizeWhen(database, flag, "finished_at")
    provider = _ArmableCaptureFailure()
    latch = _SelectiveLatch()
    latch.arm()
    host, _conn = _host(
        conn=wrapped,
        snapshot_provider=provider,
        before_recording_commit=latch,
    )
    host.start()
    try:
        session_id = host.create_session()
        api = _api(host)
        first = api.submit({"message": "first", "session_id": session_id})
        assert first.status == 202
        _wait_until(lambda: host.snapshot().coordinator_state == "recording_pending")
        flag["armed"] = True
        latch.release()
        host.wait(first.run_id)
        assert host.snapshot().coordinator_state == "recording_failed"
        provider.fail = True

        second = api.submit({"message": "second", "session_id": session_id})

        assert second.status == 503
        assert second.code == "recording_unavailable"
        assert second.busy is not None
        assert second.busy.run_id == first.run_id
        assert second.busy.stage == "保存失败"
        assert provider.calls == 1
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
        session_id = host.create_session()
        flag["armed"] = True
        outcome = _api(host).submit({"message": "hello", "session_id": session_id})
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
        session_id = host.create_session()
        outcome = _api(host).submit({"message": "hello", "session_id": session_id})
        assert outcome.status == 500
        assert outcome.code == "admission_failed"
        # The Run was finalized interrupted and admission reopened rather
        # than left dangling on a Run nobody will execute.
        _wait_until(lambda: host.snapshot().coordinator_state == "idle")
    finally:
        host.close()


# --- the terminal snapshot survives the settlement window -------------------


def _unstarted(target):
    """A spawn that never starts a writer, so the test reads the opening
    stream directly off the handle."""

    class _Unstarted:
        def start(self):
            return None

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    return _Unstarted()


def _startup_patch(handle) -> dict:
    """The opening stream's state_patch document, exactly as it crossed."""
    for item in handle.startup:
        wire = item.wire_bytes()
        if b"event: state_patch" in wire:
            body = b"".join(
                line[len(b"data: ") :]
                for line in wire.split(b"\n")
                if line.startswith(b"data: ")
            )
            return json.loads(body.decode("utf-8"))
    raise AssertionError("no state_patch in the opening stream")


def _wired_broker(**kwargs) -> SSEBroker:
    """The broker assembled the way production wires it, minus threads."""
    return SSEBroker(
        process_instance_id=INSTANCE,
        snapshot=RuntimeSnapshot(INSTANCE, 0, "idle", None, None),
        session_is_valid=lambda _session_id: True,
        spawn=_unstarted,
        **kwargs,
    )


def test_pending_snapshot_keeps_unrecorded_terminal_outcome() -> None:
    """「回复完成 · 未保存」必须说得出结局。

    ``run.finished`` 发布、收尾事务尚未落定时，Run 的业务结论只存在于
    ``UnrecordedTerminalProjection`` 里。``active_run`` 进入 ``finished``
    的那一刻必须从这份投影复制 outcome：``finished`` 配空结局违反
    phase/outcome 两轴契约，而结算暂停期间权威快照恰好是唯一读数。
    """
    latch = _SelectiveLatch()
    host, _conn = _host(before_recording_commit=latch)
    host.start()
    try:
        # Armed up front: the finalizer pauses deterministically between
        # run.finished and the recording commit, so recording_pending is a
        # state the test observes, not a window it races for.
        latch.arm()
        submitted = host.submit(_request(message="hello", session_id=None))
        assert submitted.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        snap = host.snapshot()
        projection = snap.unrecorded_terminal_projection
        assert projection is not None
        assert projection.run_id == submitted.run_id
        assert projection.outcome == "completed"
        active = snap.active_run
        assert active is not None
        assert active.run_id == submitted.run_id
        assert active.phase == "finished"
        assert active.recording_state == "pending"
        assert active.outcome == projection.outcome
        assert active.outcome
    finally:
        latch.release()
        if submitted.run_id:
            host.wait(submitted.run_id)
        host.close()


def test_startup_patch_keeps_last_step_and_attempt_summary() -> None:
    """同进程重连的启动补丁在结算暂停期间保留最后一个 Step 摘要。

    补丁里的 step 字段必须与事件流一致：step_index 来自 ``step.started``，
    attempts 来自 attempt 终态事件——不是伪造，也不因 ``run.finished``
    而消失。
    """
    latch = _SelectiveLatch()
    broker = _wired_broker()
    probe = CapturingSink(name="probe", flush_at_run_end=True)
    host, _conn = _host(
        extra_sinks=[broker, probe],
        snapshot_listener=broker.publish_state_patch,
        before_recording_commit=latch,
    )
    host.start()
    try:
        # Armed up front: the finalizer pauses deterministically between
        # run.finished and the recording commit.
        latch.arm()
        session_id = host.create_session()
        submitted = host.submit(_request(message="hello", session_id=session_id))
        assert submitted.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        handle = broker.connect(
            connection=FakeConnection(), session_id=session_id
        )
        patch = _startup_patch(handle)
        steps = [
            event.payload
            for event in probe.events
            if event.payload.name == "step.started"
            and event.envelope.run_id == submitted.run_id
        ]
        attempts = [
            event.payload
            for event in probe.events
            if event.payload.name == "attempt.committed"
            and event.envelope.run_id == submitted.run_id
        ]
        assert steps
        step = patch["step"]
        assert step is not None
        assert step["step_index"] == steps[-1].step_index
        # The scripted client bypasses the adapter seam that emits attempt
        # events, so an empty attempt list is this stream's own truth: the
        # patch repeats the stream, it invents nothing.
        assert [(a["attempt_id"], a["outcome"]) for a in step["attempts"]] == [
            (a.attempt_id, "committed") for a in attempts
        ]
        assert patch["active_run"]["phase"] == "finished"
        assert patch["active_run"]["recording_state"] == "pending"
        assert patch["active_run"]["outcome"] == "completed"
        assert patch["unrecorded_terminal_projection"]["outcome"] == "completed"
    finally:
        latch.release()
        if submitted.run_id:
            host.wait(submitted.run_id)
        broker.close(timeout=1.0)
        host.close()


def test_failed_snapshot_keeps_same_terminal_state() -> None:
    """recording_failed 保留与 pending 相同的终态。

    结算失败只改 recording_state 徽标，不改写结局、不擦掉最后的
    Step 摘要——pending 窗口与 failed 窗口读到的是同一份终态。
    """
    flag = {"armed": False}
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    wrapped = _FailFinalizeWhen(database, flag, "finished_at")
    latch = _SelectiveLatch()
    broker = _wired_broker()
    host, _conn = _host(
        conn=wrapped,
        extra_sinks=[broker],
        snapshot_listener=broker.publish_state_patch,
        before_recording_commit=latch,
    )
    host.start()
    try:
        # Armed up front: the finalizer pauses deterministically between
        # run.finished and the recording commit, so the pending window and
        # then the failed window are both observed, not raced for.
        latch.arm()
        session_id = host.create_session()
        submitted = host.submit(_request(message="hello", session_id=session_id))
        assert submitted.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        pending = _startup_patch(
            broker.connect(connection=FakeConnection(), session_id=session_id)
        )
        assert pending["active_run"]["phase"] == "finished"
        assert pending["active_run"]["outcome"] == "completed"
        pending_step = pending["step"]
        assert pending_step is not None

        flag["armed"] = True
        latch.release()
        host.wait(submitted.run_id)
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_failed"
        )
        failed = _startup_patch(
            broker.connect(connection=FakeConnection(), session_id=session_id)
        )
        assert failed["active_run"]["recording_state"] == "failed"
        assert failed["active_run"]["phase"] == "finished"
        assert failed["active_run"]["outcome"] == "completed"
        assert failed["step"] == pending_step
        snap = host.snapshot()
        projection = snap.unrecorded_terminal_projection
        assert projection is not None
        assert projection.recording_state == "failed"
        assert snap.active_run is not None
        assert snap.active_run.outcome == projection.outcome
        assert snap.active_run.outcome
    finally:
        latch.release()
        broker.close(timeout=1.0)
        host.close()


def test_old_run_progress_stops_masquerading_after_recorded() -> None:
    """recorded 权威快照发布并释放租约后，旧 Run 不再冒充活动进度。

    租约仍被旧 Run 持有（recorded 快照已发布、idle 尚未发布）时，它仍是
    活动 Run，终态 Step 摘要仍在；租约一释放，旧 Run 的进度必须随
    recorded→idle 的既有清理路径一起消失——后续重连读到的是干净的
    idle 快照，而不是旧 Run 的进度。
    """
    after_recorded = threading.Event()
    broker = _wired_broker()
    host, _conn = _host(
        extra_sinks=[broker],
        snapshot_listener=broker.publish_state_patch,
        after_recorded_snapshot=after_recorded,
    )
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(_request(message="hello", session_id=session_id))
        assert submitted.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().active_run is not None
            and host.snapshot().active_run.recording_state == "recorded"
        )
        recorded = _startup_patch(
            broker.connect(connection=FakeConnection(), session_id=session_id)
        )
        assert recorded["active_run"]["run_id"] == submitted.run_id
        assert recorded["active_run"]["recording_state"] == "recorded"
        assert recorded["step"] is not None

        after_recorded.set()
        _wait_until(lambda: host.snapshot().coordinator_state == "idle")
        idle = _startup_patch(
            broker.connect(connection=FakeConnection(), session_id=session_id)
        )
        assert idle["coordinator_state"] == "idle"
        assert idle["active_run"] is None
        assert idle["step"] is None
        assert idle["unrecorded_terminal_projection"] is None
    finally:
        after_recorded.set()
        broker.close(timeout=1.0)
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


# --- the lease and the busy card are one publication -------------------------


def test_a_submit_holding_the_lease_publishes_the_busy_card_with_it() -> None:
    """A second submit in the reserve window gets the real card, not a blank.

    Between "the lease is reserved" and "the accepted row is committed"
    there is a window a second submit can walk into. The lease and the busy
    card must become observable in one step: the refused submit reads the
    authoritative snapshot that already carries the active Run, and renders
    the same card a known-busy client would get -- not a 409 whose body
    says, in effect, that nothing is running.
    """
    from contextlib import contextmanager

    from agent_alfred.runtime.work import SubmitRequest

    reserved = threading.Event()
    proceed = threading.Event()
    host, conn = _host(["pong"])
    store = host._admission._database  # noqa: SLF001 - the real store, gated

    class _GatedStore:
        def __init__(self):
            self.fail = False

        @contextmanager
        def transaction(self):
            reserved.set()
            if not proceed.wait(5.0):
                raise RuntimeError("nobody released the database gate")
            if self.fail:
                raise sqlite3.OperationalError("database is locked")
            with store.transaction() as conn:
                yield conn

        @contextmanager
        def reading(self):
            with store.reading() as conn:
                yield conn

    gated = _GatedStore()
    host._admission._database = gated  # noqa: SLF001

    host.start()
    outcomes: dict[str, object] = {}
    errors: list[tuple[str, BaseException]] = []

    def submit(name: str, message: str) -> None:
        try:
            outcomes[name] = host.submit(
                SubmitRequest(message=message, gateway="web")
            )
        except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
            errors.append((name, exc))

    try:
        first = threading.Thread(target=submit, args=("first", "first"))
        first.start()
        assert reserved.wait(5.0), "the first submit never reached its write"

        # The lease is held, and the authoritative snapshot already says so.
        published = host.snapshot()
        assert published.coordinator_state == "accepted"
        assert published.active_run is not None
        assert published.active_run.prompt_preview == "first"
        assert published.active_run.phase == "accepted"

        second = threading.Thread(target=submit, args=("second", "second"))
        second.start()
        second.join(5.0)
        assert not second.is_alive()
        assert errors == []
        refused = outcomes["second"]
        assert refused.kind == "run_in_progress"
        card = busy_summary_from(refused.snapshot)
        assert card is not None
        payload = card.to_json()
        assert payload["purpose"] == "chat"
        assert payload["gateway"] == "web"
        assert payload["stage"] == "已接受"
        assert payload["prompt_preview"] == "first"
        assert payload["current_step"] is None
        assert payload["navigation"]["run_id"] == published.active_run.run_id
        assert payload["navigation"]["filter"] == "chat"
        assert payload["navigation"]["href"].startswith("/runs/")

        # The first submit's write now fails: the lease and the card go
        # back together, no accepted row lands, and admission reopens.
        gated.fail = True
        proceed.set()
        first.join(5.0)
        assert not first.is_alive()
        assert errors == []
        assert outcomes["first"].kind == "admission_failed"
        idle = host.snapshot()
        assert idle.coordinator_state == "idle"
        assert idle.active_run is None
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)

        # The database gate is healthy again; the lease was really reopened.
        gated.fail = False
        third = host.submit(SubmitRequest(message="third", gateway="web"))
        assert third.kind == "accepted"
        host.wait(third.run_id)
    finally:
        proceed.set()
        host.close()


def test_a_handoff_failure_retracts_the_lease_and_the_busy_card() -> None:
    """A Run whose handoff failed never leaves its card behind.

    The summary is published with the lease, so the handoff failure path
    has to take both back at once: idle coordinator, no active run, the
    Run finalized as interrupted in the index, and admission genuinely
    reopened for the next submit.
    """
    from agent_alfred.runtime.work import SubmitRequest

    remaining_failures = [1]

    def explode(item) -> None:
        # The first handoff fails; the publisher is healthy again for the
        # Run that proves admission really reopened.
        if remaining_failures[0]:
            remaining_failures[0] -= 1
            raise RuntimeError("queue gone")
        host._queue.put_nowait(item)

    host, conn = _host(["pong"], publish_work=explode)
    host.start()
    try:
        result = host.submit(SubmitRequest(message="hello", gateway="web"))
        assert result.kind == "admission_failed"
        assert result.run_id is not None
        idle = host.snapshot()
        assert idle.coordinator_state == "idle"
        assert idle.active_run is None
        assert busy_summary_from(idle) is None
        row = conn.execute(
            "SELECT phase, outcome FROM runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()
        assert row == ("finished", "interrupted")
        again = host.submit(SubmitRequest(message="again", gateway="web"))
        assert again.kind == "accepted"
        host.wait(again.run_id)
    finally:
        host.close()


# --- the real Host is the facade: DashboardApi on direct injection ----------
#
# The ``DashboardFacade`` protocol is the API's capability boundary and the
# Fake seam. The real ``RuntimeHost`` satisfies it structurally, so the
# assembly hands the Host to the API itself, with no object between them.
# These characterisations pin the behaviour that direct injection relies on:
# every member the protocol names, answered by the real Host.


def test_a_directly_injected_host_creates_a_usable_session() -> None:
    host, _conn = _host()
    host.start()
    try:
        api = DashboardApi(facade=host)
        result = api.create_session()
        assert result.status == 201
        assert result.code is None
        assert result.session_id is not None
        # The signed id opens as a real Session, and a fresh one has no
        # messages yet.
        status, payload = api.session_messages(result.session_id, {})
        assert status == 200
        assert payload["session_id"] == result.session_id
        assert payload["messages"] == []
        # The gate the create went through is closed again behind it.
        assert host.mutation_in_flight() is False
    finally:
        host.close()


def test_a_directly_injected_host_answers_the_decided_write_contracts() -> None:
    """400 and 404, decided by the API, enforced against the real Host."""
    host, _conn = _host()
    host.start()
    try:
        api = DashboardApi(facade=host)
        empty = api.submit({"message": "   "})
        assert (empty.status, empty.code) == (400, "empty_message")
        unknown_purpose = api.submit({"message": "hi", "purpose": "nonsense"})
        assert (unknown_purpose.status, unknown_purpose.code) == (
            400,
            "unknown_purpose",
        )
        # A Session the Host does not know is a 404, and no Run was
        # admitted behind the refusal.
        refused = api.submit({"message": "hi", "session_id": "nope"})
        assert (refused.status, refused.code) == (404, "unknown_session")
        assert host.snapshot().coordinator_state == "idle"
    finally:
        host.close()


def test_a_directly_injected_host_reads_sessions_runs_and_the_mainbar() -> None:
    """One Run, read back through every read route the API serves."""
    host, _conn = _host(["pong"])
    host.start()
    try:
        api = DashboardApi(facade=host)
        session_id = api.create_session().session_id
        outcome = api.submit({"message": "hello", "session_id": session_id})
        assert outcome.status == 202
        host.wait(outcome.run_id)

        # The inbox serves the Session; the title is the earliest chat
        # Run's prompt preview.
        status, inbox = api.session_inbox({})
        assert status == 200
        assert [row["session_id"] for row in inbox["sessions"]] == [session_id]
        assert inbox["sessions"][0]["title"] == "hello"

        # The runs page and the deep link serve the recorded Run.
        status, runs_page = api.runs_page({"filter": "chat"})
        assert status == 200
        assert [run["run_id"] for run in runs_page["runs"]] == [outcome.run_id]
        assert runs_page["non_terminal"] is None
        status, located = api.locate_run(outcome.run_id, {})
        assert status == 200
        assert located["runs"][0]["run_id"] == outcome.run_id

        # The Session group's run list carries the reply the Run recorded.
        status, session_runs = api.session_runs({"session_id": session_id})
        assert status == 200
        row = session_runs["runs"][0]
        assert row["run_id"] == outcome.run_id
        assert row["reply_preview"] == "pong"
        assert row["reply_source"] == "web"

        # The MainBar is the same conversation, user and assistant.
        status, mainbar = api.mainbar({"session_id": session_id})
        assert status == 200
        pair = mainbar["pairs"][0]
        assert pair["run_id"] == outcome.run_id
        assert pair["user"][0] == {"type": "text", "text": "hello"}
        assert pair["assistant"][0] == {"type": "text", "text": "pong"}
    finally:
        host.close()


def test_the_mutation_gate_authority_is_the_real_hosts() -> None:
    """``try_begin_mutation`` / ``end_mutation`` / ``mutation_in_flight``.

    The three answers the write gate asks for, from the object the gate
    asks: opening is a reservation and never a wait, a second open is
    refused in the gate's own vocabulary, and closing reopens.
    """
    host, _conn = _host()
    host.start()
    try:
        api = DashboardApi(facade=host)
        assert host.mutation_in_flight() is False
        assert host.try_begin_mutation() is None
        assert host.mutation_in_flight() is True
        # A second write is refused, not queued -- and the refusal holds
        # nothing of its own.
        assert host.try_begin_mutation() == "mutation_in_flight"
        host.end_mutation()
        assert host.mutation_in_flight() is False
        # The reopened gate lets the next write straight through.
        assert api.create_session().status == 201
    finally:
        host.close()


def test_the_gate_spans_the_whole_lease_on_the_real_host() -> None:
    """A Run holding its lease refuses a plain write until recording settles.

    The gate's judgement is the Host's, taken under the lock that decides
    admission: from ``accepted`` to the end of the lease, ``try_begin_mutation``
    answers ``mutation_in_flight`` -- and that answer is a refusal, not a
    reservation of its own.
    """
    gate = threading.Event()
    host, _conn = _host(["pong"], gate=gate)
    host.start()
    try:
        api = DashboardApi(facade=host)
        session_id = api.create_session().session_id
        outcome = api.submit({"message": "first", "session_id": session_id})
        assert outcome.status == 202
        _wait_until(lambda: host.snapshot().coordinator_state == "running")
        # The lease is held, so the gate's authority refuses a write...
        assert host.try_begin_mutation() == "mutation_in_flight"
        # ...and the refusal left no hold behind: the Run owns the gate.
        assert host.mutation_in_flight() is False
        gate.set()
        host.wait(outcome.run_id)
        # The lease is back, so the gate opens again for the next write.
        assert host.try_begin_mutation() is None
        host.end_mutation()
    finally:
        gate.set()
        host.close()


def test_the_read_contracts_hold_on_the_real_host() -> None:
    """404 and 400 on the read side, against the real store."""
    host, _conn = _host()
    host.start()
    try:
        api = DashboardApi(facade=host)
        unknown = (404, {"code": "unknown_session"})
        assert api.session_messages("nope", {}) == unknown
        assert api.mainbar({"session_id": "nope"}) == unknown
        assert api.session_runs({"session_id": "nope"}) == unknown
        assert api.locate_run("nope", {}) == (404, {"code": "unknown_run"})
        # Missing and empty are different facts; only absence is a bad
        # request, refused before any read runs.
        missing = (400, {"code": "missing_session_id"})
        assert api.mainbar({}) == missing
        assert api.session_runs({}) == missing
    finally:
        host.close()
