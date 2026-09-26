"""Retrieval decisions, whole-record selection, and body-free gate evidence."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

RULE_VERSION = "memory-gate-rules-v1"
_GREETINGS = frozenset(
    "你好|您好|嗨|哈喽|早上好|下午好|晚上好|晚安|谢谢|多谢|感谢|再见|拜拜|"
    "hi|hello|hey|thanks|thank you|bye|goodbye|good morning|good afternoon|"
    "good evening|good night".split("|")
)
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


@dataclass(frozen=True)
class GateDecision:
    retrieve: bool
    query: str | None
    reason_code: str


def _arithmetic(text: str) -> bool:
    """Recognize the grammar iteratively; never evaluate user expressions."""
    text = text.strip()
    if text.endswith(("=", "?")):
        text = text[:-1]
    pos, depth, binary_count = 0, 0, 0
    operand = True
    while pos < len(text):
        char = text[pos]
        if char.isspace():
            pos += 1
            continue
        if operand:
            if char in "+-":
                pos += 1
                continue
            if char == "(":
                depth += 1
                pos += 1
                continue
            number = _NUMBER.match(text, pos)
            if number is None:
                return False
            pos = number.end()
            operand = False
        elif char == ")" and depth:
            depth -= 1
            pos += 1
        elif char in "+-*/×÷":
            binary_count += 1
            operand = True
            pos += 1
        else:
            return False
    return not operand and depth == 0 and binary_count > 0


def fallback_decision(user_text: str) -> GateDecision:
    """Classify with R04's closed whitelist, preserving retrieval text verbatim."""
    normalized = unicodedata.normalize("NFKC", user_text)
    greeting = (
        " ".join(normalized.split())
        .translate(
            str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        )
        .rstrip("!?.。！？")
    )
    if greeting in _GREETINGS:
        return GateDecision(False, None, "greeting")
    if _arithmetic(normalized):
        return GateDecision(False, None, "arithmetic")
    return GateDecision(True, user_text, "conservative_retrieve")


def parse_model_decision(raw: str) -> GateDecision:
    """Reject prose, duplicate keys, extra fields, and contradictory decisions."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid_gate_output")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except ValueError, TypeError, RecursionError:
        raise ValueError("invalid_gate_output") from None
    if not isinstance(value, dict) or set(value) != {
        "retrieve",
        "query",
        "reason_code",
    }:
        raise ValueError("invalid_gate_output")
    decision, query, reason = (value["retrieve"], value["query"], value["reason_code"])
    if type(decision) is not bool or not isinstance(reason, str):
        raise ValueError("invalid_gate_output")
    if decision:
        valid = (
            isinstance(query, str)
            and bool(query.strip())
            and reason
            in {"personal_information", "history_recall", "conservative_retrieve"}
        )
    else:
        valid = query is None and reason in {"greeting", "arithmetic"}
    if not valid:
        raise ValueError("invalid_gate_output")
    return GateDecision(decision, query, reason)


@dataclass(frozen=True)
class GateResult:
    evidence: dict[str, Any]
    reference_text: str | None
    selected_references: tuple[dict[str, Any], ...]
    failure_code: str | None = None
    reference_set: ReferenceSet | None = None


@dataclass(frozen=True)
class ReferenceSet:
    """Immutable selected input; edits remove records without a new retrieval.

    Serialized records retain the exact measured text. No omitted record or Store
    is retained here, so invalidation cannot promote or reload a replacement.
    """

    _items: tuple[tuple[str, str], ...]
    _partial: bool = False

    @property
    def selected_references(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {"kind": kind, "memory_id": item["id"], "record_version": item["version"]}
            for kind, encoded in self._items
            for item in (json.loads(encoded),)
        )

    @property
    def reference_text(self) -> str | None:
        if not self._items:
            return None
        marker = "本次仅提供部分召回\n" if self._partial else ""
        groups = [
            kind
            + "=["
            + ",".join(
                encoded for item_kind, encoded in self._items if item_kind == kind
            )
            + "]"
            for kind in ("semantic", "episodic")
        ]
        return "检索参考资料\n" + marker + "\n".join(groups)

    def invalidate(self, kind: str, memory_id: str) -> ReferenceSet:
        """Return the remaining input, preserving order and original versions."""
        if kind not in ("semantic", "episodic"):
            raise ValueError("invalid_memory_kind")
        remaining = tuple(
            (item_kind, encoded)
            for item_kind, encoded in self._items
            if item_kind != kind or json.loads(encoded)["id"] != memory_id
        )
        if remaining == self._items:
            return self
        return ReferenceSet(remaining, _partial=True)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _item(kind: str, record: Any) -> dict[str, Any]:
    from agent_alfred.memory.types import origin_json

    item = {"id": record.id, "version": record.record_version}
    if kind == "semantic":
        item.update(subject=record.subject, fact=record.fact)
    else:
        item.update(
            summary=record.summary,
            occurred_at=record.occurred_at.isoformat(),
            occurred_until=(
                record.occurred_until.isoformat() if record.occurred_until else None
            ),
        )
    item["origin"] = origin_json(record.origin)
    return item


def retrieve(
    decision: GateDecision,
    semantic_store: Any,
    episodic_store: Any,
    *,
    fallback_reason: str | None = None,
    gate_step_index: int | None = None,
    model_ref: dict[str, str] | None = None,
    model_ms: float | None = None,
    started_at: float | None = None,
    per_store_limit: int = 5,
    per_store_character_budget: int = 4000,
    clock: Any = None,
) -> GateResult:
    """Search in Store order and measure exactly the JSON passed to the model."""
    from time import monotonic

    from agent_alfred.memory.types import EpisodeQuery, FactQuery

    clock = clock or monotonic
    started_at = clock() if started_at is None else started_at
    for value in (per_store_limit, per_store_character_budget):
        if type(value) is not int or value <= 0:
            raise ValueError("invalid_memory_gate_config")
    stores = {
        kind: {"status": "not_queried", "hit_count": None, "error_code": None}
        for kind in ("semantic", "episodic")
    }
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "decision": decision.retrieve,
        "outcome": "skip",
        "decision_source": "deterministic_fallback" if fallback_reason else "model",
        "reason_code": decision.reason_code,
        "fallback_reason": fallback_reason,
        "rule_version": RULE_VERSION if fallback_reason else None,
        "gate_step_index": gate_step_index,
        "model_ref": (
            {key: model_ref[key] for key in ("endpoint_id", "model_id", "wire_style")}
            if model_ref is not None
            else None
        ),
        "latency_ms": 0.0,
        "timing": {"model_ms": model_ms, "search_ms": None, "selection_ms": None},
        "stores": stores,
        "hit_count": 0,
        "selected_count": 0,
        "references": [],
        "input_disposition": "not_needed",
    }
    if not decision.retrieve:
        evidence["latency_ms"] = max(0.0, (clock() - started_at) * 1000)
        return GateResult(evidence, None, ())
    search_start = clock()
    groups = {}
    for kind, store, query in (
        (
            "semantic",
            semantic_store,
            FactQuery(text=decision.query, limit=per_store_limit),
        ),
        (
            "episodic",
            episodic_store,
            EpisodeQuery(text=decision.query, limit=per_store_limit),
        ),
    ):
        try:
            hits = list(store.search(query))
            groups[kind] = [_item(kind, hit.record) for hit in hits]
        except Exception as exc:
            stores[kind].update(
                status="failed",
                error_code=(
                    "storage_unavailable"
                    if isinstance(exc, OSError)
                    else "storage_read_failed"
                ),
            )
            for successful_kind, items in groups.items():
                for rank, item in enumerate(items, 1):
                    evidence["references"].append(
                        {
                            "kind": successful_kind,
                            "memory_id": item["id"],
                            "record_version": item["version"],
                            "origin": item["origin"],
                            "rank": rank,
                            "selected": False,
                            "omission_reason": "store_error",
                        }
                    )
            evidence.update(
                outcome="error", hit_count=None, input_disposition="store_error"
            )
            evidence["timing"]["search_ms"] = max(0.0, (clock() - search_start) * 1000)
            evidence["latency_ms"] = max(0.0, (clock() - started_at) * 1000)
            return GateResult(evidence, None, (), "memory_storage_error")
        stores[kind].update(status="succeeded", hit_count=len(hits))
        from agent_alfred.memory.chinese_recall import METHOD

        if any(hit.relevance == METHOD for hit in hits):
            stores[kind]["retrieval_method"] = METHOD
    evidence["timing"]["search_ms"] = max(0.0, (clock() - search_start) * 1000)
    selection_start = clock()
    selected_items, selected = [], []
    for kind, items in groups.items():
        prefix: list[dict[str, Any]] = []
        stopped = False
        for rank, item in enumerate(items, 1):
            reason = None
            if stopped:
                reason = "after_prefix_stop"
            elif len(_json([*prefix, item])) > per_store_character_budget:
                stopped = True
                reason = "character_budget"
            else:
                prefix.append(item)
                selected_items.append((kind, _json(item)))
                selected.append(
                    {
                        "kind": kind,
                        "memory_id": item["id"],
                        "record_version": item["version"],
                    }
                )
            evidence["references"].append(
                {
                    "kind": kind,
                    "memory_id": item["id"],
                    "record_version": item["version"],
                    "origin": item["origin"],
                    "rank": rank,
                    "selected": reason is None,
                    "omission_reason": reason,
                }
            )
    hit_count = sum(len(items) for items in groups.values())
    disposition = "partial" if len(selected) < hit_count else "ready"
    evidence.update(
        outcome="hit",
        hit_count=hit_count,
        selected_count=len(selected),
        input_disposition=disposition,
    )
    evidence["timing"]["selection_ms"] = max(0.0, (clock() - selection_start) * 1000)
    evidence["latency_ms"] = max(0.0, (clock() - started_at) * 1000)
    if not hit_count:
        evidence.update(outcome="miss", input_disposition="no_hits")
        return GateResult(evidence, None, ())
    if not selected:
        evidence["input_disposition"] = "all_excluded"
        return GateResult(evidence, None, (), "memory_input_unavailable")
    reference_set = ReferenceSet(tuple(selected_items), disposition == "partial")
    return GateResult(
        evidence,
        reference_set.reference_text,
        tuple(selected),
        reference_set=reference_set,
    )
