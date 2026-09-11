"""Reserve the admission slot, capture config, persist accepted, hand off.

Admission touches the Host only through two narrow seams, the same shape the
RunRecorder already uses:

- an :class:`AdmissionCoordinator` -- the same-lock admission observation,
  atomic lease transitions, work handoff, and result publication. The 409/503
  orderings live on the other side of this seam and cannot be bypassed here;
- the Host-owned :class:`~agent_alfred.runtime.recording.RecordingStore` --
  the write connection under its lock, with the caller owning every
  transaction.

It never reads or writes the Host's private state, so the state machine's
invariants (the lease holds until recording settles; the failed state lands
before anything answers 503) are structural, not conventions.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import Final, Literal, Protocol

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.model import (
    EndpointUnconfigured,
    ModelClientFactory,
    ModelUnsupported,
)
from agent_alfred.redact import Redactor
from agent_alfred.resource_rollback import (
    ResumableRollback,
    RollbackSlot,
    dominant_error,
)
from agent_alfred.runtime.config import ConfigSnapshotProvider, InvalidProbeTarget
from agent_alfred.runtime.recording import RecordingStore
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.runtime.work import (
    AdmissionObservationKind,
    ReserveKind,
    SubmitRequest,
    SubmitResult,
    WorkItem,
)
from agent_alfred.settings import Settings

_PROCESS_CONTROL = (KeyboardInterrupt, SystemExit, GeneratorExit)
_DurableUnstartedState = Literal["absent", "finalized", "execution_owned", "failed"]
# How far one submission got before it failed. The recovery it needs is a
# function of this, so it is a closed set rather than a free string.
_AdmissionStage = Literal["reserving", "client", "persisting", "publishing"]


def _classify_unstarted_row(
    row: tuple[str, str | None, str | None, str] | None,
) -> _DurableUnstartedState:
    """Interpret the same durable evidence in a read or a write transaction."""
    if row is None:
        return "absent"
    phase, outcome, started_at, admission_state = row
    if admission_state == "admitted":
        return "execution_owned"
    if phase == "finished" and outcome == "interrupted" and started_at is None:
        return "finalized"
    if phase != "accepted":
        return "execution_owned"
    return "failed"


class _SettledHandoff:
    """A caller-reachable settlement of who owns one interrupted handoff.

    ``admission_recover_handoff`` answers from the coordinator's monotonic
    publication decision, but a caller that holds that answer only in a
    return register loses it to any asynchronous exception landing before
    its own store. This slot exists before the call, so the answer is stored
    one frame lower instead of two: an interrupted caller resumes from the
    recorded fact rather than reading a missing return as "nothing was
    published", which would drive a worker-claimed Run through the unstarted
    finalizer and reopen admission underneath the worker that owns it.

    ``settle`` is a pre-built C-backed ``setattr`` callback rather than a
    Python method. The lower layer therefore stores the answer before Python
    regains an instruction boundary; recovery never needs another call to
    rediscover an answer it already produced.
    """

    _UNSETTLED: Final = object()

    def __init__(self) -> None:
        self._answer: bool | None | object = self._UNSETTLED
        self.settle: Callable[[bool | None], None] = partial(
            setattr, self, "_answer"
        )

    @property
    def settled(self) -> bool:
        return self._answer is not self._UNSETTLED

    @property
    def owner_is_execution(self) -> bool | None:
        """``True`` once execution owns the run; ``None`` while unsettled."""
        return None if self._answer is self._UNSETTLED else self._answer

class AdmissionCleanupOwner:
    """Persistent, serialized ownership of an abandoned submit's two exits."""

    _RESULT_SLOT: Final = "result_slot"
    _HANDOFF: Final = "handoff"

    def __init__(
        self,
        *,
        discard_result_slot: Callable[[], None],
        complete_handoff: Callable[[], None],
    ) -> None:
        self._lock = threading.Lock()
        self._slot = RollbackSlot()
        self._result_owner = ResumableRollback()
        self._handoff_owner = ResumableRollback()
        self._result_owner.own(object(), discard_result_slot)
        self._handoff_owner.own(object(), complete_handoff)
        # ``RollbackSlot`` retries independent owners in reverse registration
        # order. Result retirement remains first, matching the public recovery
        # order, while a failure in either branch cannot suppress the other.
        self._slot.begin(self._handoff_owner)
        self._slot.begin(self._result_owner)

    @property
    def complete(self) -> bool:
        with self._lock:
            return self._complete_locked()

    def retry_step(
        self, step: Literal["result_slot", "handoff"]
    ) -> tuple[bool, BaseException | None]:
        """Retry one public step and preserve its exact close progress."""
        with self._lock:
            owner = (
                self._result_owner
                if step == self._RESULT_SLOT
                else self._handoff_owner
            )
            if not self._slot.owns(owner):
                return True, None
            complete = owner.retry()
            error = owner.process_control or next(iter(owner.errors), None)
            if complete:
                self._slot.complete(owner)
            return complete, error

    def retry_all(self) -> tuple[bool, BaseException | None]:
        """Let the Host resume every still-owned branch during close."""
        with self._lock:
            first_error: BaseException | None = None
            for owner in (self._result_owner, self._handoff_owner):
                if not self._slot.owns(owner):
                    continue
                complete = owner.retry()
                error = owner.process_control or next(iter(owner.errors), None)
                first_error = dominant_error(first_error, error)
                if complete:
                    self._slot.complete(owner)
            return self._complete_locked(), first_error

    def _complete_locked(self) -> bool:
        return not self._slot.owns(
            self._result_owner
        ) and not self._slot.owns(self._handoff_owner)


class AdmissionCoordinator(Protocol):
    """The lease transitions, handoff, and publication admission may drive."""

    def admission_observe(
        self,
    ) -> tuple[AdmissionObservationKind, RuntimeSnapshot]:
        """Observe an immediate refusal, or permission to prepare a Run.

        This has no side effects. An admissible observation is not a lease;
        admission must still call :meth:`admission_reserve` after preparing
        the Run so a concurrent winner cannot slip through the capture window.
        """

    def admission_reserve(
        self,
        run_id: str,
        summary: ActiveRunSummary,
        *,
        wait_for_result: bool,
    ) -> tuple[ReserveKind, RuntimeSnapshot]:
        """Reserve the lease and publish the busy card, or say why neither
        happened. The two answers are one atomic step: a refused submit
        must be able to render the card of whoever holds the lease, and a
        reserved one must never be observable without its card."""

    def admission_release(self, run_id: str) -> None:
        """Take back the lease and the card this run_id published."""

    def admission_recover_release(self, run_id: str) -> None:
        """Resume a pre-handoff release whose public call was interrupted."""

    def admission_close_idle(self, run_id: str) -> None:
        """Reopen admission only if this run still owns the busy card."""

    def admission_fail_recording(
        self, summary: ActiveRunSummary, projection: UnrecordedTerminalProjection
    ) -> None: ...

    def admission_discard_result_slot(self, run_id: str) -> None:
        """Discard a waiter/result pair that no caller can consume."""

    def admission_publish_handoff(self, item: WorkItem) -> None:
        """Publish work and retire its close fence as one coordinator step."""

    def admission_recover_handoff(
        self,
        run_id: str,
        *,
        settle: Callable[[bool | None], None],
    ) -> None:
        """Write interrupted publication's owner into a caller-held slot."""

    def admission_complete_handoff(self, run_id: str) -> None:
        """Retire the close fence after publication or recovery is complete."""

    def admission_retain_cleanup(
        self, run_id: str, owner: AdmissionCleanupOwner
    ) -> AdmissionCleanupOwner:
        """Make an abandoned submission's cleanup Host-reachable."""

    def admission_retire_cleanup(
        self, run_id: str, owner: AdmissionCleanupOwner
    ) -> None:
        """Forget a cleanup owner only after both branches have settled."""

    def publish_run_result(self, run_id: str, result: LoopResult) -> None: ...

    def notify_run_done(self, run_id: str) -> None: ...


class RunAdmission:
    def __init__(
        self,
        *,
        clock: Clock,
        settings: Settings,
        redactor: Redactor,
        factory: ModelClientFactory,
        snapshot_provider: ConfigSnapshotProvider,
        database: RecordingStore,
        coordinator: AdmissionCoordinator,
        bind_run: Callable[[sqlite3.Connection, WorkItem], None] | None = None,
    ):
        self._clock = clock
        self._settings = settings
        self._redactor = redactor
        self._factory = factory
        self._snapshot_provider = snapshot_provider
        self._database = database
        self._coordinator = coordinator
        self._bind_run = bind_run

    def submit(self, request: SubmitRequest) -> SubmitResult:
        observed, snapshot = self._coordinator.admission_observe()
        if observed != "admissible":
            return SubmitResult(kind=observed, snapshot=snapshot)

        creates_session = request.purpose == "chat" and request.session_id is None

        # Everything the lease's busy card needs is minted before the
        # reserve: run id, timestamp, the server-side session id, the
        # redacted preview and the summary itself. None of it is a decision
        # -- every decision (conflict, lease, publication) lives inside the
        # one reserve call below, which is what keeps the lease and the
        # card from ever being observable apart.
        run_id = uuid.uuid4().hex
        accepted_at = format_instant(self._clock.wall_utc())
        session_id = request.session_id
        if creates_session:
            session_id = uuid.uuid4().hex
        # The capture has to precede the preview it feeds: the key it
        # carries enters the shared redactor first, so the preview this
        # submit publishes -- in the summary and later in the database --
        # is redacted against its own key on the very first run. A capture
        # that fails never reaches the reserve, so there is nothing to
        # take back.
        if request.purpose == "inference_probe" and (
            type(request.endpoint_id) is not str
            or type(request.model_id) is not str
            or not request.endpoint_id
            or not request.model_id
        ):
            return SubmitResult(kind="invalid_probe_target")
        try:
            if request.purpose == "inference_probe":
                captured = self._snapshot_provider.capture(
                    stream=request.stream,
                    endpoint_id=request.endpoint_id,
                    model_id=request.model_id,
                )
            else:
                captured = self._snapshot_provider.capture(stream=request.stream)
            if request.purpose == "consolidation":
                captured = replace(
                    captured,
                    overall_deadline_s=self._settings.consolidation_deadline_s,
                )
            self._redactor.remember(captured.api_key, credential=True)
            self._redactor.remember(captured.retrieval_gate_api_key, credential=True)
        except InvalidProbeTarget:
            return SubmitResult(kind="invalid_probe_target")
        except Exception:
            return SubmitResult(kind="admission_failed")
        if request.purpose == "inference_probe" and not (
            captured.api_key or ""
        ).strip():
            return SubmitResult(kind="endpoint_unconfigured")
        summary = ActiveRunSummary(
            run_id=run_id,
            purpose=request.purpose,
            gateway=request.gateway,
            phase="accepted",
            session_id=session_id,
            prompt_preview=self._preview(request.message),
            started_at=None,
            recording_state=None,
        )
        # This one try is the lifetime of every owner acquired below. Stage
        # changes happen *before* the work they describe, so an asynchronous
        # BaseException between two calls still lands in a truthful recovery
        # branch. Host publication has its own pending/published decision, so
        # recovery never guesses whether a physically queued item may execute.
        stage: _AdmissionStage = "reserving"
        item: WorkItem | None = None
        try:
            kind, snapshot = self._coordinator.admission_reserve(
                run_id,
                summary,
                wait_for_result=request.wait_for_result,
            )
            if kind != "reserved":
                # Refused before the lease: the snapshot that came back already
                # carries whoever holds it, so the caller renders the same busy
                # card a known-busy client gets.
                return SubmitResult(kind=kind, snapshot=snapshot)

            stage = "client"
            client = self._factory.create(captured)
            item = WorkItem(
                run_id=run_id,
                request=request,
                snapshot=captured,
                client=client,
                session_id=session_id,
                prompt_preview=summary.prompt_preview,
                accepted_at=accepted_at,
            )
            stage = "persisting"
            with self._database.transaction() as conn:
                if creates_session:
                    schema.insert_session(
                        conn, session_id=session_id, created_at=accepted_at
                    )
                schema.insert_accepted_run(
                    conn,
                    run_id=run_id,
                    purpose=request.purpose,
                    session_id=session_id,
                    gateway=request.gateway,
                    accepted_at=accepted_at,
                    entry_surface_id=request.entry_surface_id,
                    prompt_preview=summary.prompt_preview,
                )
                if self._bind_run is not None:
                    self._bind_run(conn, item)
                conn.commit()

            stage = "publishing"
            self._coordinator.admission_publish_handoff(item)
            # Keep construction and RETURN_VALUE inside the ownership scope.
            # If process control lands after publication but before the caller
            # receives this id, recovery retires its unreachable result slot.
            return SubmitResult(
                kind="accepted",
                run_id=run_id,
                session_id=session_id,
                snapshot=snapshot,
            )
        except BaseException as exc:
            return self._recover_submission(
                failure=exc,
                stage=stage,
                run_id=run_id,
                item=item,
            )

    def _recover_submission(
        self,
        *,
        failure: BaseException,
        stage: _AdmissionStage,
        run_id: str,
        item: WorkItem | None,
    ) -> SubmitResult:
        """Discharge the submission owner, preserving its first failure."""
        if stage in {"reserving", "client"}:
            cleanup_error = self._release_reserved(run_id)
            if isinstance(failure, _PROCESS_CONTROL):
                raise failure
            if cleanup_error is not None:
                raise cleanup_error from failure
            if stage == "reserving":
                raise failure
            if isinstance(failure, ModelUnsupported):
                return SubmitResult(kind="model_unsupported")
            if isinstance(failure, EndpointUnconfigured):
                return SubmitResult(kind="endpoint_unconfigured")
            return SubmitResult(kind="admission_failed")

        assert item is not None
        settled = _SettledHandoff()
        first_error: BaseException | None = None
        while not settled.settled:
            try:
                self._coordinator.admission_recover_handoff(
                    run_id, settle=settled.settle
                )
            except BaseException as exc:
                first_error = dominant_error(first_error, exc)
                # A process-control interruption may have landed before the
                # lower layer stored its monotonic decision. Keep resolving
                # until that pre-existing slot owns the answer, then propagate
                # the first control signal below. An ordinary failure is not a
                # signal storm and leaves the unstarted reconciler to decide.
                if not isinstance(exc, _PROCESS_CONTROL):
                    break

        unstarted = _SettledHandoff()
        if settled.owner_is_execution is not True:
            try:
                unstarted.settle(
                    self.interrupt_unstarted(
                        item, acceptance_uncertain=stage == "persisting"
                    )
                )
            except BaseException as exc:
                first_error = dominant_error(first_error, exc)

        # A submit that cannot return an id owns no single-consumer result slot
        # or close fence. Publish one persistent owner into the Host before
        # either cleanup starts. Two immediate attempts preserve the public
        # behavior, but exhaustion no longer loses the work: close() resumes
        # this exact owner instead of waiting forever on the handoff fence.
        cleanup_owner, retain_error = self._retain_cleanup(run_id)
        first_error = dominant_error(first_error, retain_error)
        for cleanup_step in (
            AdmissionCleanupOwner._RESULT_SLOT,
            AdmissionCleanupOwner._HANDOFF,
        ):
            cleanup_error = self._resume_cleanup_step(
                cleanup_owner, cleanup_step
            )
            first_error = dominant_error(first_error, cleanup_error)
        if cleanup_owner.complete:
            try:
                self._coordinator.admission_retire_cleanup(run_id, cleanup_owner)
            except BaseException as exc:
                first_error = dominant_error(first_error, exc)

        if isinstance(failure, _PROCESS_CONTROL):
            raise failure
        if isinstance(first_error, _PROCESS_CONTROL):
            raise first_error from failure
        if settled.owner_is_execution or unstarted.owner_is_execution:
            raise failure
        if first_error is not None:
            raise first_error from failure
        kind = "admission_failed" if stage == "persisting" else "handoff_failed"
        return SubmitResult(
            kind=kind,
            run_id=run_id if kind == "handoff_failed" else None,
        )

    def _release_reserved(self, run_id: str) -> BaseException | None:
        """Release the pre-handoff owner without swallowing control signals."""
        try:
            self._coordinator.admission_release(run_id)
        except BaseException as exc:
            try:
                self._coordinator.admission_recover_release(run_id)
            except BaseException as retry_error:
                return dominant_error(exc, retry_error)
            return exc if isinstance(exc, _PROCESS_CONTROL) else None
        return None

    @staticmethod
    def _resume_idempotent(
        operation: Callable[[], object]
    ) -> BaseException | None:
        """Resume one public ownership step after one asynchronous interruption."""
        try:
            operation()
        except BaseException as first_error:
            try:
                operation()
            except BaseException as retry_error:
                return dominant_error(first_error, retry_error)
            if isinstance(first_error, _PROCESS_CONTROL):
                return first_error
        return None

    def _retain_cleanup(
        self, run_id: str
    ) -> tuple[AdmissionCleanupOwner, BaseException | None]:
        """Publish cleanup ownership, retrying a lost retain return edge."""
        candidate = AdmissionCleanupOwner(
            discard_result_slot=lambda: self._coordinator.admission_discard_result_slot(
                run_id
            ),
            complete_handoff=lambda: self._coordinator.admission_complete_handoff(
                run_id
            ),
        )
        first_error: BaseException | None = None
        while True:
            try:
                retained = self._coordinator.admission_retain_cleanup(
                    run_id, candidate
                )
            except _PROCESS_CONTROL as exc:
                first_error = dominant_error(first_error, exc)
                continue
            return retained, first_error

    @staticmethod
    def _resume_cleanup_step(
        owner: AdmissionCleanupOwner,
        step: Literal["result_slot", "handoff"],
    ) -> BaseException | None:
        """Try one owned step twice while leaving later retries reachable."""
        first_error: BaseException | None = None
        for _attempt in range(2):
            complete, error = owner.retry_step(step)
            first_error = dominant_error(first_error, error)
            if complete:
                if isinstance(first_error, _PROCESS_CONTROL):
                    return first_error
                return None
        return first_error or RuntimeError(f"{step} cleanup remains incomplete")

    def _preview(self, message: str) -> str:
        text = self._redactor.redact_text(message)
        limit = self._settings.prompt_preview_max_chars
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def interrupt_unstarted(
        self, item: WorkItem, *, acceptance_uncertain: bool = False
    ) -> bool:
        """The handoff failed before any execution: finalize the run as
        finished/interrupted (only a null started_at may show an execution
        pause) and reopen admission, or fail closed when even that fails.

        ``True`` means execution had already moved the durable row and owns
        settlement; recovery must not rewrite it as an unstarted failure.
        """
        durable_state, durable_error = self._reconcile_unstarted_durable(item)
        if durable_state == "execution_owned":
            if durable_error is not None:
                raise durable_error
            return True
        if durable_state == "absent" or (
            durable_state == "failed"
            and acceptance_uncertain
            and not self._database.available
        ):
            cleanup_error = self._release_reserved(item.run_id)
            if isinstance(durable_error, _PROCESS_CONTROL):
                cleanup_error = dominant_error(durable_error, cleanup_error)
            if cleanup_error is not None:
                raise cleanup_error
            return False

        first_error: BaseException | None = None
        if durable_state == "failed":
            state_error = self._resume_idempotent(lambda: self._fail_closed(item))
        else:
            state_error = self._resume_idempotent(
                lambda: self._coordinator.admission_close_idle(item.run_id)
            )
        first_error = dominant_error(first_error, state_error)
        try:
            self._publish_handoff_interrupted(item)
        except BaseException as exc:
            first_error = dominant_error(first_error, exc)
        if isinstance(durable_error, _PROCESS_CONTROL):
            raise durable_error
        if first_error is not None:
            raise first_error
        return False

    def _reconcile_unstarted_durable(
        self, item: WorkItem
    ) -> tuple[_DurableUnstartedState, BaseException | None]:
        """Read back an uncertain write and resume it once when necessary."""
        first_error: BaseException | None = None
        for _attempt in range(2):
            try:
                state = self._finish_unstarted_once(item)
            except BaseException as exc:
                first_error = dominant_error(first_error, exc)
                continue
            if isinstance(first_error, _PROCESS_CONTROL):
                return state, first_error
            return state, None
        try:
            observed = self._observe_unstarted_once(item)
        except BaseException as exc:
            first_error = dominant_error(first_error, exc)
        else:
            if observed != "failed":
                if isinstance(first_error, _PROCESS_CONTROL):
                    return observed, first_error
                return observed, None
        return "failed", first_error

    def _observe_unstarted_once(self, item: WorkItem) -> _DurableUnstartedState:
        """Classify an uncertain acceptance through the independent read seam."""
        with self._database.reading() as conn:
            row = conn.execute(
                "SELECT phase, outcome, started_at, admission_state "
                "FROM runs WHERE run_id = ?",
                (item.run_id,),
            ).fetchone()
        return _classify_unstarted_row(row)

    def _finish_unstarted_once(
        self, item: WorkItem
    ) -> Literal["absent", "finalized", "execution_owned"]:
        """Classify or durably finalize one work item in a single transaction."""
        now = format_instant(self._clock.wall_utc())
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT phase, outcome, started_at, admission_state "
                "FROM runs WHERE run_id = ?",
                (item.run_id,),
            ).fetchone()
            state = _classify_unstarted_row(row)
            if state != "failed":
                return state
            revision = schema.allocate_activity_revision(conn)
            schema.update_run_phase(
                conn,
                run_id=item.run_id,
                from_phase="accepted",
                to_phase="finished",
                activity_revision=revision,
                outcome="interrupted",
                finished_at=now,
                session_id=item.session_id,
            )
            conn.execute(
                """UPDATE runs SET admission_state = 'rejected'
                   WHERE run_id = ? AND admission_state = 'pending'""",
                (item.run_id,),
            )
            conn.commit()
        return "finalized"

    def _publish_handoff_interrupted(self, item: WorkItem) -> None:
        """The one result an unstarted Run can produce, published exactly
        once with its done notification."""
        def publish() -> None:
            self._coordinator.publish_run_result(
                item.run_id,
                LoopResult(
                    outcome="interrupted",
                    reply=None,
                    error="handoff_failed",
                    step_count=0,
                    duration_ms=0,
                ),
            )

        first_error = self._resume_idempotent(publish)
        notify_error = self._resume_idempotent(
            lambda: self._coordinator.notify_run_done(item.run_id)
        )
        first_error = dominant_error(first_error, notify_error)
        if first_error is not None:
            raise first_error

    def _fail_closed(self, item: WorkItem) -> None:
        """Even the interrupted finalize failed: recording_failed closes
        admission and keeps one bounded projection of the lost run."""
        summary = ActiveRunSummary(
            run_id=item.run_id,
            purpose=item.request.purpose,
            gateway=item.request.gateway,
            phase="finished",
            session_id=item.session_id,
            prompt_preview=item.prompt_preview,
            started_at=None,
            recording_state="failed",
            outcome="interrupted",
        )
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
        self._coordinator.admission_fail_recording(summary, projection)
