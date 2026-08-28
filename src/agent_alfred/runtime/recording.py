"""Finalizer, recovery, and the recording lease.

The recorder touches the Host only through two narrow seams: a
:class:`RecordingCoordinator` (the atomic coordinator state transitions and
result publication) and a :class:`RecordingStore` (the Host-owned connection
under its write lock). It never reaches into Host private state itself, so
the state-machine invariants cannot be bypassed from here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Protocol

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import EventEnvelope, FanOutSink, RunFinished
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import (
    Message,
    blocks_to_jsonable,
    message_plain_text,
    text_message,
)
from agent_alfred.model import ModelResult
from agent_alfred.outcomes import RunOutcome
from agent_alfred.redact import Redactor
from agent_alfred.runtime.snapshot import UnrecordedTerminalProjection
from agent_alfred.runtime.telemetry import serialize_run_telemetry
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT

_REASON_LIMIT = 500


class RecordingCoordinator(Protocol):
    """The atomic coordinator transitions the recorder may trigger."""

    def recording_enter_pending(
        self, projection: UnrecordedTerminalProjection
    ) -> None: ...

    def recording_enter_failed(
        self, projection: UnrecordedTerminalProjection
    ) -> None: ...

    def recording_publish_recorded_then_release(self) -> None: ...

    def publish_run_result(self, run_id: str, result: LoopResult) -> None: ...

    def notify_run_done(self, run_id: str) -> None: ...


class RecordingStore:
    """The Host-owned write connection under its lock; the caller commits."""

    def __init__(self, conn: sqlite3.Connection, db_lock: threading.Lock):
        self._conn = conn
        self._db_lock = db_lock

    @contextmanager
    def transaction(self):
        with self._db_lock:
            try:
                yield self._conn
            finally:
                if self._conn.in_transaction:
                    self._conn.rollback()

    @contextmanager
    def reading(self):
        """Read access under the write lock; no transaction is owned."""
        with self._db_lock:
            yield self._conn


class RunRecorder:
    def __init__(
        self,
        *,
        clock: Clock,
        fanout: FanOutSink,
        redactor: Redactor,
        store: RecordingStore,
        coordinator: Any,
        before_recording_commit: threading.Event | None = None,
    ):
        self._clock = clock
        self._fanout = fanout
        self._redactor = redactor
        self._store = store
        self._coordinator: RecordingCoordinator = coordinator
        self._before_recording_commit = before_recording_commit

    def recover(self) -> None:
        now = format_instant(self._clock.wall_utc())
        with self._store.transaction() as conn:
            rows = conn.execute(
                """SELECT run_id, session_id, phase
                   FROM runs WHERE phase != 'finished'"""
            ).fetchall()
            for run_id, session_id, phase in rows:
                revision = schema.allocate_activity_revision(conn)
                schema.update_run_phase(
                    conn,
                    run_id=run_id,
                    from_phase=phase,
                    to_phase="finished",
                    activity_revision=revision,
                    outcome="interrupted",
                    finished_at=now,
                    session_id=session_id,
                )
            conn.commit()

    def settle(
        self,
        item: WorkItem,
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
        # A central publish failure must survive: it is merged into the
        # barrier result below, so the telemetry can never claim a complete
        # trace whose terminal event never reached the sinks.
        publish_failed = False
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
        except Exception as exc:
            publish_failed = True
            self._fanout.note_publish_failure(item.run_id, exc)
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
        self._coordinator.recording_enter_pending(projection)
        self._coordinator.publish_run_result(
            item.run_id,
            LoopResult(
                outcome=outcome,
                reply=reply,
                error=error,
                step_count=step_count,
                duration_ms=duration_ms,
                model_results=model_results,
            ),
        )
        try:
            incomplete, reason = self._fanout.flush_barrier(item.run_id)
        except Exception as exc:
            incomplete, reason = True, type(exc).__name__
        if publish_failed and not incomplete:
            incomplete = True
            reason = "run.finished publish failed"
        reason = _finalize_reason(reason, self._redactor)
        telemetry = serialize_run_telemetry(
            model_results, incomplete, reason, redactor=self._redactor
        )
        if self._before_recording_commit is not None:
            self._before_recording_commit.wait()
        try:
            with self._store.transaction() as conn:
                self._finalize(
                    conn,
                    item,
                    outcome,
                    reply,
                    telemetry,
                    now=format_instant(self._clock.wall_utc()),
                )
        except Exception:
            self._coordinator.recording_enter_failed(projection)
            self._coordinator.notify_run_done(item.run_id)
            return
        self._coordinator.recording_publish_recorded_then_release()
        self._coordinator.notify_run_done(item.run_id)

    def _finalize(
        self,
        conn: sqlite3.Connection,
        item: WorkItem,
        outcome: RunOutcome,
        reply: Message | None,
        telemetry: str,
        *,
        now: str,
    ) -> None:
        phase = conn.execute(
            "SELECT phase FROM runs WHERE run_id = ?", (item.run_id,)
        ).fetchone()
        from_phase = "running" if phase is None else phase[0]
        if from_phase == "finished":
            conn.commit()
            return
        revision = schema.allocate_activity_revision(conn)
        schema.update_run_phase(
            conn,
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
                conn,
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
                    assistant = text_message("assistant", CONTROLLED_FAILURE_TEXT)
                stored_text = self._redactor.redact_text(
                    message_plain_text(assistant)
                )
                if stored_text != message_plain_text(assistant):
                    assistant = text_message("assistant", stored_text)
                _insert_log_message(
                    conn,
                    session_id=item.session_id,
                    role="assistant",
                    message=assistant,
                    source=item.request.gateway,
                    created_at=now,
                    run_id=item.run_id,
                )
        conn.commit()


def _finalize_reason(
    reason: str | None, redactor: Redactor | None
) -> str | None:
    """Bound and centrally redact the machine-judgeable barrier reason."""
    if reason is None:
        return None
    text = reason[:_REASON_LIMIT]
    if redactor is not None:
        try:
            text = redactor.redact_text(text)
        except Exception:
            text = "trace_incomplete"
    return text


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
