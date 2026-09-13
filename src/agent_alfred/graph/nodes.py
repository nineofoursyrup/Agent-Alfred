"""Controlled bindings of model and tool capabilities to graph nodes."""

from agent_alfred.loop.assistant import tool_boundary_result
from agent_alfred.loop.budget import StepBudgetExceeded
from agent_alfred.messages import message_plain_text, text_message
from agent_alfred.tools.metering import MeteringError

from .context import ForcedStop, RunForcedStop
from .types import NodeFactory, NodeOutcome, freeze, thaw


class NodeExecutionFailed(Exception):
    pass


def _model_node(kind, prompt, *, client, model, assistant, output_key, tool_names=()):
    def execute(state, context):
        run = context.run
        tools = None
        if kind == "agent" and run.tools is not None:
            tools = run.tools.restricted(tool_names)

        def started(lease):
            run.steps.append(
                dict(
                    node_id=context.node_id,
                    step_index=lease.step_index,
                    outcome="provisional",
                    attempt_ids=[],
                )
            )

        from agent_alfred.runtime.execution import AttemptLedger

        def observed(result, events):
            run.steps[-1]["attempt_ids"] = [a.attempt_id for a in result.attempts]

        ledger = AttemptLedger(client, records=run.model_results, observe=observed)
        try:
            result = assistant.respond(
                prompt(state) if callable(prompt) else prompt,
                client=ledger,
                model=model,
                budget=run.budget,
                working_memory=(*run.working_memory, *run.transcript),
                run_id=run.run_id,
                session_id=run.session_id,
                events=run.events,
                source=run.source,
                node_id=context.node_id,
                single_step=kind == "llm",
                tools=tools,
                tool_permission=run.tool_permission,
                tool_state=run.tool_state,
                on_step_started=started,
            )
        except MeteringError as exc:
            _metering_stop(run, exc)
        except BaseException:
            run.abort((context.node_id,))
            raise
        if run.tool_state.get("finalization_reason"):
            run.forced_stop = ForcedStop(
                result.outcome, result.reply, result.error, freeze(run.tool_state)
            )
            raise RunForcedStop()
        if result.outcome == "max_steps":
            raise StepBudgetExceeded("node budget exhausted")
        if result.outcome != "completed":
            raise NodeExecutionFailed(result.error or "model_failed")
        run.pending_transcripts[context.node_id] = result.transcript
        return NodeOutcome({output_key: message_plain_text(result.reply)})

    return NodeFactory(kind, execute, tuple(tool_names))


def llm_node(prompt, *, client, model, assistant, output_key):
    return _model_node(
        "llm",
        prompt,
        client=client,
        model=model,
        assistant=assistant,
        output_key=output_key,
    )


def agent_node(prompt, *, client, model, assistant, output_key, tool_names=()):
    return _model_node(
        "agent",
        prompt,
        client=client,
        model=model,
        assistant=assistant,
        output_key=output_key,
        tool_names=tuple(tool_names),
    )


def tool_node(tool_name, arguments, *, output_key):
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.tools import ToolContext

    from .types import freeze

    if not isinstance(tool_name, str):
        raise ValueError("tool_node accepts only a registered tool name")
    saved = arguments if callable(arguments) else freeze(arguments)

    def execute(state, context):
        run = context.run
        if run.tools is None:
            raise NodeExecutionFailed("tools unavailable")
        # A disjoint durable zero-Step namespace; node identity is trusted,
        # not a model-provided call id. The wire Step stays absent.
        call_id = "graph:" + context.node_id
        args = saved(state) if callable(saved) else saved
        call = ToolCallBlock(call_id, tool_name, thaw(args))
        try:
            execution = run.tools.execute(
                call,
                ToolContext(
                    run.run_id,
                    None,
                    call_id,
                    run.source,
                    float("inf"),
                    run.session_id,
                    run.tool_permission,
                    node_id=context.node_id,
                ),
                events=run.events,
            )
        except MeteringError as exc:
            _metering_stop(run, exc)
        if execution.system_receipt is not None:
            run.tool_state.setdefault("system_receipts", []).append(
                execution.system_receipt
            )
        if execution.stop_reason:
            outcome, reply, error = tool_boundary_result(
                execution, call, [], run.tool_state
            )
            run.forced_stop = ForcedStop(outcome, reply, error, freeze(run.tool_state))
            raise RunForcedStop()
        if execution.block.is_error:
            raise NodeExecutionFailed("tool_failed")
        return NodeOutcome(
            {output_key: "\n".join(b.text for b in execution.block.content)}
        )

    return NodeFactory("tool", execute, (tool_name,))


def _metering_stop(run, error):
    run.tool_state["finalization_reason"] = "metering_unconfirmed"
    run.forced_stop = ForcedStop(
        "failed",
        text_message("assistant", "工具计量无法确认，本次后续调用已停止。"),
        "metering_unconfirmed",
        freeze(run.tool_state),
    )
    raise RunForcedStop() from error
