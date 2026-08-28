"""Reserve the admission slot, capture config, persist accepted, hand off."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from typing import Any

from agent_alfred import schema
from agent_alfred.clock import format_instant
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    UnrecordedTerminalProjection,
)
from agent_alfred.runtime.work import SubmitRequest, SubmitResult, WorkItem


class RunAdmission:
    def __init__(self, host: Any):
        self._host = host

    def submit(self, request: SubmitRequest) -> SubmitResult:
        h = self._host
        with h._lock:
            snap = h._states.get()
            if h._coord == "recording_failed":
                return SubmitResult(
                    kind="recording_unavailable", snapshot=snap
                )
            if h._coord != "idle":
                return SubmitResult(kind="run_in_progress", snapshot=snap)
            run_id = uuid.uuid4().hex
            h._coord = "accepted"
            h._done[run_id] = threading.Event()

        session_id = request.session_id
        accepted_at = format_instant(h._clock.wall_utc())
        try:
            snapshot = h._snapshot_provider.capture(stream=request.stream)
            h._redactor.remember(snapshot.api_key)
            preview = self._preview(request.message)
            client = h._factory.create(snapshot)
        except Exception:
            self._release(run_id)
            return SubmitResult(kind="admission_failed")

        try:
            with h._db_lock:
                if request.purpose == "chat" and session_id is None:
                    session_id = uuid.uuid4().hex
                    schema.insert_session(
                        h._conn, session_id=session_id, created_at=accepted_at
                    )
                schema.insert_accepted_run(
                    h._conn,
                    run_id=run_id,
                    purpose=request.purpose,
                    session_id=session_id,
                    gateway=request.gateway,
                    accepted_at=accepted_at,
                    entry_surface_id=request.entry_surface_id,
                    prompt_preview=preview,
                )
                h._conn.commit()
        except Exception:
            with h._db_lock:
                try:
                    h._conn.rollback()
                except Exception:
                    pass
            self._release(run_id)
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
        with h._lock:
            h._active_summary = summary
        h._states.replace(coordinator_state="accepted", active_run=summary)

        item = WorkItem(
            run_id=run_id,
            request=request,
            snapshot=snapshot,
            client=client,
            session_id=session_id,
            prompt_preview=preview,
            accepted_at=accepted_at,
        )
        try:
            publisher = h._publish_work or h._queue.put_nowait
            publisher(item)
        except Exception:
            self.interrupt_unstarted(item)
            return SubmitResult(kind="admission_failed", run_id=run_id)

        return SubmitResult(
            kind="accepted",
            run_id=run_id,
            session_id=session_id,
            snapshot=h._states.get(),
        )

    def _preview(self, message: str) -> str:
        h = self._host
        text = h._redactor.redact_text(message)
        limit = h._settings.prompt_preview_max_chars
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def _release(self, run_id: str) -> None:
        h = self._host
        with h._lock:
            h._done.pop(run_id, None)
            h._coord = "idle"
            h._active_summary = None
        h._states.replace(coordinator_state="idle", active_run=None)

    def interrupt_unstarted(self, item: WorkItem) -> None:
        h = self._host
        now = format_instant(h._clock.wall_utc())
        try:
            with h._db_lock:
                revision = schema.allocate_activity_revision(h._conn)
                schema.update_run_phase(
                    h._conn,
                    run_id=item.run_id,
                    from_phase="accepted",
                    to_phase="finished",
                    activity_revision=revision,
                    outcome="interrupted",
                    finished_at=now,
                    session_id=item.session_id,
                )
                h._conn.commit()
        except Exception:
            with h._db_lock:
                try:
                    h._conn.rollback()
                except Exception:
                    pass
            self._fail_closed(item)
            return
        with h._lock:
            h._coord = "idle"
            h._active_summary = None
        h._states.replace(
            coordinator_state="idle",
            active_run=None,
            unrecorded_terminal_projection=None,
        )
        if item.run_id in h._done:
            h._results[item.run_id] = LoopResult(
                outcome="interrupted",
                reply=None,
                error="handoff_failed",
                step_count=0,
                duration_ms=0,
            )
            h._done[item.run_id].set()

    def _fail_closed(self, item: WorkItem) -> None:
        h = self._host
        summary = h._active_summary
        if summary is None:
            summary = ActiveRunSummary(
                run_id=item.run_id,
                purpose=item.request.purpose,
                gateway=item.request.gateway,
                phase="accepted",
                session_id=item.session_id,
                prompt_preview=item.prompt_preview,
                started_at=None,
                recording_state="failed",
            )
        else:
            summary = replace(summary, recording_state="failed")
        projection = UnrecordedTerminalProjection(
            run_id=item.run_id,
            purpose=item.request.purpose,
            outcome="interrupted",
            reply_text=None,
            error="handoff_failed",
            recording_state="failed",
            session_id=item.session_id,
            prompt_preview=item.prompt_preview,
        )
        with h._lock:
            h._active_summary = summary
            h._coord = "recording_failed"
            h._states.replace(
                coordinator_state="recording_failed",
                active_run=summary,
                unrecorded_terminal_projection=projection,
            )
        if item.run_id in h._done:
            h._results[item.run_id] = LoopResult(
                outcome="interrupted",
                reply=None,
                error="handoff_failed",
                step_count=0,
                duration_ms=0,
            )
            h._done[item.run_id].set()
