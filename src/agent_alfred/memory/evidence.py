"""Strict interpretation of persisted R06 v1 evidence, without reconstructing it."""

import math
from typing import Any

_FIELDS = frozenset(
    {
        "schema_version",
        "decision",
        "outcome",
        "decision_source",
        "reason_code",
        "fallback_reason",
        "rule_version",
        "gate_step_index",
        "model_ref",
        "latency_ms",
        "timing",
        "stores",
        "hit_count",
        "selected_count",
        "references",
        "input_disposition",
    }
)
_KINDS = ("semantic", "episodic")


def _shape(value, keys) -> bool:
    return isinstance(value, dict) and set(value) == set(keys)


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _duration(value) -> bool:
    return (type(value) is int and value >= 0) or (
        type(value) is float and math.isfinite(value) and value >= 0
    )


def _identity(value) -> bool:
    return isinstance(value, str) and bool(value)


def _origin(value) -> bool:
    if not isinstance(value, dict):
        return False
    branch = value.get("type")
    if branch == "manual":
        return _shape(value, ("type", "source")) and value["source"] in ("cli", "web")
    if branch == "tool":
        return _shape(value, ("type", "call_id")) and _identity(value["call_id"])
    if branch == "consolidation":
        return _shape(value, ("type", "batch_id")) and _identity(value["batch_id"])
    return False


def _model(value) -> bool:
    source, fallback = value["decision_source"], value["fallback_reason"]
    if source == "model":
        if fallback is not None or value["rule_version"] is not None:
            return False
    elif source == "deterministic_fallback":
        if (
            fallback
            not in (
                "model_unavailable",
                "model_call_failed",
                "invalid_output",
                "model_deadline",
            )
            or value["rule_version"] != "memory-gate-rules-v1"
        ):
            return False
        if value["decision"] and value["reason_code"] != "conservative_retrieve":
            return False
    else:
        return False
    step, model = value["gate_step_index"], value["model_ref"]
    if step is None:
        return (
            model is None
            and value["timing"]["model_ms"] is None
            and source == "deterministic_fallback"
            and fallback in ("model_unavailable", "model_deadline")
        )
    return (
        _count(step)
        and fallback != "model_unavailable"
        and _shape(model, ("endpoint_id", "model_id", "wire_style"))
        and all(_identity(item) for item in model.values())
        and _duration(value["timing"]["model_ms"])
    )


def _stores(value) -> bool:
    if not _shape(value["stores"], _KINDS):
        return False
    for store in value["stores"].values():
        if not _shape(store, ("status", "hit_count", "error_code")):
            return False
        status, count, error = store["status"], store["hit_count"], store["error_code"]
        if status == "succeeded":
            if not _count(count) or error is not None:
                return False
        elif status == "not_queried":
            if count is not None or error is not None:
                return False
        elif status == "failed":
            if count is not None or error not in (
                "storage_unavailable",
                "storage_read_failed",
            ):
                return False
        else:
            return False
    return True


def _references(value) -> bool:
    refs = value["references"]
    if not isinstance(refs, list):
        return False
    counts = dict.fromkeys(_KINDS, 0)
    stopped = dict.fromkeys(_KINDS, False)
    ids = set()
    previous_kind = "semantic"
    selected = 0
    for ref in refs:
        if not _shape(
            ref,
            (
                "kind",
                "memory_id",
                "record_version",
                "origin",
                "rank",
                "selected",
                "omission_reason",
            ),
        ):
            return False
        kind = ref["kind"]
        if kind not in _KINDS or (previous_kind == "episodic" and kind == "semantic"):
            return False
        previous_kind = kind
        if not _identity(ref["memory_id"]) or not _origin(ref["origin"]):
            return False
        if not _count(ref["record_version"]) or ref["record_version"] == 0:
            return False
        if (kind, ref["memory_id"]) in ids:
            return False
        ids.add((kind, ref["memory_id"]))
        counts[kind] += 1
        if (
            type(ref["rank"]) is not int
            or ref["rank"] != counts[kind]
            or type(ref["selected"]) is not bool
        ):
            return False
        reason = ref["omission_reason"]
        if value["outcome"] == "error":
            if ref["selected"] or reason != "store_error":
                return False
        elif ref["selected"]:
            if stopped[kind] or reason is not None:
                return False
            selected += 1
        else:
            if reason != ("after_prefix_stop" if stopped[kind] else "character_budget"):
                return False
            stopped[kind] = True
    for kind in _KINDS:
        store = value["stores"][kind]
        expected = store["hit_count"] if store["status"] == "succeeded" else 0
        if counts[kind] != expected:
            return False
    return selected == value["selected_count"]


def valid_gate_evidence(value: Any) -> bool:
    """Unknown, private, or contradictory evidence is never an inferred outcome."""
    if not _shape(value, _FIELDS):
        return False
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        return False
    outcome, decision = value["outcome"], value["decision"]
    if outcome not in ("skip", "hit", "miss", "error") or type(decision) is not bool:
        return False
    if (outcome == "skip") != (not decision):
        return False
    allowed_reasons = (
        ("personal_information", "history_recall", "conservative_retrieve")
        if decision
        else ("greeting", "arithmetic")
    )
    if value["reason_code"] not in allowed_reasons:
        return False
    if not _duration(value["latency_ms"]) or not _count(value["selected_count"]):
        return False
    timing = value["timing"]
    if not _shape(timing, ("model_ms", "search_ms", "selection_ms")):
        return False
    if not all(item is None or _duration(item) for item in timing.values()):
        return False
    if not _model(value) or not _stores(value) or not _references(value):
        return False
    statuses = tuple(value["stores"][kind]["status"] for kind in _KINDS)
    count, selected, disposition = (
        value["hit_count"],
        value["selected_count"],
        value["input_disposition"],
    )
    if outcome == "skip":
        return (
            statuses == ("not_queried", "not_queried")
            and type(count) is int
            and count == selected == 0
            and disposition == "not_needed"
            and timing["search_ms"] is None
            and timing["selection_ms"] is None
        )
    if not _duration(timing["search_ms"]):
        return False
    if outcome == "error":
        return (
            statuses in (("failed", "not_queried"), ("succeeded", "failed"))
            and count is None
            and selected == 0
            and disposition == "store_error"
            and timing["selection_ms"] is None
        )
    if (
        statuses != ("succeeded", "succeeded")
        or not _count(count)
        or count != sum(store["hit_count"] for store in value["stores"].values())
        or not _duration(timing["selection_ms"])
    ):
        return False
    if outcome == "miss":
        return count == selected == 0 and disposition == "no_hits"
    if count <= 0:
        return False
    if selected == 0:
        return disposition == "all_excluded"
    return disposition == ("ready" if selected == count else "partial")
