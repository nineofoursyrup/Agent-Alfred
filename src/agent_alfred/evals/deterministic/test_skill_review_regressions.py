"""Issue #24 independent c2 findings at the real Host/Graph/SQLite seams."""

import json
import sqlite3

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_runtime_skills import (
    SKIP,
    runtime,
    skill,
    submit,
    system,
)
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.events import CapturingSink
from agent_alfred.graph import (
    GraphBuilder,
    NodeOutcome,
    TerminalSpec,
    agent_node,
    fn_node,
    tool_node,
)
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.messages import ToolCallBlock, message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.settings import Settings
from agent_alfred.trace import RunBundleTraceSink


@pytest.mark.parametrize(
    "task,selected",
    [
        ("```text\n```not-a-closer\n使用 A\n```", []),
        ("> 这是引用中的说明\n使用 A", []),
        ("~~~text\n~~~not-a-closer\n使用 A\n~~~", []),
        ("- ```text\n  使用 A\n  ```", []),
        ("> 引文\n>\n> 新段落\n使用 A", []),
        ("`` 示例 ` 使用 A ``", []),
        ("\\`` 使用 A `", []),
        ("不要 **使用 A**", []),
        ("```text\n示例\n```\n使用 A", ["A"]),
        ("> 引文\n\n使用 A", ["A"]),
        ("不要使用 A；使用 B", ["B"]),
        ("use A, do not use B", ["A"]),
    ],
)
def test_spec01_markdown_exclusions_apply_to_actual_fallback_input(
    tmp_path, task, selected
):
    for name in ("A", "B"):
        skill(tmp_path / "builtin", name, f"{name}_PROCEDURE")
    with runtime(tmp_path, [OSError("selector unavailable"), SKIP, "answer"]) as (
        host,
        model,
        _,
    ):
        accepted, result = submit(host, task)
        assert result.outcome == "completed"
        evidence = host.read_run_evidence(accepted.run_id, trace_root=tmp_path)
        assert evidence["memory"]["skills"]["selected"] == selected
        for name in ("A", "B"):
            assert (f"{name}_PROCEDURE" in system(model.requests[-1])) == (
                name in selected
            )


@pytest.mark.parametrize("kind", ["ordinary", "agent", "tool"])
def test_std01_successful_delete_receipt_survives_deadline_at_tool_finished(
    tmp_path, kind
):
    clock = FakeClock()
    saved, entered = [], []

    class Model:
        model = None

        def respond(self, request, **kwargs):
            if self.model is None:
                action = ToolCallBlock(
                    "delete",
                    "delete_memory",
                    {
                        "kind": "semantic",
                        "id": saved[0]["memory_id"],
                        "expected_version": 1,
                    },
                )
                self.model = ScriptedModel(['{"skills":["A"]}', SKIP, calls(action)])
            return self.model.respond(request, **kwargs)

    class AdvanceAfterDelete(CapturingSink):
        def commit(self, prepared, event):
            super().commit(prepared, event)
            if (
                event.payload.name == "tool.finished"
                and event.payload.tool_name == "delete_memory"
                and event.payload.outcome == "ok"
            ):
                clock.monotonic_value = 5

    def graph(**bindings):
        b = GraphBuilder("delete", tools=bindings.pop("tools")).declare_input("task")
        node = (
            agent_node(
                lambda s: s["task"],
                output_key="reply",
                tool_names=("delete_memory",),
                **bindings,
            )
            if kind == "agent"
            else tool_node(
                "delete_memory",
                {
                    "kind": "semantic",
                    "id": saved[0]["memory_id"],
                    "expected_version": 1,
                },
                output_key="reply",
            )
        )
        b.add_node("delete", node, required_reads=("task",), writes=("reply",))
        for name in ("normal", "recover"):
            b.add_node(
                name,
                fn_node(lambda s, c: entered.append(c.node_id) or NodeOutcome()),
                terminal=TerminalSpec.no_action("unexpected"),
            )
        b.add_edge("delete", "normal")
        b.add_error_edge("delete", "recover")
        return b.compile()

    model = Model()
    skill(tmp_path / "builtin", "A", "Delete through authorized memory tools.")
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "trace"),
        clock=clock,
        process_instance_id="skills-test",
    )
    with runtime(
        tmp_path,
        [],
        clock=clock,
        settings=Settings(overall_deadline_s=5),
        factory=ScriptedModelFactory(model),
        extra_sinks=(trace, AdvanceAfterDelete()),
        chat_graph_factory=graph if kind != "ordinary" else None,
    ) as (host, _, capture):
        saved.append(
            host.memory_service.execute(
                {
                    "operation_id": "seed",
                    "kind": "semantic",
                    "action": "save",
                    "payload": {"subject": "delete me", "fact": "private body"},
                },
                CommandContext(ManualOrigin("cli"), "cli"),
            )
        )
        accepted, result = submit(host, "delete the saved memory")
        assert host.memory_service.get("semantic", saved[0]["memory_id"]) is None
        assert result.outcome == "completed" and result.error is None
        assert "记忆删除已确认" in message_plain_text(result.reply)
        assert (
            result.memory_telemetry["finalization_reason"] == "memory_delete_boundary"
        )
        assert entered == []
        assert len(model.model.requests) == (2 if kind == "tool" else 3)
        assert sum(e.payload.name == "run.finished" for e in capture.events) == 1
        if kind != "ordinary":
            assert result.memory_telemetry["graph"]["fallback"] is False
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert conn.execute(
                "SELECT count(*) FROM runs WHERE run_id=?", (accepted.run_id,)
            ).fetchone() == (1,)
            replies = conn.execute(
                "SELECT content FROM agent_log WHERE run_id=? AND role='assistant'",
                (accepted.run_id,),
            ).fetchall()
        assert (
            len(replies) == 1
            and "记忆删除已确认" in json.loads(replies[0][0])[0]["text"]
        )


@pytest.mark.parametrize("failure", ["input_limit", "cancel", "deadline"])
def test_spec02_spec04_fatal_graph_exit_retains_wave_evidence_and_real_attempts(
    tmp_path, failure
):
    clock = FakeClock()
    entered = []

    def graph(**bindings):
        b = GraphBuilder("fatal", tools=bindings.pop("tools")).declare_input("task")
        b.add_node(
            "draft",
            agent_node("draft", output_key="draft", **bindings),
            writes=("draft",),
        )
        if failure == "input_limit":
            node = agent_node("x" * 64001, output_key="final", **bindings)
        else:

            def terminate(state, context):
                if failure == "cancel":
                    raise KeyboardInterrupt("controlled cancellation")
                clock.monotonic_value = 5
                return NodeOutcome({"final": "late"})

            node = fn_node(terminate)
        b.add_node("final", node, writes=("final",))
        b.add_node(
            "normal",
            fn_node(lambda s, c: entered.append("normal") or NodeOutcome()),
            terminal=TerminalSpec.no_action("unexpected"),
        )
        b.add_node(
            "recover",
            fn_node(lambda s, c: entered.append("recover") or NodeOutcome()),
            terminal=TerminalSpec.no_action("unexpected"),
        )
        b.add_edge("final", "normal")
        b.add_error_edge("final", "recover")
        return b.compile()

    skill(tmp_path / "builtin", "A", "FIXED PROCEDURE")
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "trace"),
        clock=clock,
        process_instance_id="skills-test",
    )
    with runtime(
        tmp_path,
        ['{"skills":["A"]}', SKIP, "provisional draft"],
        clock=clock,
        settings=Settings(overall_deadline_s=5),
        chat_graph_factory=graph,
        extra_sinks=(trace,),
    ) as (host, model, capture):
        accepted, result = submit(host, "hello")
        assert result.outcome == ("interrupted" if failure == "cancel" else "failed")
        assert (
            result.error
            == {
                "input_limit": "input_limit_exceeded",
                "cancel": "interrupted",
                "deadline": "overall_deadline",
            }[failure]
        )
        assert len(model.requests) == 3 and entered == []
        evidence = host.read_run_evidence(
            accepted.run_id, trace_root=tmp_path / "trace"
        )
        assert len(evidence["attempts"]) == 3
        graph_evidence = evidence["memory"]["graph"]
        assert graph_evidence["graph_id"] == "fatal"
        assert graph_evidence["fallback"] is False
        assert all(s["outcome"] == "aborted_by_wave" for s in graph_evidence["steps"])
        assert graph_evidence["steps"][0]["attempt_ids"] == [
            evidence["attempts"][-1]["attempt_id"]
        ]
        aborted = [
            e.envelope.node_id
            for e in capture.events
            if e.payload.name == "node.aborted"
        ]
        assert aborted == ["draft", "final"]
        assert evidence["trace_status"] == "available"
        assert [
            e["envelope"]["node_id"]
            for e in evidence["events"]
            if e["payload"]["name"] == "node.aborted"
        ] == aborted
        assert (
            sum(e["payload"]["name"] == "graph.finished" for e in evidence["events"])
            == 1
        )
        assert sum(e.payload.name == "graph.finished" for e in capture.events) == 1
        assert sum(e.payload.name == "run.finished" for e in capture.events) == 1
        assert "FIXED PROCEDURE" in system(model.requests[-1])


def test_std01_deadline_before_graph_action_never_executes_delete(tmp_path):
    clock, saved = FakeClock(), []

    class ExpireAtGraphStart(CapturingSink):
        def commit(self, prepared, event):
            super().commit(prepared, event)
            if event.payload.name == "graph.started":
                clock.monotonic_value = 5

    def graph(**bindings):
        b = GraphBuilder("expired", tools=bindings["tools"]).declare_input("task")
        b.add_node(
            "delete",
            tool_node(
                "delete_memory",
                {
                    "kind": "semantic",
                    "id": saved[0]["memory_id"],
                    "expected_version": 1,
                },
                output_key="reply",
            ),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        return b.compile()

    skill(tmp_path / "builtin", "A", "procedure")
    with runtime(
        tmp_path,
        ['{"skills":["A"]}', SKIP],
        clock=clock,
        settings=Settings(overall_deadline_s=5),
        chat_graph_factory=graph,
        extra_sinks=(ExpireAtGraphStart(),),
    ) as (
        host,
        model,
        capture,
    ):
        saved.append(
            host.memory_service.execute(
                {
                    "operation_id": "seed",
                    "kind": "semantic",
                    "action": "save",
                    "payload": {"subject": "keep me", "fact": "still present"},
                },
                CommandContext(ManualOrigin("cli"), "cli"),
            )
        )
        _, result = submit(host, "delete memory")
        assert result.outcome == "failed" and result.error == "overall_deadline"
        assert host.memory_service.get("semantic", saved[0]["memory_id"]) is not None
        assert len(model.requests) == 2
        assert not any(e.payload.name.startswith("tool.") for e in capture.events)
        assert sum(e.payload.name == "graph.finished" for e in capture.events) == 1
        assert result.memory_telemetry["graph"]["fallback"] is False
