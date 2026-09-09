"""Host-exclusive ModelSettingsStore, admission capture, and MutationGate (#34)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import StoreBackedSnapshotProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.model_settings import ModelSettingsError, ModelSettingsStore
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.settings import (
    DEFAULT_ENDPOINT_ID,
    DEFAULT_MODEL_ID,
    DEFAULT_WIRE_STYLE,
    OPENCODE_API_KEY_ENV,
    Settings,
)

SECRET = "sk-test-s2-secret"


def test_capture_includes_assignment_style_and_in_memory_key(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    provider = StoreBackedSnapshotProvider(
        store,
        Settings(),
        environ={OPENCODE_API_KEY_ENV: SECRET},
    )
    captured = provider.capture()
    assert captured.primary.endpoint_id == DEFAULT_ENDPOINT_ID
    assert captured.primary.model_id == DEFAULT_MODEL_ID
    assert captured.primary.wire_style == DEFAULT_WIRE_STYLE
    assert captured.api_key == SECRET
    assert captured.config_version


def _host_with_store(
    tmp_path: Path,
    *,
    script: list | None = None,
    factory=None,
    extra_sinks=None,
    environ: dict[str, str] | None = None,
):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    settings = Settings()
    provider = StoreBackedSnapshotProvider(
        store, settings, environ=environ or {OPENCODE_API_KEY_ENV: SECRET}
    )
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    sinks = [capture, *(extra_sinks or ())]
    model = ScriptedModel(script or ["pong"])
    host = RuntimeHost(
        conn=conn,
        factory=factory or ScriptedModelFactory(model),
        settings=settings,
        clock=FakeClock(),
        fanout=FanOutSink(sinks, process_instance_id="proc-s2"),
        process_instance_id="proc-s2",
        snapshot_provider=provider,
        model_settings=store,
        secrets=(SECRET,),
    )
    return host, store, provider


def test_settings_invalid_blocks_chat_and_writeback(tmp_path: Path) -> None:
    path = tmp_path / "model_settings.json"
    path.write_bytes(b"{broken")
    original = path.read_bytes()
    store = ModelSettingsStore(path)
    store.load()
    provider = StoreBackedSnapshotProvider(store, Settings(), environ={})
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(["pong"])),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink(
            [CapturingSink(name="c", flush_at_run_end=True)],
            process_instance_id="proc-s2-invalid",
        ),
        process_instance_id="proc-s2-invalid",
        snapshot_provider=provider,
        model_settings=store,
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind != "accepted"
        assert store.snapshot().status == "settings_invalid"
        with pytest.raises(ModelSettingsError) as caught:
            host.mutate_model_settings(0, lambda snap: snap)
        assert caught.value.code == "settings_invalid"
        assert path.read_bytes() == original
    finally:
        host.close()


def test_run_holds_gate_against_settings_write(tmp_path: Path) -> None:
    gate = threading.Event()
    model = ScriptedModel(["one"], gate=gate)
    host, store, _provider = _host_with_store(
        tmp_path, factory=ScriptedModelFactory(model)
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hold"))
        assert submitted.kind == "accepted"
        assert model.entered.wait(timeout=2)
        assert host.try_begin_mutation() == "mutation_in_flight"
        assert store.snapshot().revision == 0
        assert not (tmp_path / "model_settings.json").exists()
    finally:
        gate.set()
        host.close()


def test_successful_mutation_bumps_version_and_evicts_pool(tmp_path: Path) -> None:
    built: list[str] = []

    def build(snapshot):
        built.append(snapshot.config_version)
        return object()

    pool = VersionedTransportPool(build)

    class PoolFactory(ScriptedModelFactory):
        def __init__(self):
            super().__init__(ScriptedModel(["pong"]))
            self.pool = pool

        def create(self, snapshot):
            self.pool.client_for(snapshot)
            return super().create(snapshot)

        def invalidate_endpoint(self, endpoint_id: str) -> None:
            self.pool.discard(endpoint_id)

    factory = PoolFactory()
    host, _store, provider = _host_with_store(tmp_path, factory=factory)
    first = provider.capture()
    factory.create(first)
    pool.store_catalog(DEFAULT_ENDPOINT_ID, {"models": ("cached",)})
    assert pool.cached_catalog(DEFAULT_ENDPOINT_ID) == {"models": ("cached",)}
    host.start()
    try:
        assert host.try_begin_mutation() is None
        try:
            host.mutate_model_settings(0, lambda snap: snap)
        finally:
            host.end_mutation()
        assert pool.cached_catalog(DEFAULT_ENDPOINT_ID) is None
        second = provider.capture()
        assert second.config_version != first.config_version
        factory.create(second)
        assert built == [first.config_version, second.config_version]
    finally:
        host.close()


def test_dotenv_reread_invalidates_pool_and_next_capture_sees_file_value(
    tmp_path: Path, monkeypatch
) -> None:
    import os

    from agent_alfred.connections import CredentialOverlay

    env_file = tmp_path / ".env"
    env_file.write_text(f"{OPENCODE_API_KEY_ENV}=file-key-aaaa\n", encoding="utf-8")
    monkeypatch.delenv(OPENCODE_API_KEY_ENV, raising=False)
    before = os.environ.get(OPENCODE_API_KEY_ENV)
    overlay = CredentialOverlay(process_env={}, dotenv_path=str(env_file))
    pool = VersionedTransportPool(lambda snapshot: snapshot)

    class PoolFactory(ScriptedModelFactory):
        def __init__(self):
            super().__init__(ScriptedModel(["pong"]))

        def invalidate_all(self) -> None:
            pool.discard_all()

    factory = PoolFactory()
    host, _store, provider = _host_with_store(
        tmp_path, factory=factory, environ=overlay.values()
    )
    host._credentials = overlay
    first = provider.capture()
    assert first.api_key == "file-key-aaaa"
    pool.store_catalog(DEFAULT_ENDPOINT_ID, {"models": ("cached",)})
    pool.store_observation(DEFAULT_ENDPOINT_ID, {"state": "connected"})
    env_file.write_text(f"{OPENCODE_API_KEY_ENV}=file-key-bbbb\n", encoding="utf-8")
    host.reread_dotenv()
    assert pool.cached_catalog(DEFAULT_ENDPOINT_ID) is None
    assert pool.cached_observation(DEFAULT_ENDPOINT_ID) is None
    second = provider.capture()
    assert second.api_key == "file-key-bbbb"
    assert second.config_version != first.config_version
    assert os.environ.get(OPENCODE_API_KEY_ENV) == before


def test_stale_catalog_on_models_page_keeps_success_error_and_retry(
    tmp_path: Path,
) -> None:
    from agent_alfred.evals.deterministic.test_catalog import ScriptedTransport

    pool = VersionedTransportPool(lambda snapshot: snapshot)

    class PoolFactory(ScriptedModelFactory):
        transport_pool = pool

    transport = ScriptedTransport(
        [
            {"status": 200, "body": {"data": [{"id": "kept"}]}},
            {"status": 500, "body": {"error": "boom"}},
        ]
    )
    host, _store, _provider = _host_with_store(
        tmp_path, factory=PoolFactory(ScriptedModel(["pong"]))
    )
    host._catalog_transport = transport
    clock = host._clock
    first = host.models()
    assigned = next(
        group
        for group in first["endpoints"]
        if group["endpoint_id"] == DEFAULT_ENDPOINT_ID
    )
    assert assigned["catalog"]["health"] == "fresh"
    assert assigned["catalog"]["last_success_at"]
    clock.monotonic_value = 300
    second = host.models()
    assigned = next(
        group
        for group in second["endpoints"]
        if group["endpoint_id"] == DEFAULT_ENDPOINT_ID
    )
    assert assigned["catalog"]["health"] == "stale"
    assert assigned["catalog"]["last_success_at"]
    assert assigned["catalog"]["last_error"] == "http_500"
    assert assigned["catalog"]["retry_at"]


def test_catalog_failure_leaves_assignment_and_scripted_chat_accepted(
    tmp_path: Path,
) -> None:
    host, store, _provider = _host_with_store(tmp_path)
    before = store.snapshot().assignments
    page = host.models()
    assigned = next(
        group
        for group in page["endpoints"]
        if group["endpoint_id"] == DEFAULT_ENDPOINT_ID
    )
    assert assigned["catalog"]["health"] in {"unavailable", "unfetched"}
    assert store.snapshot().assignments == before
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="still-works"))
        assert submitted.kind == "accepted"
        host.wait(submitted.run_id)
    finally:
        host.close()


def test_chat_and_inference_probe_write_connection_observations(
    tmp_path: Path,
) -> None:
    pool = VersionedTransportPool(lambda snapshot: snapshot)

    class ObservedFactory(ScriptedModelFactory):
        transport_pool = pool

    factory = ObservedFactory(ScriptedModel([
        '{"retrieve":false,"query":null,"reason_code":"greeting"}',
        "pong", "probe-ok"
    ]))
    host, _store, _provider = _host_with_store(tmp_path, factory=factory)
    host.start()
    try:
        chat = host.submit(SubmitRequest(message="hello"))
        host.wait(chat.run_id)
        observed = pool.cached_observation(DEFAULT_ENDPOINT_ID)
        assert observed is not None
        assert observed["state"] == "connected"
        assert observed["checked_via"] == "real_run"
        assert observed["checked_at"]
        probe = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id=DEFAULT_ENDPOINT_ID,
                model_id=DEFAULT_MODEL_ID,
            )
        )
        host.wait(probe.run_id)
        observed = pool.cached_observation(DEFAULT_ENDPOINT_ID)
        assert observed["checked_via"] == "inference_probe"
        assert observed["state"] == "connected"
    finally:
        host.close()
