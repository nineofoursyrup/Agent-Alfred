"""Public source service: bounded real reads and automatic-consumption permission."""

import json
from collections.abc import Mapping

from agent_alfred.aggregation import KINDS
from agent_alfred.memory.types import EpisodeQuery, FactQuery
from agent_alfred.messages import TextBlock
from agent_alfred.runtime.memory import (
    InputDeadlineExceeded,
    InputEvidenceError,
    InputResolutionError,
)
from agent_alfred.stream_fallback import OverallDeadlineExceeded
from agent_alfred.tools import Tool, ToolFailure, ToolSuccess
from agent_alfred.tools.metering import MeteringError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def numbered_sources(items, *, project=None):
    """Measure the Registry projection; add labels only after that projection."""
    counts = {k: 0 for k in KINDS}
    numbered = []
    for item in items:
        kind = item["kind"]
        counts[kind] += 1
        label = dict(semantic="S", episodic="E", history="H")[kind] + str(counts[kind])
        projected = project(item) if project is not None else item
        numbered.append(dict(projected, label=label))
    return numbered


class AggregationSources:
    def __init__(self, service, recording_store, *, project=None):
        self.service, self.store = service, recording_store
        self.project = project

    def read(self, kind, request, context):
        context.checkpoint()
        if kind == "history":
            return self.history(request)
        limit = request["limits"]["per_store_limit"]
        with self.service.reading_stores() as stores:
            store = stores[0 if kind == "semantic" else 1]
            query = (
                FactQuery(text=request["keywords"], limit=limit)
                if kind == "semantic"
                else EpisodeQuery(text=request["keywords"], limit=limit)
            )
            hits = store.search(query)
            identities = tuple(
                (kind, str(h.record.id), h.record.record_version) for h in hits
            )
            permitted = self.service.forgetting.evaluate_automatic_records(
                identities, connection=store._conn
            )
            if "error" in permitted:
                raise InputEvidenceError("source_permission_unavailable")
            allowed = set(permitted["allowed"])
            items = []
            for hit in hits:
                record = hit.record
                identity = (kind, str(record.id), record.record_version)
                if identity not in allowed:
                    continue
                content = (
                    dict(subject=record.subject, fact=record.fact)
                    if kind == "semantic"
                    else dict(
                        summary=record.summary,
                        occurred_at=record.occurred_at.isoformat(),
                        ended_at=record.occurred_until.isoformat()
                        if record.occurred_until
                        else None,
                    )
                )
                items.append(
                    dict(
                        kind=kind,
                        memory_id=str(record.id),
                        record_version=record.record_version,
                        content=content,
                    )
                )
        approved = len(items)
        budget = request["limits"]["per_store_character_budget"]
        kept = []
        for item in items:
            if (
                len(encoded(numbered_sources([*kept, item], project=self.project)))
                <= budget
            ):
                kept.append(item)
        return dict(
            schema_version=1,
            kind=kind,
            items=kept,
            candidate_count=len(hits),
            approved_count=approved,
            permission_excluded=len(hits) - approved,
            capacity_excluded=approved - len(kept),
            remaining="unknown" if len(hits) == limit else "none",
        )

    def history(self, request):
        with self.store.reading() as conn:
            rows = conn.execute(
                """SELECT r.run_id,u.content,a.content FROM runs r
                JOIN agent_log u ON u.run_id=r.run_id AND u.role='user'
                JOIN agent_log a ON a.run_id=r.run_id AND a.role='assistant'
                WHERE r.session_id=? AND r.purpose='chat' AND r.phase='finished'
                AND r.outcome='completed' AND r.admission_state='admitted'
                AND u.session_id=r.session_id AND a.session_id=r.session_id
                ORDER BY u.id DESC""",
                (request["session_id"],),
            ).fetchall()
            permitted = self.service.forgetting.evaluate_history(
                [r[0] for r in rows], purpose="working_window", connection=conn
            )
            if "error" in permitted:
                raise InputEvidenceError("source_permission_unavailable")
            allowed = set(permitted["allowed"])
            usable = [r for r in rows if r[0] in allowed]
            selected = usable[: request["limits"]["working_memory_rounds"]]
            items = [
                dict(
                    kind="history",
                    run_id=r[0],
                    session_id=request["session_id"],
                    content=dict(user=json.loads(r[1]), assistant=json.loads(r[2])),
                )
                for r in reversed(selected)
            ]
        return dict(
            schema_version=1,
            kind="history",
            items=items,
            candidate_count=len(rows),
            approved_count=len(usable),
            permission_excluded=len(rows) - len(usable),
            capacity_excluded=0,
            round_excluded=len(usable) - len(selected),
            remaining="none",
        )


def validate_source(value, kind, request, *, project=None):
    """Validate before a source key can enter the graph's committed snapshot."""
    if (
        not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("kind") != kind
    ):
        raise ValueError("invalid_source_result")
    fields = {
        "schema_version",
        "kind",
        "items",
        "candidate_count",
        "approved_count",
        "permission_excluded",
        "capacity_excluded",
        "remaining",
    }
    if kind == "history":
        fields.add("round_excluded")
    if set(value) != fields:
        raise ValueError("invalid_source_result")
    items = value.get("items")
    limit = request["limits"][
        "working_memory_rounds" if kind == "history" else "per_store_limit"
    ]
    if not isinstance(items, (tuple, list)) or len(items) > limit:
        raise ValueError("invalid_source_result")
    for name in (
        "candidate_count",
        "approved_count",
        "permission_excluded",
        "capacity_excluded",
    ):
        if type(value.get(name)) is not int or value[name] < 0:
            raise ValueError("invalid_source_result")
    rounds = value.get("round_excluded", 0)
    if (
        type(rounds) is not int
        or rounds < 0
        or value["candidate_count"]
        != value["approved_count"] + value["permission_excluded"]
        or value["approved_count"] != len(items) + value["capacity_excluded"] + rounds
    ):
        raise ValueError("invalid_source_result")
    if value.get("remaining") not in ("none", "unknown"):
        raise ValueError("invalid_source_result")
    seen = set()
    for item in items:
        if (
            not isinstance(item, Mapping)
            or item.get("kind") != kind
            or not isinstance(item.get("content"), Mapping)
        ):
            raise ValueError("invalid_source_result")
        key = item.get("run_id" if kind == "history" else "memory_id")
        if type(key) is not str or not key or key in seen:
            raise ValueError("invalid_source_result")
        seen.add(key)
        expected = (
            {"kind", "content", "run_id", "session_id"}
            if kind == "history"
            else {"kind", "content", "memory_id", "record_version"}
        )
        content_fields = (
            {"user", "assistant"}
            if kind == "history"
            else (
                {"subject", "fact"}
                if kind == "semantic"
                else {"summary", "occurred_at", "ended_at"}
            )
        )
        if set(item) != expected or set(item["content"]) != content_fields:
            raise ValueError("invalid_source_result")
        if kind == "history":
            from agent_alfred.messages import blocks_from_jsonable, blocks_to_jsonable

            if item.get("session_id") != request["session_id"]:
                raise ValueError("invalid_source_result")
            for role in ("user", "assistant"):
                raw = item["content"][role]
                if not isinstance(raw, (list, tuple)) or not all(
                    isinstance(block, Mapping) for block in raw
                ):
                    raise ValueError("invalid_source_result")
                # The legacy decoder deliberately coerces some fields. This
                # source boundary requires exact types, including nested blocks.
                canonical = blocks_to_jsonable(blocks_from_jsonable(raw))
                if encoded(canonical) != encoded(raw):
                    raise ValueError("invalid_source_result")
        else:
            if (
                type(item.get("record_version")) is not int
                or item["record_version"] < 1
            ):
                raise ValueError("invalid_source_result")
            fields = (
                ("subject", "fact")
                if kind == "semantic"
                else ("summary", "occurred_at")
            )
            if any(type(item["content"].get(f)) is not str for f in fields):
                raise ValueError("invalid_source_result")
            if kind == "episodic":
                from datetime import datetime

                begin = datetime.fromisoformat(item["content"]["occurred_at"])
                end = item["content"]["ended_at"]
                if end is not None and type(end) is not str:
                    raise ValueError("invalid_source_result")
                end = datetime.fromisoformat(end) if end is not None else None
                if begin.utcoffset() is None or (
                    end is not None and (end.utcoffset() is None or end < begin)
                ):
                    raise ValueError("invalid_source_result")
    if (
        kind != "history"
        and items
        and len(encoded(numbered_sources(items, project=project)))
        > request["limits"]["per_store_character_budget"]
    ):
        raise ValueError("invalid_source_result")
    return value


def declarations(service, *, project=None):
    def read(kind, request, context):
        try:
            value = service.read(kind, request, context)
        except MeteringError:
            raise
        except InputDeadlineExceeded, OverallDeadlineExceeded:
            return ToolFailure(
                "execution_error",
                (TextBlock("input_deadline"),),
                stop_reason="aggregation_input_deadline",
            )
        except InputEvidenceError, InputResolutionError:
            return ToolFailure(
                "execution_error",
                (TextBlock("input_evidence_unavailable"),),
                stop_reason="aggregation_input_unavailable",
            )
        except TimeoutError:
            return ToolFailure("timeout", (TextBlock("local_timeout"),))
        except Exception:
            return ToolFailure("execution_error", (TextBlock("read_failed"),))
        try:
            value = validate_source(value, kind, request, project=project)
        except ValueError, KeyError, TypeError:
            return ToolFailure("execution_error", (TextBlock("invalid_source_result"),))
        # Managed summaries hold counts only; source bodies exist solely in the
        # bounded execution payload and original model trace, never a new cache.
        return ToolSuccess(
            (TextBlock(encoded(dict(kind=kind, count=len(value["items"])))),),
            structured=value,
        )

    return tuple(
        Tool(
            "aggregation_" + k,
            "Read selected " + k + " source",
            {"type": "object", "additionalProperties": True},
            lambda args, context, kind=k: read(kind, args, context),
            "local_read",
        )
        for k in KINDS
    )
