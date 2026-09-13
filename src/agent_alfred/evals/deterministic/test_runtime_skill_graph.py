"""SKILL-SPEC-r1 CE-25/26: Host-owned chat Graph execution and finalizer."""

import json
import sqlite3

import pytest

from agent_alfred.evals.deterministic.test_runtime_skills import (
    SKIP,
    runtime,
    skill,
    submit,
    system,
)
from agent_alfred.graph import (
    GraphBuilder,
    GraphRegistry,
    NodeOutcome,
    TerminalSpec,
    agent_node,
    fn_node,
)
from agent_alfred.messages import message_plain_text
from agent_alfred.settings import Settings


def two_assistants(*, client, model, assistant, tools):
    b = GraphBuilder("chat", tools=tools).declare_input("task")
    b.add_node(
        "draft",
        agent_node(
            lambda s: s["task"],
            client=client,
            model=model,
            assistant=assistant,
            output_key="draft",
        ),
        required_reads=("task",),
        writes=("draft",),
    )
    b.add_node(
        "final",
        agent_node(
            lambda s: "Improve: " + s["draft"],
            client=client,
            model=model,
            assistant=assistant,
            output_key="final",
        ),
        required_reads=("draft",),
        writes=("final",),
        terminal=TerminalSpec.result("final"),
    )
    b.add_edge("draft", "final")
    return GraphRegistry({"chat": b}).get("chat")


def test_ce25_host_graph_assistants_share_skill_budget_and_only_final_reply_is_saved(
    tmp_path,
):
    skill(tmp_path / "builtin", "A", "unique whole procedure")
    with runtime(
        tmp_path,
        ['{"skills":["A"]}', SKIP, "draft text", "final text"],
        chat_graph_factory=two_assistants,
    ) as (host, model, _):
        accepted, result = submit(host, "hello")
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "final text"
        assert result.step_count == 4 and len(model.requests) == 4
        assert all("unique whole procedure" in system(r) for r in model.requests[-2:])
        assert "Improve: draft text" == message_plain_text(
            model.requests[-1].messages[-1]
        )
        assert [a["purpose"] for a in result.memory_telemetry["input_attempts"]] == [
            "skill_selector",
            "gate",
            "answer",
            "answer",
        ]
        assert [s["node_id"] for s in result.memory_telemetry["graph"]["steps"]] == [
            "draft",
            "final",
        ]
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            rows = conn.execute(
                "SELECT role,content FROM agent_log WHERE run_id=?", (accepted.run_id,)
            ).fetchall()
        assert len(rows) == 2
        assert json.loads(dict(rows)["assistant"])[0]["text"] == "final text"


@pytest.mark.parametrize("max_steps", [3, 4])
def test_ce26_legal_fallback_keeps_selection_and_spent_graph_steps(tmp_path, max_steps):
    skill(tmp_path / "builtin", "A", "stable body")

    def failing_graph(**bindings):
        b = GraphBuilder("failed").declare_input("task")
        b.add_node(
            "draft",
            agent_node(
                "draft",
                output_key="draft",
                **{k: v for k, v in bindings.items() if k != "tools"},
            ),
            writes=("draft",),
        )
        b.add_node(
            "invalid",
            fn_node(lambda s, c: NodeOutcome({"illegal": True})),
            terminal=TerminalSpec.no_action("none"),
        )
        return GraphRegistry({"failed": b}).get("failed")

    script = ['{"skills":["A"]}', SKIP, "withdrawn", "fallback"]
    with runtime(
        tmp_path,
        script,
        settings=Settings(max_steps=max_steps),
        chat_graph_factory=failing_graph,
    ) as (host, model, _):
        _, result = submit(host, "hello")
        assert result.outcome == ("completed" if max_steps == 4 else "max_steps")
        assert result.step_count == max_steps and len(model.requests) == max_steps
        assert result.memory_telemetry["graph"]["fallback"] is True
        assert all("stable body" in system(r) for r in model.requests[2:])
        assert (
            len(
                [
                    a
                    for a in result.memory_telemetry["input_attempts"]
                    if a["purpose"] == "skill_selector"
                ]
            )
            == 1
        )
        if max_steps == 4:
            assert message_plain_text(result.reply) == "fallback"
            assert "withdrawn" not in " ".join(
                message_plain_text(m) for m in model.requests[-1].messages
            )


@pytest.mark.parametrize("effect", ["occurred", "unknown"])
def test_ce26_side_effect_graph_failure_never_repeats_tools_or_falls_back(
    tmp_path, effect
):
    from agent_alfred.graph import tool_node
    from agent_alfred.messages import TextBlock
    from agent_alfred.tools import Tool, ToolFailure, ToolPolicy, ToolSuccess
    from agent_alfred.tools.calendar import object_schema

    seen = []

    def external(args, context):
        seen.append(context.run_id)
        if effect == "unknown":
            return ToolFailure(
                "execution_error",
                (TextBlock("unknown"),),
                stop_reason="tool_result_unverified",
            )
        return ToolSuccess((TextBlock("done"),))

    tool = Tool(
        "external_fixture", "external test", object_schema({}, ()), external, "external"
    )

    def graph(**bindings):
        b = GraphBuilder("effects", tools=bindings["tools"]).declare_input("task")
        b.add_node(
            "action",
            tool_node("external_fixture", {}, output_key="action"),
            writes=("action",),
        )
        b.add_node(
            "fail",
            fn_node(lambda s, c: NodeOutcome({"illegal": True})),
            terminal=TerminalSpec.no_action("done"),
        )
        b.add_edge("action", "fail")
        return GraphRegistry({"effects": b}).get("effects")

    skill(tmp_path / "builtin", "A", "body")
    with runtime(
        tmp_path,
        ['{"skills":["A"]}', SKIP],
        extra_tools=(tool,),
        tool_policies={tool.name: ToolPolicy(configured=True, authorization="allowed")},
        chat_graph_factory=graph,
    ) as (host, model, _):
        identity = next(
            t["identity"]
            for t in host.tools_catalog()["tools"]
            if t["name"] == tool.name
        )
        assert "error" not in host.save_tool_authorization(identity, "allowed", 0)
        accepted, result = submit(host, "hello")
        assert result.outcome == "failed"
        assert seen == [accepted.run_id], (result.error, result.memory_telemetry)
        assert len(model.requests) == 2 and result.step_count == 2
        assert result.memory_telemetry["graph"]["fallback"] is False


def test_ce26_graph_total_deadline_has_no_recovery_node_or_fallback(tmp_path):
    from agent_alfred.clock import FakeClock

    clock = FakeClock()
    entered = []

    def graph(**bindings):
        def expire(state, context):
            clock.monotonic_value = 5
            raise ValueError("node failed at the deadline")

        b = GraphBuilder("deadline").declare_input("task")
        b.add_node("expire", fn_node(expire))
        b.add_node(
            "recover",
            fn_node(lambda s, c: entered.append(True) or NodeOutcome()),
            terminal=TerminalSpec.no_action("done"),
        )
        b.add_error_edge("expire", "recover")
        return GraphRegistry({"deadline": b}).get("deadline")

    skill(tmp_path / "builtin", "A", "body")
    with runtime(
        tmp_path,
        ['{"skills":["A"]}', SKIP],
        clock=clock,
        settings=Settings(overall_deadline_s=5),
        chat_graph_factory=graph,
    ) as (host, model, _):
        _, result = submit(host, "hello")
        assert result.outcome == "failed" and result.error == "overall_deadline"
        assert entered == [] and len(model.requests) == 2


def test_ce26_ce27_no_action_and_classification_never_fabricate_a_skill_answer(
    tmp_path,
):
    from agent_alfred.graph import llm_node

    def graph(**bindings):
        bindings.pop("tools")
        b = GraphBuilder("classify").declare_input("task")
        b.add_node(
            "classify",
            llm_node("classify only", output_key="classification", **bindings),
            writes=("classification",),
        )
        b.add_node(
            "none",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("nothing_to_do"),
        )
        b.add_edge("classify", "none")
        return GraphRegistry({"classify": b}).get("classify")

    skill(tmp_path / "builtin", "A", "CLASSIFIER_MUST_NOT_GET_BODY")
    with runtime(
        tmp_path, ['{"skills":["A"]}', SKIP, "classification"], chat_graph_factory=graph
    ) as (host, model, _):
        accepted, result = submit(host, "hello")
        assert result.outcome == "completed" and result.reply is None
        assert "CLASSIFIER_MUST_NOT_GET_BODY" not in system(model.requests[-1])
        assert result.memory_telemetry["input_attempts"][-1]["skills"] == []
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert conn.execute(
                "SELECT role FROM agent_log WHERE run_id=?", (accepted.run_id,)
            ).fetchall() == [("user",)]
