"""Assemble a RuntimeHost from injected seams."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from pathlib import Path

from agent_alfred import schema
from agent_alfred.clock import Clock, SystemClock
from agent_alfred.events import EventSink, FanOutSink
from agent_alfred.model import ClientSnapshot, ModelClient, ModelClientFactory, ModelRef
from agent_alfred.openai_compatible import OpenAICompatibleAdapter, UnconfiguredClient
from agent_alfred.redact import Redactor
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.settings import OPENCODE_GO_BASE_URL, Settings, resolve_state_dir


class OpenCodeGoFactory:
    def __init__(self, *, clock: Clock, settings: Settings):
        self._clock = clock
        self._settings = settings

    def create(self, snapshot: ClientSnapshot) -> ModelClient:
        if snapshot.api_key is None:
            return UnconfiguredClient()
        from openai import OpenAI

        client = OpenAI(
            base_url=OPENCODE_GO_BASE_URL,
            api_key=snapshot.api_key,
        )
        return OpenAICompatibleAdapter(
            client=client,
            model=ModelRef(
                endpoint_id=snapshot.endpoint_id, model_id=snapshot.model_id
            ),
            clock=self._clock,
        )


def open_database(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass
    path = state_dir / "db.sqlite3"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    schema.migrate(conn)
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


def build_host(
    *,
    conn: sqlite3.Connection,
    factory: ModelClientFactory,
    settings: Settings | None = None,
    clock: Clock | None = None,
    extra_sinks: Sequence[EventSink] = (),
    process_instance_id: str | None = None,
    preview_redactor: None | object = None,
) -> RuntimeHost:
    settings = settings or Settings()
    clock = clock or SystemClock()
    instance_id = process_instance_id or uuid.uuid4().hex
    secrets = _secrets_from_env(settings)
    redactor = Redactor(secrets)
    fanout = FanOutSink(
        extra_sinks, process_instance_id=instance_id, redactor=redactor
    )
    redact = preview_redactor if callable(preview_redactor) else redactor.redact_text
    return RuntimeHost(
        conn=conn,
        factory=factory,
        settings=settings,
        clock=clock,
        fanout=fanout,
        process_instance_id=instance_id,
        secrets=secrets,
        preview_redactor=redact,
    )


def build_default_host(*, state_dir: Path | None = None) -> RuntimeHost:
    clock = SystemClock()
    settings = Settings()
    directory = state_dir or resolve_state_dir()
    conn = open_database(directory)
    factory = OpenCodeGoFactory(clock=clock, settings=settings)
    return build_host(conn=conn, factory=factory, settings=settings, clock=clock)
