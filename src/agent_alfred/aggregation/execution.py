"""Bridge aggregation to the Host's unique recording owner."""

from dataclasses import asdict

from agent_alfred.aggregation import KINDS
from agent_alfred.graph import GraphRunContext
from agent_alfred.graph.types import BudgetExhausted, Failed, NoAction, thaw
from agent_alfred.loop.assistant import LoopResult
from agent_alfred.messages import text_message


def run_aggregation(
    item,
    *,
    graph,
    tools,
    clock,
    events,
    budget,
    deadline,
    ledger,
    assistant,
    service,
    store,
    before_send=None,
):
    request = asdict(item.request.aggregation)
    settings = item.aggregation["settings"]
    request.update(
        persona=item.aggregation["persona"],
        max_tokens=settings.max_tokens,
        limits={
            k: getattr(settings, k)
            for k in (
                "per_store_limit",
                "per_store_character_budget",
                "working_memory_rounds",
                "input_character_limit",
            )
        },
    )
    facts = dict(
        schema_version=1,
        request=asdict(item.request.aggregation),
        reply_disposition="no_reply",
        fallback={"decision": "blocked", "reason": "aggregation_policy"},
        sources={
            k: {
                "selected": k in request["sources"],
                "read_outcome": "not_started" if k in request["sources"] else "skipped",
            }
            for k in KINDS
        },
    )
    item.memory_telemetry["aggregation"] = facts
    item.record_reply = False
    context = GraphRunContext(
        budget=budget,
        run_id=item.run_id,
        session_id=item.session_id,
        clock=clock,
        events=events,
        source=item.request.gateway,
        tools=tools,
        tool_permission=item.memory_permission,
        tool_state=item.memory_telemetry,
        deadline=deadline,
    )
    from agent_alfred.aggregation.attempts import AggregationModel
    from agent_alfred.model import ModelRef
    from agent_alfred.runtime.memory import InputEvidenceError, memory_context

    registered = service.forgetting.register_group(
        item.run_id,
        kind="run",
        container_id=item.session_id,
        evidence="complete",
        evidence_id="input-tracking:" + item.run_id,
        occurred_at=item.accepted_at,
        context=memory_context(item),
    )
    if "error" in registered:
        raise InputEvidenceError("input_evidence_unavailable")
    model = ModelRef(item.snapshot.endpoint_id, item.snapshot.model_id)
    context.model_bindings = {
        "aggregation": (
            AggregationModel(
                item=item,
                graph_context=context,
                ledger=ledger,
                service=service,
                store=store,
                before_send=before_send,
            ),
            model,
            assistant,
        )
    }
    result = None
    try:
        result = graph.invoke(
            {"request": request},
            context=context,
            disabled=tuple(k + "_source" for k in KINDS if k not in request["sources"]),
        )
    finally:
        facts.update(
            context.graph_identity,
            graph_result=type(result).__name__ if result else "Failed",
            steps=context.steps,
        )
        item.memory_telemetry["graph"] = dict(
            context.graph_identity,
            result=facts["graph_result"],
            steps=context.steps,
            fallback=False,
        )
        # A join failure must still report completed reads truthfully. The
        # durable tool ledger also covers a source wave aborted before commit.
        try:
            reads = {r["tool_name"]: r for r in tools.read_metering(item.run_id)}
        except Exception:
            reads = {}
        for kind, fact in facts["sources"].items():
            fact.update(
                actual_input_count=0,
                prepared_count=0,
                dispatch_state="not_sent",
                candidate_count=None,
                approved_count=None,
                permission_excluded=None,
                capacity_excluded=None,
                request_excluded=None,
                remaining="unknown",
                code=None,
            )
            read = reads.get("aggregation_" + kind)
            source = context.committed_state.get(kind + "_source")
            if source is not None:
                fact.update(
                    {
                        k: v
                        for k, v in thaw(source).items()
                        if k not in ("items", "kind", "schema_version")
                    }
                )
                fact["read_outcome"] = "succeeded"
            elif read and read["start_confirmation"] == "confirmed":
                fact["read_outcome"] = (
                    "succeeded" if read["result"] == "succeeded" else "failed"
                )
                fact["code"] = read.get("reason")
            recovery = context.committed_state.get(kind + "_recovery")
            if recovery is not None:
                fact.update(read_outcome="failed", code=recovery["code"])
        prepared = thaw(context.committed_state.get("prepared", {}))
        if prepared:
            facts["sources"] = prepared["sources"]
            facts["input_characters"] = prepared["input_characters"]
            sent = bool(item.memory_telemetry["input_attempts"])
            facts["provided"] = (
                [
                    {k: v for k, v in i.items() if k != "content"}
                    for i in prepared["items"]
                ]
                if sent
                else []
            )
            for source in facts["sources"].values():
                source["dispatch_state"] = (
                    "sent" if sent and source["prepared_count"] else "not_sent"
                )
                source["actual_input_count"] = source["prepared_count"] if sent else 0
        facts["recoveries"] = [asdict(r) for r in getattr(result, "recoveries", ())]
    if isinstance(result, NoAction):
        facts["reason_code"] = result.reason_code
        return LoopResult("completed", None, None, budget.used, context.duration_ms)
    if isinstance(result, (Failed, BudgetExhausted)):
        facts["error"] = result.error.code
        if isinstance(result, Failed) and result.forced_stop:
            stop = result.forced_stop
            facts["error"] = stop.error
            return LoopResult(
                stop.outcome, None, stop.error, budget.used, context.duration_ms
            )
        return LoopResult(
            "max_steps" if isinstance(result, BudgetExhausted) else "failed",
            None,
            result.error.code,
            budget.used,
            context.duration_ms,
        )
    item.record_reply = True
    facts["reply_disposition"] = "reply"
    return LoopResult(
        "completed",
        text_message("assistant", result.output),
        None,
        budget.used,
        context.duration_ms,
    )
