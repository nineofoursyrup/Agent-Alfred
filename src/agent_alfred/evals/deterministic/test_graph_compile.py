"""Independent invalid definitions fail through compile, before any IO."""

import pytest

from agent_alfred.graph import (
    GraphBuilder,
    GraphCompileError,
    NodeOutcome,
    TerminalSpec,
    fn_node,
)


def empty():
    return fn_node(lambda s, c: NodeOutcome())


def valid():
    b = GraphBuilder("compile")
    b.add_node("done", empty(), terminal=TerminalSpec.no_action("nothing"))
    return b


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "no_terminal",
        "bad_id",
        "reserved",
        "dunder",
        "duplicate",
        "input_writer",
        "two_writers",
        "required_missing",
        "optional_missing",
        "bypass",
        "conditional_twice",
        "empty_map",
        "missing_target",
        "shared_error",
        "mixed_error",
        "skip_error",
        "two_errors",
        "error_chain",
        "bad_output_owner",
        "bad_reason",
        "terminal_edge",
        "terminal_conditional",
        "cycle",
        "optional_input_required",
        "self_read",
        "duplicate_input",
    ],
)
def test_ce04_ce11_ce12_invalid_graphs(case):
    b = valid()
    if case == "empty":
        b = GraphBuilder("empty")
    elif case == "no_terminal":
        b = GraphBuilder("no_terminal")
        b.add_node("a", empty())
    elif case in ("bad_id", "reserved", "dunder"):
        b.add_node(
            {"bad_id": "Bad-name", "reserved": "start", "dunder": "__x"}[case], empty()
        )
    elif case == "duplicate":
        b.add_node("done", empty())
    elif case == "input_writer":
        b.declare_input("x")
        b.add_node("a", empty(), writes=("x",))
    elif case == "two_writers":
        b.add_node("a", empty(), writes=("x",))
        b.add_node("b", empty(), writes=("x",))
    elif case in ("required_missing", "optional_missing"):
        b.add_node("a", empty(), **{case.replace("_missing", "_reads"): ("x",)})
    elif case == "bypass":
        b.add_node("a", empty(), writes=("x",), skippable_by_config=True)
        b.add_node("b", empty())
        b.add_node("c", empty(), required_reads=("x",))
        b.add_edge("a", "c")
        b.add_edge("b", "c")
    elif case == "conditional_twice":
        b.add_node("a", empty())
        b.add_conditional_edges("a", lambda s: "x", {"x": "done"})
        b.add_conditional_edges("a", lambda s: "y", {"y": "done"})
    elif case == "empty_map":
        b.add_node("a", empty())
        b.add_conditional_edges("a", lambda s: "x", {})
    elif case == "missing_target":
        b.add_edge("a", "done")
    elif case in (
        "shared_error",
        "mixed_error",
        "skip_error",
        "two_errors",
        "error_chain",
    ):
        b.add_node("a", empty())
        b.add_node("b", empty())
        b.add_node("recovery", empty(), skippable_by_config=case == "skip_error")
        b.add_error_edge("a", "recovery")
        if case == "shared_error":
            b.add_error_edge("b", "recovery")
        if case == "mixed_error":
            b.add_edge("b", "recovery")
        if case == "two_errors":
            b.add_error_edge("a", "b")
        if case == "error_chain":
            b.add_error_edge("recovery", "b")
    elif case == "bad_output_owner":
        b.add_node("a", empty(), terminal=TerminalSpec.result("missing"))
    elif case == "bad_reason":
        b.add_node("a", empty(), terminal=TerminalSpec.no_action("Not a code!"))
    elif case == "terminal_edge":
        b.add_node("a", empty())
        b.add_edge("done", "a")
    elif case == "terminal_conditional":
        b.add_node("a", empty())
        b.add_conditional_edges("done", lambda s: "x", {"x": "a"})
    elif case == "cycle":
        b.add_node("a", empty())
        b.add_node("b", empty())
        b.add_edge("a", "b")
        b.add_edge("b", "a")
    elif case == "optional_input_required":
        b.declare_input("x", required=False)
        b.add_node("a", empty(), required_reads=("x",))
    elif case == "self_read":
        b.add_node("a", empty(), writes=("x",), required_reads=("x",))
    elif case == "duplicate_input":
        b.declare_input("x")
        b.declare_input("x")
    with pytest.raises(GraphCompileError):
        b.compile()


def test_ce04_skippable_writer_dominates_taken_path():
    b = GraphBuilder("dominated")
    b.add_node("writer", empty(), writes=("x",), skippable_by_config=True)
    b.add_node(
        "finish",
        empty(),
        required_reads=("x",),
        terminal=TerminalSpec.no_action("done"),
    )
    b.add_edge("writer", "finish")
    b.compile()


def test_ce14_registry_freeze_and_unknown_name():
    from agent_alfred.graph import GraphRegistry

    b = valid()
    b.presentation = {"label": "first"}
    registry = GraphRegistry({"toy": b})
    g = registry.get("toy")
    description = g.describe()
    b.presentation["label"] = "changed"
    b.nodes.clear()
    assert g.describe()["presentation"]["label"] == "first"
    assert g.invoke({}).reason_code == "nothing"
    with pytest.raises(KeyError):
        registry.get("absent")
    assert g.describe() == description


def test_ce09_hash_excludes_presentation_and_preserves_node_order():
    one = valid()
    one.presentation = {"label": "one"}
    two = valid()
    two.presentation = {"label": "two"}
    assert (
        one.compile().describe()["topology_hash"]
        == two.compile().describe()["topology_hash"]
    )
    one.add_node("extra", empty())
    two.nodes.insert(0, one.nodes[-1])
    assert (
        one.compile().describe()["topology_hash"]
        != two.compile().describe()["topology_hash"]
    )


def test_ce09_description_is_json_serializable_and_cannot_mutate_execution():
    import json

    graph = valid().compile()
    description = graph.describe()
    assert json.loads(json.dumps(description))["schema_version"] == 1
    description["topology"]["nodes"].clear()
    assert graph.describe()["topology"]["nodes"]


@pytest.mark.parametrize("names", [("absent",), ("query_events", "query_events")])
def test_ce04_invalid_tool_whitelists(names):
    from agent_alfred.evals.deterministic.test_graph_nodes import registry_fixture
    from agent_alfred.graph import agent_node
    from agent_alfred.loop.assistant import Assistant
    from agent_alfred.model import ModelRef, ScriptedModel
    from agent_alfred.settings import Settings

    conn, store, tools, clock = registry_fixture()
    try:
        b = GraphBuilder("whitelist", tools=tools)
        b.add_node(
            "agent",
            agent_node(
                "x",
                client=ScriptedModel([]),
                model=ModelRef("offline", "x"),
                assistant=Assistant(clock=clock, settings=Settings()),
                output_key="reply",
                tool_names=names,
            ),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        with pytest.raises(GraphCompileError):
            b.compile()
    finally:
        conn.close()
