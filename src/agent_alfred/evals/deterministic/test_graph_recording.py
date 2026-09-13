"""Real admission, services, tool ledger and RunRecorder, without a graph UI."""

import sqlite3
import threading

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.graph import (
    GraphBuilder,
    GraphRunContext,
    NodeOutcome,
    TerminalSpec,
    fn_node,
)
from agent_alfred.loop.budget import RunBudget
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.redact import Redactor
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.recording import RecordingStore, RunRecorder
from agent_alfred.settings import Settings
from agent_alfred.tools import ToolRegistry
from agent_alfred.tools.calendar import CalendarTools
from agent_alfred.tools.ledger import ExternalToolLedger
from agent_alfred.tools.memory import MemoryTools
from agent_alfred.tools.metering import ToolMetering


def setup_run(tmp_path, before_start=None):
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.trace import RunBundleTraceSink

    clock = FakeClock()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    captured = CapturingSink()
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "trace"),
        clock=clock,
        process_instance_id="graph-test",
    )
    fanout = FanOutSink(
        [trace, captured], process_instance_id="graph-test", redactor=Redactor(())
    )
    items = []
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel([])),
        settings=Settings(),
        clock=clock,
        fanout=fanout,
        process_instance_id="graph-test",
        publish_work=items.append,
    )
    store = RecordingStore(conn, threading.Lock())
    registry = ToolRegistry(
        (
            *CalendarTools(store, clock).declarations(),
            *MemoryTools(host.memory_service).declarations(),
        ),
        clock=clock,
        metering=ToolMetering(store, clock),
        external_ledger=ExternalToolLedger(store, clock),
    )
    recorder = RunRecorder(
        clock=clock, fanout=fanout, redactor=Redactor(()), store=store, coordinator=host
    )
    if before_start is not None:
        before_start(host)
    host.start()
    host.submit(SubmitRequest(message="graph request"))
    item = items[0]
    with store.transaction() as target:
        revision = schema.allocate_activity_revision(target)
        schema.update_run_phase(
            target,
            run_id=item.run_id,
            from_phase="accepted",
            to_phase="running",
            activity_revision=revision,
            started_at=item.accepted_at,
            session_id=item.session_id,
        )
        target.commit()
    host.execution_mark_running(item.accepted_at)
    ctx = GraphRunContext(
        run_id=item.run_id,
        session_id=item.session_id,
        budget=RunBudget(4),
        tools=registry,
        clock=clock,
        events=fanout,
        tool_permission=item.memory_permission,
    )
    return host, conn, item, ctx, recorder, captured


def test_ce16_no_action_recorder_does_not_invent_assistant_message(tmp_path):
    from agent_alfred.graph import settle_graph

    host, conn, item, ctx, recorder, captured = setup_run(tmp_path)
    try:
        b = GraphBuilder("noaction")
        b.add_node(
            "done",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("nothing"),
        )
        result = b.compile().invoke({}, context=ctx)
        settle_graph(recorder, item, result, ctx)
        assert conn.execute(
            "SELECT role FROM agent_log WHERE run_id=?", (item.run_id,)
        ).fetchall() == [("user",)]
        assert (
            conn.execute(
                "SELECT outcome FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == "completed"
        )
    finally:
        host.close()


def test_ce08_ce16_delete_forces_original_completed_receipt_without_success_terminal(
    tmp_path,
):
    from agent_alfred.graph import Failed, settle_graph, tool_node
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    saved = []

    def seed(host):
        saved.append(
            host.memory_service.execute(
                {
                    "operation_id": "seed",
                    "kind": "semantic",
                    "action": "save",
                    "payload": {"subject": "private subject", "fact": "private body"},
                },
                CommandContext(ManualOrigin("cli"), "cli"),
            )
        )

    host, conn, item, ctx, recorder, captured = setup_run(tmp_path, before_start=seed)
    try:
        b = GraphBuilder("delete", tools=ctx.tools)
        b.add_node(
            "delete",
            tool_node(
                "delete_memory",
                {
                    "kind": "semantic",
                    "id": saved[0]["memory_id"],
                    "expected_version": 1,
                },
                output_key="receipt",
            ),
            writes=("receipt",),
        )
        for name in ("ordinary", "recover"):
            b.add_node(
                name,
                tool_node(
                    "create_event",
                    {"title": "must not run", "starts_at": "2026-09-13T00:00:00Z"},
                    output_key=name,
                ),
                writes=(name,),
                terminal=TerminalSpec.result(name),
            )
        b.add_edge("delete", "ordinary")
        b.add_error_edge("delete", "recover")
        result = b.compile().invoke({}, context=ctx)
        assert isinstance(result, Failed) and result.forced_stop is not None
        assert result.forced_stop.outcome == "completed"
        assert (
            result.forced_stop.facts["finalization_reason"] == "memory_delete_boundary"
        )
        assert host.memory_service.get("semantic", saved[0]["memory_id"]) is None
        assert conn.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0] == 0
        assert len(ctx.tools.read_metering(item.run_id)) == 1
        assert result.side_effect_state == "occurred"
        settle_graph(recorder, item, result, ctx)
        reply = conn.execute(
            "SELECT content FROM agent_log WHERE run_id=? AND role='assistant'",
            (item.run_id,),
        ).fetchone()[0]
        assert "private body" not in reply and "记忆删除已确认" in reply
        assert (
            conn.execute(
                "SELECT outcome FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            len(
                conn.execute(
                    "SELECT * FROM agent_log WHERE run_id=?", (item.run_id,)
                ).fetchall()
            )
            == 2
        )
    finally:
        host.close()


def test_ce16_only_final_graph_reply_is_recorded_with_all_attempts(tmp_path):
    import json

    from agent_alfred.graph import llm_node, settle_graph
    from agent_alfred.loop.assistant import Assistant
    from agent_alfred.model import ModelRef

    host, conn, item, ctx, recorder, captured = setup_run(tmp_path)
    try:
        model = ScriptedModel(["private classification", "actual reply"])
        assistant = Assistant(clock=ctx.clock, settings=Settings())
        b = GraphBuilder("reply")
        b.add_node(
            "classify",
            llm_node(
                "classify",
                client=model,
                model=ModelRef("offline", "x"),
                assistant=assistant,
                output_key="category",
            ),
            writes=("category",),
        )
        b.add_node(
            "reply",
            llm_node(
                "respond",
                client=model,
                model=ModelRef("offline", "x"),
                assistant=assistant,
                output_key="reply",
            ),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        b.add_edge("classify", "reply")
        result = b.compile().invoke({}, context=ctx)
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_log WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == 0
        )
        settle_graph(recorder, item, result, ctx)
        rows = conn.execute(
            "SELECT role,content FROM agent_log WHERE run_id=?", (item.run_id,)
        ).fetchall()
        assert (
            len(rows) == 2
            and "actual reply" in dict(rows)["assistant"]
            and "classification" not in str(rows)
        )
        telemetry = json.loads(
            conn.execute(
                "SELECT telemetry FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
        )
        assert len(telemetry["attempts"]) == 2
        assert [s["outcome"] for s in telemetry["memory"]["graph"]["steps"]] == [
            "committed",
            "committed",
        ]
    finally:
        host.close()


def test_ce08_file_publication_loss_forces_failed_receipt(tmp_path, monkeypatch):
    from agent_alfred.graph import Failed, settle_graph, tool_node
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.tools.files import FileTools

    host, conn, item, ctx, recorder, captured = setup_run(tmp_path)
    lease = ManagedStateDirectory.acquire(tmp_path / "files")
    store = RecordingStore(conn, threading.Lock())
    files = FileTools(store, lease, ctx.clock)
    original = FileTools.publish

    def publish_then_fail(self, target, content):
        original(self, target, content)
        raise OSError("injected publication receipt loss")

    monkeypatch.setattr(FileTools, "publish", publish_then_fail)
    try:
        ctx.tools = ToolRegistry(
            files.declarations(),
            clock=ctx.clock,
            metering=ToolMetering(store, ctx.clock),
        )
        b = GraphBuilder("file", tools=ctx.tools)
        b.add_node(
            "draft",
            tool_node("draft_message", {"body": "real draft"}, output_key="draft"),
            writes=("draft",),
        )
        b.add_node(
            "after",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("done"),
        )
        b.add_error_edge("draft", "after")
        result = b.compile().invoke({}, context=ctx)
        assert (
            isinstance(result, Failed)
            and result.forced_stop.error == "file_result_unverified"
        )
        assert result.not_executed == ("after",)
        assert len(ctx.tools.read_metering(item.run_id)) == 1
        settle_graph(recorder, item, result, ctx)
        assert (
            conn.execute(
                "SELECT outcome FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == "failed"
        )
        assert (
            "文件结果待核验"
            in conn.execute(
                "SELECT content FROM agent_log WHERE role='assistant'"
            ).fetchone()[0]
        )
    finally:
        host.close()
        lease.close()


def test_ce08_metering_commit_failure_cannot_follow_error_edge(tmp_path):
    from agent_alfred.graph import Failed, settle_graph, tool_node

    host, conn, item, ctx, recorder, captured = setup_run(tmp_path)
    try:
        conn.execute(
            "CREATE TRIGGER fail_meter BEFORE UPDATE ON tool_metering "
            "BEGIN SELECT RAISE(ABORT,'injected IO boundary'); END"
        )
        conn.commit()
        b = GraphBuilder("meter", tools=ctx.tools)
        b.add_node(
            "write",
            tool_node(
                "create_event",
                {"title": "atomic", "starts_at": "2026-09-13T00:00:00Z"},
                output_key="receipt",
            ),
            writes=("receipt",),
        )
        b.add_node(
            "recover",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("done"),
        )
        b.add_error_edge("write", "recover")
        result = b.compile().invoke({}, context=ctx)
        assert (
            isinstance(result, Failed)
            and result.forced_stop.error == "metering_unconfirmed"
        )
        assert result.not_executed == ("recover",)
        assert conn.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0] == 0
        settle_graph(recorder, item, result, ctx)
        assert (
            conn.execute(
                "SELECT outcome FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == "failed"
        )
    finally:
        host.close()


def test_ce08_mcp_invalid_external_result_stops_graph_and_records_unknown(tmp_path):
    import json

    from agent_alfred.evals.deterministic.test_mcp import FIXTURE, configure
    from agent_alfred.graph import Failed, settle_graph, tool_node
    from agent_alfred.mcp import MCPBridge
    from agent_alfred.tools import ToolPolicy

    behavior = tmp_path / "behavior.json"
    behavior.write_text(json.dumps({"result": {"content": "invalid"}}))
    state = configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
    host, conn, item, ctx, recorder, captured = setup_run(tmp_path)
    state.chmod(0o700)
    bridge = MCPBridge(state, {}, ctx.clock, Redactor(()))
    bridge.start()
    store = RecordingStore(conn, threading.Lock())
    try:
        declarations = bridge.declarations()
        assert declarations
        ctx.tools = ToolRegistry(
            declarations,
            clock=ctx.clock,
            policies={
                t.name: ToolPolicy(configured=True, authorization="allowed")
                for t in declarations
            },
            metering=ToolMetering(store, ctx.clock),
            external_ledger=ExternalToolLedger(store, ctx.clock),
        )
        b = GraphBuilder("mcp", tools=ctx.tools)
        b.add_node(
            "external",
            tool_node(declarations[0].name, {}, output_key="reply"),
            writes=("reply",),
        )
        b.add_node(
            "recover",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("done"),
        )
        b.add_error_edge("external", "recover")
        result = b.compile().invoke({}, context=ctx)
        assert (
            isinstance(result, Failed)
            and result.forced_stop.error == "tool_result_unverified"
        )
        assert result.not_executed == ("recover",) and (state / "called").exists()
        rows = ctx.tools.read_metering(item.run_id)
        assert len(rows) == 1 and rows[0]["result"] == "unknown"
        assert result.side_effect_state == "unknown"
        settle_graph(recorder, item, result, ctx)
        assert (
            conn.execute(
                "SELECT outcome FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == "failed"
        )
    finally:
        bridge.close()
        host.close()


def test_ce08_late_mcp_success_never_reopens_failed_graph(tmp_path, monkeypatch):
    import json
    import os
    import select

    from agent_alfred.evals.deterministic.test_mcp import FIXTURE, configure
    from agent_alfred.graph import Failed, settle_graph, tool_node
    from agent_alfred.mcp import MCPBridge, transport
    from agent_alfred.tools import ToolPolicy

    event, release = tmp_path / "event", tmp_path / "release"
    os.mkfifo(event)
    os.mkfifo(release)
    event_fd = os.open(event, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    behavior = tmp_path / "behavior.json"
    behavior.write_text(
        json.dumps(
            {
                "block_call": True,
                "ignore_eof": True,
                "duplicate": True,
                "event_fifo": str(event),
                "release_fifo": str(release),
            }
        )
    )
    state = configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
    state.chmod(0o700)
    host, conn, item, ctx, recorder, captured = setup_run(tmp_path)
    bridge = MCPBridge(state, {}, ctx.clock, Redactor(()))
    bridge.start()
    store = RecordingStore(conn, threading.Lock())
    pid = int((state / "spawned").read_text())
    actual = transport.os.killpg

    def blocked(group, sig):
        if group == pid and sig:
            raise PermissionError("controlled cleanup boundary")
        return actual(group, sig)

    thread = None
    try:
        monkeypatch.setattr(transport.os, "killpg", blocked)
        declarations = bridge.declarations()
        ctx.tools = ToolRegistry(
            declarations,
            clock=ctx.clock,
            policies={
                t.name: ToolPolicy(configured=True, authorization="allowed")
                for t in declarations
            },
            metering=ToolMetering(store, ctx.clock),
            external_ledger=ExternalToolLedger(store, ctx.clock),
        )
        b = GraphBuilder("late", tools=ctx.tools)
        b.add_node(
            "external",
            tool_node(declarations[0].name, {}, output_key="receipt"),
            writes=("receipt",),
        )
        b.add_node(
            "recover",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("done"),
        )
        b.add_error_edge("external", "recover")
        results = []
        graph = b.compile()
        thread = threading.Thread(
            target=lambda: results.append(graph.invoke({}, context=ctx))
        )
        thread.start()
        assert select.select([event_fd], [], [], 5)[0]
        assert b"called" in os.read(event_fd, 1024)
        ctx.clock.monotonic_value += 61
        assert select.select([event_fd], [], [], 5)[0]
        assert b"cancelled" in os.read(event_fd, 1024)
        thread.join(5)
        assert not thread.is_alive()
        assert (
            isinstance(results[0], Failed)
            and results[0].forced_stop.error == "tool_result_unverified"
        )
        before = ctx.tools.read_metering(item.run_id)
        settle_graph(recorder, item, results[0], ctx)
        os.write(release_fd, b"release\n")
        assert select.select([event_fd], [], [], 5)[0]
        assert b"late_sent" in os.read(event_fd, 1024)
        assert ctx.tools.read_metering(item.run_id) == before
        assert (
            conn.execute(
                "SELECT outcome FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == "failed"
        )
        assert not any(e.envelope.node_id == "recover" for e in captured.events)
    finally:
        monkeypatch.setattr(transport.os, "killpg", actual)
        bridge.close()
        host.close()
        if thread is not None:
            thread.join(5)
        os.close(event_fd)
        os.close(release_fd)


def test_ce16_aborted_classification_never_enters_fallback_or_agent_log(tmp_path):
    from agent_alfred.graph import Failed, llm_node
    from agent_alfred.loop.assistant import Assistant
    from agent_alfred.messages import message_plain_text
    from agent_alfred.model import ModelRef

    host, conn, item, ctx, recorder, captured = setup_run(tmp_path)
    try:
        client = ScriptedModel(["withdrawn classification", "actual fallback"])
        assistant = Assistant(clock=ctx.clock, settings=Settings())
        model = ModelRef("offline", "x")
        b = GraphBuilder("withdrawn")
        b.add_node(
            "classify",
            llm_node(
                "classify",
                client=client,
                model=model,
                assistant=assistant,
                output_key="classification",
            ),
            writes=("classification",),
        )
        b.add_node(
            "invalid",
            fn_node(lambda s, c: NodeOutcome({"illegal": True})),
            terminal=TerminalSpec.no_action("nothing"),
        )
        result = b.compile().invoke({}, context=ctx)
        assert isinstance(result, Failed) and not ctx.transcript
        fallback = assistant.respond(
            "answer",
            client=client,
            model=model,
            budget=ctx.budget,
            working_memory=ctx.transcript,
            run_id=item.run_id,
            session_id=item.session_id,
        )
        assert all(
            "withdrawn" not in message_plain_text(m)
            for m in client.requests[-1].messages
        )
        recorder.settle(
            item,
            outcome=fallback.outcome,
            reply=fallback.reply,
            error=fallback.error,
            step_count=ctx.budget.used,
            duration_ms=0,
            model_results=(*ctx.model_results, *fallback.model_results),
        )
        rows = conn.execute(
            "SELECT role,content FROM agent_log WHERE run_id=?", (item.run_id,)
        ).fetchall()
        assert len(rows) == 2 and "withdrawn" not in str(rows)
        assert "actual fallback" in dict(rows)["assistant"]
    finally:
        host.close()
