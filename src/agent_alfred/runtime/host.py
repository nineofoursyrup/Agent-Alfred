"""RuntimeHost: process-unique facade over admission, execution, and recording."""

from __future__ import annotations

import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import FanOutSink
from agent_alfred.loop.assistant import Assistant, LoopResult
from agent_alfred.model import ModelClientFactory
from agent_alfred.redact import Redactor
from agent_alfred.runtime.admission import RunAdmission
from agent_alfred.runtime.config import (
    ConfigSnapshotProvider,
    SettingsBackedSnapshotProvider,
)
from agent_alfred.runtime.execution import RunExecutor
from agent_alfred.runtime.recording import RunRecorder
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    CoordinatorState,
    RunStateStore,
    RuntimeSnapshot,
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
    """Process-unique owner of seq, the write connection, and admission."""

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
        self._recorder = RunRecorder(self)
        self._worker = threading.Thread(
            target=self._executor.run_loop, name="run-worker", daemon=True
        )

    @property
    def process_instance_id(self) -> str:
        return self._process_instance_id

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

    def submit(self, request: SubmitRequest) -> SubmitResult:
        return self._admission.submit(request)

    def wait(self, run_id: str, timeout: float = 60.0) -> LoopResult:
        event = self._done.get(run_id)
        if event is None:
            raise KeyError(run_id)
        if not event.wait(timeout):
            raise TimeoutError(f"timed out waiting for run {run_id}")
        return self._results[run_id]
