"""Assemble a RuntimeHost from injected seams."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path, PurePath
from typing import Any

from agent_alfred.clock import Clock, SystemClock
from agent_alfred.database import open_database
from agent_alfred.events import BarrierFlushResult, EventSink, FanOutSink
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_PORT,
    EntryDescriptor,
    ProcessLock,
)
from agent_alfred.gateway.web.server import DashboardRuntime, DatabaseOpener
from agent_alfred.managed_state import (
    ManagedPathSecurityError,
    ManagedStateDirectory,
    ManagedStateLease,
    ManagedTraceRoot,
)
from agent_alfred.model import (
    ClientSnapshot,
    EndpointUnconfigured,
    ModelClient,
    ModelClientFactory,
    ModelRef,
)
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.redact import Redactor
from agent_alfred.resource_rollback import (
    ConstructionOwner,
    ResumableRollback,
    RollbackSlot,
)
from agent_alfred.retry import RetryPolicy, SystemSleeper
from agent_alfred.runtime.config import SettingsBackedSnapshotProvider
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.runtime.snapshot import RuntimeSnapshot
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.settings import (
    OPENCODE_GO_BASE_URL,
    Settings,
    load_settings,
    resolve_state_dir,
)
from agent_alfred.stream_fallback import StreamFallback
from agent_alfred.trace import RunBundleTraceSink


class OpenCodeGoFactory:
    def __init__(self, *, clock: Clock):
        self._clock = clock
        self._pool = VersionedTransportPool(self._build_transport)

    def _build_transport(self, snapshot: ClientSnapshot) -> object:
        if snapshot.api_key is None:
            raise EndpointUnconfigured("endpoint_unconfigured")
        from openai import OpenAI

        return OpenAI(
            base_url=OPENCODE_GO_BASE_URL,
            api_key=snapshot.api_key,
        )

    def create(self, snapshot: ClientSnapshot) -> ModelClient:
        if snapshot.api_key is None:
            raise EndpointUnconfigured("endpoint_unconfigured")
        transport = self._pool.client_for(snapshot)
        model = ModelRef(
            endpoint_id=snapshot.endpoint_id, model_id=snapshot.model_id
        )
        streaming = OpenAICompatibleAdapter(
            client=transport, model=model, stream=True
        )
        nonstream = OpenAICompatibleAdapter(
            client=transport, model=model, stream=False
        )
        return RetryPolicy(
            StreamFallback(
                streaming,
                clock=self._clock,
                stream=snapshot.stream,
                stream_fallback=snapshot.stream_fallback,
                per_attempt_timeout_s=snapshot.per_attempt_timeout_s,
                nonstream=nonstream,
            ),
            clock=self._clock,
            sleeper=SystemSleeper(),
        )


def _secrets_from_env(settings: Settings) -> tuple[str, ...]:
    import os

    raw = os.environ.get(settings.api_key_env)
    if raw is None:
        return ()
    value = raw.strip()
    if len(value) < 8:
        return ()
    return (value,)


class UnavailableTraceSink:
    """Fail-closed stand-in when the real TraceSink cannot be assembled.

    It accepts every event (counting it as lost) and reports ``failed`` on
    every flush, so a Run served through it can never be recorded as
    ``trace_incomplete=false``. The reply itself is still delivered.
    """

    name = "trace"
    flush_at_run_end = True

    def __init__(self, *, detail: str):
        self._detail = detail
        self._dropped = 0

    def prepare(self, event: object) -> object:
        del event
        return None

    def commit(self, prepared: object, event: object) -> None:
        del prepared, event
        self._dropped += 1

    def flush(self, run_id: str) -> BarrierFlushResult:
        del run_id
        return BarrierFlushResult(
            outcome="failed", dropped_events=self._dropped, detail=self._detail
        )

    def close(self) -> None:
        return None


TraceRootRequest = tuple[ManagedStateLease, PurePath]


def _trace_sink(
    trace_root: Path | ManagedTraceRoot | TraceRootRequest,
    clock: Clock,
    instance_id: str,
    *,
    _rollback: ResumableRollback | None = None,
) -> EventSink:
    """The production durability-critical sink, or an honest failure stub.

    Initialization failure must not remove durability from the barrier: the
    stub keeps the barrier critical and permanently incomplete instead of
    letting a Run claim a trace that was never written.

    Trace initialization is the one construction step allowed to fail and let
    the build continue, so its cleanup runs in a scope of its own: retrying
    the caller's owner here would close the connection and the state lease the
    Host is about to be handed, and mark them complete besides. Only the built
    sink crosses into ``_rollback``, the caller's construction owner, and it
    does so before this returns -- so one reachable owner spans this return
    edge and the caller's store.

    Either way this **consumes** the trace root: one already minted by the
    caller is owned by this scope exactly like one acquired here, and a
    failure closes it before the honest stub goes back. A root that outlived
    the sink built on it would have no owner left to close it.

    The scope is why this seam does not use :class:`ConstructionOwner`: that
    is for a seam whose cleanup may reach the caller's own resources, and
    this one must never.
    """
    acquired: ManagedTraceRoot | None = None
    scope = ResumableRollback()
    try:
        if isinstance(trace_root, tuple):
            state, relative = trace_root
            acquired = state.ensure_trace_directory(relative, _rollback=scope)
        elif isinstance(trace_root, Path):
            acquired = ManagedStateDirectory.acquire_trace_root(
                trace_root, _rollback=scope
            )
        else:
            acquired = trace_root
        scope.own(acquired)
        sink = RunBundleTraceSink(
            root=acquired,
            clock=clock,
            process_instance_id=instance_id,
            _rollback=scope,
        )
        if _rollback is not None:
            # Own-before-release into the caller's owner. The sink is newer
            # than whatever that owner already holds, so it takes the last
            # position and reverse-order cleanup reaches it first -- which a
            # transfer, landing it at the earliest position, would invert.
            # The brief overlap closes nothing twice: ``close`` is idempotent
            # and resumable, and both sides reach the same sink.
            _rollback.own(sink, sink.close)
            scope.transfer(sink)
        return sink
    except ManagedPathSecurityError as exc:
        scope.raise_failure(exc)
    except Exception as exc:
        if not scope.retry():
            scope.raise_incomplete(exc)
        return UnavailableTraceSink(
            detail=f"trace_sink_init_failed {type(exc).__name__}"
        )
    except BaseException as exc:
        scope.raise_failure(exc)


def build_host(
    *,
    conn: sqlite3.Connection,
    factory: ModelClientFactory,
    settings: Settings | None = None,
    clock: Clock | None = None,
    extra_sinks: Sequence[EventSink] = (),
    process_instance_id: str | None = None,
    trace_root: Path | ManagedTraceRoot | TraceRootRequest | None = None,
    snapshot_listener: Callable[[RuntimeSnapshot], None] | None = None,
    _rollback: ResumableRollback | None = None,
) -> RuntimeHost:
    settings = settings or Settings()
    clock = clock or SystemClock()
    instance_id = process_instance_id or uuid.uuid4().hex
    secrets = _secrets_from_env(settings)
    redactor = Redactor(secrets)
    owner = ConstructionOwner(_rollback)
    rollback = owner.rollback
    try:
        sinks: list[EventSink] = []
        if trace_root is not None:
            trace_sink = _trace_sink(
                trace_root, clock, instance_id, _rollback=rollback
            )
            sinks.append(trace_sink)
            rollback.own(trace_sink)
        for sink in extra_sinks:
            sinks.append(sink)
            rollback.own(sink)
        fanout = FanOutSink(
            sinks, process_instance_id=instance_id, redactor=redactor
        )
        rollback.own(fanout)
        for sink in sinks:
            rollback.transfer(sink)
        provider = SettingsBackedSnapshotProvider(settings)
        host = RuntimeHost(
            conn=conn,
            factory=factory,
            settings=settings,
            clock=clock,
            fanout=fanout,
            process_instance_id=instance_id,
            redactor=redactor,
            snapshot_provider=provider,
            snapshot_listener=snapshot_listener,
        )
        # The Host owns the FanOut from here; publishing the aggregate before
        # its part retires keeps one reachable owner across this return edge
        # and the caller's store. Both closes are resumable and idempotent, so
        # the overlap can never close a sink twice.
        owner.publish(host, parts=(fanout,))
        return host
    except BaseException as exc:
        owner.fail(exc)


def build_dashboard(
    *,
    state_dir: Path,
    settings: Settings | None = None,
    factory: ModelClientFactory | None = None,
    clock: Clock | None = None,
    trace_root: Path | None = None,
    port: int = DEFAULT_PORT,
    extra_sinks: Sequence[EventSink] = (),
    instance_id: str | None = None,
    open_database: DatabaseOpener | None = None,
    server_factory: Any = None,
    write_descriptor: Callable[[Path, EntryDescriptor], Path] | None = None,
    lock: Callable[[Any], ProcessLock] | None = None,
    pid: int | None = None,
) -> DashboardRuntime:
    """Build the one Dashboard object. Take no ownership yet.

    Nothing here takes the lock, binds a socket or opens the database --
    those are process-level facts and :meth:`DashboardRuntime.start` owns
    them, in that order. What this function assembles is the one thing that
    has to exist *before* any of that can happen and *after* it is decided:
    how a Host and a broker are built around one connection.

    That order inside :meth:`start` is forced by a real dependency loop
    rather than by convention:

    1. the broker is built first, because the Host's authoritative state
       store publishes patches into it from its very first transition --
       including the ones start-up recovery makes;
    2. the Host is built with the broker as an event sink *and* as the
       snapshot listener, so what a browser sees is the state the Host
       decided, published after the decision;
    3. the broker is then pointed at the Host, which is the only thing that
       can answer "does this Session exist".
    """
    clock = clock or SystemClock()
    settings = settings or Settings()
    resolved_factory = factory or OpenCodeGoFactory(clock=clock)
    captured_state: list[ManagedStateLease] = []
    construction_rollback = RollbackSlot()

    def dashboard_database(
        state: ManagedStateLease, *, _rollback: ResumableRollback
    ) -> sqlite3.Connection:
        captured_state[:] = [state]
        opener = open_database or globals()["open_database"]
        return opener(state, _rollback=_rollback)

    def assemble(
        conn: sqlite3.Connection, instance: str
    ) -> tuple[RuntimeHost, SSEBroker]:
        state = captured_state[0]
        if trace_root is None or Path(trace_root) == state.path / "traces":
            managed_trace: Path | TraceRootRequest = (
                state,
                PurePath("traces"),
            )
        else:
            managed_trace = Path(trace_root)
        rollback = ResumableRollback()
        construction_rollback.begin(rollback)
        try:
            broker = SSEBroker(
                process_instance_id=instance,
                snapshot=RuntimeSnapshot(
                    process_instance_id=instance,
                    state_revision=0,
                    coordinator_state="idle",
                    active_run=None,
                    unrecorded_terminal_projection=None,
                ),
            )
            rollback.own(broker)
            host = build_host(
                conn=conn,
                factory=resolved_factory,
                settings=settings,
                clock=clock,
                trace_root=managed_trace,
                extra_sinks=[broker, *extra_sinks],
                process_instance_id=instance,
                snapshot_listener=broker.publish_state_patch,
                _rollback=rollback,
            )
            rollback.own(host)
            broker.bind_session_check(host.transport_session_validity)

            def note_dispatcher_fatal(exc: BaseException) -> None:
                """Publish only the fixed, machine-safe dispatcher diagnosis.

                The broker keeps the original exception in its in-memory fatal
                latch for local causal diagnosis. Its message may contain user
                or credential text, so this domain-event boundary deliberately
                drops the callback reference instead of copying it into an event
                or trace.
                """
                del exc
                assert host is not None
                host.note_sink_disabled(broker.name, "dispatch")

            # A dead dispatcher is the one failure the broker cannot fix on its
            # own, so it is reported where a process-level fact belongs: into
            # the trace, through the same notice every other sink failure uses.
            broker.bind_fatal_handler(note_dispatcher_fatal)
            # The construction owner is *not* retired here: the assembled pair
            # is still a return value nobody has stored. DashboardRuntime
            # settles this slot only after both ``_host`` and ``_broker`` are
            # published, so one reachable owner spans that edge -- including
            # the interrupt point between those two stores.
            return host, broker
        except BaseException as exc:
            rollback.raise_failure(exc)

    return DashboardRuntime(
        state_dir=state_dir,
        assemble=assemble,
        port=port,
        instance_id=instance_id,
        open_database=dashboard_database,
        server_factory=server_factory,
        write_descriptor=write_descriptor,
        lock=lock,
        pid=pid,
        construction_rollback=construction_rollback,
        trace_root=trace_root,
    )


def build_default_host(
    *,
    state_dir: Path | None = None,
    settings: Settings | None = None,
    factory: ModelClientFactory | None = None,
    _rollback: ResumableRollback | None = None,
) -> RuntimeHost:
    """Build the standalone Host and everything it owns.

    ``_rollback`` is a caller-established construction owner. A caller that
    offers one keeps a reachable owner for the assembled Host across this
    return edge and its own store; without one, the Host that reaches the
    caller is its own sole owner, exactly as before.
    """
    clock = SystemClock()
    settings = settings or load_settings()
    directory = state_dir or resolve_state_dir()
    owner = ConstructionOwner(_rollback)
    rollback = owner.rollback
    try:
        state = ManagedStateDirectory.acquire(directory, _rollback=rollback)
        # Idempotent: the seam already registered its result in this rollback.
        # Re-stating it here keeps the construction owner correct for any
        # substituted opener that does not take the owner it was offered.
        rollback.own(state)
        conn = open_database(state, _rollback=rollback)
        rollback.own(conn)
        if factory is None:
            factory = OpenCodeGoFactory(clock=clock)
        host = build_host(
            conn=conn,
            factory=factory,
            settings=settings,
            clock=clock,
            trace_root=(state, PurePath("traces")),
            _rollback=rollback,
        )
        rollback.own(host)
        host.attach_owned_resources(conn, state, source=rollback)
        owner.publish(host)
    except BaseException as exc:
        owner.fail(exc)
    return host
