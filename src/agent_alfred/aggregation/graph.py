"""One startup-compiled topology; selection only disables source nodes."""

from agent_alfred.aggregation import KINDS
from agent_alfred.aggregation.input import join, model_request, validate_result
from agent_alfred.aggregation.sources import validate_source
from agent_alfred.graph import (
    GraphBuilder,
    GraphRegistry,
    NodeOutcome,
    TerminalSpec,
    fn_node,
    llm_node,
    tool_node,
)
from agent_alfred.graph.types import thaw

REASONS = (
    "sources_not_selected",
    "sources_unavailable",
    "capacity_excluded_all",
    "sources_excluded_all",
    "no_matching_sources",
)


def build_graph(tools):
    b = GraphBuilder("manual_aggregation", tools=tools)
    b.declare_input("request")
    b.add_node(
        "aggregation_input",
        fn_node(lambda s, c: NodeOutcome({"anchor": True})),
        required_reads=("request",),
        writes=("anchor",),
    )
    optional = []
    for kind in KINDS:
        source, recovery = kind + "_source", kind + "_recovery"
        optional.extend((source, recovery))
        b.add_node(
            source,
            tool_node(
                "aggregation_" + kind,
                lambda s: dict(s["request"]),
                output_key=source,
                structured=True,
                # Check the actual centrally redacted payload before committing
                # a source key. The read-side projection is only a sizing view.
                structured_validator=lambda value, state, k=kind: validate_source(
                    thaw(value), k, thaw(state["request"])
                ),
            ),
            required_reads=("request", "anchor"),
            writes=(source,),
            skippable_by_config=True,
        )
        b.add_node(
            recovery,
            fn_node(lambda s, c, k=recovery: NodeOutcome({k: {"code": c.error.code}})),
            writes=(recovery,),
        )
        b.add_edge("aggregation_input", source)
        b.add_error_edge(source, recovery)
        b.add_edge(source, "join")
        b.add_edge(recovery, "join")
    b.add_node(
        "join",
        fn_node(join),
        required_reads=("request", "anchor"),
        optional_reads=optional,
        writes=("prepared",),
    )
    b.add_edge("aggregation_input", "join")
    b.add_node(
        "synthesis",
        llm_node(
            lambda s: s["request"]["goal"],
            output_key="candidate",
            binding="aggregation",
            request_builder=lambda s, m: model_request(
                s["prepared"], m, s["request"]["max_tokens"]
            ),
        ),
        required_reads=("request", "prepared"),
        writes=("candidate",),
    )
    b.add_node(
        "validate_result",
        fn_node(validate_result),
        required_reads=("candidate", "prepared"),
        writes=("draft",),
        terminal=TerminalSpec.result("draft"),
    )
    b.add_edge("synthesis", "validate_result")
    routes = {"synthesize": "synthesis"}
    for reason in REASONS:
        b.add_node(
            reason,
            fn_node(lambda s, c: NodeOutcome()),
            terminal=TerminalSpec.no_action(reason),
        )
        routes[reason] = reason
    b.add_conditional_edges(
        "join",
        lambda s: s["prepared"]["decision"],
        routes,
        required_reads=("prepared",),
    )
    return GraphRegistry({"manual_aggregation": b}).get("manual_aggregation")
