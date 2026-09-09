"""A54: a production queue prefix barrier does not finish its Run."""

import json
from datetime import datetime, timezone

from agent_alfred.clock import FakeClock
from agent_alfred.events import EventEnvelope, FanOutSink, RunStarted, StepFinished
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.trace import RunBundleTraceSink


def test_prefix_is_durable_and_the_run_can_continue(tmp_path):
    sink = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(wall=datetime(2026, 9, 9, tzinfo=timezone.utc)),
        process_instance_id="process",
    )
    events = FanOutSink([sink], process_instance_id="process")
    envelope = EventEnvelope(0, "run-one", None, None, None, None)
    try:
        events.emit(RunStarted(purpose="chat", user_message="before"), envelope)
        result = sink.checkpoint("run-one")
        assert result.outcome == "flushed"
        trace = next((tmp_path / "traces").rglob("trace.jsonl"))
        assert "before" in trace.read_text()
        events.emit(StepFinished(step_index=1), envelope)
        assert sink.flush("run-one").outcome == "flushed"
        assert [
            json.loads(line)["payload_name"] for line in trace.read_text().splitlines()
        ] == ["run.started", "step.finished"]
    finally:
        sink.close()


def test_failed_production_prefix_prevents_delete_and_keeps_final_failure(
    tmp_path, monkeypatch
):
    import os
    import sqlite3
    import threading

    from agent_alfred import schema
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import CommandContext, MemoryCommandService
    from agent_alfred.memory.types import ManualOrigin
    from agent_alfred.runtime.recording import RecordingStore

    conn = sqlite3.connect(tmp_path / "memory.db")
    schema.migrate(conn)
    sink = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(wall=datetime(2026, 9, 9, tzinfo=timezone.utc)),
        process_instance_id="process",
    )
    events = FanOutSink([sink], process_instance_id="process")
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        delete_barrier=events.checkpoint_barrier,
    )
    context = CommandContext(ManualOrigin("web"), "web", run_id="run-one")
    saved = memory.execute(
        {
            "operation_id": "save",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "me", "fact": "tea"},
        },
        context,
    )
    try:
        events.emit(
            RunStarted(purpose="chat", user_message="before"),
            EventEnvelope(0, "run-one", None, None, None, None),
        )

        def fail_sync(fd):
            raise OSError("private disk detail")

        monkeypatch.setattr(os, "fsync", fail_sync)
        result = memory.execute(
            {
                "operation_id": "delete",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": saved["memory_id"]},
            },
            context,
        )
        assert result == {"error": {"code": "trace_barrier_failed"}}
        assert memory.get("semantic", saved["memory_id"]) is not None
        assert memory.get_operation("delete") is None
        assert events.flush_barrier("run-one")[0] is True
    finally:
        monkeypatch.undo()
        sink.close()
        conn.close()


def test_pending_prefix_orders_manual_delete_and_timeout_never_deletes_later(
    tmp_path, monkeypatch
):
    import os
    import sqlite3
    import threading

    import agent_alfred.trace as trace_module
    from agent_alfred import schema
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import CommandContext, MemoryCommandService
    from agent_alfred.memory.types import ManualOrigin
    from agent_alfred.runtime.recording import RecordingStore

    conn = sqlite3.connect(tmp_path / "memory.db", check_same_thread=False)
    schema.migrate(conn)
    entered, release, barrier_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_write = os.write

    def blocked_write(fd, data):
        if bytes(data).startswith(b'{"seq"'):
            entered.set()
            assert release.wait(5)
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", blocked_write)
    sink = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(wall=datetime(2026, 9, 9, tzinfo=timezone.utc)),
        process_instance_id="process",
    )
    events = FanOutSink([sink], process_instance_id="process")

    def checkpoint(run_id):
        barrier_entered.set()
        return events.checkpoint_barrier(run_id)

    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        delete_barrier=checkpoint,
    )
    context = CommandContext(ManualOrigin("web"), "web")
    saved = memory.execute(
        {
            "operation_id": "save",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "me", "fact": "tea"},
        },
        context,
    )
    command = {
        "operation_id": "delete",
        "kind": "semantic",
        "action": "delete",
        "expected_version": 1,
        "payload": {"id": saved["memory_id"]},
    }
    result = []
    thread = None
    try:
        events.emit(
            RunStarted(purpose="chat", user_message="old-body"),
            EventEnvelope(0, "run-one", None, None, None, None),
        )
        assert entered.wait(5)
        # A zero deadline deterministically expires while the OS seam is held.
        monkeypatch.setattr(trace_module, "_FLUSH_TIMEOUT_S", 0)
        assert memory.execute(command, context) == {
            "error": {"code": "trace_barrier_failed"}
        }
        assert memory.get_operation("delete") is None
        release.set()
        assert sink.checkpoint("run-one").outcome in ("failed", "flushed")
        monkeypatch.setattr(trace_module, "_FLUSH_TIMEOUT_S", 5)
        assert sink.checkpoint("run-one").outcome == "flushed"
        assert memory.get("semantic", saved["memory_id"]) is not None
        # The timed-out FanOut conservatively retains loss until Run finalization.
        events.flush_barrier("run-one")
        # A new Run proves the success ordering separately, with no sleep race.
        entered.clear()
        release.clear()
        barrier_entered.clear()
        events.emit(
            RunStarted(purpose="chat", user_message="second-body"),
            EventEnvelope(0, "run-two", None, None, None, None),
        )
        assert entered.wait(5)
        thread = threading.Thread(
            target=lambda: result.append(memory.execute(command, context))
        )
        thread.start()
        assert barrier_entered.wait(5)
        assert memory.get("semantic", saved["memory_id"]) is not None
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert result[0]["status"] == "deleted"
        assert any(
            "second-body" in p.read_text()
            for p in (tmp_path / "traces").rglob("trace.jsonl")
        )
        events.emit(
            StepFinished(step_index=1),
            EventEnvelope(0, "run-two", None, None, None, None),
        )
        assert events.flush_barrier("run-two")[0] is False
    finally:
        release.set()
        if thread is not None:
            thread.join(5)
        sink.close()
        conn.close()


def test_overflow_and_partial_write_cannot_authorize_delete(tmp_path, monkeypatch):
    import os
    import sqlite3
    import threading

    import agent_alfred.trace as trace_module
    from agent_alfred import schema
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import CommandContext, MemoryCommandService
    from agent_alfred.memory.types import ManualOrigin
    from agent_alfred.runtime.recording import RecordingStore

    original_write = os.write
    for fault in ("overflow", "partial"):
        conn = sqlite3.connect(tmp_path / f"{fault}.db")
        schema.migrate(conn)
        sink = RunBundleTraceSink(
            root=ManagedStateDirectory.acquire_trace_root(tmp_path / fault),
            clock=FakeClock(wall=datetime(2026, 9, 9, tzinfo=timezone.utc)),
            process_instance_id="process",
        )
        events = FanOutSink([sink], process_instance_id="process")
        memory = MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=AuditKey("test", b"x" * 32),
            clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            delete_barrier=events.checkpoint_barrier,
        )
        context = CommandContext(ManualOrigin("web"), "web", run_id="run")
        saved = memory.execute(
            {
                "operation_id": "save",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "me", "fact": "private-body"},
            },
            context,
        )
        partially_written = False

        def partial_write(fd, data):
            nonlocal partially_written
            if partially_written:
                raise OSError("private-write-error")
            if bytes(data).startswith(b'{"seq"'):
                partially_written = True
                return original_write(fd, bytes(data)[:10])
            return original_write(fd, data)

        try:
            if fault == "overflow":
                monkeypatch.setattr(trace_module, "_QUEUE_LIMIT", 0)
            else:
                monkeypatch.setattr(os, "write", partial_write)
            events.emit(
                RunStarted(purpose="chat", user_message="private-body"),
                EventEnvelope(0, "run", None, None, None, None),
            )
            assert memory.execute(
                {
                    "operation_id": "delete",
                    "kind": "semantic",
                    "action": "delete",
                    "expected_version": 1,
                    "payload": {"id": saved["memory_id"]},
                },
                context,
            ) == {"error": {"code": "trace_barrier_failed"}}
            assert memory.get("semantic", saved["memory_id"]) is not None
            assert memory.forgetting.get_forgetting("delete") is None
        finally:
            monkeypatch.undo()
            sink.close()
            conn.close()


def test_successful_checkpoint_then_transaction_rollback_preserves_run_owner(tmp_path):
    import sqlite3
    import threading

    from agent_alfred import schema
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import CommandContext, MemoryCommandService
    from agent_alfred.memory.types import ManualOrigin
    from agent_alfred.runtime.recording import RecordingStore

    conn = sqlite3.connect(tmp_path / "db")
    schema.migrate(conn)
    sink = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(wall=datetime(2026, 9, 9, tzinfo=timezone.utc)),
        process_instance_id="process",
    )
    events = FanOutSink([sink], process_instance_id="process")
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        delete_barrier=events.checkpoint_barrier,
    )
    context = CommandContext(ManualOrigin("web"), "web", run_id="run")
    saved = memory.execute(
        {
            "operation_id": "save",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "me", "fact": "private-body"},
        },
        context,
    )
    conn.execute(
        (
            "CREATE TRIGGER delete_fault BEFORE INSERT ON "
            "forget_operations BEGIN SELECT RAISE(ABORT,'private-error'); "
            "END"
        )
    )
    try:
        events.emit(
            RunStarted(purpose="chat", user_message="old-body"),
            EventEnvelope(0, "run", None, None, None, None),
        )
        assert memory.execute(
            {
                "operation_id": "delete",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": saved["memory_id"]},
            },
            context,
        ) == {"error": {"code": "storage_write_failed"}}
        assert memory.get("semantic", saved["memory_id"]) is not None
        assert memory.get_operation("delete") is None
        events.emit(
            StepFinished(step_index=1), EventEnvelope(0, "run", None, None, None, None)
        )
        assert events.flush_barrier("run")[0] is False
        assert (
            len(
                next((tmp_path / "traces").rglob("trace.jsonl"))
                .read_text()
                .splitlines()
            )
            == 2
        )
    finally:
        sink.close()
        conn.close()
