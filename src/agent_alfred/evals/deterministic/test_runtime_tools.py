"""Issue #19 public Run seam using SQLite and a model that consumes results."""

import json

import pytest

from agent_alfred.evals.deterministic.test_runtime import _host
from agent_alfred.messages import ToolCallBlock, ToolResultBlock, message_plain_text
from agent_alfred.model import (
    AttemptRecord,
    ModelRef,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.runtime.host import SubmitRequest


def calls(*blocks):
    return ModelResult(
        attempts=(AttemptRecord("a-" + blocks[0].id, False, "committed", Usage()),),
        response=ModelResponse(tuple(blocks), "tool_use", ModelRef("test", "test")),
        final_error=None,
    )


def test_calendar_created_then_queried_across_steps_and_only_final_pair_recorded():
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "create",
                    "create_event",
                    {
                        "title": "test appointment",
                        "starts_at": "2026-09-10T12:00:00+08:00",
                    },
                )
            ),
            calls(ToolCallBlock("query", "query_events", {})),
            "Created and found the appointment.",
        ]
    )
    host, conn, sink = _host(factory=ScriptedModelFactory(model))
    host.start()
    try:
        session_id = host.create_session()
        accepted = host.submit(
            SubmitRequest(
                message="Create and find an appointment",
                gateway="cli",
                session_id=session_id,
            )
        )
        result = host.wait(accepted.run_id, timeout=5)
        assert result.outcome == "completed"
        assert len(model.requests) == 4
        create_result = model.requests[2].messages[-1].blocks[0]
        query_result = model.requests[3].messages[-1].blocks[0]
        assert isinstance(create_result, ToolResultBlock)
        assert create_result.call_id == "create"
        assert isinstance(query_result, ToolResultBlock)
        assert "test appointment" in query_result.content[0].text
        assert conn.execute("SELECT count(*) FROM calendar_entries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_log").fetchone()[0] == 2
        assert message_plain_text(result.reply).startswith(
            "Created and found the appointment."
        )
        assert json.loads(create_result.content[0].text)["id"] > 0
    finally:
        host.close()


def test_memory_save_uses_shared_service_and_single_ledger():
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock("save", "save_fact", {"subject": "test", "fact": "blue"})
            ),
            "Saved.",
        ]
    )
    host, conn, _ = _host(factory=ScriptedModelFactory(model))
    host.start()
    try:
        accepted = host.submit(SubmitRequest(message="明确保存测试事实：blue"))
        result = host.wait(accepted.run_id, timeout=5)
        assert result.outcome == "completed"
        receipt = json.loads(model.requests[2].messages[-1].blocks[0].content[0].text)
        assert receipt["status"] == "saved"
        assert host.memory_service.get("semantic", receipt["memory_id"]).fact == "blue"
        assert conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1
    finally:
        host.close()


@pytest.mark.parametrize("receipt_loss", [False, True, "barrier", "deadline"])
def test_deleted_memory_stops_batch_and_model_with_body_free_system_receipt(
    tmp_path, monkeypatch, receipt_loss
):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    host, conn, sink = _host()
    saved = host.memory_service.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "private subject", "fact": "private body"},
        },
        CommandContext(ManualOrigin("cli"), "cli"),
    )
    model = ScriptedModel(
        [
            '{"retrieve":true,"query":"private","reason_code":"history_recall"}',
            calls(
                ToolCallBlock(
                    "delete",
                    "delete_memory",
                    {
                        "kind": "semantic",
                        "id": saved["memory_id"],
                        "expected_version": 1,
                    },
                ),
                ToolCallBlock(
                    "later",
                    "create_event",
                    {"title": "must not run", "starts_at": "2026-09-10T12:00:00Z"},
                ),
            ),
            "must not request",
        ]
    )
    # The factory is an injected model boundary, shared by gate and answer.
    host.close()
    from agent_alfred.clock import FakeClock
    from agent_alfred.events import CapturingSink, FanOutSink, ToolFinished
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.runtime.host import RuntimeHost
    from agent_alfred.settings import Settings
    from agent_alfred.trace import RunBundleTraceSink

    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="p",
    )
    capture = CapturingSink()
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(model),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([trace, capture], process_instance_id="p"),
        process_instance_id="p",
    )
    if receipt_loss is True:
        original = host.memory_service.execute

        def commit_then_lose_receipt(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("after command commit")

        monkeypatch.setattr(host.memory_service, "execute", commit_then_lose_receipt)
    if receipt_loss in ("barrier", "deadline"):
        checkpoint = trace.checkpoint

        def delayed_checkpoint(run_id=None):
            result = checkpoint(run_id)
            if receipt_loss == "deadline":
                host._clock.monotonic_value = 31
                return result
            from agent_alfred.events import BarrierFlushResult

            return BarrierFlushResult(
                outcome="failed", dropped_events=0, detail="injected"
            )

        monkeypatch.setattr(trace, "checkpoint", delayed_checkpoint)
    host.start()
    try:
        accepted = host.submit(SubmitRequest(message="删除该记忆"))
        result = host.wait(accepted.run_id, timeout=5)
        if receipt_loss in ("barrier", "deadline"):
            assert host.memory_service.get("semantic", saved["memory_id"]) is not None
            assert result.outcome == "completed"
            assert len(model.requests) == 3
            assert (
                conn.execute("SELECT count(*) FROM calendar_entries").fetchone()[0] == 1
            )
            assert all(
                event.payload.tool_name != "delete_memory"
                or event.payload.outcome == "error"
                for event in capture.events
                if isinstance(event.payload, ToolFinished)
            )
            return
        assert result.outcome == "completed"
        assert len(model.requests) == 2
        finished = [
            event for event in capture.events if isinstance(event.payload, ToolFinished)
        ]
        assert len(finished) == 1
        payload = finished[0].payload
        assert "private" not in payload.audit_content
        assert "private" not in payload.model_content
        receipt = json.loads(payload.audit_content)
        assert receipt["fingerprint"] and receipt["key_id"]
        assert receipt["cleanup_state"] in (
            "needs_scope",
            "cleaning",
            "failed",
            "complete",
        )
        from agent_alfred.gateway.web.broker import SSEBroker
        from agent_alfred.runtime.snapshot import RuntimeSnapshot

        broker = SSEBroker(
            process_instance_id="p",
            snapshot=RuntimeSnapshot("p", 0, "idle", None, None),
        )
        try:
            frames = broker.prepare(finished[0]).with_checkpoint(1, "p")
            assert b"private" not in b"".join(frames.wire_frames())
        finally:
            broker.close()
        assert host.memory_service.get("semantic", saved["memory_id"]) is None
        assert conn.execute("SELECT count(*) FROM calendar_entries").fetchone()[0] == 0
        assert "private" not in message_plain_text(result.reply)
        assert "later" in message_plain_text(result.reply)
        assert (
            result.memory_telemetry["finalization_reason"] == "memory_delete_boundary"
        )
    finally:
        host.close()


def test_memory_edit_invalidates_reference_before_next_step(tmp_path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    model = ScriptedModel([])
    host, conn, sink = _host(factory=ScriptedModelFactory(model))
    seed = host.memory_service.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "preference", "fact": "blue unique"},
        },
        CommandContext(ManualOrigin("cli"), "cli"),
    )
    model = ScriptedModel(
        [
            '{"retrieve":true,"query":"blue","reason_code":"history_recall"}',
            calls(
                ToolCallBlock(
                    "edit",
                    "update_memory",
                    {
                        "kind": "semantic",
                        "id": seed["memory_id"],
                        "expected_version": 1,
                        "fact": "green unique",
                    },
                )
            ),
            "Updated.",
        ]
    )
    host.close()
    host, conn, sink = _host(conn=conn, factory=ScriptedModelFactory(model))
    host.start()
    try:
        result = host.wait(
            host.submit(SubmitRequest(message="change preference")).run_id
        )
        assert result.outcome == "completed"
        assert "blue unique" in str(model.requests[1].messages)
        assert "blue unique" not in str(model.requests[2].messages)
        assert (
            host.memory_service.get("semantic", seed["memory_id"]).fact
            == "green unique"
        )
    finally:
        host.close()


def test_memory_query_and_get_return_real_record_versions():
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    model = ScriptedModel([])
    host, conn, _ = _host(factory=ScriptedModelFactory(model))
    seed = host.memory_service.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "color", "fact": "turquoise"},
        },
        CommandContext(ManualOrigin("cli"), "cli"),
    )
    host.close()
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "query", "query_memory", {"kind": "semantic", "text": "turquoise"}
                ),
                ToolCallBlock(
                    "get", "get_memory", {"kind": "semantic", "id": seed["memory_id"]}
                ),
            ),
            "Found.",
        ]
    )
    host, conn, _ = _host(conn=conn, factory=ScriptedModelFactory(model))
    host.start()
    try:
        host.wait(host.submit(SubmitRequest(message="查找记忆")).run_id)
        query, get = model.requests[-1].messages[-1].blocks
        assert (
            json.loads(query.content[0].text)["records"][0]["id"] == seed["memory_id"]
        )
        assert json.loads(get.content[0].text)["record_version"] == 1
    finally:
        host.close()


def test_saved_fact_keeps_system_receipt_when_following_model_request_fails():
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock("save", "save_fact", {"subject": "color", "fact": "blue"})
            ),
            RuntimeError("model unavailable"),
        ]
    )
    host, conn, _ = _host(factory=ScriptedModelFactory(model))
    host.start()
    try:
        result = host.wait(host.submit(SubmitRequest(message="保存事实")).run_id)
        assert result.outcome == "failed"
        assert '"status": "saved"' in message_plain_text(result.reply)
        assert conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 1
    finally:
        host.close()
