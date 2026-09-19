"""RuntimeHost: process-unique facade over admission, execution, and recording."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from functools import partial
from pathlib import Path

from agent_alfred import schema
from agent_alfred.clock import Clock, format_instant
from agent_alfred.connections import CredentialOverlay
from agent_alfred.events import (
    EventEnvelope,
    FanOutSink,
    Notice,
    SequencedEvent,
    StepStarted,
)
from agent_alfred.loop.assistant import Assistant, LoopResult
from agent_alfred.managed_state import ManagedStateLease
from agent_alfred.model import ModelClientFactory
from agent_alfred.redact import Redactor
from agent_alfred.resource_rollback import (
    BackgroundCloseStep,
    OwnedLock,
    ResumableRollback,
    RollbackSlot,
    dominant_error,
    thread_exit_confirmed,
    thread_start_effect_happened,
)
from agent_alfred.runtime import replies, runs
from agent_alfred.runtime import sessions as session_store
from agent_alfred.runtime.admission import AdmissionCleanupOwner, RunAdmission
from agent_alfred.runtime.config import (
    ConfigSnapshotProvider,
    SettingsBackedSnapshotProvider,
)
from agent_alfred.runtime.execution import RunExecutor
from agent_alfred.runtime.model_settings import (
    ModelSettingsError,
    ModelSettingsSnapshot,
    ModelSettingsStore,
)
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
    is_unaddressable_unstarted_handoff_failure,
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
from agent_alfred.support_overrides import (
    SupportOverrides,
    SupportRecorder,
    SupportRule,
)

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


class _HandoffCell:
    """One queued handoff whose publication decision survives interruption."""

    def __init__(self, run_id: str, store: RecordingStore) -> None:
        self.run_id = run_id
        self._store = store
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._state = "pending"
        self._item: WorkItem | None = None

    def publish(self, item: WorkItem) -> None:
        with self._lock:
            if self._state != "pending":
                raise RuntimeError(f"handoff {self.run_id!r} is not pending")
            self._item = item
            # The queue already owns this cell, but receive() cannot expose
            # its work yet. This commit is the logical handoff decision; a
            # lost return is reconciled from SQLite before any cancellation.
            with self._store.transaction() as conn:
                updated = conn.execute(
                    """UPDATE runs SET admission_state = 'admitted'
                       WHERE run_id = ? AND admission_state = 'pending'
                         AND phase = 'accepted'""",
                    (self.run_id,),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("handoff has no pending accepted Run")
                conn.commit()
            self._state = "published"
            self._ready.set()

    def recover(self, settle: Callable[[bool], None]) -> None:
        """Resolve an interrupted publisher into a caller-reachable slot."""
        with self._lock:
            if self._state == "pending" and self._item is not None:
                with self._store.reading() as conn:
                    row = conn.execute(
                        "SELECT admission_state FROM runs WHERE run_id = ?",
                        (self.run_id,),
                    ).fetchone()
                if row == ("admitted",):
                    self._state = "published"
            published = self._state == "published"
            if not published:
                self._state = "cancelled"
                self._item = None
            # ``publish`` may have changed the state before an asynchronous
            # exception skipped Event.set(). Recovery completes that step.
            self._ready.set()
            settle(published)

    def receive(self) -> WorkItem | None:
        self._ready.wait()
        with self._lock:
            return self._item if self._state == "published" else None


class _HandoffQueue:
    """Queue a cell first, then expose its item only after logical publish.

    If an asynchronous exception lands after ``Queue.put`` has physically
    inserted the cell, recovery resolves that same cell as cancelled and the
    worker skips it. If logical publication already happened, recovery sees
    the monotonic decision and leaves execution as the sole owner.
    """

    def __init__(self, store: RecordingStore) -> None:
        self._store = store
        self._queued: deque[_HandoffCell | WorkItem] = deque()
        self._not_empty = threading.Condition()
        self._cells_lock = threading.Lock()
        self._cells: dict[str, _HandoffCell] = {}
        # Stop is a monotonic queue fact, not an ordinary item. That makes a
        # retry after an interrupted request idempotent while still letting
        # get() drain every handoff already ahead of shutdown.
        self._stop_requested = False

    def reserve(self, run_id: str) -> None:
        with self._cells_lock:
            if run_id in self._cells:
                raise RuntimeError(f"handoff {run_id!r} is already reserved")
            self._cells[run_id] = _HandoffCell(run_id, self._store)

    def publish(self, item: WorkItem, *, enqueue: bool) -> None:
        with self._cells_lock:
            cell = self._cells.get(item.run_id)
        if cell is None:
            raise RuntimeError(f"handoff {item.run_id!r} is not reserved")
        if enqueue:
            # The cell, not the WorkItem, enters the queue. A BaseException
            # after this call therefore leaves a resolvable pending cell.
            self.put_nowait(cell)
        cell.publish(item)

    def put_nowait(self, item: _HandoffCell | WorkItem) -> None:
        """Append one item and wake the worker without Queue inheritance."""
        if isinstance(item, WorkItem):
            with self._cells_lock:
                cell = self._cells.get(item.run_id)
            if cell is None:
                raise RuntimeError("work has no reserved handoff")
            item = cell
        with self._not_empty:
            self._queued.append(item)
            self._not_empty.notify()

    def qsize(self) -> int:
        """Return the current physical handoff count for diagnostics."""
        with self._not_empty:
            return len(self._queued)

    def empty(self) -> bool:
        """Return whether no physical handoff is currently queued."""
        with self._not_empty:
            return not self._queued

    def recover(
        self,
        run_id: str,
        *,
        settle: Callable[[bool | None], None] | None = None,
    ) -> None:
        """Cancel pending work and optionally publish its monotonic owner."""
        with self._cells_lock:
            cell = self._cells.get(run_id)
        if cell is None:
            if settle is not None:
                settle(None)
            return
        cell.recover(settle or (lambda _published: None))
        # If publication was interrupted after enqueue but before its wakeup,
        # recovery also completes that physical notification.
        with self._not_empty:
            self._not_empty.notify_all()

    def retire(self, run_id: str) -> None:
        with self._cells_lock:
            self._cells.pop(run_id, None)

    def request_stop(self) -> bool:
        """Publish one idempotent stop fact and wake an empty-queue waiter."""
        with self._not_empty:
            if self._stop_requested:
                # An earlier call may have been interrupted after publishing
                # the fact but before its notification reached a waiter.
                self._not_empty.notify_all()
                return False
            self._stop_requested = True
            self._not_empty.notify_all()
            return True

    def get(self) -> WorkItem | None:
        while True:
            with self._not_empty:
                while not self._queued:
                    if self._stop_requested:
                        return None
                    self._not_empty.wait()
                queued = self._queued.popleft()
            item = queued.receive()
            if item is not None:
                return item


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
      ``admission_fail_recording`` / ``admission_discard_result_slot`` /
      ``admission_publish_handoff``;
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
        support_overrides: SupportOverrides | None = None,
        support_rule: SupportRule | None = None,
        model_settings: ModelSettingsStore | None = None,
        credentials: CredentialOverlay | None = None,
        audit_key=None,
        file_state=None,
        skill_builtin=None,
        chat_graph_factory=None,
        behaviour_store=None,
        routing_graph_builder=None,
        routing_fallback_checkpoint=None,
        aggregation_sources=None,
        aggregation_before_send=None,
        memory_notifier=None,
        extra_tools=(),
        tool_policies=None,
        tool_authorization_path=None,
    ):
        self._support_overrides = support_overrides or SupportOverrides()
        self._conn = conn
        self._routing_statistics_cleanup = RollbackSlot()
        self._routing_statistics_path = conn.execute(
            "PRAGMA database_list"
        ).fetchone()[2]
        self._factory = factory
        from agent_alfred.runtime.behaviour import BehaviourStore
        from agent_alfred.runtime.routing import build_routing_graph

        self._routing_graph_builder = routing_graph_builder or build_routing_graph
        self._routing_graph = None
        self._routing_generation = 0
        self._routing_error = None
        # A single reference publishes identity and immutable description together.
        # Observers never enter Run admission or wait for graph compilation.
        self._routing_publication = (None, 0)
        self._behaviour = behaviour_store or (
            BehaviourStore(file_state.path / "behaviour.json")
            if file_state is not None
            else None
        )
        self._settings = settings
        self._clock = clock
        self._fanout = fanout
        from agent_alfred.runtime.run_path import RunPathSink

        self._run_path = RunPathSink()
        self._fanout.add_sink(self._run_path)
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
        # reached the work queue -- or been given up on -- and whenever an
        # ordinary mutation finishes. Both are admission facts under _lock
        # that must settle before close() can release shared resources.
        self._handoff = threading.Condition(self._lock)
        self._pending_handoff: set[str] = set()
        self._admission_cleanups: dict[str, AdmissionCleanupOwner] = {}
        self._db_lock = threading.Lock()
        # A write from a door that is not ``submit`` -- a Session creation
        # today, memory or settings tomorrow. It shares ``_lock`` with every
        # admission decision, so "is a mutation in flight" and "is the lease
        # held" are one question with one answer rather than two flags that
        # can disagree for a moment.
        self._mutating = False
        self._mutation_owner = None
        self._implicit_mutation = threading.local()
        self._memory_run_permission = None
        self._coord: CoordinatorState = "idle"
        self._active_summary: ActiveRunSummary | None = None
        self._store = RecordingStore(conn, self._db_lock)
        from agent_alfred.memory.audit import AuditKey
        from agent_alfred.memory.commands import MemoryCommandService

        if audit_key is None:
            import secrets
            filename = conn.execute("PRAGMA database_list").fetchone()[2]
            if filename:
                audit_key = AuditKey.load_or_create(
                    Path(filename).parent / "memory-audit" / "audit.key", self._redactor
                )
            else:
                audit_key = AuditKey(secrets.token_hex(16), secrets.token_bytes(32))
        self._memory_service = MemoryCommandService(
            recording_store=self._store, audit_key=audit_key,
            clock=self._clock.wall_utc, admission=self,
            delete_barrier=self._fanout.checkpoint_barrier,
            memory_notifier=memory_notifier, file_state=file_state,
            process_instance_id=process_instance_id,
        )
        self.database_console = None
        self.trace_exports = None
        from agent_alfred.memory.consolidation import ConsolidationLimits

        self._memory_service.consolidation._limits = ConsolidationLimits(
            source_threshold=settings.consolidation_source_threshold,
            candidate_count_limit=settings.consolidation_candidate_count_limit,
            candidate_character_limit=settings.consolidation_candidate_character_limit,
            request_character_limit=settings.input_character_limit,
        )
        self._queue = _HandoffQueue(self._store)
        self._done: dict[str, threading.Event] = {}
        self._results: dict[str, LoopResult] = {}
        self._started = False
        self._worker_started: bool | None = False
        self._start_error: BaseException | None = None
        # Monotonic once execution has received process control. It moves
        # under the admission lock before recording can release the Run's
        # lease, closing the otherwise-idle window before run_loop records
        # the worker's terminal exception.
        self._executor_stopping = False
        self._closing = False
        self._closed = False
        self._stop_sent = False
        self._fanout_closed = False
        self._transport_close = BackgroundCloseStep("alfred-transport-close")
        self._owned_resources: ResumableRollback | None = None
        self._publish_work = publish_work
        self._before_recording_commit = before_recording_commit
        self._after_recorded_snapshot = after_recorded_snapshot
        self._before_recording_failed = before_recording_failed
        self._snapshot_provider = snapshot_provider or SettingsBackedSnapshotProvider(
            settings
        )
        self._model_settings = model_settings
        self._credentials = credentials
        self._configuration_lock = threading.RLock()
        self._connections_revision = 0
        self._connections_snapshot = None
        self._integration_application = "applied"
        self._catalog_scheduler_obj = None
        self._catalog_transport = None
        self._auth_probe_transport = None
        self._recorder = RunRecorder(
            clock=clock,
            fanout=fanout,
            redactor=self._redactor,
            store=self._store,
            coordinator=self,
            before_recording_commit=before_recording_commit,
            finalize_run=self._finalize_consolidation_run,
            notify_finalized_run=self._notify_consolidation_run,
            after_recorded=self._schedule_saved_chat,
        )
        self._admission = RunAdmission(
            clock=clock,
            settings=settings,
            redactor=self._redactor,
            factory=factory,
            snapshot_provider=self._snapshot_provider,
            database=self._store,
            coordinator=self,
            bind_run=self._bind_consolidation_retry,
            capture_aggregation=lambda: dict(
                persona=self._aggregation_persona.current(), settings=self._settings
            ),
        )
        from agent_alfred.tools import ToolRegistry
        from agent_alfred.tools.calendar import CalendarTools
        from agent_alfred.tools.files import FileTools
        from agent_alfred.tools.ledger import ExternalToolLedger
        from agent_alfred.tools.memory import MemoryTools
        from agent_alfred.tools.persona import PersonaTools
        from agent_alfred.tools.skills import SkillTools

        self._file_tools = FileTools(self._store, file_state, clock)

        persona_tools = PersonaTools(self._file_tools, settings)
        skill_tools = SkillTools(self._file_tools, builtin=skill_builtin)
        self._skill_catalog = skill_tools.catalog
        self._external_tools = ExternalToolLedger(self._store, clock)
        from agent_alfred.tools.metering import ToolMetering

        self._tool_metering = ToolMetering(self._store, clock)
        from agent_alfred.runtime.tool_history import ToolHistory

        self._tool_history = ToolHistory(self._store, self._redactor)
        from agent_alfred.runtime.accounting import AccountingSnapshots

        self._accounting = AccountingSnapshots(
            self._store,
            self._tool_metering,
            clock,
            process_instance_id,
            self._accounting_prices,
        )
        from agent_alfred.integrations import Integrations

        self._integrations = Integrations(self._credential_env(), clock, self._redactor)
        from agent_alfred.mcp import MCPBridge

        self._mcp = MCPBridge(
            file_state.path if file_state is not None else None,
            self._credential_env(), clock, self._redactor,
        )
        tools = ToolRegistry(
            (
                *CalendarTools(self._store, clock).declarations(),
                *MemoryTools(self._memory_service).declarations(),
                *self._file_tools.declarations(),
                *persona_tools.declarations(),
                *skill_tools.declarations(),
                *self._integrations.declarations(),
                *extra_tools,
            ),
            clock=clock,
            redactor=self._redactor,
            policies={**self._integrations.policies(), **(tool_policies or {})},
            external_ledger=self._external_tools,
            metering=self._tool_metering,
        )
        self._tools = tools
        from agent_alfred.tools.authorization import ToolAuthorization

        authorization_path = tool_authorization_path or (
            file_state.path / "tool_authorizations.json"
            if file_state is not None
            else None
        )
        self._tool_authorization = ToolAuthorization(
            authorization_path, tools, self._publish_tools
        )
        tools = self._tools
        if self._routing_graph is None:
            try:
                self._routing_graph = self._routing_graph_builder(self._tools)
                self._routing_generation += 1
                self._routing_publication = (
                    self._routing_graph, self._routing_generation
                )
            except Exception as error:
                from agent_alfred.resource_rollback import raise_if_rollback_pending

                raise_if_rollback_pending(error)
                self._routing_error = type(error).__name__
        from agent_alfred.aggregation.graph import build_graph
        from agent_alfred.aggregation.sources import AggregationSources, declarations

        source_service = AggregationSources(
            self._memory_service, self._store, project=self._redactor.redact_jsonable
        )
        self._aggregation_sources = (
            aggregation_sources(source_service)
            if callable(aggregation_sources)
            else aggregation_sources or source_service
        )
        self._aggregation_persona = persona_tools
        self._aggregation_tools = ToolRegistry(
            declarations(
                self._aggregation_sources, project=self._redactor.redact_jsonable
            ),
            clock=clock,
            redactor=self._redactor,
            metering=self._tool_metering,
        )
        self._aggregation_graph = build_graph(self._aggregation_tools)
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
            memory_service=self._memory_service,
            tools=tools,
            file_tools=self._file_tools,
            persona_tools=persona_tools,
            skill_tools=skill_tools,
            chat_graph_factory=chat_graph_factory,
            routing_fallback_checkpoint=routing_fallback_checkpoint,
            aggregation_graph=self._aggregation_graph,
            aggregation_tools=self._aggregation_tools,
            aggregation_before_send=aggregation_before_send,
            factory=factory,
            support_recorder=SupportRecorder(
                self._support_overrides, self._redactor, clock, support_rule
            ),
        )
        self._worker = threading.Thread(
            target=self._executor.run_loop, name="run-worker", daemon=True
        )

    def validate_memory_permission(self, permission, run_id) -> bool:
        with self._lock:
            current = self._memory_run_permission
            return (
                current is not None and permission is current[1]
                and run_id == current[0]
                and self._states.get().active_run is not None
                and self._states.get().active_run.run_id == run_id
                and self._coord in ("accepted", "running")
            )

    @property
    def memory_service(self):
        return self._memory_service

    @property
    def skill_catalog(self):
        """The read-only Skill index built at startup; None without a state dir."""
        return self._skill_catalog

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
                try:
                    # Publish the unresolved concrete identity before start.
                    # No close caller can observe it while _lifecycle is held;
                    # either this frame or a later close resolves the same
                    # native-handle fact before deciding whether to join it.
                    self._worker_started = None
                    self._worker.start()
                except BaseException as failure:
                    # The retained Thread's native handle distinguishes no
                    # effect from CPython's handle-created/_started-unset
                    # window. A real worker remains close-owned through both.
                    try:
                        started = thread_start_effect_happened(self._worker)
                    except BaseException:
                        # Keep the tri-state unresolved for close() to retry;
                        # the recovery probe must not replace start's failure.
                        raise failure
                    self._worker_started = started
                    raise failure
                self._worker_started = True
                self._started = True
            except BaseException as exc:  # noqa: BLE001 - recorded, then raised
                # Honest state: a Host whose recovery or worker failed is
                # not started, and it must never be mistaken for one.
                self._start_error = exc
                raise

    def attach_trace_exports(self, state, trace_root):
        from agent_alfred.trace_export.service import (
            ExportCleanupPort,
            ExportProjection,
            TraceExports,
        )

        if self.trace_exports is not None:
            return self.trace_exports
        exports = TraceExports(self, state, trace_root)
        self.trace_exports = exports
        self._memory_service._projection_participants += (ExportProjection(),)
        inner = self._memory_service._cleanup_port
        if inner is not None:
            self._memory_service._cleanup_port = ExportCleanupPort(exports, inner)
        notifier = self._memory_service._memory_notifier
        def notify(*args, **kwargs):
            exports.invalidate()
            if notifier is not None:
                return notifier(*args, **kwargs)
        self._memory_service._memory_notifier = notify
        previous = self._redactor._on_change
        def changed():
            exports.invalidate()
            if previous is not None:
                previous()
        self._redactor._on_change = changed
        return exports

    def attach_database_console(self, console) -> None:
        from contextlib import contextmanager

        from agent_alfred.database_console.service import (
            ConsoleCleanupPort,
            DiagnosticProjection,
        )

        self.database_console = console
        original = self._store.transaction

        @contextmanager
        def transaction():
            console.note_writer()
            with original() as conn:
                yield conn

        self._store.transaction = transaction
        self._memory_service._projection_participants += (DiagnosticProjection(),)
        inner = self._memory_service._cleanup_port
        if inner is not None:
            self._memory_service._cleanup_port = ConsoleCleanupPort(console, inner)
        notifier = self._memory_service._memory_notifier

        def notify(*args, **kwargs):
            console.invalidate()
            if notifier is not None:
                return notifier(*args, **kwargs)

        self._memory_service._memory_notifier = notify
        previous_change = self._redactor._on_change
        def changed():
            console.invalidate()
            console._publish_protection()
            if previous_change is not None:
                previous_change()
        self._redactor._on_change = changed

    def close(self, timeout: float | None = None) -> bool:
        """Stop admission, finish mutations and the worker, then release sinks.

        Returns True only when the Host is fully closed.

        The FanOut covers the whole process (ADR-0004), and the worker is the
        thread that emits into it: events, the durability barrier, and the
        finalizer all run there. Closing the sinks on a timer therefore cut
        the ground out from under a Run that was still being recorded. The
        order here is the other way round -- refuse new work, let the
        in-flight Run finish, wait for the worker to actually stop, and only
        then close the sinks to completion.

        A bounded wait that expires returns False and closes nothing: an
        honest "not yet closed" beats a FanOut pulled out from under a live
        Run, and the caller may ask again.
        """
        deadline = time.monotonic() + (
            _WORKER_JOIN_TIMEOUT_S if timeout is None else timeout
        )
        with self._lock, self._lifecycle:
            # The same lock decides ordinary mutation admission. Once this
            # fence lands, every prior mutation is either still counted by
            # _mutating or has already finished; no later one can enter.
            self._closing = True
            if self._worker_started is None:
                self._worker_started = thread_start_effect_happened(
                    self._worker
                )
            worker_started = self._worker_started
        # 1. Let every Run already admitted reach the queue. Admission is
        #    closed from here on, so this drains to zero -- and the stop
        #    stop fact is only published afterwards, which is what keeps an
        #    accepted Run from arriving after the worker has stopped. The
        #    flag is raised under _lifecycle and the drain observed
        #    under _lock; admission_reserve holds _lock across the same pair,
        #    so no Run can slip in between the two. Ordinary mutations must
        #    also finish before their database or other shared resources close.
        if not self._await_handoffs_and_mutations(deadline):
            return False
        with self._lifecycle:
            if not self._stop_sent:
                # The queue owns the monotonic stop effect. If this call is
                # interrupted before the effect, retry publishes it; if it is
                # interrupted afterwards, retry only re-notifies. The Host's
                # completion bit therefore moves only after a definite return
                # without risking duplicate physical sentinels.
                self._queue.request_stop()
                self._stop_sent = True
        # 2. Wait for the worker. Nothing downstream is torn down before it
        #    stops. It stays a daemon thread so a wedged model call cannot
        #    hold the interpreter open, but close() says so rather than
        #    letting the exit hide an unfinished finalizer.
        if worker_started:
            if not thread_exit_confirmed(
                self._worker,
                max(0.0, deadline - time.monotonic()),
            ):
                return False
        # Settlement callbacks are owned by the recorder beyond the worker
        # frame. They must finish before FanOut or the database can close, and
        # before close() claims success for waiters that still need a result.
        if self.trace_exports is not None and not self.trace_exports.close(
            max(0.0, deadline - time.monotonic())
        ):
            return False
        if not self._recorder.retry_pending_settlements():
            return False
        if hasattr(self, "_mcp_control") and not self._mcp_control.close(deadline):
            return False
        if not self._mcp.close(deadline):
            return False
        with self._lifecycle:
            invalidate = getattr(self._factory, "invalidate_all", None)
            if callable(invalidate) and not self._transport_close.complete(
                invalidate, max(0.0, deadline - time.monotonic())
            ):
                return False
            if not self._fanout_closed:
                # The bit moves only behind a confirmed FanOut close. A sink
                # that reports False is still draining; a sink that raises
                # propagates that exception. Either way, the next close()
                # asks the unfinished sink again. Setting the bit first would
                # make that retry skip the FanOut entirely -- a Host reported
                # closed with a sink nobody ever closed.
                if not self._fanout.close(
                    timeout=max(0.0, deadline - time.monotonic())
                ):
                    return False
                self._fanout_closed = True
            if (self._memory_service.mirrors is not None
                    and not self._memory_service.mirrors.close()):
                return False
            if not self._file_tools.close():
                return False
            if not self._tool_history.close():
                return False
            self._routing_statistics_cleanup.close()
            if self._owned_resources is not None:
                self._owned_resources.close()
            self._closed = True
        return True

    def attach_owned_resources(
        self,
        conn: sqlite3.Connection,
        state: ManagedStateLease,
        *,
        source: ResumableRollback,
    ) -> None:
        """Transfer standalone resources from construction before start."""
        with self._lifecycle:
            if self._started or self._closing or self._owned_resources is not None:
                raise RuntimeError(
                    "owned resources must be attached exactly once before start"
                )
            owner = ResumableRollback()
            self._owned_resources = owner
            source.transfer_many_to(owner, (state, conn))

    def _await_handoffs_and_mutations(self, deadline: float) -> bool:
        """Finish pending Run handoffs and ordinary writes before teardown."""
        while True:
            if not self._retry_admission_cleanups():
                return False
            with self._handoff:
                if not self._pending_handoff and not self._mutating:
                    return True
                # A cleanup owner may have arrived after the snapshot above.
                # Retry it instead of sleeping through its notification.
                if self._admission_cleanups:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._handoff.wait(remaining)

    def _retry_admission_cleanups(self) -> bool:
        """Resume every abandoned submit whose owner reached this Host."""
        with self._handoff:
            cleanups = tuple(self._admission_cleanups.items())
        complete = True
        process_control: BaseException | None = None
        for run_id, owner in cleanups:
            owner_complete, error = owner.retry_all()
            if error is not None and process_control is None and isinstance(
                error, (KeyboardInterrupt, SystemExit, GeneratorExit)
            ):
                process_control = error
            if owner_complete:
                self.admission_retire_cleanup(run_id, owner)
            else:
                complete = False
        if process_control is not None:
            raise process_control
        return complete

    def recover(self) -> None:
        self._recorder.recover()
        self._memory_service.forgetting.recover_input_registrations()
        self._memory_service.consolidation.recover_abandoned()
        self._memory_service.consolidation.scheduling.recover()
        self._external_tools.recover()
        from agent_alfred.tools.metering import MeteringError

        try:
            self._tool_metering.recover()
        except MeteringError:
            # Historical reads remain available while new Runs stay refused.
            pass
        if self._memory_service.mirrors is not None:
            from agent_alfred.memory.commands import CommandContext
            from agent_alfred.memory.types import ManualOrigin

            self._memory_service.forgetting.reconcile_projections(
                CommandContext(ManualOrigin("cli"), "cli")
            )
        # Recovery can settle Run evidence without changing a terminal batch.
        # Publish only after all startup transactions and file recovery finish.
        self._memory_service.consolidation.notify_finalized()

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        now = format_instant(self._clock.wall_utc())
        with self._store.transaction() as conn:
            schema.insert_session(conn, session_id=session_id, created_at=now)
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

    def try_begin_mutation(
        self, *, owner: ResumableRollback | None = None,
    ) -> str | None:
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
        - ``admission_failed`` -- Host shutdown permanently closed the gate.
        """
        implicit = owner is None
        if implicit:
            owner = ResumableRollback()
        lock = OwnedLock(self._lock)
        owner.own(lock)
        try:
            try:
                lock.acquire()
                if self._closing:
                    return "admission_failed"
                if self._coord == "recording_failed":
                    return "recording_unavailable"
                if self._mutating or self._coord != "idle":
                    return "mutation_in_flight"
                if not self._store.available:
                    return "recording_unavailable"
                if implicit:
                    self._implicit_mutation.owner = owner
                self._mutation_owner = owner
                self._mutating = True
                return None
            finally:
                lock.close()
        except BaseException as failure:
            if implicit:
                recovery = ResumableRollback()
                recovery.own(owner, partial(self.end_mutation, owner=owner))
                recovery.raise_failure(failure)
            raise

    def end_mutation(self, *, owner: ResumableRollback | None = None) -> None:
        implicit = owner is None
        if implicit:
            # A legacy caller may only release its own thread's successful claim.
            owner = getattr(self._implicit_mutation, "owner", None)
            if owner is None:
                return
        try:
            owner.close()
            lock = OwnedLock(self._lock)
            owner.own(lock)
            try:
                lock.acquire()
                if self._mutation_owner is not owner:
                    return
                self._mutating = False
                self._handoff.notify_all()
                self._mutation_owner = None
            finally:
                lock.close()
        except BaseException as failure:
            if implicit:
                recovery = ResumableRollback()
                recovery.own(owner, partial(self.end_mutation, owner=owner))
                recovery.raise_failure(failure)
            raise

    def execute_mutation(self, operation):
        """Keep admission owned across acquisition, callback and return boundaries."""
        owner = ResumableRollback()
        cleanup = ResumableRollback()
        cleanup.own(owner, partial(self.end_mutation, owner=owner))
        try:
            reason = self.try_begin_mutation(owner=owner)
            if reason is not None:
                return None, reason
            return operation(), None
        finally:
            original = sys.exception()
            if not cleanup.retry():
                cleanup.raise_failure(
                    dominant_error(original, cleanup.process_control)
                    or cleanup.errors[0]
                )

    def mutate_model_settings(
        self,
        expected_revision: int,
        transform,
    ) -> ModelSettingsSnapshot:
        if self._model_settings is None:
            raise RuntimeError("model settings store is not attached")
        previous = self._model_settings.snapshot()
        result = self._model_settings.mutate(expected_revision, transform)
        invalidate = getattr(self._factory, "invalidate_endpoint", None)
        if callable(invalidate):
            invalidate(previous.assignments.primary.endpoint_id)
            new_id = result.assignments.primary.endpoint_id
            if new_id != previous.assignments.primary.endpoint_id:
                invalidate(new_id)
        return result

    def reread_dotenv(self) -> dict[str, str]:
        with self._configuration_lock:
            return self._reread_dotenv()

    def _reread_dotenv(self) -> dict[str, str]:
        if self._credentials is None or not self._credentials.can_reread:
            raise RuntimeError("dotenv_unavailable")
        from dataclasses import replace

        from agent_alfred.connections import merge_credentials

        old_overlay = dict(self._credentials._overlay)
        old_values = self._credentials.values()
        old_integration_state = self._integrations.checkpoint()
        old_mcp_environment = self._mcp.environment_checkpoint()
        old_application = self._integration_application
        previous = self._tool_authorization.registry
        bind = getattr(self._snapshot_provider, "bind_environ", None)
        try:
            overlay = self._credentials.prepare()
            values = merge_credentials(self._credentials._process, overlay)
            self._integrations.prepare(values)
            mcp_affected = self._mcp.affected_env(values)
            policies = {
                t.name: previous.policy(t.name) for t in previous.declarations()
            }
            for name, available in self._integrations.policies(values).items():
                policies[name] = replace(
                    available, authorization=policies[name].authorization
                )
            for name, (key, _) in self._mcp.aliases.items():
                if key in mcp_affected:
                    policies[name] = replace(
                        policies[name], unavailable_reason="restart_required"
                    )
            block = previous.external_block
            if block and block.startswith("configuration_not_applied"):
                block = None
            candidate = previous.with_policies(policies, external_block=block)
            if callable(bind):
                bind(values)
            invalidate = getattr(self._factory, "invalidate_all", None)
            if callable(invalidate):
                invalidate()
            if self._catalog_scheduler_obj is not None:
                self._catalog_scheduler_obj.bump()
            self._integration_application = "applied"
            self._publish_tools(candidate)
            self._credentials.publish(overlay)
            self._integrations.publish(values)
            self._tool_authorization.registry = candidate
            self._mcp.publish_env(values, mcp_affected)
            self._integration_application = "applied"
        except BaseException as exc:
            self._integration_application = old_application
            try:
                self._credentials.publish(old_overlay)
                self._integrations.restore(old_integration_state)
                self._mcp.restore_environment(old_mcp_environment)
                if callable(bind):
                    bind(old_values)
                self._publish_tools(previous)
                self._tool_authorization.registry = previous
            except BaseException:
                self._integration_application = "not_applied"
                previous.suspend_external(
                    "configuration_not_applied; reread .env to repair"
                )
                self._tools.suspend_external(
                    "configuration_not_applied; reread .env to repair"
                )
                self._tool_authorization.registry = self._tools
                if not isinstance(exc, Exception):
                    raise exc
            if not isinstance(exc, Exception):
                raise
            from agent_alfred.resource_rollback import raise_if_rollback_pending

            raise_if_rollback_pending(exc)
            raise RuntimeError("dotenv_reload_failed") from None
        self._mcp.clean_affected(mcp_affected)
        return values

    def connections(self) -> dict:
        with self._configuration_lock:
            return self._connections()

    def _connections(self) -> dict:
        from agent_alfred.catalog import CatalogState
        from agent_alfred.connections import project_connections

        endpoints = self._endpoint_rows()
        env = self._credential_env()
        observations: dict[str, dict] = {}
        catalogs: dict[str, CatalogState] = {}
        pool = getattr(self._factory, "transport_pool", None)
        if pool is not None:
            for endpoint in endpoints:
                observed = pool.cached_observation(endpoint.endpoint_id)
                if isinstance(observed, dict):
                    observations[endpoint.endpoint_id] = observed
                catalog = pool.cached_catalog(endpoint.endpoint_id)
                if isinstance(catalog, CatalogState):
                    catalogs[endpoint.endpoint_id] = catalog
        result = project_connections(
            endpoints=endpoints,
            env=env,
            overlay={},
            observations=observations,
            catalogs=catalogs,
        )

        result["mcp"] = self._mcp.snapshot()
        if hasattr(self, "_mcp_control"):
            result["mcp"].update(self._mcp_control.preview())
        result["integrations"] = self._integrations.snapshot()
        result["integration_revision"] = self._integrations.revision
        result["integration_application"] = self._integration_application
        result["process_instance_id"] = self._process_instance_id
        # Models, configuration application and integrations change independently.
        # Version the complete public view under _configuration_lock; a Tavily
        # revision alone cannot order model observations or failed publication.
        if result != self._connections_snapshot:
            self._connections_revision += 1
            self._connections_snapshot = deepcopy(result)
        result["connections_revision"] = self._connections_revision
        return result

    def models(self, *, expand: str | None = None, refresh: bool = False) -> dict:
        from agent_alfred.candidates import project_models_page
        from agent_alfred.catalog import CatalogState

        if self._model_settings is None:
            return {
                "status": "settings_invalid",
                "revision": 0,
                "assignments": None,
                "endpoints": [],
            }
        snapshot = self._model_settings.snapshot()
        endpoints = self._endpoint_rows()
        catalogs: dict[str, CatalogState] = {}
        keys: dict[str, str | None] = {}
        if snapshot.status == "ok":
            scheduler = self._catalog_scheduler()
            keys = {
                endpoint.endpoint_id: self._key_for(endpoint.endpoint_id)
                for endpoint in endpoints
            }
            if expand:
                endpoint = next(
                    (row for row in endpoints if row.endpoint_id == expand),
                    None,
                )
                if endpoint is not None:
                    scheduler.ensure(
                        endpoint, api_key=keys.get(expand), refresh=refresh
                    )
                catalogs = scheduler.cached_states()
            else:
                catalogs = scheduler.open_assigned(
                    assigned_endpoint_id=snapshot.assignments.primary.endpoint_id,
                    keys=keys,
                )
        observed = {
            row["endpoint_id"]: row["observation"]
            for row in self.connections()["endpoints"]
        }
        return project_models_page(
            snapshot=snapshot,
            endpoints=endpoints,
            catalogs=catalogs,
            observations=observed,
            keys=keys,
        )

    def routing_snapshot(self):
        from agent_alfred.tools import capability_identity

        state = self._behaviour.snapshot() if self._behaviour else None
        if state is None or (state["status"] == "ok" and not state["enabled"]):
            return None
        return dict(
            settings=state,
            graph=self._routing_graph,
            graph_error=self._routing_error,
            generation=self._routing_generation,
            capabilities=[capability_identity(t) for t in self._tools.declarations()],
        )

    def routing_statistics(self, window="7d", *, cancelled=None):
        from agent_alfred.routing_statistics.service import query_statistics

        deadline = time.monotonic() + 2.0
        try:
            with self._lifecycle:
                self._routing_statistics_cleanup.close()
            return query_statistics(
                self._routing_statistics_path, window=window,
                as_of=self._clock.wall_utc(),
                process_instance_id=self._process_instance_id, cancelled=cancelled,
                _deadline=deadline,
            )
        except BaseException as exc:
            with self._lifecycle:
                self._routing_statistics_cleanup.capture_failure(exc)
            raise

    def workflow_topology(self, workflow):
        if workflow == "message_routing":
            graph, generation = self._routing_publication
        elif workflow == "manual_aggregation":
            graph, generation = self._aggregation_graph, 1
        else:
            raise ValueError("unknown_workflow")
        envelope = dict(
            workflow=workflow,
            graph_id=graph.graph_id if graph is not None else workflow,
            process_instance_id=self._process_instance_id,
            publication_generation=generation,
            read_at=format_instant(self._clock.wall_utc()),
        )
        if graph is None:
            return dict(
                envelope, status="unavailable", reason="graph_not_published",
                description=None,
            )
        return dict(envelope, status="available", description=graph.describe())

    def behaviour(self):
        if self._behaviour is None:
            return dict(
                schema_version=1,
                revision=0,
                enabled=False,
                status="ok",
                fingerprint=None,
                backup_path=None,
            )
        return self._behaviour.read()

    def apply_behaviour(self, body):
        from agent_alfred.runtime.behaviour import BehaviourError

        if self._behaviour is None:
            raise BehaviourError("settings_unavailable")
        action = body.get("action")
        expected = body.get("expected_revision")
        if action == "save":
            return self._behaviour.save(
                expected_revision=expected, enabled=body.get("enabled")
            )
        if action == "recover":
            return self._behaviour.recover(
                expected_revision=expected, fingerprint=body.get("fingerprint")
            )
        raise BehaviourError("settings_invalid")

    def apply_settings(self, body: dict) -> dict:
        from decimal import Decimal

        from agent_alfred.settings_commands import (
            assign,
            pin,
            set_display_name,
            set_price_override,
            set_style,
            unpin,
        )

        op = body.get("op")
        expected = body.get("expected_revision")
        if type(expected) is not int:
            raise ModelSettingsError("settings_invalid")
        if op not in {"pin", "unpin", "style", "display", "price", "assign"}:
            raise ModelSettingsError("settings_invalid")
        if type(body.get("endpoint_id")) is not str:
            raise ModelSettingsError("settings_invalid")
        if type(body.get("model_id")) is not str:
            raise ModelSettingsError("settings_invalid")

        def transform(snapshot):
            endpoint_id = body.get("endpoint_id")
            model_id = body.get("model_id")
            if op == "pin":
                return pin(snapshot, endpoint_id=endpoint_id, model_id=model_id)
            if op == "unpin":
                return unpin(snapshot, endpoint_id=endpoint_id, model_id=model_id)
            if op == "style":
                return set_style(
                    snapshot,
                    endpoint_id=endpoint_id,
                    model_id=model_id,
                    wire_style=body["wire_style"],
                )
            if op == "display":
                return set_display_name(
                    snapshot,
                    endpoint_id=endpoint_id,
                    model_id=model_id,
                    display_name=body.get("display_name"),
                )
            if op == "price":
                raw = body.get("value")
                value = None if raw is None else Decimal(str(raw))
                return set_price_override(
                    snapshot,
                    endpoint_id=endpoint_id,
                    model_id=model_id,
                    dimension=body["dimension"],
                    value=value,
                )
            if op == "assign":
                return assign(
                    snapshot,
                    slot=body.get("slot") or "primary",
                    endpoint_id=endpoint_id,
                    model_id=model_id,
                )
            raise ModelSettingsError("settings_invalid")

        self.mutate_model_settings(expected, transform)
        return self.models()

    def probe_auth(self, endpoint_id: str) -> None:
        if endpoint_id == "tavily":
            self._integrations.probe()
            return
        from agent_alfred.auth_probe import (
            AuthProbeRefused,
            UrllibAuthProbeTransport,
            run_auth_probe,
        )

        endpoint = next(
            (row for row in self._endpoint_rows() if row.endpoint_id == endpoint_id),
            None,
        )
        if endpoint is None:
            raise AuthProbeRefused("unknown_endpoint", 404)
        if endpoint.auth_probe is None:
            raise AuthProbeRefused("auth_probe_unavailable", 400)
        raw = None
        env = self._credential_env()
        value = env.get(endpoint.api_key_env)
        if value is not None and value.strip():
            raw = value
        if raw is None:
            raise AuthProbeRefused("endpoint_unconfigured", 409)
        transport = self._auth_probe_transport or UrllibAuthProbeTransport()
        observed = run_auth_probe(
            endpoint, api_key=raw, transport=transport, clock=self._clock
        )
        pool = getattr(self._factory, "transport_pool", None)
        if pool is not None:
            pool.store_observation(endpoint.endpoint_id, observed)

    def record_connection_observation(
        self, item, outcome: str, error: str | None
    ) -> None:
        if item.request.purpose == "aggregation" and not item.memory_telemetry.get(
            "input_attempts"
        ):
            return
        pool = getattr(self._factory, "transport_pool", None)
        if pool is None or outcome not in {"completed", "failed"}:
            return
        via = (
            "inference_probe"
            if item.request.purpose == "inference_probe"
            else "real_run"
        )
        pool.store_observation(
            item.snapshot.endpoint_id,
            {
                "state": "connected" if outcome == "completed" else "error",
                "checked_at": format_instant(self._clock.wall_utc()),
                "checked_via": via,
                "reason": None if outcome == "completed" else error,
            },
        )

    def _endpoint_rows(self):
        from agent_alfred.endpoints import list_endpoints

        rows = getattr(self._factory, "_endpoints", None)
        if rows is not None:
            return tuple(rows)
        return list_endpoints()

    def _credential_env(self) -> dict[str, str]:
        import os

        if self._credentials is not None:
            return self._credentials.values()
        environ = getattr(self._snapshot_provider, "_environ", None)
        if environ is not None:
            return dict(environ)
        return dict(os.environ)

    def _key_for(self, endpoint_id: str) -> str | None:
        from agent_alfred.runtime.config import _key_for

        return _key_for(endpoint_id, self._credential_env())

    def _catalog_scheduler(self):
        if getattr(self, "_catalog_scheduler_obj", None) is None:
            from agent_alfred.catalog_schedule import CatalogScheduler
            from agent_alfred.runtime.transport import VersionedTransportPool

            pool = getattr(self._factory, "transport_pool", None)
            if pool is None:
                pool = VersionedTransportPool(lambda snapshot: snapshot)
                transport = _NullCatalogTransport()
            else:
                transport = self._catalog_transport or _UrllibCatalogTransport()
            self._catalog_scheduler_obj = CatalogScheduler(
                clock=self._clock,
                pool=pool,
                transport=transport,
                endpoints=self._endpoint_rows(),
            )
        return self._catalog_scheduler_obj

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
        # A process-control exception is announced while its Run still owns
        # the lease. Preserve that Run's ordinary 409 recording-pending
        # contract; the announcement becomes the terminal refusal exactly
        # when recording releases the coordinator to idle.
        if self._executor_stopping and self._coord == "idle":
            return "admission_failed"
        if self._executor.stopped_by is not None:
            return "admission_failed"
        with self._lifecycle:
            unstartable = self._start_error is not None or self._closing or self._closed
        if unstartable:
            return "admission_failed"
        if self._coord == "recording_failed":
            return "recording_unavailable"
        if self._coord != "idle":
            return "run_in_progress"
        if self._tool_metering.failed.is_set():
            return "recording_unavailable"
        if not self._store.available:
            return "recording_unavailable"
        if self._mutating:
            return "mutation_in_flight"
        self._tool_authorization.check_disk_health()
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
        # publish the stop fact. Lock order is _lock -> _lifecycle, never the
        # reverse; no path takes _lifecycle and then _lock.
        with self._lock:
            snap = self._states.get()
            refusal = self._admission_refusal_locked()
            if refusal is not None:
                return refusal, snap
            self._coord = "accepted"
            self._pending_handoff.add(run_id)
            self._queue.reserve(run_id)
            if wait_for_result:
                self._done[run_id] = threading.Event()
            # The safe busy card and the authoritative snapshot move under
            # the same lock that takes the lease, so no refused submit can
            # observe the one without the other.
            self._active_summary = summary
            snapshot = self._states.replace(
                owner_run_id=run_id,
                coordinator_state="accepted",
                active_run=summary,
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
        self.admission_recover_release(run_id)

    def admission_recover_release(self, run_id: str) -> None:
        """Idempotently resume release after an interrupted public call."""
        self._queue.recover(run_id)
        self._queue.retire(run_id)
        with self._lock:
            self._done.pop(run_id, None)
            self._results.pop(run_id, None)
            self._pending_handoff.discard(run_id)
            self._handoff.notify_all()
            if self._active_summary is not None:
                if self._active_summary.run_id != run_id:
                    return
            self._replace_terminal_state_locked(
                owner_run_id=run_id,
                coordinator_state="idle",
                active_run=None,
                unrecorded_terminal_projection=None,
            )

    def admission_close_idle(self, run_id: str) -> None:
        """Reopen only while the finalized unstarted run still owns admission."""
        with self._lock:
            if (
                self._active_summary is not None
                and self._active_summary.run_id != run_id
            ):
                return
            self._replace_terminal_state_locked(
                owner_run_id=run_id,
                coordinator_state="idle",
                active_run=None,
                unrecorded_terminal_projection=None,
            )

    def admission_discard_result_slot(self, run_id: str) -> None:
        """Atomically retire an admission result no caller can consume.

        Admission calls this only after publishing the terminal result and
        its done notification, once it knows submit will not return
        ``accepted``. Accepted Runs retain the slot until :meth:`wait`
        consumes it, while Web submissions never create one.
        """
        with self._lock:
            self._results.pop(run_id, None)
            self._done.pop(run_id, None)

    def admission_recover_handoff(
        self,
        run_id: str,
        *,
        settle: Callable[[bool | None], None],
    ) -> None:
        """Resolve interrupted publication into the admission-owned slot.

        ``True`` means the worker owns the published item, ``False`` means
        the pending cell was cancelled, and ``None`` means execution may
        already have retired the decision after durably marking the Run.
        """
        self._queue.recover(run_id, settle=settle)

    def admission_complete_handoff(self, run_id: str) -> None:
        """Retire a recovered handoff only after its outcome is visible."""
        # An earlier decision read may have failed. Complete that same cell
        # before retiring it; the retained cleanup owner retries on failure.
        self._queue.recover(run_id)
        self._queue.retire(run_id)
        with self._handoff:
            self._pending_handoff.discard(run_id)
            self._handoff.notify_all()

    def admission_retain_cleanup(
        self, run_id: str, owner: AdmissionCleanupOwner
    ) -> AdmissionCleanupOwner:
        """Publish one cleanup owner before admission starts retiring it."""
        with self._handoff:
            retained = self._admission_cleanups.setdefault(run_id, owner)
            self._handoff.notify_all()
            return retained

    def admission_retire_cleanup(
        self, run_id: str, owner: AdmissionCleanupOwner
    ) -> None:
        """Retire only the same owner whose two cleanup branches settled."""
        with self._handoff:
            if self._admission_cleanups.get(run_id) is owner:
                self._admission_cleanups.pop(run_id)
            self._handoff.notify_all()

    def admission_fail_recording(
        self,
        summary: ActiveRunSummary,
        projection: UnrecordedTerminalProjection,
    ) -> None:
        """Publish admission's terminal summary and projection, then close.

        Admission owns the result of an unstarted handoff failure, including
        the exceptional path where its interrupted finalize cannot commit.
        Publishing that pair together keeps the active lifecycle and bounded
        terminal projection as one fact before anything answers 503.
        """
        with self._lock:
            current = self._states.get()
            already_committed = (
                current.coordinator_state == "recording_failed"
                and current.active_run == summary
                and current.unrecorded_terminal_projection == projection
            )
            if not already_committed and (
                self._active_summary is None
                or self._active_summary.run_id != summary.run_id
            ):
                # No owner is not this owner.  In particular, a delayed retry
                # from an old failed admission may arrive after a successor
                # has itself completed and released back to idle; it must not
                # resurrect the old Run's failure card.
                return
            self._replace_terminal_state_locked(
                owner_run_id=summary.run_id,
                coordinator_state="recording_failed",
                active_run=summary,
                unrecorded_terminal_projection=projection,
            )

    def admission_publish_handoff(self, item: WorkItem) -> None:
        """Publish work and retire its close fence as one coordinator step.

        The queue cell records the publication decision before the worker can
        observe a WorkItem. If this call is interrupted, the idempotent
        recovery seam completes the decision and fence retirement.
        """
        with self._lock:
            self._memory_run_permission = (item.run_id, item.memory_permission)
        if self._publish_work is not None:
            self._publish_work(item)
            self._queue.publish(item, enqueue=False)
        else:
            self._queue.publish(item, enqueue=True)
        with self._handoff:
            self._pending_handoff.discard(item.run_id)
            self._handoff.notify_all()

    # -- execution transition ----------------------------------------------

    def execution_mark_running(self, started_at: str) -> ActiveRunSummary | None:
        run_id: str | None = None
        try:
            with self._lock:
                self._coord = "running"
                if self._active_summary is not None:
                    run_id = self._active_summary.run_id
                    self._active_summary = replace(
                        self._active_summary,
                        phase="running",
                        started_at=started_at,
                    )
                summary = self._active_summary
                # Same-lock snapshot replacement: the running state is never
                # observable apart from the summary that describes it.
                self._states.replace(
                    owner_run_id=run_id,
                    coordinator_state="running",
                    active_run=summary,
                )
            return summary
        finally:
            if run_id is not None:
                # The durable phase already says execution owns the Run.  A
                # snapshot listener may interrupt the public transition, but
                # it cannot retain admission's cell (and the client it owns)
                # after execution has claimed the item.
                self._queue.retire(run_id)

    def execution_mark_stopping(self) -> None:
        """Publish worker unavailability before its final lease release."""
        with self._lock:
            self._executor_stopping = True

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
                owner_run_id=published.envelope.run_id,
                coordinator_state="running",
                active_run=self._active_summary,
            )

        return fanout.emit_linearized(
            payload,
            envelope,
            boundary=self._lock,
            after_commit=project,
        )

    # -- recording-settlement transitions -----------------------------------

    def _replace_terminal_state_locked(
        self,
        *,
        owner_run_id: str,
        coordinator_state: CoordinatorState,
        active_run: ActiveRunSummary | None,
        unrecorded_terminal_projection: UnrecordedTerminalProjection | None,
    ) -> None:
        """Commit or resume one owner-scoped terminal snapshot transition.

        A matching current snapshot means an earlier call committed but may
        have lost its listener return edge.  Resume that exact delivery -- do
        not mint another revision.  A newer owner's absolute replacement has
        already superseded the old token, so owner matching also makes an old
        Run's retry harmless after a successor is admitted.
        """
        current = self._states.get()
        if (
            current.coordinator_state == coordinator_state
            and current.active_run == active_run
            and current.unrecorded_terminal_projection == unrecorded_terminal_projection
        ):
            # The authoritative snapshot may have moved before an asynchronous
            # exception interrupted Host's own mirror assignments.  Repair the
            # mirrors before retrying the separately tracked listener delivery.
            self._coord = coordinator_state
            self._active_summary = active_run
            self._states.resume_pending(owner_run_id)
            return
        try:
            self._states.replace(
                owner_run_id=owner_run_id,
                coordinator_state=coordinator_state,
                active_run=active_run,
                unrecorded_terminal_projection=(unrecorded_terminal_projection),
            )
        finally:
            if self._states.get() is not current:
                self._coord = coordinator_state
                self._active_summary = active_run

    def recording_enter_pending(self, projection: UnrecordedTerminalProjection) -> None:
        """running -> recording_pending. The lease is NOT released here.

        The terminal outcome already exists -- it is the projection's -- so
        the finished summary copies it: a ``finished`` phase with an empty
        outcome would be a snapshot that cannot say what the Run concluded,
        precisely in the window (ADR-0026) where this projection is the only
        place the conclusion exists.
        """
        # The resumable settlement owner already retains the WorkItem, even
        # when execution failed before marking running. Retire only this
        # handoff; its durable admitted decision remains available to recovery.
        self._queue.retire(projection.run_id)
        with self._lock:
            if (
                self._active_summary is None
                or self._active_summary.run_id != projection.run_id
            ):
                return
            pending = replace(
                self._active_summary,
                phase="finished",
                recording_state="pending",
                outcome=projection.outcome,
            )
            self._replace_terminal_state_locked(
                owner_run_id=projection.run_id,
                coordinator_state="recording_pending",
                active_run=pending,
                unrecorded_terminal_projection=projection,
            )

    def recording_enter_failed(self, projection: UnrecordedTerminalProjection) -> None:
        """recording_pending -> recording_failed, keeping the same projection.

        The same terminal outcome travels with it: the failure is a
        recording_state badge, not a rewriting of what the Run concluded,
        so the failed summary shows exactly what the pending one showed.
        """
        if self._before_recording_failed is not None:
            self._before_recording_failed.wait()
        with self._lock:
            if (
                self._active_summary is None
                or self._active_summary.run_id != projection.run_id
            ):
                return
            failed_projection = replace(projection, recording_state="failed")
            summary = self._active_summary
            summary = replace(
                summary,
                recording_state="failed",
                phase="finished",
                outcome=projection.outcome,
            )
            self._replace_terminal_state_locked(
                owner_run_id=projection.run_id,
                coordinator_state="recording_failed",
                active_run=summary,
                unrecorded_terminal_projection=failed_projection,
            )

    def recording_publish_recorded_then_release(self, run_id: str) -> None:
        """Authority first, lease second: the recorded snapshot becomes
        visible while admission is still closed, then the lease releases.

        Both halves are owner-scoped. An interruption before the recorded
        snapshot commits retains admission for retry; a listener failure after
        commit still permits release. If an exception lands after the second half
        and a successor is admitted before the caller retries, the old Run's
        recovery observes the new owner and becomes a no-op instead of
        marking or releasing the successor.
        """
        release_owned = False
        try:
            with self._lock:
                summary = self._active_summary
                if summary is None:
                    if self._coord == "idle":
                        self._replace_terminal_state_locked(
                            owner_run_id=run_id,
                            coordinator_state="idle",
                            active_run=None,
                            unrecorded_terminal_projection=None,
                        )
                    return
                if summary.run_id != run_id:
                    return
                release_owned = True
                recorded = replace(summary, recording_state="recorded")
                self._run_path.retire(run_id)
                self._replace_terminal_state_locked(
                    owner_run_id=run_id,
                    coordinator_state="recording_pending",
                    active_run=recorded,
                    unrecorded_terminal_projection=None,
                )
            if self._after_recorded_snapshot is not None:
                self._after_recorded_snapshot.wait()
        finally:
            if release_owned:
                with self._lock:
                    current = self._states.get()
                    summary = current.active_run
                    if summary is None:
                        if current.coordinator_state == "idle":
                            self._replace_terminal_state_locked(
                                owner_run_id=run_id,
                                coordinator_state="idle",
                                active_run=None,
                                unrecorded_terminal_projection=None,
                            )
                    elif (
                        summary.run_id == run_id
                        and summary.recording_state == "recorded"
                    ):
                        self._replace_terminal_state_locked(
                            owner_run_id=run_id,
                            coordinator_state="idle",
                            active_run=None,
                            unrecorded_terminal_projection=None,
                        )

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

    def _accounting_prices(self):
        import copy

        from agent_alfred.pricing import PriceChain, PriceQuote, StaticPriceBook

        catalog = None
        catalog_prices = getattr(self._factory, "catalog_prices", None)
        if callable(catalog_prices):
            live = catalog_prices()
            catalog = (
                live.freeze()
                if callable(getattr(live, "freeze", None))
                else copy.deepcopy(live)
            )
        pins = ()
        if self._model_settings is not None:
            snapshot = self._model_settings.snapshot()
            if snapshot.status == "ok":
                pins = snapshot.pins

        class PinThenCatalog:
            def quote(self, endpoint_id, model_id, dimension):
                pin = next(
                    (
                        item
                        for item in pins
                        if item.endpoint_id == endpoint_id and item.model_id == model_id
                    ),
                    None,
                )
                if pin is not None and pin.price_override is not None:
                    value = pin.price_override.explicit(dimension)
                    if value is not None:
                        return PriceQuote(unit_price=value, source="user_override")
                if catalog is not None:
                    return catalog.quote(endpoint_id, model_id, dimension)
                return None

        return PriceChain(catalog=PinThenCatalog(), static=StaticPriceBook.packaged())

    def read_run_path(self, run_id: str, *, trace_root: Path) -> dict | None:
        from agent_alfred.runtime.run_path import read_path

        with self._lock:
            active = self._states.get().active_run
            live = None
            if (active is not None and active.run_id == run_id
                    and active.recording_state != "recorded"):
                captured = self._run_path.capture(run_id)
                live = (captured, dict(
                    phase=active.phase, outcome=active.outcome,
                    recording_state=active.recording_state,
                ))
        if live is not None:
            captured, run = live
            state, events = self._run_path.materialize(captured)
            live = state, events, run
        return read_path(
            self._store, self._redactor, run_id, trace_root,
            self._process_instance_id, format_instant(self._clock.wall_utc()), live,
        )

    def read_run_evidence(
        self, run_id: str, *, trace_root: Path, accounting_attempts: list | None = None
    ) -> dict | None:
        from agent_alfred.runtime.evidence import read_evidence

        value = read_evidence(
            self._store,
            self._redactor,
            run_id,
            trace_root,
            overrides=self._support_overrides,
            prices=self._accounting_prices() if accounting_attempts is None else None,
            computed_at=format_instant(self._clock.wall_utc()),
            accounting_attempts=accounting_attempts,
        )

        if value and value.get("memory", {}).get("aggregation"):
            from agent_alfred.aggregation.views import source_availability

            value["memory"]["aggregation"] = source_availability(
                value["memory"]["aggregation"], self._memory_service
            )
        return value

    def accounting_snapshot(self, filters):
        return self._accounting.create(
            filters, recording_failed=self._recording_failed_run_ids()
        )

    def accounting_page(self, snapshot_id, offset=0):
        return self._accounting.page(snapshot_id, offset)

    def accounting_detail(self, snapshot_id, run_id):
        value = self._accounting.detail(snapshot_id, run_id)
        current = {item["identity"] for item in self.tools_catalog()["tools"]}
        for item in value["run"]["tools"]:
            item["currently_registered"] = item["identity"] in current
        return value

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
        recording_failed = self._recording_failed_run_ids()
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

    def _recording_failed_run_ids(self) -> frozenset[str]:
        """Run ids the authoritative in-process projection says cannot record."""
        projection = self._states.get().unrecorded_terminal_projection
        if projection is None or projection.recording_state != "failed":
            return frozenset()
        return frozenset({projection.run_id})

    def _unaddressable_run_ids(self) -> frozenset[str]:
        """Unexecuted failed-handoff ids that have no Web destination."""
        snapshot = self._states.get()
        if not is_unaddressable_unstarted_handoff_failure(snapshot):
            return frozenset()
        assert snapshot.active_run is not None
        return frozenset({snapshot.active_run.run_id})

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

    def transport_session_validity(self, session_id: str | None) -> SessionValidity:
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
        recording_failed = self._unaddressable_run_ids()
        with self._store.reading() as conn:
            return runs.list_runs(
                conn,
                filter=filter,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
                recording_failed_run_ids=recording_failed,
            )

    def locate_run(self, run_id: str, *, limit: int = 25) -> runs.RunPage | None:
        """The page a deep link to one Run should open on."""
        recording_failed = self._unaddressable_run_ids()
        with self._store.reading() as conn:
            return runs.locate_run(
                conn,
                run_id=run_id,
                limit=limit,
                redactor=self._redactor,
                recording_failed_run_ids=recording_failed,
            )

    def list_session_chat_runs(
        self,
        *,
        session_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> runs.SessionChatRunsPage:
        """One Session's admitted chat Runs, keyset paged."""
        recording_failed = self._unaddressable_run_ids()
        with self._store.reading() as conn:
            return runs.list_session_chat_runs(
                conn,
                session_id=session_id,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
                reply_max_chars=self._settings.prompt_preview_max_chars,
                recording_failed_run_ids=recording_failed,
            )

    def recover_reply(
        self,
        *,
        process_instance_id: str,
        session_id: str,
        run_id: str,
    ) -> replies.RecoveredReply:
        """Recover formal reply text without changing Run or recording state."""
        return replies.recover_reply(
            self.snapshot(),
            self._store,
            self._redactor,
            process_instance_id=process_instance_id,
            session_id=session_id,
            run_id=run_id,
        )

    def mainbar_pairs(
        self,
        *,
        session_id: str,
        limit: int = runs.DEFAULT_MAINBAR_LIMIT,
        cursor: str | None = None,
    ) -> runs.MainBarPage:
        """The MainBar's Run pairs and historic messages for one Session."""
        recording_failed = self._recording_failed_run_ids()
        with self._store.reading() as conn:
            page = runs.mainbar_pairs(
                conn,
                session_id=session_id,
                limit=limit,
                cursor=cursor,
                redactor=self._redactor,
                recording_failed_run_ids=recording_failed,
            )

        from dataclasses import replace

        from agent_alfred.aggregation.views import source_availability

        return replace(
            page,
            items=tuple(
                replace(
                    item,
                    aggregation=source_availability(
                        item.aggregation, self._memory_service
                    ),
                )
                if isinstance(item, runs.MainBarRunPair) and item.aggregation
                else item
                for item in page.items
            ),
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

    def tools_catalog(self):
        with self._lock, self._configuration_lock:
            result = self._tool_authorization.snapshot(
                suspend_external=self._coord == "idle" and not self._mutating
            )
            integrations = {
                row["integration_id"]: row for row in self._integrations.snapshot()
            }
            for tool in result["tools"]:
                integration = integrations.get(tool["source_id"])
                if integration:
                    tool["observation"] = integration["observation"]
                    tool["connection"] = integration["observation"]["state"]
                    tool["integration_revision"] = integration["revision"]
            result["process_instance_id"] = self._process_instance_id
            result["mcp"] = self._mcp.snapshot()
            result["mcp_history"] = self._mcp.historical_catalog()
            for tool in result["tools"]:
                entry = self._mcp.aliases.get(tool["name"])
                if entry:
                    key, item = entry
                    row = self._mcp.rows[key]
                    tool["description"] = self._redactor.redact_text(
                        tool["description"]
                    )
                    tool.update(
                        server_key=self._redactor.redact_text(key),
                        original_name=self._redactor.redact_text(item["name"]),
                        alias=tool["name"],
                        connection=row["state"],
                    )
                    if row["state"] != "connected":
                        tool.update(
                            exposure="hidden",
                            availability=row["reason"],
                            reason=row["reason"],
                        )
            return result

    def initialize_mcp(self):
        from agent_alfred.runtime.mcp_control import MCPControl

        self._mcp.on_unavailable = self._suspend_mcp_server
        self._mcp_control = MCPControl(self)
        self._mcp.start()
        try:
            self._publish_mcp()
        except Exception:
            pass  # Optional MCP remains visibly paused; core construction survives.

    def _suspend_mcp_server(self, key, reason):
        with self._configuration_lock:
            self._tools.suspend_tools(
                [name for name, (server, _) in self._mcp.aliases.items()
                 if server == key], reason,
            )

    def _publish_mcp(self):
        from agent_alfred.tools import ToolRegistry, capability_identity

        previous = self._tools
        base = tuple(t for t in previous.declarations() if t.schema_mode != "mcp")
        declarations = self._mcp.declarations(t.name for t in base)
        observed = {
            key: [
                capability_identity(t)
                for t in declarations
                if t.source_id == "mcp:" + row.get("source", "")
            ]
            for key, row in self._mcp.rows.items()
            if row["state"] == "connected"
        }
        removed = set(self._tool_authorization.mcp_active) - set(self._mcp.config)
        try:
            if self._mcp.error is None:
                self._tool_authorization.reconcile_mcp(observed, removed)
            policies = {
                **{t.name: previous.policy(t.name) for t in base},
                **self._mcp.policies(),
            }
            candidate = ToolRegistry(
                (*base, *declarations),
                clock=self._clock,
                redactor=self._redactor,
                policies=policies,
                external_ledger=self._external_tools,
                metering=self._tool_metering,
                external_block=previous.external_block,
            )
            self._tool_authorization.registry = candidate
            self._tool_authorization.apply()
            if self._tool_authorization.application != "applied":
                raise ValueError("mcp_publication_unconfirmed")
        except Exception:
            self._mcp.pause("publication_unconfirmed")
            # Old registries also consult the bridge before entering stdio.
            self._tools = previous
            self._tool_authorization.registry = previous
            raise

    def mcp_control(self, body):
        return self._mcp_control.submit(body)

    def mcp_operation(self, operation_id):
        return self._mcp_control.get(operation_id)

    def wait_mcp_operation(self, operation_id, timeout=30):
        return self._mcp_control.wait(operation_id, timeout)

    def _publish_tools(self, registry):
        graph = self._routing_graph_builder(registry)
        if self._integration_application != "applied":
            registry.suspend_external(
                "configuration_not_applied; reread .env to repair"
            )
        self._routing_graph = graph
        self._routing_error = None
        self._routing_generation += 1
        self._tools = registry
        self._routing_publication = (graph, self._routing_generation)
        if hasattr(self, "_executor"):
            self._executor._tools = registry

    def save_tool_authorization(self, identity, authorization, expected_revision):
        result, refusal = self.execute_mutation(
            lambda: self._tool_authorization.save(
                identity, authorization, expected_revision
            )
        )
        return {"error": {"code": refusal}} if refusal else result

    def reapply_tool_authorization(self, expected_revision):
        result, refusal = self.execute_mutation(
            lambda: self._tool_authorization.reapply(expected_revision)
        )
        return {"error": {"code": refusal}} if refusal else result

    def tool_verification(self, run_id, step_index, call_id):
        rows = self.tool_requests(run_id)
        request = next(
            (
                r
                for r in rows
                if r["step_index"] == step_index and r["call_id"] == call_id
            ),
            None,
        )
        if request is None:
            raise ValueError("unknown_request")
        op = request["operation_id"]
        result = {
            "operation_id": op,
            "observed_at": format_instant(self._clock.wall_utc()),
            "state": "unrecorded",
            "related_run": None,
        }
        if op:
            with self._store.reading() as conn:
                row = conn.execute(
                    "SELECT state,verified_at,evidence_source,related_run "
                    "FROM tool_operation_verifications WHERE operation_id=?",
                    (op,),
                ).fetchone()
            if row:
                result.update(
                    zip(
                        ("state", "verified_at", "evidence_source", "related_run"),
                        row,
                        strict=True,
                    )
                )
        return result

    def read_tool_history(self, trace_root, **query):
        return self._tool_history.read(trace_root, **query)

    def tool_requests(self, run_id):
        return self._tool_metering.read(run_id)

    def recover_tool_metering(self):
        from agent_alfred.tools.metering import MeteringError

        try:
            return self.execute_mutation(self._tool_metering.recover)
        except MeteringError:
            return None, "metering_unconfirmed"

    def aggregate(
        self,
        *,
        session_id,
        goal,
        keywords,
        sources,
        gateway="cli",
        stream=False,
        wait_for_result=True,
    ):
        from agent_alfred.aggregation import AggregationRequest

        request = AggregationRequest(session_id, goal, keywords, sources)
        if not self.session_exists(request.session_id):
            raise ValueError("session_not_found")
        return self.submit(
            SubmitRequest(
                goal,
                purpose="aggregation",
                session_id=session_id,
                gateway=gateway,
                stream=stream,
                wait_for_result=wait_for_result,
                aggregation=request,
            )
        )

    def submit(self, request: SubmitRequest) -> SubmitResult:
        return self._admission.submit(request)

    def generate_consolidation(self, session_id: str) -> SubmitResult:
        """Trusted internal entry for one sessionless consolidation system Run."""
        if type(session_id) is not str or not session_id:
            raise ValueError("invalid_consolidation_session")
        return self.submit(
            SubmitRequest(
                message=session_id,
                purpose="consolidation",
                session_id=None,
                gateway="cli",
                wait_for_result=True,
            )
        )

    def _finalize_consolidation_run(self, conn, item) -> None:
        if item.request.purpose == "consolidation":
            self._memory_service.consolidation.finalize_run(conn, item.run_id)
        elif item.request.purpose == "chat":
            self._memory_service.consolidation.scheduling.record_chat(conn, item.run_id)

    def _schedule_saved_chat(self, run_id):
        scheduling = self._memory_service.consolidation.scheduling
        try:
            session_id, refusal = self.execute_mutation(
                lambda: scheduling.claim(run_id)
            )
        except sqlite3.Error:
            scheduling.finish_admission(run_id, "storage_read_failed")
            return
        if refusal is not None:
            scheduling.finish_admission(run_id, refusal)
            return
        if session_id is None:
            return
        try:
            result = self.submit(
                SubmitRequest(
                    message=session_id,
                    purpose="consolidation",
                    gateway="cli",
                    wait_for_result=False,
                    consolidation_trigger_run_id=run_id,
                )
            )
        except BaseException:
            scheduling.finish_admission(run_id, "scheduling_interrupted")
            raise
        scheduling.finish_admission(run_id, result.kind)

    def _notify_consolidation_run(self, item) -> None:
        self._memory_service.consolidation.notify_finalized()

    def _bind_consolidation_retry(self, conn, item) -> None:
        """Bind the retry intent in the accepted Run's own transaction."""
        request = item.request
        if request.purpose == "chat":
            import json

            from agent_alfred.routing_statistics import admission, initial_result

            item.routing = self.routing_snapshot()
            settings = (
                item.routing["settings"] if item.routing is not None
                else self._behaviour.snapshot() if self._behaviour is not None
                else {"status": "ok", "enabled": False}
            )
            conn.execute(
                "UPDATE runs SET routing_admission=? WHERE run_id=?",
                (json.dumps(admission(settings)), item.run_id),
            )
            item.memory_telemetry["routing_statistics"] = initial_result()
        if request.consolidation_trigger_run_id is not None:
            self._memory_service.consolidation.scheduling.bind_run(
                conn, request.consolidation_trigger_run_id, request.message, item.run_id
            )
        if request.purpose == "consolidation" and request.retry_batch_id is not None:
            self._memory_service.consolidation.bind_retry_run(
                conn,
                request.operation_id,
                request.retry_batch_id,
                request.expected_revision,
                item.run_id,
            )

    def retry_consolidation(
        self, batch_id: str, expected_revision: int, *, operation_id: str
    ):
        """Retry a failed batch: commit a valid candidate or start one new Run."""
        from agent_alfred.memory.commands import CommandContext
        from agent_alfred.memory.types import ManualOrigin

        context = CommandContext(ManualOrigin("cli"), "cli")
        result = self._memory_service.consolidation.retry(
            batch_id,
            expected_revision,
            context=context,
            operation_id=operation_id,
        )
        if result.get("error", {}).get("code") != "generation_required":
            return result
        batch = self._memory_service.consolidation.get_batch(batch_id)
        if batch is None:
            return result
        return self.submit(
            SubmitRequest(
                message=batch["session_id"],
                purpose="consolidation",
                session_id=None,
                gateway="cli",
                wait_for_result=True,
                retry_batch_id=batch_id,
                expected_revision=expected_revision,
                operation_id=operation_id,
            )
        )

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


class _NullCatalogTransport:
    def get(self, url, *, headers, timeout):
        del url, headers, timeout
        raise RuntimeError("catalog_transport_unavailable")


class _UrllibCatalogTransport:
    def get(self, url, *, headers, timeout):
        import json
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return {
                    "status": response.status,
                    "body": json.loads(response.read().decode("utf-8") or "{}"),
                }
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "body": {}}
