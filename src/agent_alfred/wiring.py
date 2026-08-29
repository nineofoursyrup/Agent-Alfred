"""Assemble a RuntimeHost from injected seams."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from agent_alfred import schema
from agent_alfred.clock import Clock, SystemClock
from agent_alfred.events import BarrierFlushResult, EventSink, FanOutSink
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.lifecycle import DEFAULT_HOST, DEFAULT_PORT
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


def open_database(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(mode=0o700, exist_ok=True)
    state_dir.chmod(0o700)
    path = state_dir / "db.sqlite3"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        path.chmod(0o600)
        schema.migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


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
    conn: sqlite3.Connection,
    state_dir: Path,
    settings: Settings | None = None,
    factory: ModelClientFactory | None = None,
    clock: Clock | None = None,
    trace_root: Path | None = None,
    port: int = DEFAULT_PORT,
    bind_host: str = DEFAULT_HOST,
    extra_sinks: Sequence[EventSink] = (),
) -> tuple[RuntimeHost, DashboardRuntime]:
    """One process: one Host, one broker, one socket, one descriptor.

    The order is the decided one, and it is forced by a real dependency loop
    rather than by convention:

    1. the broker is built first, because the Host's authoritative state
       store publishes patches into it from its very first transition --
       including the ones start-up recovery makes;
    2. the Host is built with the broker as an event sink *and* as the
       snapshot listener, so what a browser sees is the state the Host
       decided, published after the decision;
    3. the broker is then pointed at the Host, which is the only thing that
       can answer "does this Session exist";
    4. the Dashboard runtime takes the lock, binds, describes -- in that
       order -- and only then serves.

    Nothing can reach the socket before step 4, so the broker is never asked
    a question it cannot answer.
    """
    clock = clock or SystemClock()
    settings = settings or Settings()
    instance_id = uuid.uuid4().hex
    broker = SSEBroker(
        process_instance_id=instance_id,
        snapshot=RuntimeSnapshot(
            process_instance_id=instance_id,
            state_revision=0,
            coordinator_state="idle",
            active_run=None,
            unrecorded_terminal_projection=None,
        ),
    )
    if factory is None:
        factory = OpenCodeGoFactory(clock=clock)
    host = build_host(
        conn=conn,
        factory=factory,
        settings=settings,
        clock=clock,
        trace_root=trace_root,
        extra_sinks=[broker, *extra_sinks],
        process_instance_id=instance_id,
        snapshot_listener=broker.publish_state_patch,
    )
    broker.bind_session_check(host.session_exists)
    # A dead dispatcher is the one failure the broker cannot fix on its own,
    # so it is reported where a process-level fact belongs: into the trace,
    # through the same notice every other sink failure uses.
    broker.bind_fatal_handler(
        lambda exc: host.note_sink_disabled("sse", "dispatch")
    )
    dashboard = DashboardRuntime(
        host=host,
        state_dir=state_dir,
        broker=broker,
        port=port,
        bind_host=bind_host,
    )
    return host, dashboard


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
