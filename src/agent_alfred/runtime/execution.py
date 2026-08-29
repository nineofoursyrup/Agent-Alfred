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
from agent_alfred.model import ModelClient, ModelRef, ModelRequest, ModelResult
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


# Process-control exceptions that keep unwinding after the Run is settled.
#
# SystemExit is the only one: it means the interpreter is going away, so
# nothing after it will ever run and hiding it would be a lie. It is re-raised
# from a finally-safe position -- the settle has already happened -- and the
# Host records that it has no execution thread left.
#
# KeyboardInterrupt is deliberately not in this set. Python delivers SIGINT
# to the main thread only, so a KeyboardInterrupt arriving here was raised by
# code, not by the user's Ctrl-C; cancelling the one execution thread over it
# would leave the Host silently unable to serve. It settles the Run as
# interrupted -- the outcome the index can prove -- and the Run's terminal
# state, not the exception's propagation, is what records it.
_CONTROL_EXCEPTIONS = (SystemExit,)


class _AttemptLedger:
    """The Run's own record of every ModelResult the loop really produced.

    The Run settles from this rather than from the loop's return value. A
    BaseException escaping the loop takes the return value with it, and the
    Attempts behind it were real network round-trips that were really
    billed -- losing them would understate the Run's cost. It wraps the outer
    edge of the client chain, so one entry is one Step; the retries and the
    streaming fallback a Step spent are already inside ``ModelResult.attempts``.
    """

    def __init__(self, client: ModelClient):
        self._client = client
        self.model_results: tuple[ModelResult, ...] = ()

    def respond(
        self,
        request: ModelRequest,
        *,
        events: FanOutSink | None = None,
        deadline: float | None = None,
    ) -> ModelResult:
        result = self._client.respond(request, events=events, deadline=deadline)
        self.model_results += (result,)
        return result


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
        self._stopped_by: BaseException | None = None

    @property
    def stopped_by(self) -> BaseException | None:
        """The control exception that unwound the execution thread, if any.

        A Run that died this way was already settled; what is left is the
        fact that this Host has no thread left to execute anything, which
        admission needs in order to answer honestly instead of accepting
        Runs into a queue nobody drains.
        """
        return self._stopped_by

    def run_loop(self) -> None:
        try:
            while True:
                item = self._work_queue.get()
                if item is None:
                    return
                self.execute(item)
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            self._stopped_by = exc
            raise

    def execute(self, item: WorkItem) -> None:
        outcome: RunOutcome = "interrupted"
        reply: Message | None = None
        error: str | None = None
        step_count = 0
        duration_ms = 0
        ledger = _AttemptLedger(item.client)
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
                client=ledger,
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
        except KeyboardInterrupt:
            # Cancellation: the index cannot prove a business outcome for
            # this Run, so it settles interrupted and the Host keeps serving.
            # The settle below runs first, in the finally.
            outcome = "interrupted"
            error = "interrupted"
            reply = None
        except _CONTROL_EXCEPTIONS as exc:
            # The process is going away. settle() runs from the finally, so
            # the Run is decided and the lease released before the unwind.
            outcome = "interrupted"
            error = type(exc).__name__
            reply = None
            raise
        except Exception as exc:
            outcome = "failed"
            error = type(exc).__name__
            if reply is None:
                reply = text_message("assistant", CONTROLLED_FAILURE_TEXT)
            del exc
        except BaseException as exc:
            # Not a control signal and not an ordinary failure: settling it as
            # interrupted states what the index can prove, and the type name
            # rides in run.finished, so nothing is swallowed. It is not
            # re-raised because killing the only execution thread over an
            # unrecognised fault would silently disable the assistant for a
            # Run that has just been recorded honestly.
            outcome = "interrupted"
            error = type(exc).__name__
            reply = None
            del exc
        finally:
            # The one thing no exception may skip. run.finished, the
            # durability barrier, the database finalizer and the lease
            # release all happen inside settle(); a Run that reached this
            # point is decided, whatever brought it here.
            self._recorder.settle(
                item,
                outcome=outcome,
                reply=reply,
                error=error,
                step_count=step_count,
                duration_ms=duration_ms,
                model_results=ledger.model_results,
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
