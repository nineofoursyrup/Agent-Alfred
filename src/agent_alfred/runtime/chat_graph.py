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
):
    memory._check_input_deadline(None)
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
    )
    context.checkpoint()
    graph = factory(client=client, model=model, assistant=assistant, tools=tools)
    result_name = "Failed"
    try:
        result = graph.invoke({"task": task}, context=context)
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
    if isinstance(result, (Failed, BudgetExhausted)):
        if isinstance(result, Failed) and result.forced_stop is not None:
            stop = result.forced_stop
            return LoopResult(
                stop.outcome, stop.reply, stop.error, budget.used, context.duration_ms
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
