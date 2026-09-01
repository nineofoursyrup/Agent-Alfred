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

import uuid
from typing import Protocol

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.model import ModelClientFactory
from agent_alfred.redact import Redactor
from agent_alfred.runtime.config import ConfigSnapshotProvider
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

    def admission_close_idle(self) -> None: ...

    def admission_fail_recording(
        self, summary: ActiveRunSummary, projection: UnrecordedTerminalProjection
    ) -> None: ...

    def admission_discard_result_slot(self, run_id: str) -> None:
        """Discard a waiter/result pair that no caller can consume."""

    def publish_work_item(self, item: WorkItem) -> None: ...

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
    ):
        self._clock = clock
        self._settings = settings
        self._redactor = redactor
        self._factory = factory
        self._snapshot_provider = snapshot_provider
        self._database = database
        self._coordinator = coordinator

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
        try:
            captured = self._snapshot_provider.capture(stream=request.stream)
            self._redactor.remember(captured.api_key)
        except Exception:
            return SubmitResult(kind="admission_failed")
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

        try:
            client = self._factory.create(captured)
        except Exception:
            self._coordinator.admission_release(run_id)
            return SubmitResult(kind="admission_failed")

        try:
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
                conn.commit()
        except Exception:
            self._coordinator.admission_release(run_id)
            return SubmitResult(kind="admission_failed")

        item = WorkItem(
            run_id=run_id,
            request=request,
            snapshot=captured,
            client=client,
            session_id=session_id,
            prompt_preview=summary.prompt_preview,
            accepted_at=accepted_at,
        )
        try:
            self._coordinator.publish_work_item(item)
        except Exception:
            self.interrupt_unstarted(item)
            # The terminal result and done notification above remain visible
            # to their ordinary observers, but this submit will not return
            # ``accepted`` and therefore cannot give a caller a run_id to
            # wait on. Retire that unreachable single-consumer slot only now.
            self._coordinator.admission_discard_result_slot(run_id)
            return SubmitResult(kind="handoff_failed", run_id=run_id)

        return SubmitResult(
            kind="accepted",
            run_id=run_id,
            session_id=session_id,
            snapshot=snapshot,
        )

    def _preview(self, message: str) -> str:
        text = self._redactor.redact_text(message)
        limit = self._settings.prompt_preview_max_chars
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def interrupt_unstarted(self, item: WorkItem) -> None:
        """The handoff failed before any execution: finalize the run as
        finished/interrupted (only a null started_at may show an execution
        pause) and reopen admission, or fail closed when even that fails."""
        now = format_instant(self._clock.wall_utc())
        try:
            with self._database.transaction() as conn:
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
                conn.commit()
        except Exception:
            self._fail_closed(item)
            return
        self._coordinator.admission_close_idle()
        self._publish_handoff_interrupted(item)

    def _publish_handoff_interrupted(self, item: WorkItem) -> None:
        """The one result an unstarted Run can produce, published exactly
        once with its done notification."""
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
        self._coordinator.notify_run_done(item.run_id)

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
        self._publish_handoff_interrupted(item)
