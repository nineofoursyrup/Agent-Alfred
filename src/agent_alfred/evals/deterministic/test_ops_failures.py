"""Fault injection is confined to persistence and process-control seams."""

from decimal import Decimal

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.tools import Tool, ToolCost, ToolPolicy, ToolSuccess
from agent_alfred.tools.calendar import object_schema


def external(name="external_fixture", source="fixture", fn=None):
    return Tool(
        name,
        "External test fixture",
        object_schema({}),
        fn
        or (
            lambda a, c: ToolSuccess(
                (TextBlock("fixture result"),),
                cost=ToolCost(Decimal("0.1"), "credits", "receipt"),
            )
        ),
        "external",
        source_id=source,
        capability_id="stable-capability",
        cost_units=("credits",),
        cost_sources=("receipt",),
    )


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("authorization", ["unset", "allowed", "denied"])
def test_external_matrix_uses_same_schema_and_execution_policy(
    tmp_path, configured, authorization
):
    tool = external()
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("c", tool.name, {})), "Done"],
        tools=(tool,),
        policies={tool.name: ToolPolicy(configured=configured)},
    ) as (host, model, _, _):
        row = next(t for t in host.tools_catalog()["tools"] if t["name"] == tool.name)
        saved = host.save_tool_authorization(row["identity"], authorization, 0)
        assert saved["application_state"] == "applied"
        identifier, _ = run(host)
        schemas = {t.name: t for t in model.requests[1].tools}
        exposed = authorization != "denied" and (
            not configured or authorization == "allowed"
        )
        assert (tool.name in schemas) == exposed
        if exposed and not configured:
            assert not schemas[tool.name].input_schema["properties"]
        request = host.tool_requests(identifier)[0]
        assert request["start_confirmation"] == (
            "confirmed" if configured and authorization == "allowed" else "not_started"
        )
        assert request["cost"]["kind"] == (
            "reported" if configured and authorization == "allowed" else "not_billable"
        )


@pytest.mark.parametrize("stage", ["registration", "finish"])
def test_metering_failure_stops_effects_and_next_model_and_requires_recovery(
    tmp_path, stage
):
    script = [
        GATE,
        calls(
            ToolCallBlock("one", "query_events", {}),
            ToolCallBlock("two", "query_events", {}),
        ),
        "Must not send",
    ]
    with ops_host(tmp_path, script) as (host, model, conn, _):
        if stage == "registration":
            conn.execute(
                "CREATE TRIGGER meter_fault BEFORE INSERT ON tool_metering "
                "BEGIN SELECT RAISE(FAIL,'fault'); END"
            )
        else:
            conn.execute(
                "CREATE TRIGGER meter_fault BEFORE UPDATE OF finished_at "
                "ON tool_metering BEGIN SELECT RAISE(FAIL,'fault'); END"
            )
        conn.commit()
        identity, result = run(host)
        assert result.outcome == "failed"
        assert len(model.requests) == 2
        assert (
            host.submit(SubmitRequest(message="New request")).kind
            == "recording_unavailable"
        )
        view = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert view["summary"]["run_count"] == 1
        # Recovery must not lift the gate while the actual write fault remains.
        assert host.recover_tool_metering()[1] is not None
        assert (
            host.submit(SubmitRequest(message="Still blocked")).kind
            == "recording_unavailable"
        )
        conn.execute("DROP TRIGGER meter_fault")
        conn.commit()
        _, refusal = host.recover_tool_metering()
        assert refusal is None
        assert len(model.requests) == 2
        if stage == "finish":
            rows = host.tool_requests(identity)
            assert rows[0]["start_confirmation"] == "unconfirmed"
            assert rows[1]["start_confirmation"] == "not_started"


def test_atomic_calendar_failure_rolls_back_business_and_ledger(tmp_path):
    with ops_host(
        tmp_path,
        [
            GATE,
            calls(
                ToolCallBlock(
                    "create",
                    "create_event",
                    {"title": "must roll back", "starts_at": "2026-09-12T00:00:00Z"},
                )
            ),
            "unused",
        ],
    ) as (host, _, conn, _):
        conn.execute(
            "CREATE TRIGGER atomic_fault BEFORE UPDATE OF finished_at "
            "ON tool_metering BEGIN SELECT RAISE(FAIL,'fault'); END"
        )
        conn.commit()
        identity, result = run(host)
        assert result.outcome == "failed"
        conn.execute("DROP TRIGGER atomic_fault")
        conn.commit()
        assert host.recover_tool_metering()[1] is None
        # Public query through the actual registered calendar capability.
        from agent_alfred.tools import ToolContext

        result = host._tools.execute(
            ToolCallBlock("inspect", "query_events", {}),
            ToolContext(identity, 99, "inspect", "cli", float("inf")),
        )
        assert result.block.content[0].text == "[]"
        assert host.tool_requests(identity)[0]["result"] != "succeeded"


def test_atomic_success_only_fault_stops_subsequent_model(tmp_path):
    with ops_host(
        tmp_path,
        [
            GATE,
            calls(
                ToolCallBlock(
                    "c",
                    "create_event",
                    {"title": "rollback", "starts_at": "2026-09-12T00:00:00Z"},
                )
            ),
            "unused",
        ],
    ) as (host, model, conn, _):
        conn.execute(
            "CREATE TRIGGER atomic_fault BEFORE UPDATE OF finished_at "
            "ON tool_metering WHEN NEW.result='succeeded' "
            "BEGIN SELECT RAISE(FAIL,'fault'); END"
        )
        conn.commit()
        assert run(host)[1].outcome == "failed"
        assert len(model.requests) == 2


def test_intent_write_expiring_budget_cannot_start_tool(tmp_path, monkeypatch):
    entered = []
    tool = external(
        fn=lambda a, c: entered.append(c) or ToolSuccess((TextBlock("done"),))
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("c", tool.name, {})), "Done"],
        tools=(tool,),
        policies={tool.name: ToolPolicy(configured=True)},
    ) as (host, _, _, clock):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == tool.name
        )
        host.save_tool_authorization(identity, "allowed", 0)
        original = host._tool_metering.intent

        def expire(context):
            original(context)
            clock.monotonic_value += 1000

        monkeypatch.setattr(host._tool_metering, "intent", expire)
        identifier, _ = run(host)
        assert entered == []
        assert host.tool_requests(identifier)[0]["start_confirmation"] == "not_started"


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_publication_receipt_failure_blocks_candidate_too(
    tmp_path, monkeypatch, failure
):
    entered = []
    tool = external(
        fn=lambda a, c: entered.append(c) or ToolSuccess((TextBlock("done"),))
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("c", tool.name, {})), "Done"],
        tools=(tool,),
        policies={tool.name: ToolPolicy(configured=True)},
    ) as (host, model, _, _):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == tool.name
        )
        original = host._tool_authorization.publish

        def fail_after_publish(candidate):
            original(candidate)
            raise failure("receipt failure")

        monkeypatch.setattr(host._tool_authorization, "publish", fail_after_publish)
        if failure is RuntimeError:
            assert (
                host.save_tool_authorization(identity, "allowed", 0)[
                    "application_state"
                ]
                == "pending"
            )
        else:
            with pytest.raises(failure):
                host.save_tool_authorization(identity, "allowed", 0)
        run(host)
        assert entered == []
        assert tool.name not in {t.name for t in model.requests[1].tools}


def test_interrupted_file_request_retains_independent_recovery_link(
    tmp_path, monkeypatch
):
    from agent_alfred.tools.files import FileTools

    original = FileTools._recover

    def interrupt(self, op):
        raise KeyboardInterrupt("after durable prepare")

    monkeypatch.setattr(FileTools, "_recover", interrupt)
    with ops_host(
        tmp_path,
        [
            GATE,
            calls(ToolCallBlock("draft", "draft_message", {"body": "hello"})),
            "unused",
        ],
    ) as (host, _, _, _):
        identifier, _ = run(host)
        observation = host.tool_requests(identifier)[0]
        assert observation["result"] == "unknown"
    monkeypatch.setattr(FileTools, "_recover", original)
    with ops_host(tmp_path, [GATE, "recovered"]) as (host, _, _, _):
        recovered_run, _ = run(host)
        verification = host.tool_verification(identifier, 1, "draft")
        assert verification["state"] == "complete"
        assert verification["related_run"] == recovered_run
        assert host.tool_requests(identifier)[0] == observation


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_control_exceptions_are_not_normal_success(tmp_path, control, monkeypatch):
    import threading

    controls = []
    monkeypatch.setattr(
        threading, "excepthook", lambda args: controls.append(args.exc_type)
    )

    def action(args, context):
        raise control("control")

    tool = external(fn=action)
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("c", tool.name, {})), "unused"],
        tools=(tool,),
        policies={tool.name: ToolPolicy(configured=True)},
    ) as (host, model, _, _):
        row = next(t for t in host.tools_catalog()["tools"] if t["name"] == tool.name)
        host.save_tool_authorization(row["identity"], "allowed", 0)
        identity, result = run(host)
        assert result.outcome != "completed"
        assert len(model.requests) == 2
        assert host.tool_requests(identity)[0]["result"] == "unknown"
        assert host.close()
        if control is SystemExit:
            assert controls == [SystemExit]


def test_authorization_failure_preserves_saved_choice_and_blocks_old_allowed(
    tmp_path, monkeypatch
):
    tool = external()
    with ops_host(
        tmp_path,
        [GATE, "Hi"],
        tools=(tool,),
        policies={tool.name: ToolPolicy(configured=True)},
    ) as (host, _, _, _):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == tool.name
        )
        publish = host._tool_authorization.publish

        def fail(registry):
            raise RuntimeError("application failed")

        monkeypatch.setattr(host._tool_authorization, "publish", fail)
        saved = host.save_tool_authorization(identity, "allowed", 0)
        assert saved["saved"] is True
        assert saved["application_state"] == "pending"
        assert tool.name not in {t.name for t in host._tools.schemas()}
        monkeypatch.setattr(host._tool_authorization, "publish", publish)
        assert host.reapply_tool_authorization(1)["application_state"] == "applied"


@pytest.mark.parametrize("raw", ["broken", '{"schema_version":99}'])
def test_unreadable_authorization_keeps_bytes_and_local_chat(tmp_path, raw):
    state = tmp_path / "state"
    state.mkdir()
    path = state / "tool_authorizations.json"
    path.write_text(raw)
    with ops_host(tmp_path, [GATE, "Local chat works"], tools=(external(),)) as (
        host,
        _,
        _,
        _,
    ):
        catalog = host.tools_catalog()
        assert catalog["configuration_state"] == "authorization_unreadable"
        assert run(host)[1].outcome == "completed"
        assert path.read_text() == raw


def test_authorization_corrupted_after_load_cannot_keep_old_permission(tmp_path):
    tool = external()
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("c", tool.name, {})), "Local done"],
        tools=(tool,),
        policies={tool.name: ToolPolicy(configured=True)},
    ) as (host, model, _, _):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == tool.name
        )
        host.save_tool_authorization(identity, "allowed", 0)
        path = tmp_path / "state" / "tool_authorizations.json"
        path.write_text("broken after load")
        identifier, result = run(host)
        assert result.outcome == "completed"
        assert host.tool_requests(identifier)[0]["start_confirmation"] == "not_started"
        assert tool.name not in {t.name for t in model.requests[1].tools}
        assert host.tools_catalog()["configuration_state"] == "authorization_unreadable"
        assert path.read_text() == "broken after load"


@pytest.mark.parametrize("delivery_storage_fault", [False, True])
def test_metering_stop_marks_saved_projection_not_sent(
    tmp_path, delivery_storage_fault
):
    with ops_host(
        tmp_path,
        [
            GATE,
            calls(
                ToolCallBlock("one", "query_events", {}),
                ToolCallBlock("two", "query_events", {}),
            ),
            "unused",
        ],
    ) as (host, model, conn, _):
        conn.execute(
            "CREATE TRIGGER stop_second BEFORE UPDATE OF finished_at "
            "ON tool_metering WHEN NEW.call_id='two' "
            "BEGIN SELECT RAISE(FAIL,'fault'); END"
        )
        conn.commit()
        if delivery_storage_fault:
            conn.execute(
                "CREATE TRIGGER delivery_fault BEFORE UPDATE OF model_delivery "
                "ON tool_metering BEGIN SELECT RAISE(FAIL,'fault'); END"
            )
            conn.commit()
        identifier, result = run(host)
        assert result.outcome == "failed" and len(model.requests) == 2
        projection = host.read_tool_history(
            tmp_path / "traces", run_id=identifier, step_index=1, call_id="one"
        )
        assert projection["text"] == "[]"
        expected = "unconfirmed" if delivery_storage_fault else "not_sent"
        assert all(
            t["model_delivery"] == expected for t in host.tool_requests(identifier)
        )
        conn.execute("DROP TRIGGER stop_second")
        conn.commit()
        if delivery_storage_fault:
            conn.execute("DROP TRIGGER delivery_fault")
            conn.commit()
        assert host.recover_tool_metering()[1] is None
        assert all(
            t["model_delivery"] == "not_sent" for t in host.tool_requests(identifier)
        )


@pytest.mark.parametrize("boundary", ["max_steps", "overall_deadline", "delete"])
def test_stop_fact_survives_metering_update_failure(tmp_path, boundary, monkeypatch):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    script = (
        [GATE]
        + [calls(ToolCallBlock("q" + str(i), "query_events", {})) for i in range(7)]
        if boundary == "max_steps"
        else []
    )
    with ops_host(
        tmp_path,
        script,
        overall_deadline_s=1 if boundary == "overall_deadline" else None,
    ) as (host, model, conn, _):
        if boundary == "overall_deadline":
            model._script.extend([GATE, calls(ToolCallBlock("q", "query_events", {}))])
            original = host._tool_metering.finish

            def consume_budget(*args, **kwargs):
                original(*args, **kwargs)
                host._tool_metering.clock.monotonic_value += 10000

            monkeypatch.setattr(host._tool_metering, "finish", consume_budget)
        if boundary == "delete":
            receipt = host.memory_service.execute(
                {
                    "operation_id": "seed",
                    "kind": "semantic",
                    "action": "save",
                    "payload": {"subject": "test", "fact": "delete me"},
                },
                CommandContext(ManualOrigin("cli"), "cli"),
            )
            model._script.extend(
                [
                    GATE,
                    calls(
                        ToolCallBlock(
                            "delete",
                            "delete_memory",
                            {
                                "kind": "semantic",
                                "id": receipt["memory_id"],
                                "expected_version": 1,
                            },
                        )
                    ),
                ]
            )
        conn.execute(
            "CREATE TRIGGER delivery_fault BEFORE UPDATE OF model_delivery "
            "ON tool_metering BEGIN SELECT RAISE(FAIL,'fault'); END"
        )
        conn.commit()
        rid, _ = run(host)
        assert len(model.requests) == (8 if boundary == "max_steps" else 2)
        conn.execute("DROP TRIGGER delivery_fault")
        conn.commit()
        assert host.recover_tool_metering()[1] is None
        rows = host.tool_requests(rid)
        assert rows[-1]["model_delivery"] == "not_sent"
        assert all(row["model_delivery"] == "unconfirmed" for row in rows[:-1])


def test_metering_failure_preserves_busy_until_recording_settles(tmp_path, monkeypatch):
    from threading import Event

    from agent_alfred.gateway.web.api import DashboardApi

    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("a", "query_events", {})), GATE, "Recovered"],
    ) as (host, _, conn, _):
        api = DashboardApi(facade=host)
        session_id = api.create_session().session_id
        conn.execute(
            "CREATE TRIGGER meter_fault BEFORE INSERT ON tool_metering "
            "BEGIN SELECT RAISE(FAIL,'fault'); END"
        )
        conn.commit()
        entered, release = Event(), Event()
        original = host._fanout.flush_barrier

        def pause(run_id):
            entered.set()
            assert release.wait(5)
            return original(run_id)

        monkeypatch.setattr(host._fanout, "flush_barrier", pause)
        accepted = host.submit(SubmitRequest(message="test", session_id=session_id))
        try:
            assert entered.wait(5)
            assert host._coord == "recording_pending"
            assert (
                host.submit(SubmitRequest(message="second")).kind == "run_in_progress"
            )
            busy = api.submit({"message": "second", "session_id": session_id})
            assert (busy.status, busy.code) == (409, "run_in_progress")
        finally:
            release.set()
            host.wait(accepted.run_id)
        refused = api.submit({"message": "after recording", "session_id": session_id})
        assert (refused.status, refused.code) == (503, "recording_unavailable")
        conn.execute("DROP TRIGGER meter_fault")
        conn.commit()
        assert host.recover_tool_metering()[1] is None
        assert run(host)[1].outcome == "completed"


@pytest.mark.parametrize("active", [False, True])
def test_current_catalog_reports_bad_authorization_without_switching_active_run(
    tmp_path, active
):
    from threading import Event

    from agent_alfred.gateway.web.accounting_api import read

    tool = external()
    script = [GATE, calls(ToolCallBlock("c", tool.name, {})), "Local done"]
    with ops_host(
        tmp_path,
        script * 2,
        tools=(tool,),
        policies={tool.name: ToolPolicy(configured=True)},
    ) as (host, model, _, _):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == tool.name
        )
        host.save_tool_authorization(identity, "allowed", 0)
        release = Event()
        if active:
            model._gate = release
            accepted = host.submit(SubmitRequest(message="active external request"))
            assert model.entered.wait(5)
        registry = host._tools
        schemas = registry.schemas()
        path = tmp_path / "state" / "tool_authorizations.json"
        path.write_text("broken after load")
        try:
            status, catalog = read(host, tmp_path / "traces", "/api/tools", {})
            assert status == 200
            assert catalog["configuration_state"] == "authorization_unreadable"
            assert catalog["application_state"] == "pending"
            rows = {t["name"]: t for t in catalog["tools"]}
            assert rows[tool.name]["exposure"] == ("unverified" if active else "hidden")
            assert rows["query_events"]["exposure"] == "real"
            if active:
                assert host._tools is registry and registry.schemas() is schemas
        finally:
            release.set()
            if active:
                host.wait(accepted.run_id)
        if active:
            assert (
                host.tool_requests(accepted.run_id)[0]["start_confirmation"]
                == "confirmed"
            )
        idle = host.tools_catalog()
        assert (
            next(t for t in idle["tools"] if t["name"] == tool.name)["exposure"]
            == "hidden"
        )
        rid, result = run(host)
        assert result.outcome == "completed"
        assert host.tool_requests(rid)[0]["start_confirmation"] == "not_started"
        assert path.read_text() == "broken after load"
