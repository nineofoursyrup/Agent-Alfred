"""Read-only schema1 interpretation, frozen to its historical producer contract."""

from agent_alfred.routing_statistics import ROUTES


def legacy_facts(memory):
    # Import locally: the current reader calls this only for absent admission data.
    from agent_alfred.routing_statistics.service import (
        BLOCKED,
        boolean,
        object_value,
        result_facts,
    )

    facts = result_facts({})
    routing = object_value(memory.get("routing"))
    if type(routing.get("schema_version")) is not int or routing["schema_version"] != 1:
        return "unknown", facts
    facts["missing_reason"] = "legacy_incomplete"
    settings = object_value(routing.get("settings"))
    enabled = boolean(settings.get("enabled"))
    qualification = (
        ("enabled" if enabled else "disabled")
        if enabled is not None and settings.get("status") == "ok"
        else "unknown"
    )
    route = routing.get("route")
    graph = routing.get("graph_id") == "message_routing"
    if graph and isinstance(route, str) and route in ROUTES:
        facts["route"] = route
    result = routing.get("graph_result")
    if result == "NoAction" and facts["route"] not in ("unknown", "no_action"):
        facts["route"] = "unknown"
    if (
        result in ("Completed", "CompletedWithRecovery")
        and facts["route"] == "no_action"
    ):
        facts["route"] = "unknown"
    recoveries = routing.get("recoveries")
    if graph and isinstance(recoveries, list):
        valid = all(
            isinstance(r, dict)
            and r.get("node_id") == "project_context"
            and r.get("code") == "node_failed"
            and isinstance(r.get("message"), str)
            and r.get("side_effect_state") in ("none", "occurred", "unknown")
            for r in recoveries
        )
        if valid and recoveries:
            facts["recovered"] = True
        elif valid and not recoveries and result in ("Completed", "NoAction"):
            facts["recovered"] = False
    fallback = object_value(routing.get("fallback"))
    entered = boolean(fallback.get("entered"))
    decision, reason = fallback.get("decision"), fallback.get("reason")
    complete = (
        result
        in (
            "Completed",
            "CompletedWithRecovery",
            "NoAction",
            "Failed",
            "BudgetExhausted",
        )
        and isinstance(routing.get("classification"), dict)
        and decision in ("not_needed", "allowed", "blocked")
        and type(fallback.get("model_requests")) is int
        and fallback["model_requests"] >= 0
    )
    if entered is True and decision == "allowed":
        facts["blocked_unknown"] = False
        if reason == "routing_unavailable" and not graph and result is None:
            facts.update(fallback=False, bypass=True)
        elif (
            reason == "graph_failed"
            and graph
            and result in ("Failed", "BudgetExhausted")
        ):
            # Unlike the pre-invoke default Failed, this decision was produced
            # after graph.invoke returned; it is positive execution evidence.
            facts.update(fallback=True, bypass=False)
    elif entered is False and complete and fallback["model_requests"] == 0:
        facts.update(
            fallback=False,
            bypass=False,
            blocked_unknown=decision == "blocked" and reason not in BLOCKED,
        )
        if decision == "blocked" and reason in BLOCKED:
            facts["blocked"] = reason
    return qualification, facts
