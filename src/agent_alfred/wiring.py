"""Assemble a RuntimeHost from injected seams."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
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
from agent_alfred.gateway.web.server import DashboardRuntime
from agent_alfred.model import (
    ClientSnapshot,
    EndpointUnconfigured,
    ModelClient,
    ModelClientFactory,
    ModelRef,
)
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.redact import Redactor
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


def _trace_sink(trace_root: Path, clock: Clock, instance_id: str) -> EventSink:
    """The production durability-critical sink, or an honest failure stub.

    Initialization failure must not remove durability from the barrier: the
    stub keeps the barrier critical and permanently incomplete instead of
    letting a Run claim a trace that was never written.
    """
    try:
        return RunBundleTraceSink(
            root=trace_root, clock=clock, process_instance_id=instance_id
        )
    except Exception as exc:
        return UnavailableTraceSink(
            detail=f"trace_sink_init_failed {type(exc).__name__}"
        )


def build_host(
    *,
    conn: sqlite3.Connection,
    factory: ModelClientFactory,
    settings: Settings | None = None,
    clock: Clock | None = None,
    extra_sinks: Sequence[EventSink] = (),
    process_instance_id: str | None = None,
    trace_root: Path | None = None,
    snapshot_listener: Callable[[RuntimeSnapshot], None] | None = None,
) -> RuntimeHost:
    settings = settings or Settings()
    clock = clock or SystemClock()
    instance_id = process_instance_id or uuid.uuid4().hex
    secrets = _secrets_from_env(settings)
    redactor = Redactor(secrets)
    sinks: list[EventSink] = []
    if trace_root is not None:
        sinks.append(_trace_sink(trace_root, clock, instance_id))
    sinks.extend(extra_sinks)
    fanout = FanOutSink(sinks, process_instance_id=instance_id, redactor=redactor)
    provider = SettingsBackedSnapshotProvider(settings)
    return RuntimeHost(
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
    open_database: Callable[[Path], sqlite3.Connection] | None = None,
    server_factory: Any = None,
    write_descriptor: Callable[[Path, EntryDescriptor], Path] | None = None,
    lock: ProcessLock | None = None,
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

    def assemble(
        conn: sqlite3.Connection, instance: str
    ) -> tuple[RuntimeHost, SSEBroker]:
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
        host = build_host(
            conn=conn,
            factory=resolved_factory,
            settings=settings,
            clock=clock,
            trace_root=trace_root,
            extra_sinks=[broker, *extra_sinks],
            process_instance_id=instance,
            snapshot_listener=broker.publish_state_patch,
        )
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
            host.note_sink_disabled(broker.name, "dispatch")

        # A dead dispatcher is the one failure the broker cannot fix on its
        # own, so it is reported where a process-level fact belongs: into
        # the trace, through the same notice every other sink failure uses.
        broker.bind_fatal_handler(note_dispatcher_fatal)
        return host, broker

    return DashboardRuntime(
        state_dir=state_dir,
        assemble=assemble,
        port=port,
        instance_id=instance_id,
        open_database=open_database,
        server_factory=server_factory,
        write_descriptor=write_descriptor,
        lock=lock,
        pid=pid,
    )


def build_default_host(
    *,
    state_dir: Path | None = None,
    settings: Settings | None = None,
    factory: ModelClientFactory | None = None,
) -> RuntimeHost:
    clock = SystemClock()
    settings = settings or load_settings()
    directory = state_dir or resolve_state_dir()
    conn = open_database(directory)
    if factory is None:
        factory = OpenCodeGoFactory(clock=clock)
    return build_host(
        conn=conn,
        factory=factory,
        settings=settings,
        clock=clock,
        trace_root=directory / "traces",
    )
