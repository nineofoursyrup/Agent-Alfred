"""Narrow graph-result adapter; RunRecorder remains the sole session writer."""

import json

from agent_alfred.messages import Message, text_message

from .types import (
    BudgetExhausted,
    Completed,
    CompletedWithRecovery,
    Failed,
    NoAction,
    thaw,
)


def settle_graph(recorder, item, result, context):
    if item.run_id != context.run_id:
        raise ValueError("graph Run identity mismatch")
    item.memory_telemetry.update(thaw(context.tool_state))
    item.memory_telemetry["graph"] = {
        **context.graph_identity,
        "result": type(result).__name__,
        "steps": [dict(s) for s in context.steps],
    }
    outcome, reply, error = "completed", None, None
    if isinstance(result, Failed) and result.forced_stop is not None:
        stop = result.forced_stop
        outcome, reply, error = stop.outcome, stop.reply, stop.error
        item.memory_telemetry.update(thaw(stop.facts))
    elif isinstance(result, (Completed, CompletedWithRecovery)):
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
    elif isinstance(result, NoAction):
        item.record_reply = False
    elif isinstance(result, BudgetExhausted):
        outcome, error = "max_steps", result.error.code
    elif isinstance(result, Failed):
        outcome, error = "failed", result.error.code
    else:
        raise TypeError("unknown GraphResult")
    recorder.settle(
        item,
        outcome=outcome,
        reply=reply,
        error=error,
        step_count=context.budget.used,
        duration_ms=context.duration_ms,
        model_results=tuple(context.model_results),
    )
