"""#75: real published graphs through the guarded HTTP observation seam."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.routing import build_routing_graph
from agent_alfred.wiring import build_dashboard


def get(origin, path, **headers):
    try:
        response = urlopen(Request(origin + path, headers=headers), timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def test_ce02_ce05_ce16_published_graphs_are_read_only_even_when_disabled(tmp_path):
    builds = []

    def build(tools):
        graph = build_routing_graph(tools)
        builds.append(graph)
        return graph

    port = free_loopback_port()
    model = ScriptedModel([])
    dashboard = build_dashboard(
        state_dir=tmp_path,
        port=port,
        factory=ScriptedModelFactory(model),
        routing_graph_builder=build,
    )
    dashboard.start()
    try:
        origin = f"http://127.0.0.1:{port}"
        before = get(origin, "/api/behaviour")[1]
        count = len(builds)
        for workflow, nodes, edges in (
            ("message_routing", 9, 10),
            ("manual_aggregation", 15, 20),
        ):
            path = "/api/behaviour/topology?workflow=" + workflow
            status, body = get(origin, path)
            assert status == 200, body
            assert body["status"] == "available"
            assert body["graph_id"] == workflow
            assert body["process_instance_id"] == dashboard.host.process_instance_id
            assert body["publication_generation"] >= 1
            assert body["read_at"]
            description = body["description"]
            assert len(description["topology"]["nodes"]) == nodes
            assert len(description["topology"]["edges"]) == edges
            if workflow == "message_routing":
                assert description == builds[-1].describe()
            presentation = description["presentation"]
            for node in description["topology"]["nodes"]:
                assert presentation["nodes"][node["node_id"]]["description"]
            for edge in description["topology"]["edges"]:
                if edge["label"]:
                    assert presentation["branches"][edge["source"]][edge["label"]]
            assert get(origin, path, Origin="https://untrusted.example")[0] == 403
            assert get(origin, path, Host="untrusted.example")[0] == 400
        assert get(origin, "/api/behaviour")[1] == before
        assert before["enabled"] is False
        assert model.requests == []
        assert len(builds) == count
        assert not (tmp_path / "behaviour.json").exists()
        assert get(origin, "/api/runs")[1]["runs"] == []
        assert get(origin, "/api/behaviour/topology?workflow=arbitrary")[0] == 400
    finally:
        assert dashboard.close()
