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
from collections.abc import Callable
from contextlib import contextmanager
from functools import partial
from typing import Any, Literal, Protocol

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
from agent_alfred.resource_rollback import (
    ResumableRollback,
    capture_call_result,
    dominant_error,
)
from agent_alfred.runtime.snapshot import UnrecordedTerminalProjection
from agent_alfred.runtime.telemetry import serialize_run_telemetry
from agent_alfred.runtime.work import WorkItem
from agent_alfred.settings import CONTROLLED_FAILURE_TEXT

_REASON_LIMIT = 500
_REDACTION_FAILURE_TEXT = "<redaction failed; text withheld>"


class RecordingUnavailable(RuntimeError):
    """The Store cannot authoritatively answer reads or accept writes."""


class RecordingCoordinator(Protocol):
    """The atomic coordinator transitions the recorder may trigger."""

    def recording_enter_pending(
        self, projection: UnrecordedTerminalProjection
    ) -> None: ...

    def recording_enter_failed(
        self, projection: UnrecordedTerminalProjection
    ) -> None: ...

    def recording_publish_recorded_then_release(self, run_id: str) -> None: ...

    def publish_run_result(self, run_id: str, result: LoopResult) -> None: ...

    def notify_run_done(self, run_id: str) -> None: ...


class RecordingStore:
    """The Host-owned write connection under its lock; the caller commits."""

    def __init__(self, conn: sqlite3.Connection, db_lock: threading.Lock):
        self._conn = conn
        self._db_lock = db_lock
        # A rollback failure leaves an open transaction whose contents can
        # neither be trusted nor safely hidden from later work on this same
        # connection. This fact is permanent for this Store instance; only a
        # reliably reconstructed connection can establish a new authority.
        self._poisoned = threading.Event()

    @property
    def available(self) -> bool:
        """Whether this connection remains authoritative in this process."""
        return not self._poisoned.is_set()

    def _require_available(self) -> None:
        if not self.available:
            raise RecordingUnavailable("recording store is unavailable")

    @property
    def transaction_in_progress(self) -> bool:
        """Inspection only; callers still need the shared admission capability."""
        return self._conn.in_transaction

    def validate_borrowed_transaction(self, conn: sqlite3.Connection) -> None:
        """An internal participant borrows the caller's connection and lock.

        This never acquires a second lock, commits, or rolls back. The caller
        already owns admission and the transaction; possession is not a public
        HTTP/model authorization mechanism.
        """
        self._require_available()
        if conn is not self._conn or not conn.in_transaction:
            raise ValueError("transaction_required")

    @contextmanager
    def transaction(self):
        with self._db_lock:
            self._require_available()
            try:
                yield self._conn
            finally:
                if self._conn.in_transaction:
                    try:
                        self._conn.rollback()
                    except BaseException:
                        # Publish poison before releasing _db_lock. The
                        # rollback error remains primary; Python chains the
                        # original body/commit error as its context.
                        self._poisoned.set()
                        raise

    @contextmanager
    def reading(self):
        """Read access under the write lock; no transaction is owned."""
        with self._db_lock:
            self._require_available()
            yield self._conn


_SettlementStep = Literal["pending", "result", "resolution", "notify"]
_SETTLEMENT_ORDER: tuple[_SettlementStep, ...] = (
    "pending",
    "result",
    "resolution",
    "notify",
)


class _RecordingSettlementOwner:
    """One ordered terminal publication that survives its worker frame."""

    def __init__(
        self,
        *,
        coordinator: RecordingCoordinator,
        item: WorkItem,
        projection: UnrecordedTerminalProjection,
        result: LoopResult,
    ) -> None:
        self._lock = threading.Lock()
        self._coordinator = coordinator
        self._item = item
        self._projection = projection
        self._result = result
        self._recorded: bool | None = None
        self._resolve_recording: Callable[[], bool] | None = None
        self._next = 0
        self._owners: tuple[ResumableRollback, ...] = tuple(
            self._owner_for(step) for step in _SETTLEMENT_ORDER
        )

    @property
    def complete(self) -> bool:
        with self._lock:
            return self._next == len(self._owners)

    def capture_recording_decision(self, resolve: Callable[[], bool]) -> None:
        """Own the resolver, then its decision, across both return edges."""
        with self._lock:
            if self._resolve_recording is None:
                self._resolve_recording = resolve
            self._capture_recording_decision()

    def _capture_recording_decision(self) -> None:
        if self._recorded is not None or self._resolve_recording is None:
            return
        capture_call_result(self, "_recorded", self._resolve_recording)

    def drive(
        self,
        *,
        through: _SettlementStep,
        attempts_per_step: int,
    ) -> tuple[bool, BaseException | None]:
        """Advance in order, retaining the first still-incomplete step."""
        with self._lock:
            stop = _SETTLEMENT_ORDER.index(through)
            first_error: BaseException | None = None
            while self._next <= stop and self._next < len(self._owners):
                owner = self._owners[self._next]
                step_complete = False
                for _attempt in range(attempts_per_step):
                    step_complete = owner.retry()
                    error = owner.process_control or next(
                        iter(owner.errors), None
                    )
                    first_error = dominant_error(first_error, error)
                    if step_complete:
                        break
                if not step_complete:
                    return False, first_error
                self._next += 1
            return self._next > stop, first_error

    def _owner_for(self, step: _SettlementStep) -> ResumableRollback:
        owner = ResumableRollback()
        owner.own(object(), lambda step=step: self._run(step))
        return owner

    def _run(self, step: _SettlementStep) -> None | bool:
        run_id = self._item.run_id
        if step == "pending":
            self._coordinator.recording_enter_pending(self._projection)
        elif step == "result":
            self._coordinator.publish_run_result(run_id, self._result)
        elif step == "resolution":
            self._capture_recording_decision()
            recorded = self._recorded
            if recorded is None:
                return False
            if recorded:
                self._coordinator.recording_publish_recorded_then_release(run_id)
            else:
                self._coordinator.recording_enter_failed(self._projection)
        else:
            self._coordinator.notify_run_done(run_id)
        return None


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
        self._settlement_lock = threading.Lock()
        self._pending_settlements: dict[str, _RecordingSettlementOwner] = {}

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
            # Pending is a v4 fact: logical handoff never committed. It is
            # not the same as missing evidence in a pre-v4 Run. Neither case
            # is automatically executed by this new process.
            conn.execute(
                """UPDATE runs SET admission_state = 'rejected'
                   WHERE admission_state = 'pending'"""
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
        """Drive every terminal owner to a decided, observable state.

        Sink, database, and coordinator calls are interruption points.  An
        exception raised by one of those collaborators belongs to settlement,
        not to the already completed Run body: it is contained here so it
        cannot kill the only worker after admission has reopened.  The one
        process-control exception that is allowed to unwind is the Run body's
        ``SystemExit``; :class:`RunExecutor` announces that fact before it
        enters this method, and Python resumes the original unwind only after
        this method has returned.
        """
        if (
            item.request.purpose == "chat"
            and outcome == "failed"
            and reply is None
        ):
            reply = text_message("assistant", CONTROLLED_FAILURE_TEXT)
        result = LoopResult(
            outcome=outcome,
            reply=reply,
            error=error,
            step_count=step_count,
            duration_ms=duration_ms,
            model_results=model_results,
            memory_telemetry=item.memory_telemetry,
        )
        reply_text = _projection_reply_text(reply, self._redactor)
        reply_withheld = reply is not None and reply_text is None
        projection = UnrecordedTerminalProjection(
            run_id=item.run_id,
            purpose=item.request.purpose,
            outcome=outcome,
            reply_text=_REDACTION_FAILURE_TEXT if reply_withheld else reply_text,
            reply_withheld=reply_withheld,
            error=_redact_projection_text(error, self._redactor),
            recording_state="pending",
            session_id=item.session_id,
            prompt_preview=_redact_projection_text(
                item.prompt_preview, self._redactor
            ),
        )
        owner = self._retain_settlement(item, projection, result)
        clock_failure: str | None = None
        try:
            terminal_ts = self._clock.monotonic()
        except BaseException as exc:  # noqa: BLE001 - settlement must continue
            terminal_ts = 0.0
            clock_failure = f"terminal clock raised {type(exc).__name__}"
        envelope = EventEnvelope(
            ts=terminal_ts,
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
        publish_failure = clock_failure
        try:
            self._fanout.emit(
                RunFinished(
                    finalization_reason=item.memory_telemetry.get(
                        "finalization_reason"
                    ),
                    not_executed_call_ids=tuple(
                        item.memory_telemetry.get("not_executed_call_ids", ())
                    ),
                    outcome=outcome,
                    reply=reply,
                    error=error,
                    step_count=step_count,
                    duration_ms=duration_ms,
                ),
                envelope,
            )
        except BaseException as exc:  # noqa: BLE001 - settlement owns it
            publish_failure = _merge_reasons(
                publish_failure,
                f"run.finished publish raised {type(exc).__name__}",
            )
            try:
                self._fanout.note_publish_failure(item.run_id, exc)
            except BaseException:  # noqa: BLE001 - best-effort bookkeeping
                pass
        self._drive_settlement(owner, through="pending", attempts_per_step=2)
        try:
            incomplete, reason = self._fanout.flush_barrier(item.run_id)
        except BaseException as exc:  # noqa: BLE001 - settlement owns it
            incomplete, reason = (
                True,
                f"run.finished flush raised {type(exc).__name__}",
            )
        if publish_failure is not None:
            incomplete = True
            reason = _merge_reasons(reason, publish_failure)
        if self._before_recording_commit is not None:
            try:
                self._before_recording_commit.wait()
            except BaseException as exc:  # noqa: BLE001 - testable wait seam
                incomplete = True
                reason = _merge_reasons(
                    reason,
                    f"recording gate interrupted by {type(exc).__name__}",
                )
        reason = _finalize_reason(reason, self._redactor)
        owner.capture_recording_decision(
            partial(
                self._reconcile_finalize,
                item,
                outcome=outcome,
                reply=reply,
                model_results=model_results,
                incomplete=incomplete,
                reason=reason,
            )
        )
        self._drive_settlement(
            owner, through="notify", attempts_per_step=2
        )
        self._retire_settlement(item.run_id, owner)

    def retry_pending_settlements(self) -> bool:
        """Let Host close resume every terminal publication the worker left."""
        with self._settlement_lock:
            pending = tuple(self._pending_settlements.items())
        complete = True
        process_control: BaseException | None = None
        for run_id, owner in pending:
            owner_complete, error = self._drive_settlement(
                owner, through="notify", attempts_per_step=1
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                process_control = process_control or error
            if owner_complete:
                self._retire_settlement(run_id, owner)
            else:
                complete = False
        if process_control is not None:
            raise process_control
        return complete

    def _retain_settlement(
        self,
        item: WorkItem,
        projection: UnrecordedTerminalProjection,
        result: LoopResult,
    ) -> _RecordingSettlementOwner:
        candidate = _RecordingSettlementOwner(
            coordinator=self._coordinator,
            item=item,
            projection=projection,
            result=result,
        )
        with self._settlement_lock:
            return self._pending_settlements.setdefault(item.run_id, candidate)

    @staticmethod
    def _drive_settlement(
        owner: _RecordingSettlementOwner,
        *,
        through: _SettlementStep,
        attempts_per_step: int,
    ) -> tuple[bool, BaseException | None]:
        try:
            return owner.drive(
                through=through, attempts_per_step=attempts_per_step
            )
        except BaseException as exc:  # noqa: BLE001 - retained for Host close
            return False, exc

    def _retire_settlement(
        self, run_id: str, owner: _RecordingSettlementOwner
    ) -> None:
        if not owner.complete:
            return
        with self._settlement_lock:
            if self._pending_settlements.get(run_id) is owner:
                self._pending_settlements.pop(run_id)

    def _reconcile_finalize(
        self,
        item: WorkItem,
        *,
        outcome: RunOutcome,
        reply: Message | None,
        model_results: tuple[ModelResult, ...],
        incomplete: bool,
        reason: str | None,
    ) -> bool:
        """Resolve a possibly committed terminal transaction by read-back."""
        for _attempt in range(2):
            try:
                # Retry from the Attempt ledger if serialization is interrupted.
                # Without telemetry the whole recording fails, never just its
                # accounting: an empty ledger cannot stand in for known usage.
                telemetry = serialize_run_telemetry(
                    model_results, incomplete, reason, redactor=self._redactor,
                    memory=item.memory_telemetry
                )
                with self._store.transaction() as conn:
                    self._finalize(
                        conn,
                        item,
                        outcome,
                        reply,
                        telemetry,
                        now=format_instant(self._clock.wall_utc()),
                    )
            except BaseException:  # noqa: BLE001 - outcome may be uncertain
                continue
            return True
        try:
            with self._store.reading() as conn:
                row = conn.execute(
                    "SELECT phase, outcome FROM runs WHERE run_id = ?",
                    (item.run_id,),
                ).fetchone()
        except BaseException:  # noqa: BLE001 - unavailable means fail closed
            return False
        return row == ("finished", outcome)

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
        except BaseException:  # noqa: BLE001 - settlement must continue
            text = "trace_incomplete"
    return text


def _merge_reasons(first: str | None, second: str) -> str:
    if first is None:
        return second
    if second in first.split("; "):
        return first
    return f"{first}; {second}"


def _projection_reply_text(reply: Message | None, redactor: Redactor) -> str | None:
    """Return complete redacted text, or None when it must be withheld."""
    if reply is None:
        return None
    try:
        return redactor.redact_text(message_plain_text(reply))
    except BaseException:  # noqa: BLE001 - settlement must continue
        return None


def _redact_projection_text(
    text: str | None, redactor: Redactor
) -> str | None:
    """Redact browser-visible terminal text before the wire bounds it.

    The Host injects the same central Redactor used by FanOut. Repeating it
    here is intentionally idempotent for text that was already redacted. A
    failure withholds only the affected text field with a fixed, secret-free
    marker; it must neither expose the original nor abort Run settlement.
    """
    if text is None:
        return None
    try:
        return redactor.redact_text(text)
    except BaseException:  # noqa: BLE001 - settlement must continue
        return _REDACTION_FAILURE_TEXT


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
