"""Public-path regressions for the four independently reproduced review defects."""

import os
import sqlite3
import threading
from dataclasses import replace

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, EventEnvelope, FanOutSink, ToolFinished
from agent_alfred.managed_state import ManagedFileLease, ManagedStateDirectory
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.recording import RecordingStore
from agent_alfred.tools import (
    Tool,
    ToolContext,
    ToolFailure,
    ToolPolicy,
    ToolRegistry,
    ToolSuccess,
)
from agent_alfred.tools.files import FileTools, digest
from agent_alfred.tools.ledger import ExternalToolLedger
from agent_alfred.trace import RunBundleTraceSink
from agent_alfred.wiring import build_default_host


class TimedConnection(sqlite3.Connection):
    on_commit = None

    def commit(self):
        super().commit()
        if self.on_commit:
            self.on_commit()


@pytest.fixture
def external(tmp_path):
    conn = sqlite3.connect(tmp_path / "external.db", factory=TimedConnection)
    schema.migrate(conn)
    clock = FakeClock()
    effects = []

    def action(args, context):
        context.checkpoint()
        effects.append(dict(args))
        return ToolSuccess(
            (TextBlock("original result"),), operation_id="remote-receipt"
        )

    tool = Tool("action", "Action", {"type": "object"}, action, "external")
    store = RecordingStore(conn, threading.Lock())
    ledger = ExternalToolLedger(store, clock)
    registry = ToolRegistry(
        (tool,),
        clock=clock,
        external_ledger=ledger,
        policies={"action": ToolPolicy(True, "allowed")},
    )
    try:
        yield conn, clock, effects, tool, ledger, registry
    finally:
        conn.close()


def test_external_replay_is_durable_and_checks_all_parameters(external):
    conn, clock, effects, tool, ledger, registry = external
    ctx = ToolContext("run", 1, "call", "cli", 30)
    call = ToolCallBlock("call", "action", {"non_summary_field": "first"})
    first = registry.execute(call, ctx)
    restarted_ledger = ExternalToolLedger(RecordingStore(conn, threading.Lock()), clock)
    restarted_ledger.recover()
    restarted = ToolRegistry(
        (tool,),
        clock=clock,
        external_ledger=restarted_ledger,
        policies={"action": ToolPolicy(True, "allowed")},
    )
    assert restarted.execute(call, ctx) == first
    mismatch = restarted.execute(
        replace(call, input={"non_summary_field": "other"}), ctx
    )
    assert mismatch.block.is_error
    assert len(effects) == 1
    restarted.execute(replace(call, id="new"), replace(ctx, call_id="new"))
    assert len(effects) == 2
    assert conn.execute("SELECT status FROM tool_ledger").fetchall() == [
        ("succeeded",),
        ("succeeded",),
    ]


def test_external_receipt_loss_never_repeats_action(external, monkeypatch):
    conn, _, effects, _, ledger, registry = external
    ctx = ToolContext("run", 1, "call", "cli", 30)
    call = ToolCallBlock("call", "action", {})
    original = ledger.finish

    def lost(*args, **kwargs):
        raise OSError("receipt storage failed")

    monkeypatch.setattr(ledger, "finish", lost)
    with pytest.raises(OSError):
        registry.execute(call, ctx)
    monkeypatch.setattr(ledger, "finish", original)
    ledger.recover()
    assert registry.execute(call, ctx).block.is_error
    assert effects == [{}]
    assert conn.execute("SELECT status FROM tool_ledger").fetchall() == [("unknown",)]


def test_deadline_after_ledger_commit_never_emits_started_or_enters_fn(external):
    conn, clock, effects, _, _, registry = external
    conn.on_commit = lambda: setattr(clock, "monotonic_value", 10)
    sink = CapturingSink()
    events = FanOutSink([sink], process_instance_id="p")
    result = registry.execute(
        ToolCallBlock("call", "action", {}),
        ToolContext("run", 1, "call", "cli", 5),
        events=events,
    )
    assert result.block.is_error
    assert effects == []
    assert sink.events == []
    assert conn.execute("SELECT status FROM tool_ledger").fetchall() == [("failed",)]
    assert "not started" in result.block.content[0].text.lower()


def test_deadline_after_event_preparation_does_not_publish_started(external):
    _, clock, effects, tool, _, _ = external

    class SlowPreparation(CapturingSink):
        def prepare(self, event):
            clock.monotonic_value = 20
            return super().prepare(event)

    sink = SlowPreparation()
    registry = ToolRegistry((replace(tool, effect="local_read"),), clock=clock)
    result = registry.execute(
        ToolCallBlock("call", "action", {}),
        ToolContext("run", 1, "call", "cli", 10),
        events=FanOutSink([sink], process_instance_id="p"),
    )
    assert result.block.is_error
    assert effects == []
    assert sink.events == []


@pytest.mark.parametrize(
    "target,expected",
    [
        ("outbox/draft.md", None),
        ("persona/persona.md", digest("old")),
        ("skills/new/SKILL.md", None),
    ],
)
@pytest.mark.parametrize("automatic", [False, True])
def test_published_then_deleted_target_is_not_recreated(
    tmp_path, monkeypatch, target, expected, automatic
):
    state = tmp_path / "state"
    host = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    path = state / target
    if expected:
        path.parent.mkdir(parents=True)
        path.write_text("old")
        path.chmod(0o600)
    original = FileTools.publish

    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("lost receipt")

    monkeypatch.setattr(FileTools, "publish", lost)
    result = host._file_tools.write(
        "operation",
        "draft_message",
        target,
        "new",
        expected,
        ToolContext("run", 1, "call", "cli", float("inf")),
    )
    assert isinstance(result, ToolFailure)
    assert path.exists()
    assert host.close()
    path.unlink()
    monkeypatch.undo()
    restarted = build_default_host(
        state_dir=state, factory=ScriptedModelFactory(ScriptedModel([]))
    )
    try:
        if automatic:
            restarted._file_tools.resume_pending()
        else:
            restarted._file_tools.recover("operation")
        assert not path.exists()
        assert restarted._conn.execute(
            "SELECT state FROM file_operations"
        ).fetchall() == [("conflict",)]
        assert (
            restarted._conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0]
            == 0
        )
    finally:
        restarted.close()


def test_trace_artifact_failed_close_retains_owner_until_retry(tmp_path, monkeypatch):
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="p",
    )
    events = FanOutSink([trace], process_instance_id="p")
    original = ManagedFileLease.close
    retained = {}
    fault = [True]

    def blocked(self):
        if self.path.name.startswith(".tool-") and self.path.suffix == ".tmp":
            retained[id(self)] = self.fd
            if fault[0]:
                raise OSError("before close")
        return original(self)

    monkeypatch.setattr(ManagedFileLease, "close", blocked)
    try:
        events.emit(
            ToolFinished(
                "call",
                "read",
                "ok",
                None,
                "short",
                "x" * 300000,
                True,
                300000,
                "digest",
                0,
            ),
            EventEnvelope(0, "run", None, 1, None, None),
        )
        events.flush_barrier("run")
        assert retained
        assert trace.close() is False
        for fd in retained.values():
            os.fstat(fd)
        fault[0] = False
        assert trace.close() is True
        for fd in retained.values():
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        fault[0] = False
        trace.close()


def test_event_post_commit_expiry_finishes_without_entering_fn(external):
    _, clock, effects, _, _, registry = external

    class SlowEmitter:
        def __init__(self):
            self.events = []

        def emit(self, event, envelope):
            self.events.append(event)
            if event.name == "tool.started":
                clock.monotonic_value = 20

    events = SlowEmitter()
    result = registry.execute(
        ToolCallBlock("call", "action", {}),
        ToolContext("run", 1, "call", "cli", 10),
        events=events,
    )
    assert result.block.is_error
    assert effects == []
    assert [event.name for event in events.events] == ["tool.started", "tool.finished"]
    assert "not started" in events.events[-1].audit_content
    conn = external[0]
    assert conn.execute("SELECT status FROM tool_ledger").fetchall() == [("failed",)]


def test_event_preparation_guard_does_not_poison_later_events(external):
    _, clock, _, tool, _, _ = external

    class SlowOnce(CapturingSink):
        def prepare(self, event):
            clock.monotonic_value = 20
            return super().prepare(event)

    sink = SlowOnce()
    events = FanOutSink([sink], process_instance_id="p")
    registry = ToolRegistry((replace(tool, effect="local_read"),), clock=clock)
    registry.execute(
        ToolCallBlock("call", "action", {}),
        ToolContext("run", 1, "call", "cli", 10),
        events=events,
    )
    result = registry.execute(
        ToolCallBlock("new", "action", {}),
        ToolContext("new-run", 1, "new", "cli", 100),
        events=events,
    )
    assert not result.block.is_error
    assert [e.payload.name for e in sink.events] == ["tool.started", "tool.finished"]


@pytest.mark.parametrize("after_effect", [False, True])
@pytest.mark.parametrize("phase", ["start", "finish"])
def test_external_commit_failure_keeps_one_authoritative_effect(
    external, monkeypatch, phase, after_effect
):
    conn, _, effects, _, ledger, registry = external
    ctx = ToolContext("run", 1, "call", "cli", 30)
    call = ToolCallBlock("call", "action", {})
    original = TimedConnection.commit
    commits = [0]

    def failing(self):
        commits[0] += 1
        if commits[0] == (1 if phase == "start" else 2):
            if after_effect:
                original(self)
            raise sqlite3.OperationalError("commit receipt lost")
        return original(self)

    monkeypatch.setattr(TimedConnection, "commit", failing)
    with pytest.raises(sqlite3.OperationalError):
        registry.execute(call, ctx)
    monkeypatch.setattr(TimedConnection, "commit", original)
    ledger.recover()
    replay = registry.execute(call, ctx)
    if phase == "start" and after_effect:
        assert effects == []
        assert replay.block.is_error
    else:
        assert effects == [{}]
    assert conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1


def test_external_caller_transaction_is_untouched(external):
    conn, _, effects, _, _, registry = external
    conn.execute("CREATE TABLE caller(value TEXT)")
    conn.execute("INSERT INTO caller VALUES ('uncommitted')")
    with pytest.raises(ValueError, match="caller_transaction_active"):
        registry.execute(
            ToolCallBlock("call", "action", {}),
            ToolContext("run", 1, "call", "cli", 30),
        )
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT * FROM caller").fetchall() == []
    assert effects == []


def test_external_result_replays_after_connection_restart(tmp_path):
    path = tmp_path / "restart.db"
    effects = []
    tool = Tool(
        "action",
        "Action",
        {"type": "object"},
        lambda args, ctx: effects.append(1) or ToolSuccess((TextBlock("receipt"),)),
        "external",
    )
    ctx = ToolContext("run", 1, "call", "cli", 30)
    call = ToolCallBlock("call", "action", {})
    results = []
    for _ in range(2):
        conn = sqlite3.connect(path)
        try:
            schema.migrate(conn)
            clock = FakeClock()
            ledger = ExternalToolLedger(RecordingStore(conn, threading.Lock()), clock)
            ledger.recover()
            registry = ToolRegistry(
                (tool,),
                clock=clock,
                external_ledger=ledger,
                policies={"action": ToolPolicy(True, "allowed")},
            )
            results.append(registry.execute(call, ctx))
        finally:
            conn.close()
    assert results[0] == results[1]
    assert effects == [1]


def test_trace_artifact_control_unwind_keeps_nested_owners(tmp_path, monkeypatch):
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="p",
    )
    events = FanOutSink([trace], process_instance_id="p")
    original = ManagedFileLease.close
    retained = {}
    controls = []
    fault = [True]
    control = SystemExit("artifact close interrupted")

    def blocked(self):
        if self.path.name.startswith(".tool-") and self.path.suffix == ".tmp":
            retained[id(self)] = self.fd
            if fault[0]:
                raise control
        return original(self)

    monkeypatch.setattr(ManagedFileLease, "close", blocked)
    monkeypatch.setattr(
        threading, "excepthook", lambda args: controls.append(args.exc_value)
    )
    try:
        events.emit(
            ToolFinished(
                "call",
                "read",
                "ok",
                None,
                "short",
                "x" * 300000,
                True,
                300000,
                "digest",
                0,
            ),
            EventEnvelope(0, "run", None, 1, None, None),
        )
        events.flush_barrier("run")
        assert trace.close() is False
        assert controls == [control]
        fds = tuple(retained.values())
        for fd in fds:
            os.fstat(fd)
        fault[0] = False
        assert trace.close() is True
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        fault[0] = False
        trace.close()


def test_v8_upgrade_preserves_v7_rows_and_caller_rollback(monkeypatch):
    conn = sqlite3.connect(":memory:")
    current = tuple(m for m in schema.MIGRATIONS if m.version <= 8)
    try:
        monkeypatch.setattr(schema, "MIGRATIONS", current[:-1])
        schema.migrate(conn)
        conn.execute(
            "INSERT INTO local_tool_operations "
            "VALUES ('operation','fingerprint','receipt')"
        )
        conn.commit()
        monkeypatch.setattr(schema, "MIGRATIONS", current)
        conn.execute("BEGIN")
        schema.migrate(conn)
        assert conn.in_transaction
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='external_tool_operations'"
        ).fetchone()
        conn.rollback()
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='external_tool_operations'"
            ).fetchone()
            is None
        )
        schema.migrate(conn)
        assert conn.execute("SELECT * FROM local_tool_operations").fetchall() == [
            ("operation", "fingerprint", "receipt")
        ]
        assert conn.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone() == (8,)
    finally:
        conn.close()
