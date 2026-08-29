"""Reserve the admission slot, capture config, persist accepted, hand off.

Admission touches the Host only through two narrow seams, the same shape the
RunRecorder already uses:

- an :class:`AdmissionCoordinator` -- the atomic lease transitions, the work
  handoff, and result publication. The 409/503 orderings live on the other
  side of this seam and cannot be bypassed from here;
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
    ReserveKind,
    SubmitRequest,
    SubmitResult,
    WorkItem,
)
from agent_alfred.settings import Settings


class AdmissionCoordinator(Protocol):
    """The lease transitions, handoff, and publication admission may drive."""

    def admission_reserve(
        self, run_id: str
    ) -> tuple[ReserveKind, RuntimeSnapshot]: ...

    def admission_mark_accepted(
        self, summary: ActiveRunSummary
    ) -> RuntimeSnapshot: ...

    def admission_release(self, run_id: str) -> None: ...

    def admission_close_idle(self) -> None: ...

    def admission_fail_recording(
        self, fallback: ActiveRunSummary, projection: UnrecordedTerminalProjection
    ) -> None: ...

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
        run_id = uuid.uuid4().hex
        kind, snapshot = self._coordinator.admission_reserve(run_id)
        if kind != "reserved":
            return SubmitResult(kind=kind, snapshot=snapshot)

        session_id = request.session_id
        accepted_at = format_instant(self._clock.wall_utc())
        try:
            captured = self._snapshot_provider.capture(stream=request.stream)
            self._redactor.remember(captured.api_key)
            preview = self._preview(request.message)
            client = self._factory.create(captured)
        except Exception:
            self._coordinator.admission_release(run_id)
            return SubmitResult(kind="admission_failed")

        try:
            with self._database.transaction() as conn:
                if request.purpose == "chat" and session_id is None:
                    session_id = uuid.uuid4().hex
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
                    prompt_preview=preview,
                )
                conn.commit()
        except Exception:
            self._coordinator.admission_release(run_id)
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
        snapshot = self._coordinator.admission_mark_accepted(summary)

        item = WorkItem(
            run_id=run_id,
            request=request,
            snapshot=captured,
            client=client,
            session_id=session_id,
            prompt_preview=preview,
            accepted_at=accepted_at,
        )
        try:
            self._coordinator.publish_work_item(item)
        except Exception:
            self.interrupt_unstarted(item)
            return SubmitResult(kind="admission_failed", run_id=run_id)

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
            phase="accepted",
            session_id=item.session_id,
            prompt_preview=item.prompt_preview,
            started_at=None,
            recording_state="failed",
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
