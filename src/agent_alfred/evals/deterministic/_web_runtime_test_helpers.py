"""Shared RuntimeHost construction and deterministic latch helpers."""

from __future__ import annotations

import sqlite3
import threading
import time

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink, PostCommit
from agent_alfred.gateway.web.api import (
    DashboardApi,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import SettingsBackedSnapshotProvider
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.settings import Settings

INSTANCE = "proc-runtime"


class FailNextSessionCommit:
    """Real SQLite connection whose next Session commit fails once."""

    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.fail_next_commit = False
        self.fail_next_rollback = False
        self.failed_session_id: str | None = None
        self.execute_calls = 0
        self.commit_calls = 0
        self.failed_run_id: str | None = None

    def execute(self, sql, parameters=()):
        self.execute_calls += 1
        if self.fail_next_commit and "INSERT INTO sessions" in sql:
            self.failed_session_id = parameters[0]
        if self.fail_next_commit and "INSERT INTO runs" in sql:
            self.failed_run_id = parameters[0]
        return self._inner.execute(sql, parameters)

    def commit(self):
        self.commit_calls += 1
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("injected Session commit failure")
        return self._inner.commit()

    def rollback(self):
        if self.fail_next_rollback:
            self.fail_next_rollback = False
            raise sqlite3.OperationalError("injected Session rollback failure")
        return self._inner.rollback()

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class SelectiveLatch:
    """A ``before_``/``after_`` hook that waits only while armed."""

    def __init__(self):
        self._gate = threading.Event()
        self._gate.set()
        self.entered = threading.Event()

    def arm(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def wait(self, timeout=None):
        self.entered.set()
        return self._gate.wait(timeout)


class FailFinalizeWhen:
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


class ArmableCaptureFailure:
    def __init__(self):
        self._delegate = SettingsBackedSnapshotProvider(Settings())
        self.fail = False
        self.calls = 0

    def capture(self, *, stream: bool = False):
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected capture failure")
        return self._delegate.capture(stream=stream)


class StepStartedLatch(CapturingSink):
    """Observe the real published Step without polling or sleeping."""

    def __init__(self):
        super().__init__(name="step-latch", flush_at_run_end=True)
        self.published = threading.Event()

    def commit(self, prepared, event):
        super().commit(prepared, event)
        if event.payload.name == "step.started":
            self.published.set()


class StepStartedPostCommitLatch(CapturingSink):
    """Pause after ``step.started`` is committed by the real FanOut."""

    def __init__(self):
        super().__init__(name="step-post-commit-latch", flush_at_run_end=True)
        self.committed = threading.Event()
        self._release = threading.Event()

    def commit(self, prepared, event):
        super().commit(prepared, event)
        if event.payload.name != "step.started":
            return None
        self.committed.set()
        return PostCommit(self._release.wait)

    def release(self) -> None:
        self._release.set()


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def call_within(callable_, *, seconds: float) -> tuple[bool, object]:
    """Run a callable on a thread and report whether it met the time bound."""
    box: dict = {}

    def run() -> None:
        box["value"] = callable_()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return (not thread.is_alive()), box.get("value")


def build_runtime_host(
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
    redactor=None,
):
    database = conn
    if database is None:
        database = sqlite3.connect(":memory:", check_same_thread=False)
        schema.migrate(database)
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    sinks = [capture, *(extra_sinks or ())]
    host = RuntimeHost(
        conn=database,
        factory=ScriptedModelFactory(ScriptedModel(script or ["pong"], gate=gate)),
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
        redactor=redactor,
    )
    return host, database


def dashboard_api(host: RuntimeHost) -> DashboardApi:
    # The Host satisfies the DashboardFacade protocol itself: the API is
    # driven by direct injection, with no object between them.
    return DashboardApi(facade=host)


def snapshot_patch(
    *,
    revision: int,
    instance: str = INSTANCE,
    pending: bool = False,
    run_id: str = "r1",
):
    from agent_alfred.gateway.web.state import (
        build_snapshot,
        snapshot_from_payload,
        snapshot_payload,
    )
    from agent_alfred.runtime.snapshot import (
        ActiveRunSummary,
        RuntimeSnapshot,
        UnrecordedTerminalProjection,
    )

    runtime = RuntimeSnapshot(
        process_instance_id=instance,
        state_revision=revision,
        coordinator_state="recording_pending",
        active_run=ActiveRunSummary(
            run_id=run_id,
            purpose="chat",
            gateway="web",
            phase="finished",
            outcome="completed",
            session_id="s1",
            prompt_preview="hi",
            started_at="2026-01-01T00:00:00Z",
            recording_state="pending" if pending else "recorded",
        ),
        unrecorded_terminal_projection=(
            UnrecordedTerminalProjection(
                run_id=run_id,
                purpose="chat",
                outcome="completed",
                reply_text="reply",
                error=None,
                recording_state="pending",
                session_id="s1",
                prompt_preview="hi",
            )
            if pending
            else None
        ),
    )
    built = build_snapshot(runtime, step=None, session_valid=True)
    return snapshot_from_payload(snapshot_payload(built))


def refused(patch, current) -> str:
    from agent_alfred.gateway.web.state import StatePatchRejected, apply_state_patch

    try:
        apply_state_patch(current, patch)
    except StatePatchRejected as exc:
        return exc.reason
    raise AssertionError("the patch should have been refused")
