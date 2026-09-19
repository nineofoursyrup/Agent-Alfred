"""Real #81 Host. stdin controls only explicit publication/IO fault barriers."""

import argparse
import json
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from routing_server import RoutingModel

from agent_alfred.events import CapturingSink
from agent_alfred.managed_state import ManagedFileLease
from agent_alfred.model import ScriptedModelFactory
from agent_alfred.runtime.routing import build_routing_graph
from agent_alfred.wiring import build_dashboard


class Barrier:
    def __init__(self, name):
        self.release = threading.Event()
        self.release.set()
        self.name = name
        self.entered = threading.Event()

    def wait(self):
        self.entered.set()
        if not self.release.is_set():
            print("entered " + self.name, flush=True)
        assert self.release.wait(30)


class Publication(CapturingSink):
    def __init__(self):
        super().__init__(name="path-publication-test")
        self.barrier = Barrier("wave")

    def prepare(self, event):
        if event.payload.name == "path.wave" and event.payload.wave == 0:
            self.barrier.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--threshold", type=int)
    args = parser.parse_args()
    record, publication = Barrier("recording"), Publication()
    io = Barrier("trace")
    real_write = ManagedFileLease.write_all
    mode = {"trace": None}

    def write(lease, payload, **options):
        if lease.path.name == "trace.jsonl":
            name = json.loads(payload).get("payload_name")
            target = mode["trace"]
            if target == "missing" or target == "prefix" and name == "path.wave":
                io.wait()
        return real_write(lease, payload, **options)

    ManagedFileLease.write_all = write

    class PathModel(RoutingModel):
        def __init__(self):
            super().__init__()
            from agent_alfred.messages import ToolCallBlock
            from agent_alfred.model import (
                AttemptRecord,
                ModelRef,
                ModelResponse,
                ModelResult,
                ScriptedModel,
                Usage,
            )

            self.side_effect = ScriptedModel(
                [
                    ModelResult(
                        (
                            AttemptRecord(
                                "side-effect-attempt", False, "committed", Usage()
                            ),
                        ),
                        ModelResponse(
                            (
                                ToolCallBlock(
                                    "save-one",
                                    "save_fact",
                                    {"subject": "user", "fact": "tea"},
                                ),
                            ),
                            "tool_use",
                            ModelRef("test", "m"),
                        ),
                        None,
                    ),
                    RuntimeError("after real save"),
                ]
            )

        def respond(self, request, **options):
            from agent_alfred.messages import message_plain_text

            system = "\n".join(b.text for b in request.system or ())
            if (
                any(
                    "PATH side effect" in message_plain_text(m)
                    for m in request.messages
                )
                and not system.startswith("Classify only the current task")
                and "Decide whether long-term memory" not in system
            ):
                return self.side_effect.respond(request, **options)
            result = super().respond(request, **options)
            if graph_mode[0] == "policy-overall_deadline" and system.startswith(
                "Classify only"
            ):
                clock.monotonic_value = 31
            return result

    model = PathModel()
    versions = [0]
    graph_mode = [
        "unavailable" if (args.state / "graph-unavailable").exists() else "normal"
    ]

    policy_file = args.state / "policy-case"
    if policy_file.exists():
        graph_mode[0] = "policy-" + policy_file.read_text()

    def build(tools):
        if graph_mode[0].startswith("eligibility-"):
            from agent_alfred.evals.deterministic.test_run_path import (
                fallback_eligibility_graph,
            )

            return fallback_eligibility_graph(
                tools, graph_mode[0].removeprefix("eligibility-")
            )
        if graph_mode[0].startswith("terminal"):
            from agent_alfred.evals.deterministic.test_run_path import (
                terminal_count_graph,
            )

            return terminal_count_graph(tools, int(graph_mode[0][-1]))
        if graph_mode[0] == "unavailable":
            raise ValueError("controlled graph construction failure")
        if graph_mode[0] != "normal" and not graph_mode[0].startswith("policy-"):
            from agent_alfred.graph import (
                GraphBuilder,
                NodeOutcome,
                TerminalSpec,
                fn_node,
                llm_node,
            )

            b = GraphBuilder(
                "message_routing",
                presentation={
                    "policy": "<img src=x onerror=alert(1)> literal explanation",
                },
            )
            for key in ("task", "prepared_context", "has_loaded_skills"):
                b.declare_input(key)
            if graph_mode[0] == "unknown":
                b.add_node(
                    "unknown_valid",
                    fn_node(lambda s, c: NodeOutcome()),
                    terminal=TerminalSpec.no_action("unknown_test"),
                )
            else:
                b.add_node(
                    "first",
                    llm_node(lambda s: "first", binding="answer", output_key="first"),
                    writes=("first",),
                )
                b.add_node("a", fn_node(lambda s, c: NodeOutcome()))

                def fail(s, c):
                    raise ValueError("controlled wave failure")

                b.add_node("b", fn_node(fail))
                b.add_node(
                    "last",
                    fn_node(lambda s, c: NodeOutcome()),
                    terminal=TerminalSpec.no_action("last"),
                )
                b.add_edge("first", "a")
                b.add_edge("first", "b")
                b.add_edge("a", "last")
                b.add_edge("b", "last")
            return b.compile()
        from agent_alfred.runtime.routing import project_context

        def projection(snapshot):
            if (args.state / "corrupt-context").exists():
                return project_context({**snapshot, "context_version": 999})
            return project_context(snapshot)

        graph = build_routing_graph(tools, projection=projection)
        if versions[0]:
            from dataclasses import replace

            from agent_alfred.graph.types import freeze

            description = graph.describe()
            description["presentation"]["policy"] = "新版同结构说明"
            graph = replace(graph, description=freeze(description))
        return graph

    from agent_alfred.clock import FakeClock
    from agent_alfred.settings import Settings

    clock = FakeClock()
    dashboard = build_dashboard(
        clock=clock,
        settings=Settings(
            max_steps=2 if graph_mode[0] == "policy-budget_exhausted" else 8,
            overall_deadline_s=30,
        ),
        state_dir=args.state,
        port=args.port,
        factory=ScriptedModelFactory(model),
        before_recording_commit=record,
        extra_sinks=(publication,),
        routing_graph_builder=build,
        skill_builtin=args.state / "builtin",
    )
    try:
        dashboard.start()
        print("ready", flush=True)
        retained_original = None
        for line in sys.stdin:
            command = line.strip()
            if command.startswith("stage-damage "):
                from agent_alfred.evals.deterministic.test_run_path import (
                    contradictory_stage_trace,
                )

                trace = next(args.state.rglob("trace.jsonl"))
                retained_original = trace.read_bytes()
                trace.write_bytes(
                    contradictory_stage_trace(retained_original, command.split()[1])
                )
            if command.startswith("damage "):
                from agent_alfred.evals.deterministic.test_run_path import (
                    contradictory_trace,
                )

                trace = next(args.state.rglob("trace.jsonl"))
                retained_original = trace.read_bytes()
                trace.write_bytes(
                    contradictory_trace(retained_original, command.split()[1])
                )
            if command == "restore-trace":
                trace = next(args.state.rglob("trace.jsonl"))
                assert retained_original is not None
                trace.write_bytes(retained_original)
            if command == "prune":
                import shutil

                from agent_alfred import schema

                trace = next(args.state.rglob("trace.jsonl"))
                metadata = json.loads((trace.parent / "meta.json").read_text())
                shutil.rmtree(trace.parent)
                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    schema.record_trace_prune(
                        conn,
                        run_id=metadata["run_id"],
                        prune_reason="manual",
                        prune_requested_at="2026-09-20T00:00:00Z",
                        absence_confirmed_at="2026-09-20T00:00:01Z",
                    )
            if command == "limit":
                dashboard.host._run_path.max_events = 4
            if command == "stop":
                break
            if command == "hold-recording":
                record.entered.clear()
                record.release.clear()
            if command == "wait-recording":
                assert record.entered.wait(10)
            if command == "release-recording":
                record.release.set()
            if command == "hold-wave":
                publication.barrier.entered.clear()
                publication.barrier.release.clear()
            if command == "wait-wave":
                assert publication.barrier.entered.wait(10)
            if command == "release-wave":
                publication.barrier.release.set()
            if command.startswith("hold-trace "):
                mode["trace"] = command.split()[1]
                io.entered.clear()
                io.release.clear()
            if command == "wait-trace":
                assert io.entered.wait(10)
            if command == "release-trace":
                mode["trace"] = None
                io.release.set()
            if command.startswith("graph "):
                graph_mode[0] = command.split()[1]
                dashboard.host._publish_tools(dashboard.host._tools)
            if command == "publish":
                versions[0] += 1
                dashboard.host._publish_tools(dashboard.host._tools)
            if command == "fail-recording":
                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    conn.execute(
                        "CREATE TRIGGER fail_path_recording "
                        "BEFORE UPDATE OF phase ON runs "
                        "WHEN NEW.phase='finished' "
                        "BEGIN SELECT RAISE(ABORT,'fault'); END"
                    )
            if command == "repair-recording":
                with sqlite3.connect(args.state / "db.sqlite3") as conn:
                    conn.execute("DROP TRIGGER fail_path_recording")
            print("ok " + command, flush=True)
    finally:
        record.release.set()
        publication.barrier.release.set()
        io.release.set()
        assert dashboard.close()


if __name__ == "__main__":
    main()
