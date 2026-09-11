"""Memory invalidation uses the actual bounded broker state lane."""

import json

import pytest

from agent_alfred.evals.deterministic._web_broker_test_helpers import (
    INSTANCE,
    Harness,
    drain_connection,
)
from agent_alfred.gateway.web.connection import CloseConnection
from agent_alfred.gateway.web.frames import FrameBudget, PreparedFrames


def patch(revision, **values):
    return {
        "schema_version": 1,
        "process_instance_id": INSTANCE,
        "memory_revision": revision,
        "change": None,
        **values,
    }


def memory_frames(handle):
    return [
        item
        for item in drain_connection(handle)
        if isinstance(item, PreparedFrames)
        and b"event: memory_patch" in item.wire_bytes()
    ]


def test_memory_patch_no_seq_checkpoint_or_body_and_reconnect_does_not_rewind():
    harness = Harness()
    before = harness.connect()
    drain_connection(before)
    assert harness.broker.publish_memory_patch(patch(3, change={"subject": "PRIVATE"}))
    after = harness.connect()
    opened = memory_frames(after)
    assert len(opened) == 1
    harness.deliver()
    live = memory_frames(before)
    assert len(live) == 1
    assert memory_frames(after) == []
    for frame in [*opened, *live]:
        assert frame.seq is None and not frame.id_line and not frame.replayable
        assert frame.must_deliver
        wire = frame.wire_bytes()
        assert b"PRIVATE" not in wire and b'"seq"' not in wire
        body = json.loads(wire.split(b"data: ")[1].strip())
        assert body == patch(3)
    assert not harness.broker.publish_memory_patch(patch(2))
    assert not harness.broker.publish_memory_patch(patch(4, process_instance_id="old"))


def test_memory_ingress_overflow_disconnects_and_reconnect_has_current_revision():
    harness = Harness(ingress_budget=FrameBudget(1, 4096))
    old = harness.connect()
    drain_connection(old)
    assert harness.broker.publish_memory_patch(patch(1))
    assert not harness.broker.publish_memory_patch(patch(2))
    harness.deliver()
    assert any(isinstance(item, CloseConnection) for item in drain_connection(old))
    current = memory_frames(harness.connect())
    assert b'"memory_revision":2' in current[0].wire_bytes().replace(b" ", b"")


def test_memory_connection_overflow_is_not_silent():
    harness = Harness(connection_budget=FrameBudget(4, 16384))
    handle = harness.connect()
    drain_connection(handle)
    for revision in range(6):
        assert harness.broker.publish_memory_patch(patch(revision))
        harness.deliver()
    assert any(isinstance(item, CloseConnection) for item in drain_connection(handle))


def test_memory_offer_control_keeps_reconnect_responsibility(monkeypatch):
    harness = Harness()
    handle = harness.connect()
    drain_connection(handle)
    original = harness.broker._ingress.offer

    def interrupted(item):
        raise KeyboardInterrupt("offer interrupted")

    monkeypatch.setattr(harness.broker._ingress, "offer", interrupted)
    with pytest.raises(KeyboardInterrupt):
        harness.broker.publish_memory_patch(patch(1))
    monkeypatch.setattr(harness.broker._ingress, "offer", original)
    harness.deliver()
    assert any(isinstance(item, CloseConnection) for item in drain_connection(handle))
    assert memory_frames(harness.connect())


def test_actual_dashboard_idle_and_file_only_changes_publish_persistent_revision(
    tmp_path,
):
    import queue

    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.evals.deterministic.test_memory_http import read
    from agent_alfred.evals.deterministic.test_memory_mirrors import CONTEXT, save
    from agent_alfred.gateway.web.connection import FakeConnection
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.wiring import build_dashboard

    class Observed(FakeConnection):
        def __init__(self):
            super().__init__()
            self.patches = queue.Queue()

        def write(self, data):
            super().write(data)
            if b"event: memory_patch" in data:
                self.patches.put(json.loads(data.split(b"data: ")[1].split(b"\n")[0]))

        def through(self, revision):
            while True:
                body = self.patches.get(timeout=5)
                assert body["change"] is None
                if body["memory_revision"] >= revision:
                    return body

    state = tmp_path / "state"
    dashboard = build_dashboard(
        state_dir=state,
        port=free_loopback_port(),
        factory=ScriptedModelFactory(ScriptedModel([])),
    )
    try:
        dashboard.start()
        memory = dashboard.host.memory_service
        connection = Observed()
        dashboard.broker.connect(connection=connection)
        save(memory)
        first = memory.memory_revision
        assert connection.through(first)["memory_revision"] == first
        result = memory.mirrors.retry("facts", CONTEXT, operation_id="file-only")
        assert result["status"] == "regenerated"
        second = memory.memory_revision
        assert second > first
        assert connection.through(second)["memory_revision"] == second
        assert read(dashboard, "/api/memory/mirrors")[1]["memory_revision"] == second
    finally:
        assert dashboard.close()
    dashboard = build_dashboard(
        state_dir=state,
        port=free_loopback_port(),
        factory=ScriptedModelFactory(ScriptedModel([])),
    )
    try:
        dashboard.start()
        connection = Observed()
        dashboard.broker.connect(connection=connection)
        current = dashboard.host.memory_service.memory_revision
        assert current >= second
        assert connection.through(current)["memory_revision"] == current
    finally:
        assert dashboard.close()
