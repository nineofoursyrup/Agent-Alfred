"""Row inference probes target a verified assigned model (#29 §10, #34 F1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import StoreBackedSnapshotProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.model_settings import ModelSettingsStore
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.settings import (
    DEFAULT_ENDPOINT_ID,
    DEFAULT_MODEL_ID,
    OPENCODE_API_KEY_ENV,
    Settings,
)
from agent_alfred.settings_commands import assign, pin

PRIMARY_KEY = "sk-primary-opencode-key"
OTHER_KEY = "sk-other-openai-key"
GATE_MODEL = "qwen3.7-max"
OTHER_ENDPOINT = "openai"
OTHER_MODEL = "gpt-test"


def _host(tmp_path: Path, *, environ: dict[str, str] | None = None, factory=None):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    settings = Settings()
    env = {OPENCODE_API_KEY_ENV: PRIMARY_KEY} if environ is None else environ
    model = ScriptedModel(["probe-ok"])
    chosen = factory or ScriptedModelFactory(model)
    host = RuntimeHost(
        conn=conn,
        factory=chosen,
        settings=settings,
        clock=FakeClock(),
        fanout=FanOutSink(
            [CapturingSink(name="c", flush_at_run_end=True)],
            process_instance_id="proc-f1",
        ),
        process_instance_id="proc-f1",
        snapshot_provider=StoreBackedSnapshotProvider(store, settings, environ=env),
        model_settings=store,
        secrets=(PRIMARY_KEY, OTHER_KEY),
    )
    return host, store, chosen, conn


def _mutate(host: RuntimeHost, transform):
    revision = host._model_settings.snapshot().revision
    assert host.try_begin_mutation() is None
    try:
        return host.mutate_model_settings(revision, transform)
    finally:
        host.end_mutation()


def test_same_endpoint_gate_probe_uses_gate_style_not_primary(tmp_path: Path) -> None:
    model = ScriptedModel(["probe-ok"])
    factory = ScriptedModelFactory(model)
    host, store, factory, conn = _host(tmp_path, factory=factory)
    _mutate(
        host,
        lambda snap: assign(
            pin(snap, endpoint_id=DEFAULT_ENDPOINT_ID, model_id=GATE_MODEL),
            slot="retrieval_gate",
            endpoint_id=DEFAULT_ENDPOINT_ID,
            model_id=GATE_MODEL,
        ),
    )
    before = store.snapshot().assignments
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id=GATE_MODEL,
            )
        )
        assert submitted.kind == "accepted"
        host.wait(submitted.run_id)
        captured = factory.snapshots[-1]
        assert captured.model_id == GATE_MODEL
        assert captured.endpoint_id == DEFAULT_ENDPOINT_ID
        assert captured.wire_style == "anthropic"
        assert captured.api_key == PRIMARY_KEY
        assert captured.primary.model_id == GATE_MODEL
        assert store.snapshot().assignments == before
        assert store.snapshot().assignments.primary.model_id == DEFAULT_MODEL_ID
        assert conn.execute("SELECT COUNT(*) FROM agent_log").fetchone() == (0,)
        assert model.requests[-1].model.model_id == GATE_MODEL
        assert model.requests[-1].model.endpoint_id == DEFAULT_ENDPOINT_ID
    finally:
        host.close()


def test_cross_endpoint_probe_does_not_reuse_primary_credentials(
    tmp_path: Path,
) -> None:
    pool = VersionedTransportPool(lambda snapshot: snapshot)

    class ObservedFactory(ScriptedModelFactory):
        transport_pool = pool

        def create(self, snapshot):
            pool.client_for(snapshot)
            return super().create(snapshot)

    factory = ObservedFactory(ScriptedModel(["probe-ok"]))
    host, store, factory, _conn = _host(
        tmp_path,
        factory=factory,
        environ={
            OPENCODE_API_KEY_ENV: PRIMARY_KEY,
            "OPENAI_API_KEY": OTHER_KEY,
        },
    )
    _mutate(
        host,
        lambda snap: assign(
            pin(
                snap,
                endpoint_id=OTHER_ENDPOINT,
                model_id=OTHER_MODEL,
                wire_style_override="openai",
            ),
            slot="retrieval_gate",
            endpoint_id=OTHER_ENDPOINT,
            model_id=OTHER_MODEL,
        ),
    )
    before = store.snapshot().assignments
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id=OTHER_ENDPOINT,
                model_id=OTHER_MODEL,
            )
        )
        assert submitted.kind == "accepted"
        host.wait(submitted.run_id)
        captured = factory.snapshots[-1]
        assert captured.endpoint_id == OTHER_ENDPOINT
        assert captured.model_id == OTHER_MODEL
        assert captured.api_key == OTHER_KEY
        assert captured.api_key != PRIMARY_KEY
        assert captured.wire_style == "openai"
        observed = pool.cached_observation(OTHER_ENDPOINT)
        assert observed is not None
        assert observed["checked_via"] == "inference_probe"
        assert pool.cached_observation(DEFAULT_ENDPOINT_ID) is None
        assert store.snapshot().assignments == before
    finally:
        host.close()


def test_unassigned_and_stale_probe_targets_are_rejected(tmp_path: Path) -> None:
    factory = ScriptedModelFactory(ScriptedModel(["should-not-run"]))
    host, store, factory, conn = _host(tmp_path, factory=factory)
    _mutate(
        host,
        lambda snap: pin(
            snap,
            endpoint_id=DEFAULT_ENDPOINT_ID,
            model_id=GATE_MODEL,
        ),
    )
    before = store.snapshot().assignments
    host.start()
    try:
        unassigned = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id=GATE_MODEL,
            )
        )
        assert unassigned.kind == "invalid_probe_target"
        assert unassigned.run_id is None
        assert factory.snapshots == []
        assert store.snapshot().assignments == before

        _mutate(
            host,
            lambda snap: assign(
                snap,
                slot="retrieval_gate",
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id=GATE_MODEL,
            ),
        )
        from dataclasses import replace as dc_replace

        from agent_alfred.runtime.model_settings import Assignments

        _mutate(
            host,
            lambda snap: dc_replace(
                snap,
                assignments=Assignments(
                    primary=snap.assignments.primary, retrieval_gate=None
                ),
            ),
        )
        stale = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id=GATE_MODEL,
            )
        )
        assert stale.kind == "invalid_probe_target"
        assert factory.snapshots == []
        assert store.snapshot().assignments.retrieval_gate is None
        assert store.snapshot().assignments.primary.model_id == DEFAULT_MODEL_ID
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    finally:
        host.close()


def test_probe_without_key_is_refused_before_factory(tmp_path: Path) -> None:
    factory = ScriptedModelFactory(ScriptedModel(["should-not-run"]))
    host, store, factory, conn = _host(tmp_path, factory=factory, environ={})
    before = store.snapshot().assignments
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id=DEFAULT_MODEL_ID,
            )
        )
        assert submitted.kind == "endpoint_unconfigured"
        assert submitted.run_id is None
        assert factory.snapshots == []
        assert store.snapshot().assignments == before
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    finally:
        host.close()
