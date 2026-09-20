import json

import pytest

from agent_alfred.evals.deterministic.test_graph_nodes import registry_fixture
from agent_alfred.graph import (
    Completed,
    Failed,
    GraphBuilder,
    GraphRunContext,
    NodeOutcome,
    TerminalSpec,
    fn_node,
    settle_graph,
    tool_node,
)
from agent_alfred.loop.budget import RunBudget


def test_zero_step_create_skill_builtin_override(tmp_path):
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.tools import ToolRegistry
    from agent_alfred.tools.files import FileTools
    from agent_alfred.tools.metering import ToolMetering
    from agent_alfred.tools.skills import SkillTools

    conn, store, _, clock = registry_fixture()
    lease = ManagedStateDirectory.acquire(tmp_path / "state")
    builtin = tmp_path / "builtin"
    (builtin / "existing").mkdir(parents=True)
    (builtin / "existing" / "SKILL.md").write_text(
        "---\nname: existing\ndescription: existing skill\n---\nExisting body\n"
    )
    files = FileTools(store, lease, clock)
    skills = SkillTools(files, builtin=builtin)
    registry = ToolRegistry(
        skills.declarations(), clock=clock, metering=ToolMetering(store, clock)
    )
    graph = GraphBuilder("skill_override", tools=registry)
    graph.add_node(
        "create",
        tool_node(
            "create_skill",
            {"name": "existing", "description": "new description", "body": "new body"},
            output_key="reply",
        ),
        writes=("reply",),
        terminal=TerminalSpec.result("reply"),
    )
    ctx = GraphRunContext(
        run_id="skill_override", budget=RunBudget(0), clock=clock, tools=registry
    )
    try:
        result = graph.compile().invoke({}, context=ctx)
        assert isinstance(result, Completed)
        assert conn.execute("SELECT step_index FROM skill_candidates").fetchall() == [
            (-1,)
        ]
        assert ctx.budget.used == 0
    finally:
        files.close()
        lease.close()
        conn.close()


@pytest.mark.parametrize("writes", [[], None, 3, "reply", b"reply", {"reply"}])
def test_bad_writes_shape_returns_closed_failure(writes):
    from agent_alfred.clock import FakeClock
    from agent_alfred.events import CapturingSink, FanOutSink

    seen = []
    b = GraphBuilder("badshape")
    b.add_node(
        "bad",
        fn_node(lambda s, c: NodeOutcome(writes)),
        writes=("reply",),
        terminal=TerminalSpec.result("reply"),
    )
    b.add_node(
        "sibling",
        fn_node(lambda s, c: seen.append("sibling") or NodeOutcome({"sibling": 1})),
        writes=("sibling",),
    )
    b.add_node(
        "recover",
        fn_node(lambda s, c: seen.append("recover") or NodeOutcome()),
        terminal=TerminalSpec.no_action("done"),
    )
    b.add_error_edge("bad", "recover")
    sink = CapturingSink()
    ctx = GraphRunContext(
        run_id="badshape",
        budget=RunBudget(0),
        clock=FakeClock(),
        events=FanOutSink([sink], process_instance_id="p"),
    )
    result = b.compile().invoke({}, context=ctx)
    assert isinstance(result, Failed)
    assert result.error.code == "invalid_writes"
    assert dict(result.committed_state) == {}
    assert seen == ["sibling"]
    assert [e.payload.name for e in sink.events] == [
        "path.captured",
        "graph.started",
        "node.started",
        "node.started",
        "node.aborted",
        "node.aborted",
        "path.wave",
        "graph.finished",
    ]


def test_frozen_set_output_reaches_real_recorder(tmp_path):
    from agent_alfred.evals.deterministic.test_graph_recording import setup_run

    host, conn, item, ctx, recorder, _ = setup_run(tmp_path)
    try:
        b = GraphBuilder("set_output")
        b.add_node(
            "reply",
            fn_node(
                lambda s, c: NodeOutcome(
                    {
                        "reply": {
                            "letters": {"a", "b"},
                            "bytes": b"\x00\xff",
                            "keys": {(1, 2): "value"},
                        }
                    }
                )
            ),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        result = b.compile().invoke({}, context=ctx)
        assert isinstance(result, Completed)
        settle_graph(recorder, item, result, ctx)
        from agent_alfred.graph.types import thaw

        assert json.loads(json.dumps(thaw(result.output))) == {
            "letters": ["a", "b"],
            "bytes": {"$bytes_hex": "00ff"},
            "keys": {"$map": [[[1, 2], "value"]]},
        }
        assert (
            conn.execute(
                "SELECT phase FROM runs WHERE run_id=?", (item.run_id,)
            ).fetchone()[0]
            == "finished"
        )
    finally:
        host.close()


@pytest.mark.parametrize("prior_write", [False, True])
def test_http_not_sent_does_not_count_as_occurred(prior_write):
    from agent_alfred.integrations import Integrations
    from agent_alfred.redact import Redactor
    from agent_alfred.tools import ToolPolicy, ToolRegistry
    from agent_alfred.tools.calendar import CalendarTools
    from agent_alfred.tools.ledger import ExternalToolLedger
    from agent_alfred.tools.metering import ToolMetering

    conn, store, _, clock = registry_fixture()
    integration = Integrations(
        {"TAVILY_API_KEY": "synthetic-test-key"}, clock, Redactor(())
    )

    class ForbiddenNetwork:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("HTTP must not be called")

    network = ForbiddenNetwork()
    integration.transport = network
    registry = ToolRegistry(
        (*integration.declarations(), *CalendarTools(store, clock).declarations()),
        clock=clock,
        policies={"web_search": ToolPolicy(configured=True, authorization="allowed")},
        metering=ToolMetering(store, clock),
        external_ledger=ExternalToolLedger(store, clock),
    )
    try:
        b = GraphBuilder("not_sent", tools=registry)
        if prior_write:
            b.add_node(
                "create",
                tool_node(
                    "create_event",
                    {"title": "retained", "starts_at": "2026-09-13T00:00:00Z"},
                    output_key="created",
                ),
                writes=("created",),
            )
            b.add_edge("create", "search")
        b.add_node(
            "search",
            tool_node("web_search", {"query": " "}, output_key="reply"),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        ctx = GraphRunContext(
            run_id="not_sent", budget=RunBudget(0), clock=clock, tools=registry
        )
        result = b.compile().invoke({}, context=ctx)
        assert isinstance(result, Failed)
        assert network.calls == 0
        assert (
            next(
                r
                for r in registry.read_metering(ctx.run_id)
                if r["call_id"] == "graph:search"
            )["cost"]["reason"]
            == "http_not_sent"
        )
        assert result.side_effect_state == ("occurred" if prior_write else "none")
    finally:
        conn.close()
