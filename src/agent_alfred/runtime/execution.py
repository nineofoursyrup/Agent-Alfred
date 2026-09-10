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
from dataclasses import replace
from typing import Protocol

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import (
    EventEnvelope,
    EventPayload,
    FanOutSink,
    RunStarted,
    SequencedEvent,
    StepStarted,
)
from agent_alfred.loop.assistant import Assistant, AssistantEvents
from agent_alfred.loop.budget import RunBudget
from agent_alfred.messages import (
    Message,
    TextBlock,
    blocks_from_jsonable,
    text_message,
)
from agent_alfred.model import ModelClient, ModelRef, ModelRequest, ModelResult
from agent_alfred.outcomes import RunOutcome
from agent_alfred.redact import Redactor
from agent_alfred.runtime.memory import RunMemory
from agent_alfred.runtime.recording import RecordingStore, RunRecorder
from agent_alfred.runtime.snapshot import ActiveRunSummary
from agent_alfred.runtime.telemetry import AttemptObservationFailed
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT, Settings
from agent_alfred.support_overrides import SupportRecorder


class ExecutionCoordinator(Protocol):
    """The execution-lifecycle transitions owned by the coordinator."""

    def execution_mark_running(
        self, started_at: str
    ) -> ActiveRunSummary | None: ...

    def execution_publish_step_started(
        self,
        fanout: FanOutSink,
        payload: StepStarted,
        envelope: EventEnvelope | None,
    ) -> SequencedEvent: ...

    def execution_mark_stopping(self) -> None:
        """Close admission before a control exception releases its lease."""


class _ExecutionEvents:
    """Publish Steps through the Host-owned visibility boundary.

    The FanOut remains the publication authority for domain events.  Only
    ``step.started`` advances the bounded active-Run snapshot at the same
    visibility boundary as its FanOut commit. All other events pass through
    unchanged.
    """

    def __init__(
        self, fanout: FanOutSink, coordinator: ExecutionCoordinator
    ) -> None:
        self._fanout = fanout
        self._coordinator = coordinator

    def emit(
        self, payload: EventPayload, envelope: EventEnvelope | None = None
    ) -> SequencedEvent:
        if isinstance(payload, StepStarted):
            return self._coordinator.execution_publish_step_started(
                self._fanout, payload, envelope
            )
        return self._fanout.emit(payload, envelope)

    def bind_origin(self, envelope: EventEnvelope | None) -> None:
        self._fanout.bind_origin(envelope)


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

    def __init__(
        self, client: ModelClient, observe=None, records=None, conversation_id=None,
    ):
        self._observe = observe
        self._records = records
        self._client = client
        self._conversation_id = conversation_id
        self.model_results: tuple[ModelResult, ...] = ()

    def respond(
        self,
        request: ModelRequest,
        *,
        events: AssistantEvents | None = None,
        deadline: float | None = None,
    ) -> ModelResult:
        from agent_alfred.model import ModelCallInterrupted

        if self._conversation_id is not None:
            request = replace(request, conversation_id=self._conversation_id)
        failure = None
        try:
            result = self._client.respond(request, events=events, deadline=deadline)
        except ModelCallInterrupted as interrupted:
            result = interrupted.result
            failure = interrupted.cause
        try:
            result = self._record(result, request, events)
        except BaseException as observation:
            if failure is None:
                raise
            # Bookkeeping is fatal, but may not override pending process control.
            if isinstance(failure, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                primary, secondary = failure, observation
            else:
                primary, secondary = observation, failure
            if primary is not secondary:
                # These failures arose in separate exception scopes. Preserve
                # their causes and append the old context without making a cycle.
                previous = primary.__context__
                tail = secondary
                seen = {id(primary), id(secondary)}
                while tail.__context__ is not None:
                    if id(tail.__context__) in seen:
                        break
                    tail = tail.__context__
                    seen.add(id(tail))
                tail.__context__ = (
                    previous if previous is not None and id(previous) not in seen
                    else None
                )
                primary.__context__ = secondary
            failure = primary
        if failure is not None:
            # Unwind outside the carrier's handler: preserve the original cause
            # chain rather than introducing a cycle through the carrier.
            raise failure
        return result

    def _record(self, result, request, events):
        result = replace(result, attempts=tuple(
            replace(attempt, model=request.model) for attempt in result.attempts
        ))
        self.model_results += (result,)
        if self._records is not None:
            self._records.append(result)
        if self._observe is not None:
            try:
                self._observe(result, events)
            except Exception as exc:
                raise AttemptObservationFailed(exc) from exc
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
        support_recorder: SupportRecorder | None = None,
        memory_service=None,
        factory=None,
        tools=None,
        file_tools=None,
        persona_tools=None,
        skill_tools=None,
    ):
        self._memory_service = memory_service
        self._skill_tools = skill_tools
        self._persona_tools = persona_tools
        self._file_tools = file_tools
        self._tools = tools
        self._factory = factory
        self._support_recorder = support_recorder
        self._clock = clock
        self._settings = settings
        self._redactor = redactor
        self._assistant = assistant
        self._fanout = fanout
        self._store = store
        self._recorder = recorder
        self._coordinator = coordinator
        self._work_queue = work_queue
        self._events: AssistantEvents = _ExecutionEvents(fanout, coordinator)
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
        all_results = []
        # A Session spans Runs and process restarts. Sessionless system Runs
        # get their own namespace; Step/Attempt identities never split it.
        conversation_id = (
            "session:" + item.session_id
            if item.session_id is not None
            else "run:" + item.run_id
        )
        ledger = _AttemptLedger(
            item.client,
            lambda result, events: (
                self._support_recorder.observe(
                    item.snapshot, item.run_id, result, events
                )
                if self._support_recorder is not None
                else None
            ),
            records=all_results,
            conversation_id=conversation_id,
        )

        def gate_ledger(client, snapshot):
            wrapped = _AttemptLedger(
                client,
                lambda result, events: (
                    self._support_recorder.observe(
                        snapshot, item.run_id, result, events
                    )
                    if self._support_recorder is not None
                    else None
                ),
                records=all_results,
                conversation_id=conversation_id,
            )
            return wrapped

        budget = RunBudget(self._settings.max_steps)
        try:
            # The clock is an injected collaborator and therefore belongs
            # inside the same terminal ownership scope as every later Run
            # step.  A BaseException here must still reach ``settle``.
            run_started = self._clock.monotonic()
            if self._file_tools is not None:
                self._file_tools.set_run_deadline(
                    run_started
                    + (
                        item.snapshot.overall_deadline_s
                        if item.snapshot.overall_deadline_s is not None
                        else 30.0
                    )
                )
            started_at = format_instant(self._clock.wall_utc())
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
                working_groups = ()
                history_exclusions = {}
            else:
                working_memory, working_groups, history_exclusions = (
                    self._load_working_memory(item.session_id)
                )
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
            if item.request.purpose == "chat" and self._file_tools is not None:
                command_result = self._file_tools.handle_command(item.request.message)
                if command_result is None and self._skill_tools is not None:
                    command_result = self._skill_tools.handle_command(
                        item.request.message
                    )
                if command_result is not None:
                    from agent_alfred.tools import ToolFailure

                    reply = text_message(
                        "assistant",
                        "\n".join(block.text for block in command_result.content),
                    )
                    outcome = (
                        "failed"
                        if isinstance(command_result, ToolFailure)
                        else "completed"
                    )
                    error = command_result.stop_reason
                    return
            if item.request.purpose == "chat" and self._file_tools is not None:
                self._file_tools.resume_pending()
                self._file_tools.set_run_deadline(
                    run_started + item.snapshot.overall_deadline_s
                    if item.snapshot.overall_deadline_s is not None
                    else float("inf")
                )
            memory = None
            if item.request.purpose == "chat":
                memory = RunMemory(
                    item=item,
                    clock=self._clock,
                    settings=self._settings,
                    service=self._memory_service,
                    factory=self._factory,
                    ledger_factory=gate_ledger,
                    answer_ledger=ledger,
                    events=self._events,
                    working_history_groups=working_groups,
                    recording_store=self._store,
                    history_exclusions=history_exclusions,
                    model_results=all_results,
                )
            if memory is not None and self._memory_service is not None:
                from agent_alfred.runtime.memory import (
                    InputEvidenceError,
                    memory_context,
                )

                registered = self._memory_service.forgetting.register_group(
                    item.run_id,
                    kind="run",
                    container_id=item.session_id,
                    evidence="complete",
                    evidence_id="input-tracking:" + item.run_id,
                    occurred_at=item.accepted_at,
                    context=memory_context(item),
                )
                if "error" in registered:
                    raise InputEvidenceError("input_evidence_unavailable")
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
                events=self._events,
                source=item.request.gateway,
                overall_deadline_s=(
                    None
                    if item.snapshot.overall_deadline_s is None
                    else max(
                        0.0,
                        item.snapshot.overall_deadline_s
                        - (self._clock.monotonic() - run_started),
                    )
                ),
                memory=memory,
                tools=self._tools if item.request.purpose == "chat" else None,
                tool_permission=item.memory_permission,
                tool_state=item.memory_telemetry,
                persona=self._persona_tools.current() if self._persona_tools else None,
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
            # Publish the doomed worker first: settle opens the admission
            # lease before this frame reaches run_loop's outer exception
            # handler, and accepting in that gap strands work in its queue.
            self._coordinator.execution_mark_stopping()
            outcome = "interrupted"
            error = type(exc).__name__
            reply = None
            raise
        except Exception as exc:
            if isinstance(exc, AttemptObservationFailed):
                exc = exc.cause
            from agent_alfred.runtime.input_budget import InputLimitExceeded
            from agent_alfred.runtime.memory import (
                InputDeadlineExceeded,
                InputEvidenceError,
                InputResolutionError,
            )

            outcome = "failed"
            error = type(exc).__name__
            if isinstance(exc, InputLimitExceeded):
                error = "input_limit_exceeded"
                reply = text_message("assistant", str(exc))
            from agent_alfred.stream_fallback import OverallDeadlineExceeded

            if isinstance(exc, OverallDeadlineExceeded):
                error = "overall_deadline"
                reply = text_message("assistant", "运行期限已到，未发送后续请求。")
            if isinstance(exc, InputDeadlineExceeded):
                error = "input_deadline_exceeded"
                reply = text_message("assistant", "本次请求期限已到，未发送该请求。")
            if isinstance(exc, InputResolutionError):
                error = "input_resolution_unavailable"
                reply = text_message(
                    "assistant", "输入登记待恢复，本次已停止；发送状态请查看运行详情。"
                )
                item.memory_telemetry["input_resolution_error"] = error
            if isinstance(exc, InputEvidenceError):
                error = "input_evidence_unavailable"
                reply = text_message(
                    "assistant", "输入来源或读取登记暂不可确认，本次已停止；"
                    "请检查存储状态后重试。已执行的动作不会自动重跑。"
                )
                item.memory_telemetry["input_evidence_error"] = error
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
            if self._file_tools is not None:
                self._file_tools.set_run_deadline(float("inf"))
            receipts = item.memory_telemetry.pop("system_receipts", [])
            if receipts and outcome != "interrupted":
                blocks = () if reply is None else reply.blocks
                reply = Message(
                    "assistant",
                    (*blocks, TextBlock("\n\n系统操作回执：\n" + "\n".join(receipts))),
                )
            record = getattr(self._coordinator, "record_connection_observation", None)
            if callable(record):
                record(item, outcome, error)
            # The one thing no exception may skip. run.finished, the
            # durability barrier, the database finalizer and the lease
            # release all happen inside settle(); a Run that reached this
            # point is decided, whatever brought it here.
            self._recorder.settle(
                item,
                outcome=outcome,
                reply=reply,
                error=error,
                step_count=max(step_count, budget.used),
                duration_ms=duration_ms,
                model_results=tuple(all_results),
            )

    def _load_working_memory(
        self, session_id: str | None
    ) -> tuple[tuple[Message, ...], tuple[str, ...], dict[str, int]]:
        if session_id is None:
            return (), (), {}
        excluded = {"incomplete": 0, "unsafe": 0, "round_limit": 0}
        with self._store.reading() as conn:
            rows = conn.execute(
                """SELECT r.run_id, u.content, a.content
                   FROM runs r
                   JOIN agent_log u ON u.run_id = r.run_id AND u.role = 'user'
                   JOIN agent_log a ON a.run_id = r.run_id AND a.role = 'assistant'
                   WHERE r.session_id = ? AND r.purpose = 'chat'
                     AND r.phase = 'finished'
                     AND r.outcome IN ('completed', 'failed', 'max_steps')
                     AND u.session_id = r.session_id
                     AND a.session_id = r.session_id
                   ORDER BY u.id DESC""",
                (session_id,),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE session_id=? AND purpose='chat' "
                "AND phase='finished'",
                (session_id,),
            ).fetchone()[0]
            legacy = conn.execute(
                "SELECT COUNT(*) FROM agent_log WHERE session_id=? AND run_id IS NULL",
                (session_id,),
            ).fetchone()[0]
            excluded["incomplete"] = total - len(rows) + legacy
        if self._memory_service is not None:
            from agent_alfred.runtime.memory import InputEvidenceError

            evaluated = self._memory_service.forgetting.evaluate_history(
                [row[0] for row in rows], purpose="working_window"
            )
            if "error" in evaluated:
                raise InputEvidenceError("input_evidence_unavailable")
            allowed = set(evaluated["allowed"])
            excluded["unsafe"] = len(rows) - len(allowed)
            rows = [row for row in rows if row[0] in allowed]
        excluded["round_limit"] = max(
            0, len(rows) - self._settings.working_memory_rounds
        )
        rows = rows[: self._settings.working_memory_rounds]
        messages: list[Message] = []
        groups: list[str] = []
        for run_id, user, assistant in reversed(rows):
            groups.append(run_id)
            for role, content in (("user", user), ("assistant", assistant)):
                messages.append(
                    Message(role=role, blocks=blocks_from_jsonable(json.loads(content)))
                )
        return tuple(messages), tuple(groups), excluded
