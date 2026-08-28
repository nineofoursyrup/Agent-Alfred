"""RuntimeHost: admission, execution thread, finalizer, recovery."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import (
    EventEnvelope,
    FanOutSink,
    RunFinished,
    RunStarted,
)
from agent_alfred.loop.assistant import Assistant, LoopResult
from agent_alfred.loop.budget import RunBudget
from agent_alfred.messages import (
    Message,
    blocks_from_jsonable,
    blocks_to_jsonable,
    message_plain_text,
    text_message,
)
from agent_alfred.model import ClientSnapshot, ModelClientFactory, ModelRef, ModelResult
from agent_alfred.outcomes import RunOutcome
from agent_alfred.runtime.config import (
    ConfigSnapshotProvider,
    SettingsBackedSnapshotProvider,
)
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    CoordinatorState,
    RunStateStore,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.runtime.telemetry import serialize_run_telemetry
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT, Settings

SubmitKind = Literal[
    "accepted",
    "run_in_progress",
    "recording_unavailable",
    "admission_failed",
]


@dataclass(frozen=True)
class SubmitRequest:
    message: str
    purpose: str = "chat"
    session_id: str | None = None
    gateway: str = "cli"
    entry_surface_id: str | None = None
    stream: bool = False


@dataclass(frozen=True)
class SubmitResult:
    kind: SubmitKind
    run_id: str | None = None
    session_id: str | None = None
    snapshot: RuntimeSnapshot | None = None


@dataclass
class _WorkItem:
    run_id: str
    request: SubmitRequest
    snapshot: ClientSnapshot
    session_id: str | None
    prompt_preview: str | None
    accepted_at: str


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
        preview_redactor: Callable[[str], str] | None = None,
        publish_work: Callable[[_WorkItem], None] | None = None,
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
        from agent_alfred.redact import Redactor

        self._secrets = secrets
        self._redactor = Redactor(secrets)
        self._preview_redactor = preview_redactor or self._redactor.redact_text
        self._assistant = Assistant(clock=clock, settings=settings)
        self._states = RunStateStore(process_instance_id)
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._coord: CoordinatorState = "idle"
        self._active_run_id: str | None = None
        self._active_summary: ActiveRunSummary | None = None
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._done: dict[str, threading.Event] = {}
        self._results: dict[str, LoopResult] = {}
        self._worker = threading.Thread(
            target=self._worker_loop, name="run-worker", daemon=True
        )
        self._started = False
        self._closed = False
        self._publish_work = publish_work
        self._before_recording_commit = before_recording_commit
        self._after_recorded_snapshot = after_recorded_snapshot
        self._before_recording_failed = before_recording_failed
        self._snapshot_provider = snapshot_provider or SettingsBackedSnapshotProvider(
            settings
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
        now = format_instant(self._clock.wall_utc())
        with self._db_lock:
            rows = self._conn.execute(
                """SELECT run_id, session_id, phase, started_at
                   FROM runs WHERE phase != 'finished'"""
            ).fetchall()
            for run_id, session_id, phase, _started_at in rows:
                revision = schema.allocate_activity_revision(self._conn)
                schema.update_run_phase(
                    self._conn,
                    run_id=run_id,
                    from_phase=phase,
                    to_phase="finished",
                    activity_revision=revision,
                    outcome="interrupted",
                    finished_at=now,
                    session_id=session_id,
                )
            self._conn.commit()

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
        with self._lock:
            snap = self._states.get()
            if self._coord == "recording_failed":
                return SubmitResult(
                    kind="recording_unavailable", snapshot=snap
                )
            if self._coord != "idle":
                return SubmitResult(kind="run_in_progress", snapshot=snap)
            run_id = uuid.uuid4().hex
            self._coord = "accepted"
            self._active_run_id = run_id
            self._done[run_id] = threading.Event()

        session_id = request.session_id
        preview = self._preview(request.message)
        accepted_at = format_instant(self._clock.wall_utc())
        try:
            with self._db_lock:
                if request.purpose == "chat" and session_id is None:
                    session_id = uuid.uuid4().hex
                    schema.insert_session(
                        self._conn, session_id=session_id, created_at=accepted_at
                    )
                schema.insert_accepted_run(
                    self._conn,
                    run_id=run_id,
                    purpose=request.purpose,
                    session_id=session_id,
                    gateway=request.gateway,
                    accepted_at=accepted_at,
                    entry_surface_id=request.entry_surface_id,
                    prompt_preview=preview,
                )
                self._conn.commit()
        except Exception:
            with self._db_lock:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
            with self._lock:
                self._coord = "idle"
                self._active_run_id = None
            self._states.replace(coordinator_state="idle", active_run=None)
            return SubmitResult(kind="admission_failed")

        summary = ActiveRunSummary(
            run_id=run_id,
            purpose=request.purpose,
            gateway=request.gateway,
            phase="accepted",
            session_id=session_id,
            prompt_preview=preview,
            started_at=None,
            recording_state=None,
        )
        with self._lock:
            self._active_summary = summary
        self._states.replace(coordinator_state="accepted", active_run=summary)

        item = _WorkItem(
            run_id=run_id,
            request=request,
            snapshot=self._capture_snapshot(request),
            session_id=session_id,
            prompt_preview=preview,
            accepted_at=accepted_at,
        )
        try:
            publisher = self._publish_work or self._queue.put_nowait
            publisher(item)
        except Exception:
            self._interrupt_unstarted(item)
            return SubmitResult(kind="admission_failed", run_id=run_id)

        return SubmitResult(
            kind="accepted",
            run_id=run_id,
            session_id=session_id,
            snapshot=self._states.get(),
        )

    def wait(self, run_id: str, timeout: float = 60.0) -> LoopResult:
        event = self._done.get(run_id)
        if event is None:
            raise KeyError(run_id)
        if not event.wait(timeout):
            raise TimeoutError(f"timed out waiting for run {run_id}")
        return self._results[run_id]

    def _capture_snapshot(self, request: SubmitRequest) -> ClientSnapshot:
        snapshot = self._snapshot_provider.capture(stream=request.stream)
        self._redactor.remember(snapshot.api_key)
        return snapshot

    def _preview(self, message: str) -> str:
        text = self._preview_redactor(message)
        limit = self._settings.prompt_preview_max_chars
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._execute(item)

    def _execute(self, item: _WorkItem) -> None:
        outcome: RunOutcome = "interrupted"
        reply: Message | None = None
        error: str | None = None
        step_count = 0
        duration_ms = 0
        model_results: tuple[ModelResult, ...] = ()
        started_at = format_instant(self._clock.wall_utc())
        try:
            with self._db_lock:
                revision = schema.allocate_activity_revision(self._conn)
                schema.update_run_phase(
                    self._conn,
                    run_id=item.run_id,
                    from_phase="accepted",
                    to_phase="running",
                    activity_revision=revision,
                    started_at=started_at,
                    session_id=item.session_id,
                )
                self._conn.commit()
            with self._lock:
                self._coord = "running"
                if self._active_summary is not None:
                    self._active_summary = replace(
                        self._active_summary,
                        phase="running",
                        started_at=started_at,
                    )
            self._states.replace(
                coordinator_state="running",
                active_run=self._active_summary,
            )
            envelope = EventEnvelope(
                ts=self._clock.monotonic(),
                run_id=item.run_id,
                session_id=item.session_id,
                step_index=None,
                attempt_id=None,
                node_id=None,
                source=item.request.gateway,
            )
            if item.request.purpose == "inference_probe":
                working_memory: tuple[Message, ...] = ()
            else:
                working_memory = self._load_working_memory(item.session_id)
            self._fanout.emit(
                RunStarted(
                    user_message=text_message(
                        "user", self._preview_redactor(item.request.message)
                    )
                    if item.request.purpose == "chat"
                    else None,
                    working_memory_message_count=len(working_memory),
                    purpose=item.request.purpose,
                ),
                envelope,
            )
            client = self._factory.create(item.snapshot)
            budget = RunBudget(self._settings.max_steps)
            loop_result = self._assistant.respond(
                item.request.message,
                client=client,
                budget=budget,
                working_memory=working_memory,
                model=ModelRef(
                    endpoint_id=item.snapshot.endpoint_id,
                    model_id=item.snapshot.model_id,
                ),
                run_id=item.run_id,
                session_id=item.session_id,
                events=self._fanout,
                source=item.request.gateway,
                stream=item.snapshot.stream,
                overall_deadline_s=item.snapshot.overall_deadline_s,
                per_attempt_timeout_s=item.snapshot.per_attempt_timeout_s,
            )
            outcome = loop_result.outcome
            reply = loop_result.reply
            error = loop_result.error
            step_count = loop_result.step_count
            duration_ms = loop_result.duration_ms
            model_results = loop_result.model_results
        except KeyboardInterrupt:
            outcome = "interrupted"
            error = "interrupted"
            reply = None
        except Exception as exc:
            outcome = "failed"
            error = type(exc).__name__
            if reply is None:
                reply = text_message("assistant", CONTROLLED_FAILURE_TEXT)
            del exc
        self._settle_recording(
            item,
            outcome=outcome,
            reply=reply,
            error=error,
            step_count=step_count,
            duration_ms=duration_ms,
            model_results=model_results,
        )

    def _settle_recording(
        self,
        item: _WorkItem,
        *,
        outcome: RunOutcome,
        reply: Message | None,
        error: str | None,
        step_count: int,
        duration_ms: int,
        model_results: tuple[ModelResult, ...],
    ) -> None:
        if (
            item.request.purpose == "chat"
            and outcome == "failed"
            and reply is None
        ):
            reply = text_message("assistant", CONTROLLED_FAILURE_TEXT)
        envelope = EventEnvelope(
            ts=self._clock.monotonic(),
            run_id=item.run_id,
            session_id=item.session_id,
            step_index=None,
            attempt_id=None,
            node_id=None,
            source=item.request.gateway,
        )
        try:
            self._fanout.emit(
                RunFinished(
                    outcome=outcome,
                    reply=reply,
                    error=error,
                    step_count=step_count,
                    duration_ms=duration_ms,
                ),
                envelope,
            )
        except Exception:
            pass
        projection = UnrecordedTerminalProjection(
            run_id=item.run_id,
            purpose=item.request.purpose,
            outcome=outcome,
            reply_text=None if reply is None else message_plain_text(reply),
            error=error,
            recording_state="pending",
            session_id=item.session_id,
            prompt_preview=item.prompt_preview,
        )
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
        self._results[item.run_id] = LoopResult(
            outcome=outcome,
            reply=reply,
            error=error,
            step_count=step_count,
            duration_ms=duration_ms,
            model_results=model_results,
        )
        try:
            incomplete, reason = self._fanout.flush_barrier()
        except Exception as exc:
            incomplete, reason = True, type(exc).__name__
        telemetry = serialize_run_telemetry(
            model_results, incomplete, reason, redactor=self._redactor
        )
        if self._before_recording_commit is not None:
            self._before_recording_commit.wait()
        try:
            self._finalize(item, outcome, reply, telemetry)
        except Exception:
            with self._db_lock:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
            self._enter_recording_failed(projection)
            self._done[item.run_id].set()
            return
        self._publish_recorded_then_release()
        self._done[item.run_id].set()

    def _enter_recording_failed(
        self, projection: UnrecordedTerminalProjection
    ) -> None:
        if self._before_recording_failed is not None:
            self._before_recording_failed.wait()
        failed = replace(projection, recording_state="failed")
        with self._lock:
            summary = self._active_summary
            if summary is not None:
                summary = replace(summary, recording_state="failed", phase="finished")
                self._active_summary = summary
            self._coord = "recording_failed"
            self._states.replace(
                coordinator_state="recording_failed",
                active_run=summary,
                unrecorded_terminal_projection=failed,
            )

    def _publish_recorded_then_release(self) -> None:
        with self._lock:
            recorded = None
            if self._active_summary is not None:
                recorded = replace(self._active_summary, recording_state="recorded")
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
            self._active_run_id = None
            self._active_summary = None
            self._states.replace(coordinator_state="idle", active_run=None)

    def _finalize(
        self,
        item: _WorkItem,
        outcome: RunOutcome,
        reply: Message | None,
        telemetry: str,
    ) -> None:
        now = format_instant(self._clock.wall_utc())
        with self._db_lock:
            phase = self._conn.execute(
                "SELECT phase FROM runs WHERE run_id = ?", (item.run_id,)
            ).fetchone()
            from_phase = "running" if phase is None else phase[0]
            if from_phase == "finished":
                self._conn.commit()
                return
            revision = schema.allocate_activity_revision(self._conn)
            schema.update_run_phase(
                self._conn,
                run_id=item.run_id,
                from_phase=from_phase,
                to_phase="finished",
                activity_revision=revision,
                outcome=outcome,
                finished_at=now,
                telemetry=telemetry,
                session_id=item.session_id,
            )
            if item.request.purpose == "chat" and item.session_id is not None:
                user = text_message("user", item.request.message)
                _insert_log_message(
                    self._conn,
                    session_id=item.session_id,
                    role="user",
                    message=user,
                    source=item.request.gateway,
                    created_at=item.accepted_at,
                    run_id=item.run_id,
                )
                if outcome != "interrupted":
                    assistant = reply
                    if assistant is None:
                        assistant = text_message(
                            "assistant", CONTROLLED_FAILURE_TEXT
                        )
                    stored_text = self._redactor.redact_text(
                        message_plain_text(assistant)
                    )
                    if stored_text != message_plain_text(assistant):
                        assistant = text_message("assistant", stored_text)
                    _insert_log_message(
                        self._conn,
                        session_id=item.session_id,
                        role="assistant",
                        message=assistant,
                        source=item.request.gateway,
                        created_at=now,
                        run_id=item.run_id,
                    )
            self._conn.commit()

    def _interrupt_unstarted(self, item: _WorkItem) -> None:
        now = format_instant(self._clock.wall_utc())
        try:
            with self._db_lock:
                revision = schema.allocate_activity_revision(self._conn)
                schema.update_run_phase(
                    self._conn,
                    run_id=item.run_id,
                    from_phase="accepted",
                    to_phase="finished",
                    activity_revision=revision,
                    outcome="interrupted",
                    finished_at=now,
                    session_id=item.session_id,
                )
                self._conn.commit()
        except Exception:
            with self._lock:
                self._coord = "idle"
                self._active_run_id = None
            self._states.replace(coordinator_state="idle", active_run=None)
            return
        with self._lock:
            self._coord = "idle"
            self._active_run_id = None
            self._active_summary = None
        self._states.replace(
            coordinator_state="idle",
            active_run=None,
            unrecorded_terminal_projection=None,
        )
        if item.run_id in self._done:
            self._results[item.run_id] = LoopResult(
                outcome="interrupted",
                reply=None,
                error="handoff_failed",
                step_count=0,
                duration_ms=0,
            )
            self._done[item.run_id].set()

    def _load_working_memory(self, session_id: str | None) -> tuple[Message, ...]:
        if session_id is None:
            return ()
        limit = self._settings.working_memory_rounds * 2
        with self._db_lock:
            rows = self._conn.execute(
                """SELECT role, content FROM agent_log
                   WHERE session_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        rows.reverse()
        messages: list[Message] = []
        for role, content in rows:
            blocks = blocks_from_jsonable(json.loads(content))
            messages.append(Message(role=role, blocks=blocks))
        return tuple(messages)


def _insert_log_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    role: str,
    message: Message,
    source: str,
    created_at: str,
    run_id: str,
) -> None:
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at, run_id
           ) VALUES (?, ?, ?, ?, NULL, ?, ?)""",
        (
            session_id,
            role,
            json.dumps(blocks_to_jsonable(message.blocks), ensure_ascii=False),
            source,
            created_at,
            run_id,
        ),
    )
