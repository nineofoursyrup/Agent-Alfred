"""Graph factories run the real Assistant, budget and tool accounting."""

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.graph import (
    GraphBuilder,
    GraphRunContext,
    NodeOutcome,
    TerminalSpec,
    fn_node,
)
from agent_alfred.loop.assistant import Assistant
from agent_alfred.loop.budget import RunBudget
from agent_alfred.model import ModelRef, ScriptedModel
from agent_alfred.settings import Settings


@pytest.mark.parametrize("invalid_writes", [{"illegal": 1}, []])
def test_ce06_ce15_real_model_steps_remain_consumed_after_illegal_wave(invalid_writes):
    from agent_alfred.graph import Failed, llm_node

    clock = FakeClock()
    model = ScriptedModel(["first", "second", "must not run"])
    assistant = Assistant(clock=clock, settings=Settings())
    ref = ModelRef("offline", "scripted")
    budget = RunBudget(2)
    ctx = GraphRunContext(run_id="run", budget=budget, clock=clock)
    b = GraphBuilder("accounting")
    for name in ("a", "b"):
        b.add_node(
            name,
            llm_node(
                "classify",
                client=model,
                model=ref,
                assistant=assistant,
                output_key=name,
            ),
            writes=(name,),
        )
    b.add_node(
        "c",
        fn_node(lambda s, c: NodeOutcome(invalid_writes)),
        terminal=TerminalSpec.no_action("done"),
    )
    result = b.compile().invoke({}, context=ctx)
    assert isinstance(result, Failed) and result.error.code == "invalid_writes"
    assert budget.used == 2 and len(model.requests) == 2
    assert ctx.transcript == [] and len(ctx.model_results) == 2
    assert [s["outcome"] for s in ctx.steps] == ["aborted_by_wave", "aborted_by_wave"]
    fallback = assistant.respond(
        "fallback",
        client=model,
        budget=budget,
        working_memory=ctx.transcript,
        model=ref,
        run_id="run",
        session_id=None,
    )
    assert fallback.outcome == "max_steps" and len(model.requests) == 2


def registry_fixture():
    import sqlite3
    import threading

    from agent_alfred import schema
    from agent_alfred.runtime.recording import RecordingStore
    from agent_alfred.tools import ToolRegistry
    from agent_alfred.tools.calendar import CalendarTools
    from agent_alfred.tools.ledger import ExternalToolLedger
    from agent_alfred.tools.metering import ToolMetering

    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    store = RecordingStore(conn, threading.Lock())
    clock = FakeClock()
    calendar = CalendarTools(store, clock)
    registry = ToolRegistry(
        calendar.declarations(),
        clock=clock,
        external_ledger=ExternalToolLedger(store, clock),
        metering=ToolMetering(store, clock),
    )
    return conn, store, registry, clock


def test_ce07_empty_whitelist_blocks_forced_calls():
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.graph import agent_node
    from agent_alfred.messages import ToolCallBlock

    conn, store, registry, clock = registry_fixture()
    try:
        model = ScriptedModel(
            [
                calls(
                    ToolCallBlock(
                        "forced",
                        "create_event",
                        {"title": "forbidden", "starts_at": "2026-09-13T00:00:00Z"},
                    )
                ),
                "safe",
            ]
        )
        ctx = GraphRunContext(
            run_id="restricted", budget=RunBudget(3), tools=registry, clock=clock
        )
        b = GraphBuilder("restricted", tools=registry)
        b.add_node(
            "agent",
            agent_node(
                "work",
                client=model,
                model=ModelRef("offline", "test"),
                assistant=Assistant(clock=clock, settings=Settings()),
                output_key="reply",
                tool_names=(),
            ),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        assert b.compile().invoke({}, context=ctx).output == "safe"
        assert all(not r.tools for r in model.requests)
        assert conn.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0] == 0
        assert registry.read_metering("restricted")[0]["reason"] == "unknown_tool"
    finally:
        conn.close()


def test_ce15_zero_step_tools_have_distinct_real_ledger_identities():
    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.graph import tool_node

    conn, store, registry, clock = registry_fixture()
    try:
        sink = CapturingSink()
        ctx = GraphRunContext(
            run_id="zero",
            budget=RunBudget(0),
            tools=registry,
            clock=clock,
            events=FanOutSink([sink], process_instance_id="p"),
        )
        b = GraphBuilder("zero", tools=registry)
        for name in ("first", "second"):
            b.add_node(
                name,
                tool_node(
                    "create_event",
                    {"title": name, "starts_at": "2026-09-13T00:00:00Z"},
                    output_key=name,
                ),
                writes=(name,),
            )
        b.add_node(
            "done",
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action("created"),
        )
        assert b.compile().invoke({}, context=ctx).reason_code == "created"
        rows = registry.read_metering("zero")
        assert len(rows) == 2 and len({r["call_id"] for r in rows}) == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tool_ledger WHERE run_id='zero'"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0] == 2
        assert ctx.budget.used == 0
        events = [e for e in sink.events if e.payload.name.startswith("tool.")]
        assert {e.envelope.node_id for e in events} == {"first", "second"}
        assert all(e.envelope.step_index is None for e in events)
    finally:
        conn.close()


def test_ce15_second_graph_cannot_reuse_same_run_budget():
    from agent_alfred.graph import Failed

    ctx = GraphRunContext(run_id="once", budget=RunBudget(0))
    b = GraphBuilder("one")
    b.add_node(
        "done",
        fn_node(lambda s, c: NodeOutcome()),
        terminal=TerminalSpec.no_action("nothing"),
    )
    graph = b.compile()
    assert graph.invoke({}, context=ctx).reason_code == "nothing"
    result = graph.invoke({}, context=ctx)
    assert isinstance(result, Failed) and result.error.code == "graph_already_invoked"


def test_ce06_interrupted_attempt_cost_survives_graph_failure():
    import pytest

    from agent_alfred.graph import llm_node
    from agent_alfred.model import ModelCallInterrupted, ModelResult

    model = ScriptedModel(["paid"])
    ref = ModelRef("offline", "x")
    from agent_alfred.model import ModelRequest

    paid = model.respond(ModelRequest(model=ref, system=(), messages=()))

    class InterruptedNetwork:
        def respond(self, *args, **kwargs):
            raise ModelCallInterrupted(KeyboardInterrupt(), paid)

    ctx = GraphRunContext(run_id="interrupted", budget=RunBudget(1), clock=FakeClock())
    b = GraphBuilder("interruption")
    b.add_node(
        "model",
        llm_node(
            "x",
            client=InterruptedNetwork(),
            model=ref,
            assistant=Assistant(clock=ctx.clock, settings=Settings()),
            output_key="reply",
        ),
        writes=("reply",),
        terminal=TerminalSpec.result("reply"),
    )
    with pytest.raises(KeyboardInterrupt):
        b.compile().invoke({}, context=ctx)
    assert ctx.model_results and isinstance(ctx.model_results[0], ModelResult)
    assert ctx.budget.used == 1 and ctx.transcript == []


def test_ce15_budget_exhaustion_can_recover_with_zero_step_node():
    from agent_alfred.graph import NoAction, llm_node

    clock = FakeClock()
    model = ScriptedModel(["last legal reply"])
    b = GraphBuilder("budget")
    assistant = Assistant(clock=clock, settings=Settings())
    b.add_node(
        "a",
        llm_node(
            "last",
            client=model,
            model=ModelRef("offline", "x"),
            assistant=assistant,
            output_key="a",
        ),
        writes=("a",),
    )
    b.add_node(
        "b",
        llm_node(
            "no budget",
            client=model,
            model=ModelRef("offline", "x"),
            assistant=assistant,
            output_key="b",
        ),
        writes=("b",),
    )
    b.add_node(
        "recover",
        fn_node(lambda s, c: NodeOutcome()),
        terminal=TerminalSpec.no_action("exhausted"),
    )
    b.add_edge("a", "b")
    b.add_error_edge("b", "recover")
    ctx = GraphRunContext(run_id="budget", budget=RunBudget(1), clock=clock)
    result = b.compile().invoke({}, context=ctx)
    assert (
        isinstance(result, NoAction) and result.recoveries[0].code == "budget_exhausted"
    )
    assert len(model.requests) == 1 and ctx.budget.remaining == 0


def test_ce07_all_effect_kinds_and_unconfigured_guidance_are_filtered():
    import sqlite3
    import threading

    from agent_alfred import schema
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.graph import agent_node
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.runtime.recording import RecordingStore
    from agent_alfred.tools import Tool, ToolPolicy, ToolRegistry, ToolSuccess
    from agent_alfred.tools.ledger import ExternalToolLedger
    from agent_alfred.tools.metering import ToolMetering

    conn = sqlite3.connect(":memory:")
    schema.migrate(conn)
    store = RecordingStore(conn, threading.Lock())
    clock = FakeClock()
    executed = []
    declarations = [
        Tool(
            name,
            name,
            {"type": "object"},
            lambda a, c: (
                executed.append(c.call_id) or ToolSuccess((TextBlock("done"),))
            ),
            effect,
        )
        for name, effect in [
            ("read", "local_read"),
            ("write", "local_write"),
            ("network", "external"),
            ("missing", "external"),
        ]
    ]
    registry = ToolRegistry(
        declarations,
        clock=clock,
        policies={
            "network": ToolPolicy(configured=True, authorization="allowed"),
            "missing": ToolPolicy(configured=False),
        },
        metering=ToolMetering(store, clock),
        external_ledger=ExternalToolLedger(store, clock),
    )
    try:
        model = ScriptedModel(
            [
                calls(
                    *(
                        ToolCallBlock(n, n, {})
                        for n in ["read", "write", "network", "missing"]
                    )
                ),
                "safe",
            ]
        )
        b = GraphBuilder("capabilities", tools=registry)
        b.add_node(
            "agent",
            agent_node(
                "work",
                client=model,
                model=ModelRef("offline", "x"),
                assistant=Assistant(clock=clock, settings=Settings()),
                output_key="reply",
                tool_names=(),
            ),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        ctx = GraphRunContext(
            run_id="restricted", budget=RunBudget(2), clock=clock, tools=registry
        )
        assert b.compile().invoke({}, context=ctx).output == "safe"
        assert executed == [] and all(not r.tools for r in model.requests)
        assert all(
            r["start_confirmation"] == "not_started"
            for r in registry.read_metering("restricted")
        )
        assert conn.execute("SELECT count(*) FROM tool_ledger").fetchone()[0] == 0
    finally:
        conn.close()


def test_ce07_known_tool_rejection_has_no_effect_in_real_ledger():
    from agent_alfred.graph import Failed, tool_node

    conn, store, registry, clock = registry_fixture()
    try:
        b = GraphBuilder("invalid", tools=registry)
        b.add_node(
            "bad",
            tool_node(
                "create_event",
                {"title": "bad", "starts_at": "not a timestamp"},
                output_key="reply",
            ),
            writes=("reply",),
            terminal=TerminalSpec.result("reply"),
        )
        ctx = GraphRunContext(
            run_id="invalid", budget=RunBudget(0), clock=clock, tools=registry
        )
        result = b.compile().invoke({}, context=ctx)
        assert isinstance(result, Failed) and result.side_effect_state == "none"
        assert conn.execute("SELECT COUNT(*) FROM tool_ledger").fetchone()[0] == 0
    finally:
        conn.close()


def test_ce07_pure_context_carries_no_io_capabilities(tmp_path, monkeypatch):
    import builtins
    import sqlite3

    protected = tmp_path / "protected"
    protected.write_bytes(b"unchanged")
    conn, store, registry, clock = registry_fixture()
    before = "\n".join(conn.iterdump())

    def pure(s, c):
        assert set(vars(c)) == {"node_id", "error"}
        return NodeOutcome({"reply": "pure"})

    def deny(*args, **kwargs):
        raise AssertionError("unexpected IO")

    b = GraphBuilder("pure")
    b.add_node(
        "pure", fn_node(pure), writes=("reply",), terminal=TerminalSpec.result("reply")
    )
    graph = b.compile()
    try:
        with monkeypatch.context() as guarded:
            guarded.setattr(builtins, "open", deny)
            guarded.setattr(sqlite3, "connect", deny)
            assert graph.invoke({}).output == "pure"
        assert (
            "\n".join(conn.iterdump()) == before
            and protected.read_bytes() == b"unchanged"
        )
    finally:
        conn.close()


def test_ce15_real_sdk_retry_events_keep_one_step_and_graph_node_identity():
    import httpx2

    from agent_alfred.endpoint_factory import EndpointClientFactory
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.graph import llm_node
    from agent_alfred.redact import Redactor

    clock = FakeClock()
    requests = []

    def send(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(503, json={"error": {"message": "retry"}})
        return httpx2.Response(
            200,
            json={
                "id": "result",
                "choices": [
                    {"message": {"content": "real SDK reply"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    sink = CapturingSink()
    events = FanOutSink([sink], process_instance_id="p", redactor=Redactor(()))
    ctx = GraphRunContext(run_id="sdk", budget=RunBudget(1), clock=clock, events=events)
    with httpx2.Client(transport=httpx2.MockTransport(send), trust_env=False) as http:
        factory = EndpointClientFactory(clock=clock, http_client=http)
        try:
            client = factory.create(snapshot())
            b = GraphBuilder("sdk")
            b.add_node(
                "reply",
                llm_node(
                    "answer",
                    client=client,
                    model=ModelRef("opencode-go", "deepseek-v4-flash"),
                    assistant=Assistant(clock=clock, settings=Settings()),
                    output_key="reply",
                ),
                writes=("reply",),
                terminal=TerminalSpec.result("reply"),
            )
            assert b.compile().invoke({}, context=ctx).output == "real SDK reply"
            assert ctx.budget.used == 1 and len(requests) == 2
            attempts = ctx.model_results[0].attempts
            assert [a.outcome for a in attempts] == ["aborted", "committed"]
            assert attempts[1].usage.output_tokens == 3
            nested = [
                e
                for e in sink.events
                if e.payload.name.startswith(("attempt.", "block."))
            ]
            assert nested and {e.envelope.node_id for e in nested} == {"reply"}
            assert {e.envelope.step_index for e in nested} == {0}
        finally:
            factory.close()
