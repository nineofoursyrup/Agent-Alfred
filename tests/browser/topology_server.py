"""#75 real dashboard with graph-build controls, never a mock describe.

stdin changes a builder's input and invokes the existing authorization publication
path. missing fails at startup construction. Model transport is the only substitute.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agent_alfred.graph import (
    GraphBuilder,
    GraphRegistry,
    NodeOutcome,
    TerminalSpec,
    fn_node,
)
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.routing import build_routing_graph
from agent_alfred.wiring import build_dashboard


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--threshold", type=int)
    args = parser.parse_args()
    mode_file = args.state / "graph-mode"
    mode = mode_file.read_text() if mode_file.exists() else "baseline"
    builds = 0
    model = ScriptedModel([])

    def build(tools):
        nonlocal builds
        builds += 1
        if mode == "missing":
            raise ValueError("test startup graph build failure")
        if mode == "alternate":
            b = GraphBuilder("message_routing", tools=tools)
            b.add_node("future_step", fn_node(lambda s, c: NodeOutcome()))
            b.add_node(
                "future_exit", fn_node(lambda s, c: NodeOutcome()),
                terminal=TerminalSpec.no_action("future_reason"),
            )
            b.add_conditional_edges(
                "future_step", lambda s: "future_branch",
                {"future_branch": "future_exit"},
            )
            return GraphRegistry({"message_routing": b}).get("message_routing")
        return build_routing_graph(tools)

    dashboard = build_dashboard(
        state_dir=args.state, port=args.port, skill_builtin=args.state / "builtin",
        factory=ScriptedModelFactory(model), routing_graph_builder=build,
    )
    try:
        dashboard.start()
        print("ready", flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "stop":
                break
            if command.startswith("publish-"):
                mode = command.removeprefix("publish-")
                result = dashboard.host.reapply_tool_authorization(
                    dashboard.host.tools_catalog()["revision"]
                )
                assert "error" not in result, result
            if command == "counts":
                (args.state / "counts.json").write_text(json.dumps({
                    "builds": builds, "model_requests": len(model.requests),
                }))
            print("ok " + command, flush=True)
    finally:
        assert dashboard.close()


if __name__ == "__main__":
    main()
