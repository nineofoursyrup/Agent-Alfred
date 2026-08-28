"""Run the accepted work item on the single execution thread.

Execution touches the Host only through an :class:`ExecutionCoordinator` seam
(the running transition) plus the collaborators it is constructed with: the
clock, settings, redactor, assistant, event fan-out, the shared
:class:`~agent_alfred.runtime.recording.RecordingStore` database seam, the
recorder that settles the run, and the work queue. It never reads or writes
the Host's private state.
"""

from __future__ import annotations

import json
import queue
from typing import Protocol

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import EventEnvelope, FanOutSink, RunStarted
from agent_alfred.loop.assistant import Assistant
from agent_alfred.loop.budget import RunBudget
from agent_alfred.messages import (
    Message,
    blocks_from_jsonable,
    text_message,
)
from agent_alfred.model import ModelRef, ModelResult
from agent_alfred.outcomes import RunOutcome
from agent_alfred.redact import Redactor
from agent_alfred.runtime.recording import RecordingStore, RunRecorder
from agent_alfred.runtime.snapshot import ActiveRunSummary
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT, Settings


class ExecutionCoordinator(Protocol):
    """The running transition execution may trigger on the coordinator."""

    def execution_mark_running(
        self, started_at: str
    ) -> ActiveRunSummary | None: ...


class RunExecutor:
    def __init__(
        self,
        *,
        clock: Clock,
        settings: Settings,
        redactor: Redactor,
        assistant: Assistant,
        fanout: FanOutSink,
        store: RecordingStore,
        recorder: RunRecorder,
        coordinator: ExecutionCoordinator,
        work_queue: "queue.Queue[WorkItem | None]",
    ):
        self._clock = clock
        self._settings = settings
        self._redactor = redactor
        self._assistant = assistant
        self._fanout = fanout
        self._store = store
        self._recorder = recorder
        self._coordinator = coordinator
        self._work_queue = work_queue

    def run_loop(self) -> None:
        while True:
            item = self._work_queue.get()
            if item is None:
                return
            self.execute(item)

    def execute(self, item: WorkItem) -> None:
        outcome: RunOutcome = "interrupted"
        reply: Message | None = None
        error: str | None = None
        step_count = 0
        duration_ms = 0
        model_results: tuple[ModelResult, ...] = ()
        started_at = format_instant(self._clock.wall_utc())
        try:
            with self._store.transaction() as conn:
                revision = schema.allocate_activity_revision(conn)
                schema.update_run_phase(
                    conn,
                    run_id=item.run_id,
                    from_phase="accepted",
                    to_phase="running",
                    activity_revision=revision,
                    started_at=started_at,
                    session_id=item.session_id,
                )
                conn.commit()
            self._coordinator.execution_mark_running(started_at)
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
                        "user", self._redactor.redact_text(item.request.message)
                    )
                    if item.request.purpose == "chat"
                    else None,
                    working_memory_message_count=len(working_memory),
                    purpose=item.request.purpose,
                ),
                envelope,
            )
            budget = RunBudget(self._settings.max_steps)
            loop_result = self._assistant.respond(
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
                events=self._fanout,
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
        self._recorder.settle(
            item,
            outcome=outcome,
            reply=reply,
            error=error,
            step_count=step_count,
            duration_ms=duration_ms,
            model_results=model_results,
        )

    def _load_working_memory(self, session_id: str | None) -> tuple[Message, ...]:
        if session_id is None:
            return ()
        limit = self._settings.working_memory_rounds * 2
        with self._store.reading() as conn:
            rows = conn.execute(
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
