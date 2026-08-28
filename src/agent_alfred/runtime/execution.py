"""Run the accepted work item on the single execution thread."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from agent_alfred import schema
from agent_alfred.clock import format_instant
from agent_alfred.events import EventEnvelope, RunStarted
from agent_alfred.loop.budget import RunBudget
from agent_alfred.messages import (
    Message,
    blocks_from_jsonable,
    text_message,
)
from agent_alfred.model import ModelRef, ModelResult
from agent_alfred.outcomes import RunOutcome
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT


class RunExecutor:
    def __init__(self, host: Any):
        self._host = host

    def run_loop(self) -> None:
        h = self._host
        while True:
            item = h._queue.get()
            if item is None:
                return
            self.execute(item)

    def execute(self, item: WorkItem) -> None:
        h = self._host
        outcome: RunOutcome = "interrupted"
        reply: Message | None = None
        error: str | None = None
        step_count = 0
        duration_ms = 0
        model_results: tuple[ModelResult, ...] = ()
        started_at = format_instant(h._clock.wall_utc())
        try:
            with h._db_lock:
                revision = schema.allocate_activity_revision(h._conn)
                schema.update_run_phase(
                    h._conn,
                    run_id=item.run_id,
                    from_phase="accepted",
                    to_phase="running",
                    activity_revision=revision,
                    started_at=started_at,
                    session_id=item.session_id,
                )
                h._conn.commit()
            with h._lock:
                h._coord = "running"
                if h._active_summary is not None:
                    h._active_summary = replace(
                        h._active_summary,
                        phase="running",
                        started_at=started_at,
                    )
            h._states.replace(
                coordinator_state="running",
                active_run=h._active_summary,
            )
            envelope = EventEnvelope(
                ts=h._clock.monotonic(),
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
            h._fanout.emit(
                RunStarted(
                    user_message=text_message(
                        "user", h._redactor.redact_text(item.request.message)
                    )
                    if item.request.purpose == "chat"
                    else None,
                    working_memory_message_count=len(working_memory),
                    purpose=item.request.purpose,
                ),
                envelope,
            )
            budget = RunBudget(h._settings.max_steps)
            loop_result = h._assistant.respond(
                item.request.message,
                client=item.client,
                budget=budget,
                working_memory=working_memory,
                model=ModelRef(
                    endpoint_id=item.snapshot.endpoint_id,
                    model_id=item.snapshot.model_id,
                ),
                run_id=item.run_id,
                session_id=item.session_id,
                events=h._fanout,
                source=item.request.gateway,
                overall_deadline_s=item.snapshot.overall_deadline_s,
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
        h._recorder.settle(
            item,
            outcome=outcome,
            reply=reply,
            error=error,
            step_count=step_count,
            duration_ms=duration_ms,
            model_results=model_results,
        )

    def _load_working_memory(self, session_id: str | None) -> tuple[Message, ...]:
        h = self._host
        if session_id is None:
            return ()
        limit = h._settings.working_memory_rounds * 2
        with h._db_lock:
            rows = h._conn.execute(
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
