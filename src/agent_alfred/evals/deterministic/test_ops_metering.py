"""Tools accounting through the real RuntimeHost submission boundary."""

from agent_alfred.evals.deterministic.test_runtime import _host
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest


def test_public_run_records_read_write_and_rejected_requests_without_bodies():
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock(
                    "write",
                    "create_event",
                    {
                        "title": "private appointment",
                        "starts_at": "2026-09-12T10:00:00Z",
                    },
                ),
                ToolCallBlock("read", "query_events", {}),
                ToolCallBlock("bad", "absent_tool", {}),
            ),
            "Done",
        ]
    )
    host, _, _ = _host(factory=ScriptedModelFactory(model))
    host.start()
    try:
        accepted = host.submit(SubmitRequest(message="Create and inspect appointment"))
        assert host.wait(accepted.run_id).outcome == "completed"
        rows = host.tool_requests(accepted.run_id)
        assert [(r["call_id"], r["start_confirmation"]) for r in rows] == [
            ("write", "confirmed"),
            ("read", "confirmed"),
            ("bad", "not_started"),
        ]
        assert rows[2]["reason"] == "unknown_tool"
        assert all(r["cost"]["kind"] == "not_billable" for r in rows)
        assert "private appointment" not in str(rows)
        assert all(r["source_id"] == "builtin" for r in rows[:2])
    finally:
        host.close()


def test_catalog_includes_hidden_external_with_stable_identity(tmp_path):
    from agent_alfred.clock import FakeClock
    from agent_alfred.events import FanOutSink
    from agent_alfred.runtime.host import RuntimeHost
    from agent_alfred.settings import Settings
    from agent_alfred.tools import Tool, ToolPolicy, ToolSuccess
    from agent_alfred.tools.calendar import object_schema

    tool = Tool(
        "search_fixture",
        "Fixture only",
        object_schema({}),
        lambda args, ctx: ToolSuccess(()),
        "external",
        source_id="fixture-service",
        capability_id="search-v1",
    )
    # Production declarations remain real; injection is explicit at construction.
    import sqlite3

    from agent_alfred import schema

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(["hi"])),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink((), process_instance_id="test"),
        process_instance_id="test",
        extra_tools=(tool,),
        tool_policies={"search_fixture": ToolPolicy(configured=True)},
        tool_authorization_path=tmp_path / "tool-authorizations.json",
    )
    try:
        before = host.tools_catalog()
        row = next(t for t in before["tools"] if t["name"] == "search_fixture")
        assert row["exposure"] == "hidden"
        assert row["connection"] == "unverified"
        saved = host.save_tool_authorization(row["identity"], "allowed", 0)
        assert saved["application_state"] == "applied"
        assert (
            next(t for t in saved["tools"] if t["name"] == "search_fixture")["exposure"]
            == "real"
        )
        stale = host.save_tool_authorization(row["identity"], "denied", 0)
        assert stale["error"]["code"] == "authorization_conflict"
    finally:
        host.close()


def test_accounting_snapshot_keeps_full_run_and_does_not_mix_new_records():
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("q", "query_events", {})),
            "Read",
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            "Later",
        ]
    )
    host, _, _ = _host(factory=ScriptedModelFactory(model))
    host.start()
    try:
        first = host.submit(SubmitRequest(message="Read events"))
        host.wait(first.run_id)
        view = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert view["summary"]["run_count"] == 1
        assert view["summary"]["tool_requests"] == 1
        assert view["summary"]["attempt_count"] == 3
        later = host.submit(SubmitRequest(message="Hello"))
        host.wait(later.run_id)
        detail = host.accounting_detail(view["snapshot_id"], first.run_id)
        assert detail["run"]["run_id"] == first.run_id
        assert host.accounting_page(view["snapshot_id"])["summary"]["run_count"] == 1
        assert (
            host.accounting_snapshot({"range": "all", "timezone": "UTC"})["summary"][
                "run_count"
            ]
            == 2
        )
    finally:
        host.close()
