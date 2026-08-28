"""Finalizer, recovery, and the recording lease."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

from agent_alfred import schema
from agent_alfred.clock import format_instant
from agent_alfred.events import EventEnvelope, RunFinished
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import (
    Message,
    blocks_to_jsonable,
    message_plain_text,
    text_message,
)
from agent_alfred.model import ModelResult
from agent_alfred.outcomes import RunOutcome
from agent_alfred.runtime.snapshot import UnrecordedTerminalProjection
from agent_alfred.runtime.telemetry import serialize_run_telemetry
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT


class RunRecorder:
    def __init__(self, host: Any):
        self._host = host

    def recover(self) -> None:
        h = self._host
        now = format_instant(h._clock.wall_utc())
        with h._db_lock:
            rows = h._conn.execute(
                """SELECT run_id, session_id, phase, started_at
                   FROM runs WHERE phase != 'finished'"""
            ).fetchall()
            for run_id, session_id, phase, _started_at in rows:
                revision = schema.allocate_activity_revision(h._conn)
                schema.update_run_phase(
                    h._conn,
                    run_id=run_id,
                    from_phase=phase,
                    to_phase="finished",
                    activity_revision=revision,
                    outcome="interrupted",
                    finished_at=now,
                    session_id=session_id,
                )
            h._conn.commit()

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
        h = self._host
        if (
            item.request.purpose == "chat"
            and outcome == "failed"
            and reply is None
        ):
            reply = text_message("assistant", CONTROLLED_FAILURE_TEXT)
        envelope = EventEnvelope(
            ts=h._clock.monotonic(),
            run_id=item.run_id,
            session_id=item.session_id,
            step_index=None,
            attempt_id=None,
            node_id=None,
            source=item.request.gateway,
        )
        try:
            h._fanout.emit(
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
        with h._lock:
            h._coord = "recording_pending"
            if h._active_summary is not None:
                h._active_summary = replace(
                    h._active_summary,
                    phase="finished",
                    recording_state="pending",
                )
            h._states.replace(
                coordinator_state="recording_pending",
                active_run=h._active_summary,
                unrecorded_terminal_projection=projection,
            )
        h._results[item.run_id] = LoopResult(
            outcome=outcome,
            reply=reply,
            error=error,
            step_count=step_count,
            duration_ms=duration_ms,
            model_results=model_results,
        )
        try:
            incomplete, reason = h._fanout.flush_barrier(item.run_id)
        except Exception as exc:
            incomplete, reason = True, type(exc).__name__
        telemetry = serialize_run_telemetry(
            model_results, incomplete, reason, redactor=h._redactor
        )
        if h._before_recording_commit is not None:
            h._before_recording_commit.wait()
        try:
            self._finalize(item, outcome, reply, telemetry)
        except Exception:
            with h._db_lock:
                try:
                    h._conn.rollback()
                except Exception:
                    pass
            self._enter_recording_failed(projection)
            h._done[item.run_id].set()
            return
        self._publish_recorded_then_release()
        h._done[item.run_id].set()

    def _enter_recording_failed(
        self, projection: UnrecordedTerminalProjection
    ) -> None:
        h = self._host
        if h._before_recording_failed is not None:
            h._before_recording_failed.wait()
        failed = replace(projection, recording_state="failed")
        with h._lock:
            summary = h._active_summary
            if summary is not None:
                summary = replace(
                    summary, recording_state="failed", phase="finished"
                )
                h._active_summary = summary
            h._coord = "recording_failed"
            h._states.replace(
                coordinator_state="recording_failed",
                active_run=summary,
                unrecorded_terminal_projection=failed,
            )

    def _publish_recorded_then_release(self) -> None:
        h = self._host
        with h._lock:
            recorded = None
            if h._active_summary is not None:
                recorded = replace(h._active_summary, recording_state="recorded")
                h._active_summary = recorded
            h._states.replace(
                coordinator_state="recording_pending",
                active_run=recorded,
                unrecorded_terminal_projection=None,
            )
        if h._after_recorded_snapshot is not None:
            h._after_recorded_snapshot.wait()
        with h._lock:
            h._coord = "idle"
            h._active_summary = None
            h._states.replace(coordinator_state="idle", active_run=None)

    def _finalize(
        self,
        item: WorkItem,
        outcome: RunOutcome,
        reply: Message | None,
        telemetry: str,
    ) -> None:
        h = self._host
        now = format_instant(h._clock.wall_utc())
        with h._db_lock:
            phase = h._conn.execute(
                "SELECT phase FROM runs WHERE run_id = ?", (item.run_id,)
            ).fetchone()
            from_phase = "running" if phase is None else phase[0]
            if from_phase == "finished":
                h._conn.commit()
                return
            revision = schema.allocate_activity_revision(h._conn)
            schema.update_run_phase(
                h._conn,
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
                    h._conn,
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
                    stored_text = h._redactor.redact_text(
                        message_plain_text(assistant)
                    )
                    if stored_text != message_plain_text(assistant):
                        assistant = text_message("assistant", stored_text)
                    _insert_log_message(
                        h._conn,
                        session_id=item.session_id,
                        role="assistant",
                        message=assistant,
                        source=item.request.gateway,
                        created_at=now,
                        run_id=item.run_id,
                    )
            h._conn.commit()


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
