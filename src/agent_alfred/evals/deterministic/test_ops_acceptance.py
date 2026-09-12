"""Cross-cutting Issue 55 acceptance over real Host and persistent state."""

import json
import sqlite3
import threading
from dataclasses import replace
from decimal import Decimal

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_ops_failures import external
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.runtime.tool_history import HistoryError
from agent_alfred.tools import ToolCost, ToolPolicy, ToolSuccess


def test_stable_capability_rename_and_different_source_do_not_reassign_history(
    tmp_path,
):
    first = external()
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("c", first.name, {})), "done"],
        tools=(first,),
        policies={first.name: ToolPolicy(configured=True)},
    ) as (host, _, _, _):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == first.name
        )
        host.save_tool_authorization(identity, "allowed", 0)
        original, _ = run(host)
    renamed = replace(first, name="renamed_fixture")
    different = external(source="other-service")
    with ops_host(
        tmp_path,
        [],
        tools=(renamed, different),
        policies={
            renamed.name: ToolPolicy(configured=True),
            different.name: ToolPolicy(configured=True),
        },
    ) as (host, _, _, _):
        catalog = {t["name"]: t for t in host.tools_catalog()["tools"]}
        assert catalog[renamed.name]["identity"] == identity
        assert catalog[renamed.name]["exposure"] == "real"
        assert catalog[different.name]["exposure"] == "hidden"
        assert host.tool_requests(original)[0]["source_id"] == "fixture"
    with ops_host(tmp_path, [], tools=(different,)) as (host, _, _, _):
        view = host.accounting_snapshot(
            {"range": "all", "timezone": "UTC", "tool": identity}
        )
        detail = host.accounting_detail(view["snapshot_id"], original)
        assert detail["run"]["tools"][0]["currently_registered"] is False


def test_disk_conflict_preserves_bytes_and_applied_policy(tmp_path):
    tool = external()
    with ops_host(
        tmp_path, [], tools=(tool,), policies={tool.name: ToolPolicy(configured=True)}
    ) as (host, _, _, _):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == tool.name
        )
        host.save_tool_authorization(identity, "allowed", 0)
        path = tmp_path / "state" / "tool_authorizations.json"
        disk = json.dumps(
            {"schema_version": 1, "revision": 7, "authorizations": {identity: "denied"}}
        )
        path.write_text(disk)
        conflict = host.save_tool_authorization(identity, "denied", 1)
        assert conflict["error"]["code"] == "authorization_conflict"
        assert conflict["application_state"] == "applied"
        assert conflict["authorizations"][identity] == "allowed"
        assert conflict["disk_configuration"]["authorizations"][identity] == "denied"
        assert path.read_text() == disk


def test_two_services_credits_unknown_and_local_free_are_separate(tmp_path):
    one, two = (
        external(name="first", source="first-service"),
        external(name="second", source="second-service"),
    )
    unknown = external(
        name="unknown",
        source="third-service",
        fn=lambda a, c: ToolSuccess((TextBlock("no cost"),)),
    )
    undeclared = external(
        name="undeclared",
        source="fourth-service",
        fn=lambda a, c: ToolSuccess(
            (TextBlock("bad unit"),), cost=ToolCost(Decimal(5), "USD", "receipt")
        ),
    )
    tools = (one, two, unknown, undeclared)
    script = [
        GATE,
        calls(
            *(ToolCallBlock(t.name, t.name, {}) for t in tools),
            ToolCallBlock("local", "query_events", {}),
        ),
        "done",
    ]
    with ops_host(
        tmp_path,
        script,
        tools=tools,
        policies={t.name: ToolPolicy(configured=True) for t in tools},
    ) as (host, _, _, _):
        revision = 0
        for t in host.tools_catalog()["tools"]:
            if t["effect"] == "external":
                host.save_tool_authorization(t["identity"], "allowed", revision)
                revision += 1
        run(host)
        summary = host.accounting_snapshot({"range": "all", "timezone": "UTC"})[
            "summary"
        ]
        assert summary["tool_costs"] == [
            {"service": "first-service", "unit": "credits", "units": "0.1"},
            {"service": "second-service", "unit": "credits", "units": "0.1"},
        ]
        assert summary["tool_cost_states"] == {
            "not_billable": 1,
            "reported": 2,
            "unknown": 2,
            "unrecorded": 0,
        }


def test_active_run_exposes_coverage_without_guessing_attempt_count(tmp_path):
    gate = threading.Event()
    with ops_host(tmp_path, [GATE, "done"]) as (host, model, _, _):
        model._gate = gate
        accepted = host.submit(SubmitRequest(message="wait"))
        try:
            assert model.entered.wait(2)
            view = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
            detail = host.accounting_detail(view["snapshot_id"], accepted.run_id)
            assert detail["run"]["phase"] != "finished"
            assert "attempt_count_unconfirmed" in detail["run"]["coverage"]
            assert view["summary"]["incomplete_runs"] == 1
            assert (
                host.save_tool_authorization("x", "allowed", 0)["error"]["code"]
                == "mutation_in_flight"
            )
        finally:
            gate.set()
            host.wait(accepted.run_id)


def test_database_read_failure_is_not_empty_accounting(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, conn, _):
        run(host)

        def deny(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "runs"
                else sqlite3.SQLITE_OK
            )

        conn.set_authorizer(deny)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                host.accounting_snapshot({"range": "all", "timezone": "UTC"})
            assert not conn.in_transaction
        finally:
            conn.set_authorizer(None)


@pytest.mark.parametrize(
    "damage", ["aborted", "duplicate", "cross_run", "symlink", "metadata"]
)
def test_history_rejects_unmatched_parameters_and_untrusted_files(tmp_path, damage):
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("q", "query_events", {})), "done"],
        adapter=True,
    ) as (host, _, _, _):
        identifier, _ = run(host)
        trace = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        if damage == "symlink":
            backup = trace.with_name("outside.jsonl")
            trace.rename(backup)
            trace.symlink_to(backup)
        elif damage == "metadata":
            meta = trace.with_name("meta.json")
            value = json.loads(meta.read_text())
            value["run_id"] = "another-run"
            meta.write_text(json.dumps(value))
        else:
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            for event in events:
                if damage == "cross_run":
                    event["run_id"] = "another-run"
                if (
                    event["step_index"] == 1
                    and event["payload"].get("name") == "attempt.committed"
                ):
                    if damage == "aborted":
                        event["payload"]["name"] = "attempt.aborted"
                    if damage == "duplicate":
                        event["payload"]["blocks"] *= 2
            trace.write_text("".join(json.dumps(e) + "\n" for e in events))
        with pytest.raises(HistoryError):
            host.read_tool_history(
                tmp_path / "traces",
                run_id=identifier,
                step_index=1,
                call_id="q",
                projection="parameters",
            )


def test_manual_predelete_trace_survives_but_deleted_memory_is_not_reused(tmp_path):
    with ops_host(
        tmp_path,
        [
            GATE,
            calls(
                ToolCallBlock(
                    "save",
                    "save_fact",
                    {"subject": "test", "fact": "private deletion test"},
                )
            ),
            "done",
        ],
    ) as (host, model, _, _):
        identifier, _ = run(host, message="请记住 test 的 private deletion test")
        receipt = json.loads(model.requests[2].messages[-1].blocks[0].content[0].text)
        record_id = receipt["memory_id"]
        model._script.extend(
            [
                GATE,
                calls(
                    ToolCallBlock(
                        "get", "get_memory", {"kind": "semantic", "id": record_id}
                    )
                ),
                "read",
                GATE,
                calls(
                    ToolCallBlock(
                        "delete",
                        "delete_memory",
                        {"kind": "semantic", "id": record_id, "expected_version": 1},
                    )
                ),
            ]
        )
        read_run, _ = run(host)
        deletion, _ = run(host, message="明确删除这条记忆")
        assert host.memory_service.get("semantic", record_id) is None
        before = host.read_tool_history(
            tmp_path / "traces", run_id=read_run, step_index=1, call_id="get"
        )
        assert "private deletion test" in before["text"]
        deleted = host.read_tool_history(
            tmp_path / "traces", run_id=deletion, step_index=1, call_id="delete"
        )
        assert "private deletion test" not in deleted["text"]


@pytest.mark.parametrize("stage", ["registration", "intent", "finish"])
def test_restart_keeps_only_provable_start_facts(tmp_path, monkeypatch, stage):
    from agent_alfred.tools.metering import ToolMetering

    method = {"registration": "register", "intent": "intent", "finish": "finish"}[stage]
    original = getattr(ToolMetering, method)
    entered = []
    tool = replace(
        external(
            fn=lambda a, c: entered.append(c) or ToolSuccess((TextBlock("done"),))
        ),
        effect="local_read",
    )

    def interrupt(self, *args, **kwargs):
        # Exclude startup's synthetic storage-health probe.
        context = args[1] if stage == "registration" else args[0]
        if context.run_id.startswith("metering-check-"):
            return original(self, *args, **kwargs)
        if stage != "finish":
            original(self, *args, **kwargs)
        raise KeyboardInterrupt("persistent boundary")

    monkeypatch.setattr(ToolMetering, method, interrupt)
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("q", tool.name, {})), "unused"],
        tools=(tool,),
    ) as (host, model, _, _):
        identifier, _ = run(host)
        assert len(model.requests) == 2
    monkeypatch.setattr(ToolMetering, method, original)
    with ops_host(tmp_path, [], tools=(tool,)) as (host, _, _, _):
        row = host.tool_requests(identifier)[0]
        assert row["start_confirmation"] == (
            "unconfirmed" if stage == "finish" else "not_started"
        )
        assert len(entered) == (1 if stage == "finish" else 0)
        assert (
            host.accounting_snapshot({"range": "all", "timezone": "UTC"})["summary"][
                "tool_requests"
            ]
            == 1
        )


def test_duplicate_call_identity_stops_before_dispatch_and_new_identity_is_new_action(
    tmp_path,
):
    with ops_host(
        tmp_path,
        [
            GATE,
            calls(
                ToolCallBlock("same", "query_events", {}),
                ToolCallBlock("same", "query_events", {}),
            ),
            "unused",
        ],
    ) as (host, model, _, _):
        identifier, result = run(host)
        assert result.outcome == "failed"
        assert host.tool_requests(identifier) == []
        assert len(model.requests) == 2
    with ops_host(
        tmp_path / "new",
        [
            GATE,
            calls(
                ToolCallBlock("one", "query_events", {}),
                ToolCallBlock("two", "query_events", {}),
            ),
            "done",
        ],
    ) as (host, _, _, _):
        identifier, _ = run(host)
        assert len(host.tool_requests(identifier)) == 2
        assert all(
            t["start_confirmation"] == "confirmed"
            for t in host.tool_requests(identifier)
        )


def test_http_close_drains_inflight_history_before_host_resources(
    tmp_path, monkeypatch
):
    from http.client import HTTPConnection
    from urllib.parse import urlencode

    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.tool_history import ToolHistory
    from agent_alfred.wiring import build_dashboard

    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        port=free_loopback_port(),
        factory=ScriptedModelFactory(
            ScriptedModel([GATE, calls(ToolCallBlock("q", "query_events", {})), "done"])
        ),
    )
    entered, release = threading.Event(), threading.Event()
    original = ToolHistory._read

    def paused(self, *args):
        entered.set()
        assert release.wait(5)
        return original(self, *args)

    dashboard.start()
    identifier, _ = run(dashboard.host)
    monkeypatch.setattr(ToolHistory, "_read", paused)
    results = []

    def read():
        client = HTTPConnection("127.0.0.1", dashboard.port, timeout=5)
        try:
            client.request(
                "GET",
                "/api/tools/history?"
                + urlencode({"run_id": identifier, "step_index": 1, "call_id": "q"}),
            )
            response = client.getresponse()
            results.append(
                (
                    response.status,
                    response.getheader("Cache-Control"),
                    json.loads(response.read()),
                )
            )
        finally:
            client.close()

    worker = threading.Thread(target=read)
    try:
        worker.start()
        assert entered.wait(2)
        assert dashboard.close(timeout=0.02) is False
        assert dashboard.host._closed is False
    finally:
        release.set()
        worker.join(5)
        assert dashboard.close(timeout=5)
    assert results[0][0] == 200
    assert "no-store" in results[0][1]
    assert results[0][2]["text"] == "[]"
