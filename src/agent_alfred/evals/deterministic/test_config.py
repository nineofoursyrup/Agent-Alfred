"""Immutable config snapshots and the versioned transport pool (#29)."""

from __future__ import annotations

import sqlite3
import threading

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.model import (
    ClientSnapshot,
    ModelAssignment,
    ScriptedModel,
    ScriptedModelFactory,
)
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.settings import Settings

SECRET = "supersecret-key-value"


def test_transport_pool_reuses_clients_until_the_config_version_changes() -> None:
    built: list[str] = []

    def build(snapshot: ClientSnapshot) -> object:
        built.append(snapshot.config_version)
        return object()

    pool = VersionedTransportPool(build)
    assignment = ModelAssignment(
        endpoint_id="opencode-go",
        model_id="deepseek-v4-flash",
        wire_style="openai",
    )
    first = ClientSnapshot(
        config_version="1",
        primary=assignment,
        retrieval_gate=None,
        api_key="k1",
        stream=False,
        stream_fallback=True,
        overall_deadline_s=None,
        per_attempt_timeout_s=60.0,
    )
    rotated = ClientSnapshot(
        config_version="2",
        primary=assignment,
        retrieval_gate=None,
        api_key="k2",
        stream=False,
        stream_fallback=True,
        overall_deadline_s=None,
        per_attempt_timeout_s=60.0,
    )
    a = pool.create(first)
    b = pool.create(first)
    assert a is b
    assert built == ["1"]
    c = pool.create(rotated)
    assert c is not a
    assert built == ["1", "2"]
    d = pool.create(rotated)
    assert d is c
    assert pool.cached_observation("opencode-go") is None
    assert pool.cached_catalog("opencode-go") is None


def test_same_run_keeps_the_admission_snapshot_next_run_sees_the_new_version() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    gate = threading.Event()
    model = ScriptedModel(["first", "second"], gate=gate)
    factory = ScriptedModelFactory(model)
    provider = MutableAssignmentProvider(
        endpoint_id="opencode-go",
        model_id="deepseek-v4-flash",
        wire_style="openai",
        api_key=SECRET,
        settings=Settings(),
    )
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    host = RuntimeHost(
        conn=conn,
        factory=factory,
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([capture], process_instance_id="proc-cfg"),
        process_instance_id="proc-cfg",
        snapshot_provider=provider,
        secrets=(SECRET,),
    )
    host.start()
    try:
        first = host.submit(SubmitRequest(message="one"))
        assert model.entered.wait(timeout=2)
        original = factory.snapshots[0]
        provider.assign_primary(model_id="other-model")
        gate.set()
        host.wait(first.run_id)
        assert factory.snapshots[0].primary.model_id == original.primary.model_id
        assert factory.snapshots[0].config_version == original.config_version
        second = host.submit(SubmitRequest(message="two"))
        host.wait(second.run_id)
        assert factory.snapshots[1].primary.model_id == "other-model"
        assert factory.snapshots[1].config_version != original.config_version
        dumped = repr([event.payload for event in capture.events])
        telemetry = " ".join(
            row[0]
            for row in conn.execute("SELECT telemetry FROM runs").fetchall()
            if row[0]
        )
        assert SECRET not in dumped
        assert SECRET not in telemetry
        blob = " ".join(
            str(cell)
            for row in conn.execute("SELECT * FROM runs").fetchall()
            for cell in row
        )
        assert SECRET not in blob
    finally:
        gate.set()
        host.close()
