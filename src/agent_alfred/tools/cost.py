"""Closed metering projection validated against registered service declarations."""

from decimal import Decimal, InvalidOperation


def read_tool_cost(raw, source_id):
    """Validate persisted amounts too; a damaged row must not poison sums."""
    if not isinstance(raw, dict):
        return {"kind": "unrecorded"}
    kind = raw.get("kind")
    if kind in ("not_billable", "unknown", "unrecorded"):
        if kind == "not_billable" and raw.get("reason") == "http_not_sent":
            return {"kind": kind, "reason": "http_not_sent"}
        return {"kind": kind}
    try:
        if kind != "reported" or type(raw.get("units")) not in (str, int):
            raise ValueError()
        amount = Decimal(raw["units"])
        if not amount.is_finite() or amount < 0 or raw["service"] != source_id:
            raise ValueError()
        if any(
            type(raw.get(k)) is not str or not raw[k]
            for k in ("unit", "source", "service")
        ):
            raise ValueError()
        return {
            "kind": "reported",
            "units": format(amount, "f"),
            **{k: raw[k] for k in ("unit", "source", "service")},
        }
    except ValueError, TypeError, KeyError, InvalidOperation:
        return {"kind": "unknown", "reason": "invalid_persisted_metering"}


def project_tool_cost(tool, outcome, entered):
    from agent_alfred.tools import NotSentCost, ToolCost

    if not entered or tool.effect != "external":
        return {"kind": "not_billable"}
    cost = outcome.cost
    if isinstance(cost, NotSentCost):
        return {"kind": "not_billable", "reason": "http_not_sent"}
    if not isinstance(cost, ToolCost):
        return {"kind": "unknown", "reason": "not_reported"}
    if cost.unit not in tool.cost_units or cost.source not in tool.cost_sources:
        return {"kind": "unknown", "reason": "undeclared_metering"}
    try:
        value = Decimal(cost.units)
        if not value.is_finite() or value < 0:
            raise ValueError("invalid_units")
    except ValueError, TypeError, InvalidOperation:
        return {"kind": "unknown", "reason": "invalid_units"}
    return {
        "kind": "reported",
        "units": format(value, "f"),
        "unit": cost.unit,
        "service": tool.source_id,
        "source": cost.source,
    }
