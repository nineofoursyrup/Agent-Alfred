"""RuntimeHost: process-unique facade over admission, execution, and recording."""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import (
    EventEnvelope,
    FanOutSink,
    Notice,
    SequencedEvent,
    StepStarted,
)
from agent_alfred.loop.assistant import Assistant, LoopResult
from agent_alfred.model import ModelClientFactory
from agent_alfred.redact import Redactor
from agent_alfred.runtime import runs
from agent_alfred.runtime import sessions as session_store
from agent_alfred.runtime.admission import RunAdmission
from agent_alfred.runtime.config import (
    ConfigSnapshotProvider,
    SettingsBackedSnapshotProvider,
)
from agent_alfred.runtime.execution import RunExecutor
from agent_alfred.runtime.recording import (
    RecordingStore,
    RecordingUnavailable,
    RunRecorder,
)
from agent_alfred.runtime.snapshot import (
    ActiveRunSummary,
    CoordinatorState,
    RunStateStore,
    RuntimeSnapshot,
    UnrecordedTerminalProjection,
)
from agent_alfred.runtime.work import (
    AdmissionObservationKind,
    AdmissionRefusalKind,
    ReserveKind,
    SubmitKind,
    SubmitRequest,
    SubmitResult,
    WorkItem,
)
from agent_alfred.session_validity import SessionValidity
from agent_alfred.settings import Settings

__all__ = [
    "RuntimeHost",
    "SubmitKind",
    "SubmitRequest",
    "SubmitResult",
    "WorkItem",
]

# How long close() waits for a worker that may be inside a model round-trip,
# a retry, or a finalizer. The wait's outcome changes nothing about what may
# be torn down: the FanOut outlives every Run, so it closes strictly after
# the worker stops -- a wait that expires reports "not closed" instead.
_WORKER_JOIN_TIMEOUT_S = 5.0


class RuntimeHost:
    """Process-unique owner of seq, the write connection, and admission.

    The lifecycle below the Run loop is owned here as narrow, atomic
    transitions, and admission/execution/recorder never reach into this
    object's private state: they drive it through the narrow seams they
    actually need --

    - recorder: ``recording_enter_pending`` / ``recording_enter_failed`` /
      ``recording_publish_recorded_then_release`` / ``publish_run_result`` /
      ``notify_run_done`` plus a :class:`RecordingStore`;
    - admission: ``admission_observe`` (side-effect-free preflight) /
      ``admission_reserve`` (lease and busy card together) /
      ``admission_release`` / ``admission_close_idle`` /
      ``admission_fail_recording`` / ``publish_work_item``;
    - execution: ``execution_mark_running``.

    Each method below owns one state-machine invariant (authority before
    lease release; failed state before 503; 409 while the lease holds); the
    modules on the other side of the seams cannot bypass those orderings.
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        factory: ModelClientFactory,
        settings: Settings,
        clock: Clock,
        fanout: FanOutSink,
        process_instance_id: str,
        secrets: tuple[str, ...] = (),
        redactor: Redactor | None = None,
        publish_work: Callable[[WorkItem], None] | None = None,
        before_recording_commit: threading.Event | None = None,
        after_recorded_snapshot: threading.Event | None = None,
        before_recording_failed: threading.Event | None = None,
        snapshot_provider: ConfigSnapshotProvider | None = None,
        snapshot_listener: Callable[[RuntimeSnapshot], None] | None = None,
    ):
        self._conn = conn
        self._factory = factory
        self._settings = settings
        self._clock = clock
        self._fanout = fanout
        self._process_instance_id = process_instance_id
        self._redactor = redactor or Redactor(secrets)
        self._fanout.bind_redactor(self._redactor)
        self._assistant = Assistant(clock=clock, settings=settings)
        self._states = RunStateStore(process_instance_id, listener=snapshot_listener)
        self._lock = threading.Lock()
        self._fanout.bind_projection_boundary(self._lock)
        # Admission, execution and recording decisions move under self._lock.
        # The lifecycle below moves under its own lock, and the two are only
        # ever taken in this order (never the reverse), because a lifecycle
        # transition must be able to read admission state but no admission
        # decision may wait on a lifecycle transition that waits for a Run.
        self._lifecycle = threading.Lock()
        # Signalled when the last Run admitted before close() began has
        # reached the work queue -- or been given up on. Shares _lock: the
        # handoff is an admission fact, and close() has to wait on it.
        self._handoff = threading.Condition(self._lock)
        self._pending_handoff: set[str] = set()
        self._db_lock = threading.Lock()
        # A write from a door that is not ``submit`` -- a Session creation
        # today, memory or settings tomorrow. It shares ``_lock`` with every
        # admission decision, so "is a mutation in flight" and "is the lease
        # held" are one question with one answer rather than two flags that
        # can disagree for a moment.
        self._mutating = False
        self._coord: CoordinatorState = "idle"
        self._active_summary: ActiveRunSummary | None = None
        self._queue: queue.Queue[WorkItem | None] = queue.Queue()
        self._done: dict[str, threading.Event] = {}
        self._results: dict[str, LoopResult] = {}
        self._started = False
        self._start_error: BaseException | None = None
        self._closing = False
        self._closed = False
        self._stop_sent = False
        self._fanout_closed = False
        self._publish_work = publish_work
        self._before_recording_commit = before_recording_commit
        self._after_recorded_snapshot = after_recorded_snapshot
        self._before_recording_failed = before_recording_failed
        self._snapshot_provider = snapshot_provider or SettingsBackedSnapshotProvider(
            settings
        )
        self._store = RecordingStore(conn, self._db_lock)
        self._recorder = RunRecorder(
            clock=clock,
            fanout=fanout,
            redactor=self._redactor,
            store=self._store,
            coordinator=self,
            before_recording_commit=before_recording_commit,
        )
        self._admission = RunAdmission(
            clock=clock,
            settings=settings,
            redactor=self._redactor,
            factory=factory,
            snapshot_provider=self._snapshot_provider,
            database=self._store,
            coordinator=self,
        )
        self._executor = RunExecutor(
            clock=clock,
            settings=settings,
            redactor=self._redactor,
            assistant=self._assistant,
            fanout=fanout,
            store=self._store,
            recorder=self._recorder,
            coordinator=self,
            work_queue=self._queue,
        )
        self._worker = threading.Thread(
            target=self._executor.run_loop, name="run-worker", daemon=True
        )

    @property
    def process_instance_id(self) -> str:
        return self._process_instance_id

    @property
    def settings(self) -> Settings:
        """The immutable settings injected at assembly. Never re-read."""
        return self._settings

    @property
    def started(self) -> bool:
        """Whether this Host actually came up. False after a failed start."""
        return self._started

    @property
    def closed(self) -> bool:
        """Whether the sinks are released and the worker has stopped."""
        return self._closed

    @property
    def start_error(self) -> BaseException | None:
        """Why this Host did not come up, if it did not."""
        return self._start_error

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._states.get()

    def start(self) -> None:
        """Bring the Host up exactly once.

        Recovery belongs to the first start and to nothing else. Running it
        before the "already started" check meant a second ``start()``
        rewrote the executing Run's index row to finished/interrupted
        underneath the worker still running it, after which that worker's
        finalizer saw a terminal Run and silently dropped the reply -- the
        index, the returned result and the session record then disagreed.

        The lifecycle lock covers the check, the recovery, the thread start
        and the publication of the started flag as one step, so concurrent
        callers can neither double-recover nor double-start nor observe a
        half-started Host.
        """
        with self._lifecycle:
            if self._closing or self._closed:
                # Checked before "already started": a Host that came up and
                # was then closed cannot be restarted -- its worker thread
                # cannot be started twice and its sinks are released -- so
                # answering with a silent no-op would claim it is up.
                raise RuntimeError("a closed RuntimeHost cannot be started")
            if self._started:
                return
            if self._start_error is not None:
                # A stable refusal, not a retry: the first attempt's partial
                # result is a fact this Host has no way to account for, and
                # recovering twice would rewrite Runs a second time.
                raise RuntimeError(
                    "this RuntimeHost did not start and will not retry"
                ) from self._start_error
            try:
                self.recover()
                self._worker.start()
            except BaseException as exc:  # noqa: BLE001 - recorded, then raised
                # Honest state: a Host whose recovery or worker failed is
                # not started, and it must never be mistaken for one.
                self._start_error = exc
                raise
            self._started = True

    def close(self, timeout: float | None = None) -> bool:
        """Stop taking work, let the worker finish, then release the sinks.

        Returns True only when the Host is fully closed.

        The FanOut covers the whole process (ADR-0004), and the worker is the
        thread that emits into it: events, the durability barrier, and the
        finalizer all run there. Closing the sinks on a timer therefore cut
        the ground out from under a Run that was still being recorded. The
        order here is the other way round -- refuse new work, let the
        in-flight Run finish, wait for the worker to actually stop, and only
        then close the sinks, once.

        A bounded wait that expires returns False and closes nothing: an
        honest "not yet closed" beats a FanOut pulled out from under a live
        Run, and the caller may ask again.
        """
        deadline = time.monotonic() + (
            _WORKER_JOIN_TIMEOUT_S if timeout is None else timeout
        )
        with self._lifecycle:
            started = self._started
            self._closing = True
        # 1. Let every Run already admitted reach the queue. Admission is
        #    closed from here on, so this drains to zero -- and the stop
        #    sentinel is only posted afterwards, which is what keeps an
        #    accepted Run from being enqueued behind a sentinel nobody will
        #    read. The flag is raised under _lifecycle and the drain observed
        #    under _lock; admission_reserve holds _lock across the same pair,
        #    so no Run can slip in between the two.
        if not self._await_handoffs(deadline):
            return False
        with self._lifecycle:
            if not self._stop_sent:
                self._stop_sent = True
                self._queue.put(None)
        # 2. Wait for the worker. Nothing downstream is torn down before it
        #    stops. It stays a daemon thread so a wedged model call cannot
        #    hold the interpreter open, but close() says so rather than
        #    letting the exit hide an unfinished finalizer.
        if started:
            self._worker.join(max(0.0, deadline - time.monotonic()))
            if self._worker.is_alive():
                return False
        with self._lifecycle:
            if not self._fanout_closed:
                # The bit moves only behind a close() that returned: a sink
                # that raises leaves the FanOut unfinished, this exception
                # propagates, and the next close() asks it again. Setting
                # the bit first would make that retry skip the FanOut
                # entirely -- a Host reported closed with a sink nobody
                # ever closed.
                self._fanout.close()
                self._fanout_closed = True
            self._closed = True
        return True

    def _await_handoffs(self, deadline: float) -> bool:
        """Wait until no Run admitted before close() is still unpublished."""
        with self._handoff:
            while self._pending_handoff:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._handoff.wait(remaining)
        return True

    def recover(self) -> None:
        self._recorder.recover()

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        now = format_instant(self._clock.wall_utc())
        with self._store.transaction() as conn:
            schema.insert_session(
                conn, session_id=session_id, created_at=now
            )
            conn.commit()
        return session_id

    # -- the mutation gate's authority -------------------------------------
    #
    # These three are the whole interface the entry-side write gate needs,
    # and they are deliberately not a lock object handed out to callers: the
    # judgement has to be this Host's, taken under the same lock that
    # decides admission, because a lease spans the whole Run while a plain
    # write spans one call. A short lock around the call would answer "free"
    # for the entire window in which a Run is still holding the lease.

    def try_begin_mutation(self) -> str | None:
        """Reserve the one mutation slot, or say why not. Never waits.

        Returns ``None`` when the write may begin, and otherwise the reason
        in the coordinator's own vocabulary, so the caller can say
        something true rather than guessing from "no":

        - ``recording_unavailable`` -- admission is *closed*, not busy.
          After a recording failure nothing will be admitted until the
          process restarts (ADR-0026), so calling that "busy" would send
          the client back to try again in a moment.
        - ``mutation_in_flight`` -- the gate is merely held: a Run has the
          lease (``accepted``, ``running`` and ``recording_pending`` all
          still hold it) or another write is inside it.
        """
        with self._lock:
            if self._coord == "recording_failed" or not self._store.available:
                return "recording_unavailable"
            if self._mutating or self._coord != "idle":
                return "mutation_in_flight"
            self._mutating = True
            return None

    def end_mutation(self) -> None:
        with self._lock:
            self._mutating = False

    def mutation_in_flight(self) -> bool:
        """Whether a write from another door is inside the gate right now."""
        with self._lock:
            return self._mutating

    # -- admission lease transitions (the only writer of these states) -----

    def admission_observe(
        self,
    ) -> tuple[AdmissionObservationKind, RuntimeSnapshot]:
        """Read the current admission result and its snapshot atomically.

        This preflight never takes a lease. It only avoids doing configuration
        I/O when the coordinator can already give an authoritative refusal;
        an idle caller must still return through :meth:`admission_reserve`.
        """
        with self._lock:
            snapshot = self._states.get()
            refusal = self._admission_refusal_locked()
            if refusal is not None:
                return refusal, snapshot
            return "admissible", snapshot

    def _admission_refusal_locked(self) -> AdmissionRefusalKind | None:
        """Return the current refusal while the caller holds ``_lock``."""
        if self._executor.stopped_by is not None:
            return "admission_failed"
        with self._lifecycle:
            unstartable = (
                self._start_error is not None
                or self._closing
                or self._closed
            )
        if unstartable:
            return "admission_failed"
        if self._coord == "recording_failed" or not self._store.available:
            return "recording_unavailable"
        if self._coord != "idle":
            return "run_in_progress"
        if self._mutating:
            return "mutation_in_flight"
        return None

    def admission_reserve(
        self,
        run_id: str,
        summary: ActiveRunSummary,
        *,
        wait_for_result: bool,
    ) -> tuple[ReserveKind, RuntimeSnapshot]:
        """Atomically decide 409/503/reserve -- and, on reserve, publish
        the busy card in the same critical section.

        The lease, once reserved, holds until the recording settles: a
        second submit during recording_pending gets ``run_in_progress``; a
        recording-failed coordinator answers ``recording_unavailable``.

        ``summary`` is published with the lease or not at all. Reserving
        here and publishing the card later would leave a window in which a
        second submit is refused against a snapshot that still says "idle"
        -- a 409 whose body claims nothing is running, from a coordinator
        that is in fact busy. Every failure after the reserve takes both
        back through :meth:`admission_release` or
        :meth:`admission_close_idle`, so the card can never outlive the
        lease it was published with."""
        # _lock is held across the _lifecycle read below on purpose. close()
        # raises its flag under _lifecycle and then waits for the pending set
        # under _lock, so holding _lock here is what makes the two orderings
        # safe: either this reserve sees the flag and refuses, or it lands
        # its Run in the pending set before close() can observe that set and
        # post the stop sentinel. Lock order is _lock -> _lifecycle, never the
        # reverse; no path takes _lifecycle and then _lock.
        with self._lock:
            snap = self._states.get()
            refusal = self._admission_refusal_locked()
            if refusal is not None:
                return refusal, snap
            self._coord = "accepted"
            self._pending_handoff.add(run_id)
            if wait_for_result:
                self._done[run_id] = threading.Event()
            # The safe busy card and the authoritative snapshot move under
            # the same lock that takes the lease, so no refused submit can
            # observe the one without the other.
            self._active_summary = summary
            snapshot = self._states.replace(
                coordinator_state="accepted", active_run=summary
            )
            return "reserved", snapshot

    def admission_release(self, run_id: str) -> None:
        """Take back the lease after a failed admission; nothing was handed
        off. The busy card published with the lease goes with it: idle
        coordinator, no active run, one authoritative replacement -- a card
        that outlived the lease it was published with would keep telling
        every reader a Run is running that nobody is running.

        A Run that failed before the handoff still has to leave the pending
        set, or close() would wait out its whole budget for a handoff that
        was never going to happen.
        """
        with self._lock:
            self._done.pop(run_id, None)
            self._pending_handoff.discard(run_id)
            self._handoff.notify_all()
            self._coord = "idle"
            self._active_summary = None
            # The authoritative snapshot moves under the same lock as the
            # coordinator state, so no reader ever sees a snapshot that
            # disagrees with the decision the coordinator has made.
            self._states.replace(coordinator_state="idle", active_run=None)

    def admission_close_idle(self) -> None:
        """An unstarted run finalized interrupted; admission reopens."""
        with self._lock:
            self._coord = "idle"
            self._active_summary = None
            self._states.replace(
                coordinator_state="idle",
                active_run=None,
                unrecorded_terminal_projection=None,
            )

    def admission_fail_recording(
        self,
        fallback: ActiveRunSummary,
        projection: UnrecordedTerminalProjection,
    ) -> None:
        """recording_failed: keep the same projection, mark the summary failed,
        and close admission. Ordering is the #30 contract: the failed state is
        authoritative before anything answers 503."""
        with self._lock:
            summary = self._active_summary or fallback
            summary = replace(summary, recording_state="failed")
            self._active_summary = summary
            self._coord = "recording_failed"
            self._states.replace(
                coordinator_state="recording_failed",
                active_run=summary,
                unrecorded_terminal_projection=projection,
            )

    def publish_work_item(self, item: WorkItem) -> None:
        """Hand a prepared work item to the single execution thread.

        The handoff is accounted for either way it ends -- enqueued or
        refused -- so close() never waits on a Run that is no longer coming.
        """
        publisher = (
            self._publish_work
            if self._publish_work is not None
            else self._queue.put_nowait
        )
        try:
            publisher(item)
        finally:
            with self._handoff:
                # discard, not a counter: an admission that fails before the
                # handoff clears the same reservation through
                # admission_release, and the two must not cancel out into a
                # negative that hides a Run still in flight.
                self._pending_handoff.discard(item.run_id)
                self._handoff.notify_all()

    # -- execution transition ----------------------------------------------

    def execution_mark_running(self, started_at: str) -> ActiveRunSummary | None:
        with self._lock:
            self._coord = "running"
            if self._active_summary is not None:
                self._active_summary = replace(
                    self._active_summary,
                    phase="running",
                    started_at=started_at,
                )
            summary = self._active_summary
            # Same-lock snapshot replacement: the running state is never
            # observable apart from the summary that describes it.
            self._states.replace(coordinator_state="running", active_run=summary)
        return summary

    def execution_publish_step_started(
        self,
        fanout: FanOutSink,
        payload: StepStarted,
        envelope: EventEnvelope | None,
    ) -> SequencedEvent:
        """Commit a Step and expose its projection as one observable fact."""

        def project(published: SequencedEvent) -> None:
            summary = self._active_summary
            if (
                self._coord != "running"
                or summary is None
                or summary.run_id != published.envelope.run_id
            ):
                return
            self._active_summary = replace(
                summary, current_step=published.payload.step_index
            )
            self._states.replace(
                coordinator_state="running", active_run=self._active_summary
            )

        return fanout.emit_linearized(
            payload,
            envelope,
            boundary=self._lock,
            after_commit=project,
        )

    # -- recording-settlement transitions -----------------------------------

    def recording_enter_pending(
        self, projection: UnrecordedTerminalProjection
    ) -> None:
        """running -> recording_pending. The lease is NOT released here.

        The terminal outcome already exists -- it is the projection's -- so
        the finished summary copies it: a ``finished`` phase with an empty
        outcome would be a snapshot that cannot say what the Run concluded,
        precisely in the window (ADR-0026) where this projection is the only
        place the conclusion exists.
        """
        with self._lock:
            self._coord = "recording_pending"
            if self._active_summary is not None:
                self._active_summary = replace(
                    self._active_summary,
                    phase="finished",
                    recording_state="pending",
                    outcome=projection.outcome,
                )
            self._states.replace(
                coordinator_state="recording_pending",
                active_run=self._active_summary,
                unrecorded_terminal_projection=projection,
            )

    def recording_enter_failed(
        self, projection: UnrecordedTerminalProjection
    ) -> None:
        """recording_pending -> recording_failed, keeping the same projection.

        The same terminal outcome travels with it: the failure is a
        recording_state badge, not a rewriting of what the Run concluded,
        so the failed summary shows exactly what the pending one showed.
        """
        if self._before_recording_failed is not None:
            self._before_recording_failed.wait()
        with self._lock:
            failed_projection = replace(projection, recording_state="failed")
            summary = self._active_summary
            if summary is not None:
                summary = replace(
                    summary,
                    recording_state="failed",
                    phase="finished",
                    outcome=projection.outcome,
                )
                self._active_summary = summary
            self._coord = "recording_failed"
            self._states.replace(
                coordinator_state="recording_failed",
                active_run=self._active_summary,
                unrecorded_terminal_projection=failed_projection,
            )

    def recording_publish_recorded_then_release(self) -> None:
        """Authority first, lease second: the recorded snapshot becomes
        visible while admission is still closed, then the lease releases."""
        with self._lock:
            recorded = None
            if self._active_summary is not None:
                recorded = replace(
                    self._active_summary, recording_state="recorded"
                )
                self._active_summary = recorded
            self._states.replace(
                coordinator_state="recording_pending",
                active_run=recorded,
                unrecorded_terminal_projection=None,
            )
        if self._after_recorded_snapshot is not None:
            self._after_recorded_snapshot.wait()
        with self._lock:
            self._coord = "idle"
            self._active_summary = None
            self._states.replace(coordinator_state="idle", active_run=None)

    def publish_run_result(self, run_id: str, result: LoopResult) -> None:
        # Only a run whose done event exists has a waiter; anything else
        # would leak an unreadable result entry.
        with self._lock:
            if run_id in self._done:
                self._results[run_id] = result

    def notify_run_done(self, run_id: str) -> None:
        with self._lock:
            event = self._done.get(run_id)
            if event is not None:
                event.set()

    # -- public session read side (ADR-0027); callers never write SQL --

    def list_sessions(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> session_store.SessionInboxPage:
        with self._store.reading() as conn:
            return session_store.list_sessions(
                conn,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
                title_max_chars=self._settings.prompt_preview_max_chars,
            )

    def open_session(
        self,
        session_id: str,
        *,
        page_size: int,
        cursor: str | None = None,
    ) -> session_store.SessionMessagesPage:
        # The authoritative failure projection decides whether an in-flight
        # Run can still produce messages (see runtime.sessions).
        projection = self._states.get().unrecorded_terminal_projection
        recording_failed: frozenset[str] = frozenset()
        if (
            projection is not None
            and projection.recording_state == "failed"
        ):
            recording_failed = frozenset({projection.run_id})
        with self._store.reading() as conn:
            return session_store.open_session(
                conn,
                session_id=session_id,
                page_size=page_size,
                cursor=cursor,
                redactor=self._redactor,
                title_max_chars=self._settings.prompt_preview_max_chars,
                recording_failed_run_ids=recording_failed,
            )

    def session_exists(self, session_id: str | None) -> bool:
        """Whether a Session row exists. A question, not a projection.

        The Dashboard API asks this before admitting work for a named Session.
        It is deliberately the cheapest possible read: the answer is a row
        count, and no title, message or Run is derived from it. SSE uses the
        closed transport-only result below so storage loss stays observable.
        """
        with self._store.reading() as conn:
            if session_id is None:
                return False
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return row is not None

    def transport_session_validity(
        self, session_id: str | None
    ) -> SessionValidity:
        """Bounded Session truth for SSE startup and lifecycle patches.

        Ordinary reads remain fail-closed through ``session_exists``. This
        transport-only question preserves the closed unavailable result so
        a new stream cannot acquire response ownership without Store proof.
        """
        if session_id is None:
            return "invalid"
        if not self._store.available:
            return "unavailable"
        try:
            return "valid" if self.session_exists(session_id) else "invalid"
        except RecordingUnavailable:
            # Poison may land between the cheap availability check and the
            # guarded read. It is unavailable without a second read.
            return "unavailable"

    def list_runs(
        self,
        *,
        filter: str = "all",
        limit: int = 25,
        cursor: str | None = None,
    ) -> runs.RunPage:
        """The runs page: terminal Runs paged, the live Run pinned."""
        with self._store.reading() as conn:
            return runs.list_runs(
                conn,
                filter=filter,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
            )

    def locate_run(self, run_id: str, *, limit: int = 25) -> runs.RunPage | None:
        """The page a deep link to one Run should open on."""
        with self._store.reading() as conn:
            return runs.locate_run(
                conn, run_id=run_id, limit=limit, redactor=self._redactor
            )

    def list_session_chat_runs(
        self,
        *,
        session_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> runs.SessionChatRunsPage:
        """One Session's admitted chat Runs, keyset paged."""
        with self._store.reading() as conn:
            return runs.list_session_chat_runs(
                conn,
                session_id=session_id,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
                reply_max_chars=self._settings.prompt_preview_max_chars,
            )

    def mainbar_pairs(
        self,
        *,
        session_id: str,
        limit: int = runs.DEFAULT_MAINBAR_LIMIT,
        cursor: str | None = None,
    ) -> runs.MainBarPage:
        """The MainBar's message pairs for one Session's recorded chat Runs."""
        with self._store.reading() as conn:
            return runs.mainbar_pairs(
                conn,
                session_id=session_id,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
            )

    def note_sink_disabled(self, sink: str, stage: str) -> None:
        """A transport reporting that it stopped working.

        This is the one process-level fact a transport owns (#23 §9): the
        dispatcher or the replay ring failing is not any single connection's
        problem, so it is not a transport notice -- it is a domain notice,
        like any other. It lands in the trace, it takes a seq, and every
        browser is told, because a stream that silently stopped publishing
        is worse than one that says so.
        """
        self._fanout.emit(
            Notice(
                level="error",
                code="sink_disabled",
                detail=(("sink", sink), ("stage", stage)),
            )
        )

    def submit(self, request: SubmitRequest) -> SubmitResult:
        return self._admission.submit(request)

    def wait(self, run_id: str, timeout: float = 60.0) -> LoopResult:
        with self._lock:
            event = self._done.get(run_id)
        if event is None:
            raise KeyError(run_id)
        if not event.wait(timeout):
            raise TimeoutError(f"timed out waiting for run {run_id}")
        with self._lock:
            try:
                return self._results.pop(run_id)
            finally:
                # Result and waiter are one single-consumer slot. Even two
                # concurrent waits cannot leave the signalled Event behind.
                self._done.pop(run_id, None)
