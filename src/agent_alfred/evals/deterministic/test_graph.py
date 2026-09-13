"""GRAPH-SPEC-r1 public scheduling contract, using real callable nodes."""

import pytest

from agent_alfred.graph import (
    Completed,
    GraphBuilder,
    NodeOutcome,
    TerminalSpec,
    fn_node,
)


def test_ce05_declared_snapshot_and_explicit_none():
    captured = []
    builder = GraphBuilder("toy")
    builder.declare_input("input")
    builder.add_node(
        "answer",
        fn_node(
            lambda state, ctx: (
                captured.append(dict(state)) or NodeOutcome({"reply": state["input"]})
            )
        ),
        required_reads=("input",),
        writes=("reply",),
        terminal=TerminalSpec.result("reply"),
    )
    graph = builder.compile()
    result = graph.invoke({"input": None})
    assert isinstance(result, Completed)
    assert result.output is None
    assert captured == [{"input": None}]


def test_ce01_handled_failure_commits_sibling_and_recovers_next_wave():
    from agent_alfred.graph import CompletedWithRecovery

    seen = []

    def fail(state, ctx):
        raise ValueError("controlled")

    b = GraphBuilder("recovery")
    b.add_node("a", fn_node(lambda s, c: NodeOutcome({"a": 7})), writes=("a",))
    b.add_node("b", fn_node(fail))
    b.add_node(
        "recover",
        fn_node(
            lambda s, c: (
                seen.append((dict(s), c.error.node_id))
                or NodeOutcome({"reply": s["a"]})
            )
        ),
        required_reads=("a",),
        writes=("reply",),
        terminal=TerminalSpec.result("reply"),
    )
    b.add_error_edge("b", "recover")
    result = b.compile().invoke({})
    assert isinstance(result, CompletedWithRecovery)
    assert result.output == 7
    assert seen == [({"a": 7}, "b")]


def _events():
    from agent_alfred.clock import FakeClock
    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.graph import GraphRunContext
    from agent_alfred.loop.budget import RunBudget

    sink = CapturingSink()
    ids = iter(str(i) for i in range(1000))
    events = FanOutSink(
        [sink], process_instance_id="p", event_id_factory=lambda: next(ids)
    )
    return GraphRunContext(
        run_id="r", budget=RunBudget(4), clock=FakeClock(), events=events
    ), sink


@pytest.mark.parametrize("mode", ["unknown", "exception", "missing"])
def test_ce10_routing_failure_aborts_entire_wave_without_recovery(mode):
    from agent_alfred.graph import Failed

    seen = []
    b = GraphBuilder("routing")
    b.add_node("a", fn_node(lambda s, c: NodeOutcome({"a": 1})), writes=("a",))
    b.add_node(
        "b",
        fn_node(
            lambda s, c: NodeOutcome({} if mode == "missing" else {"category": "bad"})
        ),
        writes=("category",),
    )
    b.add_node("recover", fn_node(lambda s, c: seen.append("recover") or NodeOutcome()))
    b.add_node(
        "done",
        fn_node(lambda s, c: NodeOutcome()),
        terminal=TerminalSpec.no_action("done"),
    )
    b.add_error_edge("b", "recover")
    route_calls = []

    def route(state):
        route_calls.append(state["category"])
        if mode == "exception":
            raise ValueError("controlled route failure")
        return state["category"]

    b.add_conditional_edges("b", route, {"ok": "done"}, required_reads=("category",))
    context, sink = _events()
    result = b.compile().invoke({}, context=context)
    assert isinstance(result, Failed) and result.error.code == "routing_failed"
    assert dict(result.committed_state) == {} and not seen
    assert route_calls == ([] if mode == "missing" else ["bad"])
    assert [e.payload.name for e in sink.events] == [
        "graph.started",
        "node.started",
        "node.started",
        "node.aborted",
        "node.aborted",
        "graph.finished",
    ]
    assert [e.envelope.node_id for e in sink.events[3:5]] == ["a", "b"]


def test_ce02_no_action_has_recovery_without_output_or_sink():
    from agent_alfred.graph import NoAction

    def fail(s, c):
        raise ValueError("failure")

    b = GraphBuilder("noaction")
    b.add_node("b", fn_node(fail))
    b.add_node(
        "r",
        fn_node(lambda s, c: NodeOutcome()),
        terminal=TerminalSpec.no_action("skipped"),
    )
    b.add_error_edge("b", "r")
    result = b.compile().invoke({})
    assert isinstance(result, NoAction) and result.reason_code == "skipped"
    assert len(result.recoveries) == 1 and not hasattr(result, "output")


def test_ce05_nested_snapshot_cannot_change_input():
    data = {"value": {"items": [1]}}
    seen = []

    def a(s, c):
        try:
            s["value"]["items"].append(2)
        except AttributeError, TypeError:
            pass
        return NodeOutcome({"unused": 2})

    b = GraphBuilder("snapshot").declare_input("value")
    b.add_node("a", fn_node(a), required_reads=("value",), writes=("unused",))
    b.add_node(
        "b",
        fn_node(lambda s, c: seen.append(dict(s)) or NodeOutcome()),
        required_reads=("value",),
        terminal=TerminalSpec.no_action("done"),
    )
    b.compile().invoke(data)
    assert data == {"value": {"items": [1]}}
    assert seen[0]["value"]["items"] == (1,) and "unused" not in seen[0]


def test_ce12_missing_output_recovers_but_explicit_none_completes():
    from agent_alfred.graph import NoAction

    b = GraphBuilder("missing")
    b.add_node(
        "t",
        fn_node(lambda s, c: NodeOutcome()),
        writes=("out",),
        terminal=TerminalSpec.result("out"),
    )
    b.add_node(
        "r",
        fn_node(lambda s, c: NodeOutcome()),
        terminal=TerminalSpec.no_action("no_result"),
    )
    b.add_error_edge("t", "r")
    result = b.compile().invoke({})
    assert isinstance(result, NoAction) and result.recoveries


def test_ce13_unhandled_failure_does_not_start_later_sibling():
    from agent_alfred.graph import Failed

    seen = []

    def fail(s, c):
        raise RuntimeError("controlled")

    b = GraphBuilder("shortcircuit")
    b.add_node("a", fn_node(lambda s, c: NodeOutcome({"a": 1})), writes=("a",))
    b.add_node("b", fn_node(fail))
    b.add_node(
        "c",
        fn_node(lambda s, c: seen.append("c") or NodeOutcome()),
        terminal=TerminalSpec.no_action("done"),
    )
    ctx, sink = _events()
    result = b.compile().invoke({}, context=ctx)
    assert isinstance(result, Failed) and not seen and result.not_executed == ("c",)
    assert all(e.envelope.node_id != "c" for e in sink.events)
    assert not any(e.payload.name == "node.finished" for e in sink.events)


def test_ce03_source_candidate_routes_without_sibling_visibility():
    import pytest

    seen = []

    def route(s):
        seen.append(dict(s))
        with pytest.raises(KeyError):
            _ = s["other"]
        return s["category"]

    b = GraphBuilder("route")
    b.add_node(
        "classify",
        fn_node(lambda s, c: NodeOutcome({"category": "yes"})),
        writes=("category",),
    )
    b.add_node(
        "sibling", fn_node(lambda s, c: NodeOutcome({"other": 2})), writes=("other",)
    )
    b.add_node(
        "chosen",
        fn_node(lambda s, c: NodeOutcome()),
        terminal=TerminalSpec.no_action("selected"),
    )
    b.add_conditional_edges(
        "classify", route, {"yes": "chosen"}, required_reads=("category",)
    )
    assert b.compile().invoke({}).reason_code == "selected"
    assert seen == [{"category": "yes"}]


def test_ce05_missing_writer_value_fails_before_consumer_call():
    from agent_alfred.graph import Failed

    seen = []
    b = GraphBuilder("missing")
    b.add_node("writer", fn_node(lambda s, c: NodeOutcome()), writes=("value",))
    b.add_node(
        "reader",
        fn_node(lambda s, c: seen.append("called") or NodeOutcome()),
        required_reads=("value",),
        terminal=TerminalSpec.no_action("done"),
    )
    b.add_edge("writer", "reader")
    assert isinstance(b.compile().invoke({}), Failed) and seen == []


def test_ce09_deterministic_events_and_conditional_fanin():
    def build():
        b = GraphBuilder("fan").declare_input("route")
        b.add_node("choose", fn_node(lambda s, c: NodeOutcome()))
        for n in ("left", "right"):
            b.add_node(n, fn_node(lambda s, c: NodeOutcome()))
        b.add_node(
            "join",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("joined"),
        )
        b.add_conditional_edges(
            "choose",
            lambda s: s["route"],
            {"left": "left", "right": "right"},
            required_reads=("route",),
        )
        b.add_edge("left", "join")
        b.add_edge("right", "join")
        return b.compile()

    records = []
    for _ in range(2):
        ctx, sink = _events()
        assert build().invoke({"route": "left"}, context=ctx).reason_code == "joined"
        records.append(sink.events)
    assert records[0] == records[1]
    skipped = [e for e in records[0] if e.payload.name == "node.skipped"]
    assert [(e.envelope.node_id, e.payload.reason) for e in skipped] == [
        ("right", "all_inbound_not_taken")
    ]


def test_ce11_two_dedicated_recoveries_merge_without_competing_error_context():
    from agent_alfred.graph import CompletedWithRecovery

    seen = []

    def fail(s, c):
        raise ValueError("controlled")

    b = GraphBuilder("errors")
    for source in ("a", "b"):
        b.add_node(source, fn_node(fail))
        b.add_node(
            "r_" + source,
            fn_node(lambda s, c: seen.append(c.error.node_id) or NodeOutcome()),
        )
        b.add_error_edge(source, "r_" + source)
    b.add_node(
        "join",
        fn_node(lambda s, c: NodeOutcome({"out": "recovered"})),
        writes=("out",),
        terminal=TerminalSpec.result("out"),
    )
    b.add_edge("r_a", "join")
    b.add_edge("r_b", "join")
    result = b.compile().invoke({})
    assert isinstance(result, CompletedWithRecovery) and seen == ["a", "b"]
    assert [e.node_id for e in result.recoveries] == ["a", "b"]


def test_ce12_zero_and_multiple_successful_terminals_fail():
    from agent_alfred.graph import Failed

    for disabled in ((), ("a", "b")):
        b = GraphBuilder("terminals")
        for n in ("a", "b"):
            b.add_node(
                n,
                fn_node(lambda s, c: NodeOutcome()),
                skippable_by_config=True,
                terminal=TerminalSpec.no_action(n),
            )
        result = b.compile().invoke({}, disabled=disabled)
        assert isinstance(result, Failed) and result.error.code == "terminal_count"


def test_ce14_inputs_rejected_before_any_node_and_invocations_are_isolated():
    from agent_alfred.graph import Failed

    seen = []
    b = (
        GraphBuilder("input")
        .declare_input("value")
        .declare_input("maybe", required=False)
    )
    b.add_node(
        "answer",
        fn_node(lambda s, c: seen.append(dict(s)) or NodeOutcome({"out": s["value"]})),
        required_reads=("value",),
        optional_reads=("maybe",),
        writes=("out",),
        terminal=TerminalSpec.result("out"),
    )
    graph = b.compile()
    for data in ({}, {"value": 1, "out": "override"}):
        ctx, sink = _events()
        result = graph.invoke(data, context=ctx)
        assert isinstance(result, Failed) and not any(
            e.payload.name == "node.started" for e in sink.events
        )
    assert seen == []
    assert graph.invoke({"value": 1, "maybe": None}).output == 1
    assert graph.invoke({"value": 2}).output == 2
    assert seen == [{"value": 1, "maybe": None}, {"value": 2}]
