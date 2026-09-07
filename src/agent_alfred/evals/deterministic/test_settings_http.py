"""HTTP DTO for Connections/Models does not leak secrets (#34)."""

from __future__ import annotations

import json
from pathlib import Path

from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import StoreBackedSnapshotProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.model_settings import ModelSettingsStore
from agent_alfred.settings import OPENCODE_API_KEY_ENV, Settings

SECRET = "sk-http-secret-key"


def _host(tmp_path: Path, *, environ=None) -> RuntimeHost:
    import sqlite3

    from agent_alfred import schema

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    settings = Settings()
    env = environ if environ is not None else {OPENCODE_API_KEY_ENV: SECRET}
    return RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(["pong"])),
        settings=settings,
        clock=FakeClock(),
        fanout=FanOutSink(
            [CapturingSink(name="c", flush_at_run_end=True)],
            process_instance_id="proc-http-s6",
        ),
        process_instance_id="proc-http-s6",
        snapshot_provider=StoreBackedSnapshotProvider(store, settings, environ=env),
        model_settings=store,
    )


def test_connections_dto_masks_secrets(tmp_path: Path) -> None:
    host = _host(tmp_path)
    status, payload = DashboardApi(facade=host).connections()
    dumped = json.dumps(payload)
    assert status == 200
    assert SECRET not in dumped
    row = next(
        item
        for item in payload["endpoints"]
        if item["endpoint_id"] == "opencode-go"
    )
    assert row["key"]["last4"] == SECRET[-4:]
    assert row["auth_probe"]["label"] == "无免费认证探针"


def test_unconfigured_supported_chat_is_endpoint_unconfigured(tmp_path: Path) -> None:
    from agent_alfred.model import EndpointUnconfigured

    class MissingKeyFactory(ScriptedModelFactory):
        def create(self, snapshot):
            if not snapshot.api_key:
                raise EndpointUnconfigured("endpoint_unconfigured")
            return super().create(snapshot)

    import sqlite3

    from agent_alfred import schema

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    settings = Settings()
    host = RuntimeHost(
        conn=conn,
        factory=MissingKeyFactory(ScriptedModel(["pong"])),
        settings=settings,
        clock=FakeClock(),
        fanout=FanOutSink(
            [CapturingSink(name="c", flush_at_run_end=True)],
            process_instance_id="proc-unconfigured",
        ),
        process_instance_id="proc-unconfigured",
        snapshot_provider=StoreBackedSnapshotProvider(
            store, settings, environ={}
        ),
        model_settings=store,
    )
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        assert submitted.kind == "endpoint_unconfigured"
        models = host.models()
        default = next(
            item
            for group in models["endpoints"]
            for item in group["models"]
            if item["endpoint_id"] == "opencode-go"
            and item["model_id"] == "deepseek-v4-flash"
        )
        assert default["assignable"] is True
    finally:
        host.close()
