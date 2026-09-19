"""Chat-only Graph adapter; the executor retains the single Run finalizer."""

import json

from agent_alfred.graph import GraphRunContext
from agent_alfred.graph.types import (
    BudgetExhausted,
    Completed,
    CompletedWithRecovery,
    Failed,
    NoAction,
    thaw,
)
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import Message, text_message


def run_chat_graph(
    factory,
    *,
    assistant,
    client,
    model,
    item,
    task,
    working_memory,
    memory,
    skills,
    persona,
    tools,
    clock,
    events,
    budget,
    deadline,
    routing=None,
):
    memory._check_input_deadline(None)
    if routing is not None and budget.remaining == 0:
        return LoopResult("max_steps", None, None, budget.used, 0)
    if budget.remaining > 0:
        working_memory = memory.prepare(
            working_memory,
            assistant.system_blocks(persona=persona, skills=skills),
            model,
            tools.schemas() if tools else (),
        )
        gate = memory.evaluate(budget, working_memory, deadline)
        if gate is not None and gate.failure_code:
            return LoopResult(
                "failed",
                text_message("assistant", "记忆检索资料不可用，本次未生成回答。"),
                gate.failure_code,
                budget.used,
                0,
            )
    context = GraphRunContext(
        budget=budget,
        run_id=item.run_id,
        session_id=item.session_id,
        clock=clock,
        events=events,
        source=item.request.gateway,
        tools=tools,
        tool_permission=item.memory_permission,
        working_memory=tuple(working_memory),
        tool_state=item.memory_telemetry,
        deadline=deadline,
        skills=skills,
        persona=persona,
        memory=memory,
        publication_generation=routing["generation"] if routing else 1,
    )
    context.model_bindings = {"answer": (client, model, assistant)}
    context.checkpoint()
    graph = factory(client=client, model=model, assistant=assistant, tools=tools)
    inputs = {"task": task}
    if routing is not None:
        inputs.update(
            prepared_context=memory.prepared_context(),
            has_loaded_skills=bool(skills and skills.section),
        )
        item.memory_telemetry["routing"] = dict(
            schema_version=1,
            settings=routing["settings"],
            generation=routing["generation"],
            capabilities=routing["capabilities"],
            classification={"status": "not_started"},
            fallback={"decision": "not_needed", "entered": False, "model_requests": 0},
            reply_disposition="reply",
        )
    stats = item.memory_telemetry.get("routing_statistics")
    if routing is not None and stats is not None:
        stats["graph_entered"] = True
    result_name = "Failed"
    result = None
    try:
        result = graph.invoke(inputs, context=context)
        result_name = type(result).__name__
    finally:
        # Input failures and cancellation propagate to the sole Run finalizer;
        # they must not discard the graph's already incurred/aborted Steps.
        item.memory_telemetry["graph"] = {
            **context.graph_identity,
            "result": result_name,
            "steps": [dict(step) for step in context.steps],
            "fallback": False,
        }
        if routing is not None:
            if stats is not None:
                stats["route"] = context.committed_state.get(
                    "route_decision", {}
                ).get("route")
                stats["recovered"] = (
                    context.committed_state.get("context_recovery") is not None
                )
            facts = item.memory_telemetry["routing"]
            facts.update(context.graph_identity, graph_result=result_name)
    if routing is not None:
        from dataclasses import asdict

        facts = item.memory_telemetry["routing"]
        facts.update(
            context.graph_identity,
            graph_result=result_name,
            recoveries=[asdict(r) for r in getattr(result, "recoveries", ())],
        )
        facts.update(thaw(context.committed_state.get("route_decision", {})))
    if isinstance(result, (Failed, BudgetExhausted)):
        if routing is not None:
            reason = (
                (
                    "overall_deadline"
                    if result.forced_stop.error == "overall_deadline"
                    else "forced_stop"
                )
                if isinstance(result, Failed) and result.forced_stop
                else "context_invalid"
                if context.context_invalid
                else "side_effect_" + result.side_effect_state
                if result.side_effect_state != "none"
                else "budget_exhausted"
                if budget.remaining == 0
                else "graph_failed"
            )
            allowed = reason == "graph_failed"
            if stats is not None and not allowed:
                stats["blocked"] = reason
            facts["fallback"] = dict(
                decision="allowed" if allowed else "blocked",
                reason=reason,
                entered=False,
                model_requests=0,
            )
            facts["error"] = result.error.code
            facts["side_effect_state"] = result.side_effect_state
            from agent_alfred.events import EventEnvelope, Notice

            events.emit(
                Notice(
                    code="routing_fallback",
                    detail=(
                        ("decision", facts["fallback"]["decision"]),
                        ("reason", reason),
                        ("graph_id", context.graph_identity.get("graph_id", "")),
                    ),
                ),
                EventEnvelope(
                    clock.monotonic(),
                    item.run_id,
                    item.session_id,
                    None,
                    None,
                    None,
                    item.request.gateway,
                ),
            )
            if not allowed:
                from agent_alfred.events import PathStage

                context.emit(PathStage("fallback", reason, False))
            if reason == "budget_exhausted":
                return LoopResult(
                    "max_steps",
                    None,
                    result.error.code,
                    budget.used,
                    context.duration_ms,
                )
            if context.context_invalid and not getattr(result, "forced_stop", None):
                return LoopResult(
                    "failed",
                    text_message("assistant", "本次上下文失效，已停止后续模型和工具。"),
                    "context_invalid",
                    budget.used,
                    context.duration_ms,
                )
        if isinstance(result, Failed) and result.forced_stop is not None:
            stop = result.forced_stop
            return LoopResult(
                stop.outcome,
                (
                    text_message("assistant", "运行期限已到，未发送后续请求。")
                    if stop.error == "overall_deadline"
                    else stop.reply
                ),
                stop.error,
                budget.used,
                context.duration_ms,
            )
        context.checkpoint()
        if result.side_effect_state == "none":
            item.memory_telemetry["graph"]["fallback"] = True
            return None
        return LoopResult(
            "max_steps" if isinstance(result, BudgetExhausted) else "failed",
            text_message(
                "assistant", "图执行未完成；已发生或无法确认的动作不会自动重做。"
            ),
            result.error.code,
            budget.used,
            context.duration_ms,
        )
    if isinstance(result, NoAction):
        if routing is not None:
            facts.update(reply_disposition="no_reply", reason_code=result.reason_code)
        item.record_reply = False
        return LoopResult("completed", None, None, budget.used, context.duration_ms)
    if not isinstance(result, (Completed, CompletedWithRecovery)):
        raise TypeError("unknown GraphResult")
    output = result.output
    reply = (
        output
        if isinstance(output, Message)
        else text_message(
            "assistant",
            output
            if isinstance(output, str)
            else json.dumps(thaw(output), ensure_ascii=False),
        )
    )
    return LoopResult("completed", reply, None, budget.used, context.duration_ms)
