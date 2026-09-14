"""#26 CE-07/13: independent durable Behaviour settings (F05/P5)."""

import json
from pathlib import Path

import pytest


def test_ce07_default_save_restart_and_both_conflicts(tmp_path):
    from agent_alfred.runtime.behaviour import BehaviourError, BehaviourStore

    path = tmp_path / "behaviour.json"
    store = BehaviourStore(path)
    assert store.read()["enabled"] is False
    assert not path.exists()
    saved = store.save(expected_revision=0, enabled=True)
    assert saved["revision"] == 1
    assert BehaviourStore(path).read()["enabled"] is True
    original = path.read_bytes()
    with pytest.raises(BehaviourError, match="stale_revision"):
        store.save(expected_revision=0, enabled=False)
    assert path.read_bytes() == original
    path.write_text('{"schema_version": 1, "revision": 2, "enabled": false}')
    external = path.read_bytes()
    with pytest.raises(BehaviourError, match="external_change"):
        store.save(expected_revision=1, enabled=False)
    assert path.read_bytes() == external


@pytest.mark.parametrize("raw", [b"broken", b'{"schema_version":999}'])
def test_ce13_recovery_requires_current_fingerprint_and_preserves_bytes(tmp_path, raw):
    from agent_alfred.runtime.behaviour import BehaviourError, BehaviourStore

    path = tmp_path / "behaviour.json"
    path.write_bytes(raw)
    store = BehaviourStore(path)
    state = store.read()
    assert state["status"] != "ok"
    assert state["enabled"] is False
    with pytest.raises(BehaviourError):
        store.save(expected_revision=state["revision"], enabled=True)
    path.write_bytes(raw + b" ")
    with pytest.raises(BehaviourError, match="external_change"):
        store.recover(
            expected_revision=state["revision"], fingerprint=state["fingerprint"]
        )
    state = store.read()
    restored = store.recover(
        expected_revision=state["revision"], fingerprint=state["fingerprint"]
    )
    assert restored["status"] == "ok"
    assert restored["enabled"] is False
    backup = Path(restored["backup_path"])
    assert backup.read_bytes() == raw + b" "
    assert backup.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_bytes())["enabled"] is False
    assert BehaviourStore(path).read()["status"] == "ok"


def test_ce07_behaviour_host_gate_and_cli_share_commands(tmp_path):
    from io import StringIO

    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.gateway.cli import _send
    from agent_alfred.gateway.web.api import DashboardApi
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.wiring import build_dashboard

    dashboard = build_dashboard(
        state_dir=tmp_path,
        port=free_loopback_port(),
        factory=ScriptedModelFactory(ScriptedModel([])),
    )
    dashboard.start()
    try:
        host = dashboard.host
        api = DashboardApi(facade=host)
        assert api.behaviour()[1]["enabled"] is False
        assert host.try_begin_mutation() is None
        try:
            assert api.mutate_behaviour(
                dict(action="save", expected_revision=0, enabled=True)
            ) == (409, {"code": "mutation_in_flight"})
        finally:
            host.end_mutation()
        output = StringIO()
        assert _send(host, "/behaviour on 0", host.create_session(), output) == 0
        assert api.behaviour()[1]["enabled"] is True
        status, body = api.mutate_behaviour(
            dict(action="save", expected_revision=0, enabled=False)
        )
        assert status == 409 and body["cause"] == "stale_revision"
    finally:
        dashboard.close()


@pytest.mark.parametrize("stage", ["backup", "rename", "readback"])
def test_ce13_failed_recovery_never_publishes_and_retains_original(tmp_path, stage):
    from agent_alfred.atomic_config import read_bytes, write_atomic
    from agent_alfred.runtime.behaviour import BehaviourError, BehaviourStore

    path = tmp_path / "behaviour.json"
    path.write_bytes(b"original corrupt bytes")

    def writer(target, data, digest):
        if stage == "rename":
            raise OSError("before rename")
        write_atomic(target, data, digest)
        raise OSError("directory durability unconfirmed")

    def backup(raw):
        raise OSError("backup unavailable")

    store = BehaviourStore(
        path,
        reader=read_bytes,
        writer=writer,
        backup=backup if stage == "backup" else None,
    )
    state = store.read()
    with pytest.raises(BehaviourError) as caught:
        store.recover(expected_revision=0, fingerprint=state["fingerprint"])
    assert caught.value.code == "settings_write_failed"
    assert store.snapshot()["enabled"] is False
    assert store.snapshot()["status"] != "ok"
    if stage == "backup":
        assert path.read_bytes() == b"original corrupt bytes"
    else:
        assert Path(caught.value.backup_path).read_bytes() == b"original corrupt bytes"
    if stage == "readback":
        assert json.loads(path.read_bytes())["enabled"] is False
        assert store.read()["status"] == "ok"
