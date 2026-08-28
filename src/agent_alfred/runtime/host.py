"""RuntimeHost: process-unique facade over admission, execution, and recording."""

from __future__ import annotations

import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import FanOutSink
from agent_alfred.loop.assistant import Assistant, LoopResult
from agent_alfred.model import ModelClientFactory
from agent_alfred.redact import Redactor
from agent_alfred.runtime import sessions as session_store
from agent_alfred.runtime.admission import RunAdmission
from agent_alfred.runtime.config import (
    ConfigSnapshotProvider,
    SettingsBackedSnapshotProvider,
)
from agent_alfred.runtime.execution import RunExecutor
from agent_alfred.runtime.recording import RecordingStore, RunRecorder
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    CoordinatorState,
    RunStateStore,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.runtime.work import (
    SubmitKind,
    SubmitRequest,
    SubmitResult,
    WorkItem,
)
from agent_alfred.settings import Settings

__all__ = [
    "RuntimeHost",
    "SubmitKind",
    "SubmitRequest",
    "SubmitResult",
    "WorkItem",
]


class RuntimeHost:
    """Process-unique owner of seq, the write connection, and admission.

    The lifecycle below the Run loop is owned here as narrow, atomic
    transitions (``recording_enter_pending``, ``recording_enter_failed``,
    ``recording_publish_recorded_then_release``); the recorder drives them
    through the :class:`~agent_alfred.runtime.recording.RecordingCoordinator`
    protocol instead of manipulating this object's private state.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        factory: ModelClientFactory,
        settings: Settings,
        clock: Clock,
        fanout: FanOutSink,
        process_instance_id: str,
        secrets: tuple[str, ...] = (),
        redactor: Redactor | None = None,
        publish_work: Callable[[WorkItem], None] | None = None,
        before_recording_commit: threading.Event | None = None,
        after_recorded_snapshot: threading.Event | None = None,
        before_recording_failed: threading.Event | None = None,
        snapshot_provider: ConfigSnapshotProvider | None = None,
    ):
        self._conn = conn
        self._factory = factory
        self._settings = settings
        self._clock = clock
        self._fanout = fanout
        self._process_instance_id = process_instance_id
        self._redactor = redactor or Redactor(secrets)
        self._fanout.bind_redactor(self._redactor)
        self._assistant = Assistant(clock=clock, settings=settings)
        self._states = RunStateStore(process_instance_id)
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._coord: CoordinatorState = "idle"
        self._active_summary: ActiveRunSummary | None = None
        self._queue: queue.Queue[WorkItem | None] = queue.Queue()
        self._done: dict[str, threading.Event] = {}
        self._results: dict[str, LoopResult] = {}
        self._started = False
        self._closed = False
        self._publish_work = publish_work
        self._before_recording_commit = before_recording_commit
        self._after_recorded_snapshot = after_recorded_snapshot
        self._before_recording_failed = before_recording_failed
        self._snapshot_provider = snapshot_provider or SettingsBackedSnapshotProvider(
            settings
        )
        self._admission = RunAdmission(self)
        self._executor = RunExecutor(self)
        self._recorder = RunRecorder(
            clock=clock,
            fanout=fanout,
            redactor=self._redactor,
            store=RecordingStore(conn, self._db_lock),
            coordinator=self,
            before_recording_commit=before_recording_commit,
        )
        self._worker = threading.Thread(
            target=self._executor.run_loop, name="run-worker", daemon=True
        )

    @property
    def process_instance_id(self) -> str:
        return self._process_instance_id

    @property
    def settings(self) -> Settings:
        """The immutable settings injected at assembly. Never re-read."""
        return self._settings

    def snapshot(self) -> RuntimeSnapshot:
        return self._states.get()

    def start(self) -> None:
        self.recover()
        if not self._started:
            self._worker.start()
            self._started = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        if self._started:
            self._worker.join(timeout=5)
        self._fanout.close()

    def recover(self) -> None:
        self._recorder.recover()

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        now = format_instant(self._clock.wall_utc())
        with self._db_lock:
            schema.insert_session(
                self._conn, session_id=session_id, created_at=now
            )
            self._conn.commit()
        return session_id

    # -- recording-settlement transitions (the only writer of these states) --

    def recording_enter_pending(
        self, projection: UnrecordedTerminalProjection
    ) -> None:
        """running -> recording_pending. The lease is NOT released here."""
        with self._lock:
            self._coord = "recording_pending"
            if self._active_summary is not None:
                self._active_summary = replace(
                    self._active_summary,
                    phase="finished",
                    recording_state="pending",
                )
            self._states.replace(
                coordinator_state="recording_pending",
                active_run=self._active_summary,
                unrecorded_terminal_projection=projection,
            )

    def recording_enter_failed(
        self, projection: UnrecordedTerminalProjection
    ) -> None:
        """recording_pending -> recording_failed, keeping the same projection."""
        if self._before_recording_failed is not None:
            self._before_recording_failed.wait()
        with self._lock:
            failed_projection = replace(projection, recording_state="failed")
            summary = self._active_summary
            if summary is not None:
                summary = replace(
                    summary, recording_state="failed", phase="finished"
                )
                self._active_summary = summary
            self._coord = "recording_failed"
            self._states.replace(
                coordinator_state="recording_failed",
                active_run=self._active_summary,
                unrecorded_terminal_projection=failed_projection,
            )

    def recording_publish_recorded_then_release(self) -> None:
        """Authority first, lease second: the recorded snapshot becomes
        visible while admission is still closed, then the lease releases."""
        with self._lock:
            recorded = None
            if self._active_summary is not None:
                recorded = replace(
                    self._active_summary, recording_state="recorded"
                )
                self._active_summary = recorded
            self._states.replace(
                coordinator_state="recording_pending",
                active_run=recorded,
                unrecorded_terminal_projection=None,
            )
        if self._after_recorded_snapshot is not None:
            self._after_recorded_snapshot.wait()
        with self._lock:
            self._coord = "idle"
            self._active_summary = None
            self._states.replace(coordinator_state="idle", active_run=None)

    def publish_run_result(self, run_id: str, result: LoopResult) -> None:
        self._results[run_id] = result

    def notify_run_done(self, run_id: str) -> None:
        event = self._done.get(run_id)
        if event is not None:
            event.set()

    # -- public session read side (ADR-0027); callers never write SQL --

    def list_sessions(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> session_store.SessionInboxPage:
        with self._db_lock:
            return session_store.list_sessions(
                self._conn,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
                title_max_chars=self._settings.prompt_preview_max_chars,
            )

    def open_session(
        self,
        session_id: str,
        *,
        page_size: int,
        cursor: str | None = None,
    ) -> session_store.SessionMessagesPage:
        with self._db_lock:
            return session_store.open_session(
                self._conn,
                session_id=session_id,
                page_size=page_size,
                cursor=cursor,
                redactor=self._redactor,
                title_max_chars=self._settings.prompt_preview_max_chars,
            )

    def submit(self, request: SubmitRequest) -> SubmitResult:
        return self._admission.submit(request)

    def wait(self, run_id: str, timeout: float = 60.0) -> LoopResult:
        event = self._done.get(run_id)
        if event is None:
            raise KeyError(run_id)
        if not event.wait(timeout):
            raise TimeoutError(f"timed out waiting for run {run_id}")
        return self._results[run_id]
