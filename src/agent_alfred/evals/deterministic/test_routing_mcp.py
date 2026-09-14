"""#26 CE-14: real stdio discovery and atomic graph/registry publication."""

import json

from agent_alfred.evals.deterministic.test_mcp import FIXTURE, configure, control
from agent_alfred.evals.deterministic.test_message_routing import enable
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, runtime, submit
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import ToolCallBlock
from agent_alfred.runtime.routing import build_routing_graph


def test_ce14_real_catalog_schema_name_change_and_failed_publication(tmp_path):
    behavior = tmp_path / "server.json"

    def directory(name="echo", schema=None):
        behavior.write_text(
            json.dumps(
                {
                    "tools": [
                        dict(
                            name=name,
                            description="fixture",
                            inputSchema=schema or {"type": "object"},
                        )
                    ]
                }
            )
        )

    directory()
    configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
    fail_build = []

    def build(tools):
        if fail_build:
            raise ValueError("controlled candidate compilation failure")
        return build_routing_graph(tools)

    script = []
    for i in range(3):
        name = "mcp_test_other" if i == 2 else "mcp_test_echo"
        script.extend([SKIP, "full", calls(ToolCallBlock(str(i), name, {})), "answer"])
    script.extend(
        [
            SKIP,
            "full",
            calls(ToolCallBlock("suspended", "mcp_test_other", {})),
            "unavailable acknowledged",
        ]
    )
    with runtime(tmp_path, script, routing_graph_builder=build) as (host, model, _):
        host.initialize_mcp()
        enable(host)
        hashes, generations, capabilities = [], [], []
        for stage in range(3):
            if stage:
                directory(
                    "echo" if stage == 1 else "other",
                    {"type": "object", "properties": {"value": {"type": "string"}}},
                )
                assert control(host, "reconnect", "test")["status"] == "completed"
            catalog = host.tools_catalog()
            tool = next(
                t for t in catalog["tools"] if t["source_id"].startswith("mcp:")
            )
            assert "error" not in host.save_tool_authorization(
                tool["identity"], "allowed", catalog["revision"]
            )
            accepted, result = submit(host, "task")
            assert host.tool_requests(accepted.run_id)[0]["result"] == "succeeded"
            assert result.outcome == "completed"
            routing = result.memory_telemetry["routing"]
            hashes.append(routing["topology_hash"])
            generations.append(routing["generation"])
            capabilities.append(routing["capabilities"])
            assert tool["name"] in [t.name for t in model.requests[-1].tools]
        assert hashes[0] == hashes[1] and hashes[1] != hashes[2]
        assert generations[0] < generations[1] < generations[2]
        assert capabilities[0] != capabilities[1]
        previous = host.routing_snapshot()
        fail_build.append(True)
        directory("third")
        assert control(host, "reconnect", "test")["status"] != "completed"
        current = host.routing_snapshot()
        assert current["graph"] is previous["graph"]
        assert current["generation"] == previous["generation"]
        assert host.connections()["mcp"]["servers"][0]["state"] != "connected"
        assert (
            json.loads((tmp_path / "state" / "called").read_text())["params"]["name"]
            == "other"
        )

        before_call = (tmp_path / "state" / "called").read_bytes()
        accepted, result = submit(host, "try suspended tool")
        rows = host.tool_requests(accepted.run_id)
        assert len(rows) == 1 and rows[0]["result"] != "succeeded"
        assert (tmp_path / "state" / "called").read_bytes() == before_call


def test_ce07_14_models_and_mcp_mutations_refused_until_recording_settles(tmp_path):
    import threading

    from agent_alfred.gateway.web.api import DashboardApi
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest

    configure(tmp_path, enabled=True)
    model_gate, recording_gate, pending = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    model = ScriptedModel(
        [SKIP, "full", "A answer", SKIP, "B ordinary"], gate=model_gate
    )

    def observed(snapshot):
        if snapshot.coordinator_state == "recording_pending":
            pending.set()

    with runtime(
        tmp_path,
        [],
        factory=ScriptedModelFactory(model),
        before_recording_commit=recording_gate,
        snapshot_listener=observed,
    ) as (host, _, _):
        host.initialize_mcp()
        enable(host)
        before = host.routing_snapshot()
        accepted = host.submit(SubmitRequest("A"))
        try:
            assert model.entered.wait(5)
            for stage in ("running", "recording_pending"):
                if stage == "recording_pending":
                    model_gate.set()
                    assert pending.wait(5)
                assert control(host, "reconnect", "test") == {
                    "error": {"code": "mutation_in_flight"}
                }
                assert DashboardApi(facade=host).mutate_settings(
                    dict(
                        action="assign",
                        slot="primary",
                        endpoint_id="other",
                        model_id="changed",
                        expected_revision=0,
                    )
                ) == (409, {"code": "mutation_in_flight"})
                assert host.routing_snapshot()["generation"] == before["generation"]
        finally:
            model_gate.set()
            recording_gate.set()
        result = host.wait(accepted.run_id)
        assert result.memory_telemetry["routing"]["generation"] == before["generation"]
        api = DashboardApi(facade=host)
        assert (
            api.mutate_behaviour(
                dict(action="save", expected_revision=1, enabled=False)
            )[0]
            == 200
        )
        _, second = submit(host, "B", accepted.session_id)
        assert "routing" not in second.memory_telemetry
        assert len(model.requests) == 5
