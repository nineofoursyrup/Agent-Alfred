"""The frozen message-routing DAG and pure, finite routing rules."""

from collections.abc import Mapping

from agent_alfred.graph import (
    GraphBuilder,
    GraphRegistry,
    NodeOutcome,
    TerminalSpec,
    agent_node,
    fn_node,
    llm_node,
)
from agent_alfred.graph.context import RunForcedStop
from agent_alfred.graph.types import ContextInvalid, GraphInvariantError, thaw
from agent_alfred.runtime.memory import (
    InputDeadlineExceeded,
    InputEvidenceError,
    InputLimitExceeded,
    InputResolutionError,
)
from agent_alfred.runtime.telemetry import AttemptObservationFailed
from agent_alfred.stream_fallback import OverallDeadlineExceeded

# The projection seam can report data failure, but never relabel a Run safety stop.
_RUN_STOPS = (
    GraphInvariantError,
    RunForcedStop,
    InputDeadlineExceeded,
    InputEvidenceError,
    InputLimitExceeded,
    InputResolutionError,
    AttemptObservationFailed,
    OverallDeadlineExceeded,
)

CONTEXT_FAILURE = "本次上下文准备失败，请稍后重试。"
SOCIAL = {
    "greeting": ("你好", "您好", "嗨", "hello", "hi"),
    "thanks": ("谢谢", "多谢", "感谢", "thanks", "thank you"),
    "no_reply": ("不用回复", "无需回复", "不必回复", "无需回复，也不需要做任何事"),
}


def normalized_task(task):
    text = task.strip().translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )
    count = len(text) - len(text.rstrip("。.!！"))
    return text[:-count].rstrip() if 0 < count <= 3 else text


def project_context(snapshot):
    """Validate a JSON snapshot without IO; return its owned, ordered projection."""

    def require(condition):
        if not condition:
            raise ContextInvalid("invalid_prepared_context")

    value = thaw(snapshot)
    try:
        require(isinstance(value, dict) and type(value["context_version"]) is int)
        require(value["context_version"] == 1)
        messages, groups = value["working_messages"], value["working_run_ids"]
        require(isinstance(messages, list) and isinstance(groups, list))
        require(len(messages) == 2 * len(groups))
        require(all(type(g) is str and g for g in groups))
        from agent_alfred.messages import blocks_from_jsonable

        for i, message in enumerate(messages):
            require(message["role"] == ("user" if i % 2 == 0 else "assistant"))
            require(isinstance(message["blocks"], list))
            blocks_from_jsonable(message["blocks"])
        require(isinstance(value["selected_references"], list))
        for ref in value["selected_references"]:
            require(ref["kind"] in ("semantic", "episodic"))
            require(type(ref["memory_id"]) is str and bool(ref["memory_id"]))
            require(type(ref["record_version"]) is int and ref["record_version"] > 0)
        require(value["reference_text"] is None or type(value["reference_text"]) is str)
        require(value["tool_text"] is None or type(value["tool_text"]) is str)
        require(isinstance(value["tool_sources"], list))
        require(
            all(
                isinstance(x, dict) and type(x["source_run"]) is str
                for x in value["tool_sources"]
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextInvalid("invalid_prepared_context") from exc
    return value


def recover_context(state, context):
    return NodeOutcome({"context_recovery": {"unavailable": True}})


def join_route(state, context):
    category = state["classification"]
    known = category in ("greeting", "thanks", "no_reply", "full")
    matches = normalized_task(state["task"]) in SOCIAL.get(category, ())
    projected = state.get("projected_context")
    recovered = state.get("context_recovery")
    if (projected is None) == (recovered is None):
        raise ContextInvalid("context_result_missing")
    if recovered is not None and recovered != {"unavailable": True}:
        raise ContextInvalid("invalid_context_recovery")
    if category == "no_reply" and matches:
        route, reason = "no_action", "user_requested_no_reply"
    elif recovered is not None:
        route, reason = "context_failure", "context_recovered"
    elif category == "full":
        route, reason = "full", "classifier_full"
    elif not known:
        route, reason = "fallback", "unknown_classification"
    elif not matches:
        route, reason = "fallback", "guard_not_matched"
    elif state["has_loaded_skills"]:
        route, reason = "fallback", "skills_require_full"
    else:
        route, reason = "quick", category
    return NodeOutcome(
        dict(
            route_decision=dict(
                route=route,
                decision_reason=reason,
                category=category if known else "unknown",
            ),
            answer_context=projected
            if projected is not None
            else {"unavailable": True},
        )
    )


def build_routing_graph(tools, *, projection=project_context, recovery=recover_context):
    b = GraphBuilder("message_routing", tools=tools)
    for key in ("task", "prepared_context", "has_loaded_skills"):
        b.declare_input(key)
    b.add_node(
        "classify",
        llm_node(
            lambda s: s["task"],
            output_key="classification",
            binding="classifier",
            input_mode="current_task",
        ),
        required_reads=("task",),
        writes=("classification",),
    )

    def project(state, context):
        try:
            result = projection(state["prepared_context"])
            result = project_context(result)
        except _RUN_STOPS:
            raise
        except Exception as exc:
            raise ContextInvalid("context_projection_invalid") from exc
        return NodeOutcome({"projected_context": result})

    def recover(state, context):
        try:
            result = recovery(state, context)
            if not isinstance(result, NodeOutcome) or not isinstance(
                result.writes, Mapping
            ):
                raise ValueError("invalid_recovery")
            if dict(result.writes) != {"context_recovery": {"unavailable": True}}:
                raise ValueError("invalid_recovery")
            return result
        except _RUN_STOPS:
            raise
        except Exception as exc:
            raise ContextInvalid("context_recovery_invalid") from exc

    b.add_node(
        "project_context",
        fn_node(project),
        required_reads=("prepared_context",),
        writes=("projected_context",),
    )
    b.add_node("recover_context", fn_node(recover), writes=("context_recovery",))
    b.add_error_edge("project_context", "recover_context")
    b.add_node(
        "join_route",
        fn_node(join_route),
        required_reads=("classification", "task", "has_loaded_skills"),
        optional_reads=("projected_context", "context_recovery"),
        writes=("route_decision", "answer_context"),
    )
    for source in ("classify", "project_context", "recover_context"):
        b.add_edge(source, "join_route")
    b.add_node(
        "full_agent",
        agent_node(
            lambda s: s["task"],
            binding="answer",
            output_key="agent_output",
            tool_names=tuple(t.name for t in tools.declarations()) if tools else (),
            context_key="answer_context",
        ),
        required_reads=("task", "answer_context"),
        writes=("agent_output",),
    )
    b.add_node(
        "full_result",
        fn_node(lambda s, c: NodeOutcome({"full_output": s["agent_output"]})),
        required_reads=("agent_output",),
        writes=("full_output",),
        terminal=TerminalSpec.result("full_output"),
    )
    b.add_edge("full_agent", "full_result")
    b.add_node(
        "quick_result",
        fn_node(
            lambda s, c: NodeOutcome(
                {
                    "quick_output": "你好！"
                    if s["route_decision"]["decision_reason"] == "greeting"
                    else "不客气。"
                }
            )
        ),
        required_reads=("route_decision",),
        writes=("quick_output",),
        terminal=TerminalSpec.result("quick_output"),
    )
    b.add_node(
        "context_failure_result",
        fn_node(lambda s, c: NodeOutcome({"context_failure_output": CONTEXT_FAILURE})),
        writes=("context_failure_output",),
        terminal=TerminalSpec.result("context_failure_output"),
    )
    b.add_node(
        "no_reply_terminal",
        fn_node(lambda s, c: NodeOutcome()),
        terminal=TerminalSpec.no_action("user_requested_no_reply"),
    )
    b.add_conditional_edges(
        "join_route",
        lambda s: s["route_decision"]["route"],
        {
            "quick": "quick_result",
            "full": "full_agent",
            "fallback": "full_agent",
            "no_action": "no_reply_terminal",
            "context_failure": "context_failure_result",
        },
        required_reads=("route_decision",),
    )
    return GraphRegistry({"message_routing": b}).get("message_routing")
