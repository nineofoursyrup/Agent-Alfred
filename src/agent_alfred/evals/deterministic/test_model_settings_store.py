"""ModelSettingsStore: durable pins and assignments (issue #34 / ADR-0022)."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from agent_alfred.model import ModelRef
from agent_alfred.pricing import UserPriceOverride
from agent_alfred.settings import DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID


def test_missing_file_yields_first_snapshot_without_writing(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import ModelSettingsStore

    path = tmp_path / "model_settings.json"
    store = ModelSettingsStore(path)
    snapshot = store.load()
    assert not path.exists()
    assert snapshot.schema_version == 1
    assert snapshot.revision == 0
    assert snapshot.status == "ok"
    pin = snapshot.pin(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID)
    assert pin is not None
    assert pin.wire_style_override is None
    assert pin.price_override is None
    assert pin.display_name is None
    assert snapshot.assignments.primary.endpoint_id == DEFAULT_ENDPOINT_ID
    assert snapshot.assignments.primary.model_id == DEFAULT_MODEL_ID
    assert snapshot.assignments.retrieval_gate is None


def test_first_mutate_persists_revision_one_and_reloads(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import (
        Assignments,
        ModelSettingsStore,
        PinRecord,
    )

    path = tmp_path / "model_settings.json"
    store = ModelSettingsStore(path)
    store.load()
    extra = PinRecord(
        "openai",
        "gpt-test",
        wire_style_override="openai",
        price_override=UserPriceOverride(output=Decimal("0")),
        display_name="测试",
    )
    gate = ModelRef("openai", "gpt-test")
    written = store.mutate(
        0,
        lambda snap: replace(
            snap,
            pins=snap.pins + (extra,),
            assignments=Assignments(
                primary=snap.assignments.primary,
                retrieval_gate=gate,
            ),
        ),
    )
    assert written.revision == 1
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    reloaded = ModelSettingsStore(path).load()
    assert reloaded.revision == 1
    assert reloaded.schema_version == 1
    pin = reloaded.pin("openai", "gpt-test")
    assert pin is not None
    assert pin.wire_style_override == "openai"
    assert pin.price_override is not None
    assert pin.price_override.output == Decimal("0")
    assert pin.price_override.uncached_input is None
    assert pin.display_name == "测试"
    assert reloaded.assignments.retrieval_gate == gate
    assert reloaded.assignments.primary.endpoint_id == DEFAULT_ENDPOINT_ID
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _write_ok(path: Path) -> bytes:
    from agent_alfred.runtime.model_settings import ModelSettingsStore

    store = ModelSettingsStore(path)
    store.load()
    store.mutate(0, lambda snap: snap)
    return path.read_bytes()


def test_stale_revision_writes_nothing(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import (
        ModelSettingsError,
        ModelSettingsStore,
    )

    path = tmp_path / "model_settings.json"
    original = _write_ok(path)
    store = ModelSettingsStore(path)
    store.load()
    with pytest.raises(ModelSettingsError) as caught:
        store.mutate(0, lambda snap: snap)
    assert caught.value.code == "settings_conflict"
    assert caught.value.cause == "stale_revision"
    assert path.read_bytes() == original
    assert store.snapshot().revision == 1


def test_external_change_writes_nothing(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import (
        ModelSettingsError,
        ModelSettingsStore,
    )

    path = tmp_path / "model_settings.json"
    _write_ok(path)
    store = ModelSettingsStore(path)
    store.load()
    tampered = json.loads(path.read_text())
    tampered["pins"][0]["display_name"] = "external"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    outside = path.read_bytes()
    with pytest.raises(ModelSettingsError) as caught:
        store.mutate(1, lambda snap: snap)
    assert caught.value.code == "settings_conflict"
    assert caught.value.cause == "external_change"
    assert path.read_bytes() == outside
    assert store.snapshot().revision == 1
    pin = store.snapshot().pin(DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID)
    assert pin is not None
    assert pin.display_name is None


def test_syntax_damage_is_settings_invalid_and_keeps_bytes(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import (
        ModelSettingsError,
        ModelSettingsStore,
    )

    path = tmp_path / "model_settings.json"
    path.write_bytes(b"{not json")
    original = path.read_bytes()
    store = ModelSettingsStore(path)
    snapshot = store.load()
    assert snapshot.status == "settings_invalid"
    with pytest.raises(ModelSettingsError) as caught:
        store.mutate(0, lambda snap: snap)
    assert caught.value.code == "settings_invalid"
    assert path.read_bytes() == original


def test_newer_schema_is_settings_schema_newer_and_keeps_bytes(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import (
        ModelSettingsError,
        ModelSettingsStore,
    )

    path = tmp_path / "model_settings.json"
    payload = {
        "schema_version": 2,
        "revision": 4,
        "pins": [
            {"endpoint_id": DEFAULT_ENDPOINT_ID, "model_id": DEFAULT_MODEL_ID}
        ],
        "assignments": {
            "primary": {
                "endpoint_id": DEFAULT_ENDPOINT_ID,
                "model_id": DEFAULT_MODEL_ID,
            },
            "retrieval_gate": None,
        },
        "future_field": True,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()
    store = ModelSettingsStore(path)
    snapshot = store.load()
    assert snapshot.status == "settings_schema_newer"
    with pytest.raises(ModelSettingsError) as caught:
        store.mutate(4, lambda snap: snap)
    assert caught.value.code == "settings_schema_newer"
    assert path.read_bytes() == original


def test_broken_primary_ref_is_settings_invalid(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import ModelSettingsStore

    path = tmp_path / "model_settings.json"
    payload = {
        "schema_version": 1,
        "revision": 1,
        "pins": [{"endpoint_id": "openai", "model_id": "gpt-test"}],
        "assignments": {
            "primary": {
                "endpoint_id": DEFAULT_ENDPOINT_ID,
                "model_id": DEFAULT_MODEL_ID,
            },
            "retrieval_gate": None,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()
    snapshot = ModelSettingsStore(path).load()
    assert snapshot.status == "settings_invalid"
    assert path.read_bytes() == original


def test_unpin_assigned_pin_is_rejected(tmp_path: Path) -> None:
    from agent_alfred.runtime.model_settings import (
        ModelSettingsError,
        ModelSettingsStore,
        PinRecord,
    )

    path = tmp_path / "model_settings.json"
    original = _write_ok(path)
    store = ModelSettingsStore(path)
    store.load()
    extra = PinRecord("openai", "gpt-test", wire_style_override="openai")
    store.mutate(1, lambda snap: replace(snap, pins=snap.pins + (extra,)))
    after_pin = path.read_bytes()
    with pytest.raises(ModelSettingsError) as caught:
        store.mutate(
            2,
            lambda snap: replace(
                snap,
                pins=tuple(
                    pin
                    for pin in snap.pins
                    if pin.key != (DEFAULT_ENDPOINT_ID, DEFAULT_MODEL_ID)
                ),
            ),
        )
    assert caught.value.code == "pin_assigned"
    assert path.read_bytes() == after_pin
    assert original != after_pin


def test_memory_revision_publishes_only_after_rename(tmp_path, monkeypatch) -> None:
    from agent_alfred import atomic_config as module
    from agent_alfred.runtime.model_settings import ModelSettingsStore

    path = tmp_path / "model_settings.json"
    store = ModelSettingsStore(path)
    store.load()
    seen: list[int] = []
    real_replace = os.replace

    def replace_and_record(src, dst):
        seen.append(store.snapshot().revision)
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", replace_and_record)
    written = store.mutate(0, lambda snap: snap)
    assert seen == [0]
    assert written.revision == 1
    assert store.snapshot().revision == 1


def test_failed_fsync_does_not_publish_or_replace_target(tmp_path, monkeypatch) -> None:
    from agent_alfred import atomic_config as module
    from agent_alfred.runtime.model_settings import ModelSettingsStore

    path = tmp_path / "model_settings.json"
    store = ModelSettingsStore(path)
    store.load()
    real_fsync = os.fsync

    def fail_file_fsync(fd):
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            raise OSError("injected fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_file_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        store.mutate(0, lambda snap: snap)
    assert not path.exists()
    assert store.snapshot().revision == 0
    leftovers = list(tmp_path.iterdir())
    assert leftovers == []


def test_failed_replace_does_not_leave_tmp_or_publish(tmp_path, monkeypatch) -> None:
    from agent_alfred import atomic_config as module
    from agent_alfred.runtime.model_settings import ModelSettingsStore

    path = tmp_path / "model_settings.json"
    store = ModelSettingsStore(path)
    store.load()

    def fail_replace(src, dst):
        del src, dst
        raise OSError("injected replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        store.mutate(0, lambda snap: snap)
    assert not path.exists()
    assert store.snapshot().revision == 0
    assert list(tmp_path.iterdir()) == []
