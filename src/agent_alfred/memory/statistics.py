"""Read durable chat retrieval evidence using one explicit UTC time range."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from agent_alfred.memory.evidence import valid_gate_evidence
from agent_alfred.memory.storage import instant


def summarize_statistics(rows, *, since: datetime, until: datetime) -> dict:
    since, until = instant(since), instant(until)
    if until < since:
        raise ValueError("invalid_input")
    counts = dict.fromkeys(("skip", "hit", "miss", "error"), 0)
    excluded = dict.fromkeys(
        ("unrecorded", "not_evaluated", "incomplete", "legacy_unknown"), 0
    )
    for row in rows:
        if row["recording_state"] != "recorded":
            excluded["unrecorded"] += 1
            continue
        telemetry = row["telemetry"]
        if isinstance(telemetry, str):
            try:
                telemetry = json.loads(telemetry)
            except ValueError, TypeError:
                telemetry = None
        memory = telemetry.get("memory") if isinstance(telemetry, dict) else None
        state = memory.get("gate_state") if isinstance(memory, dict) else None
        if state in ("not_evaluated", "incomplete", "legacy_unknown"):
            excluded[state] += 1
            continue
        gate = memory.get("gate") if isinstance(memory, dict) else None
        if state == "evaluated" and valid_gate_evidence(gate):
            counts[gate["outcome"]] += 1
        else:
            excluded["legacy_unknown"] += 1
    s, h, m, e = (counts[name] for name in ("skip", "hit", "miss", "error"))

    def ratio(numerator, denominator):
        return {
            "numerator": numerator,
            "denominator": denominator,
            "ratio": numerator / denominator if denominator else None,
        }

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "counts": counts,
        "excluded": excluded,
        "skip": ratio(s, s + h + m),
        "hit": ratio(h, h + m),
        "error": ratio(e, s + h + m + e),
    }


def read_statistics(
    recording_store,
    *,
    since=None,
    until=None,
    session_id=None,
    clock=lambda: datetime.now(UTC),
) -> dict:
    anchor = instant(clock()) if until is None else instant(until)
    start = anchor - timedelta(days=7) if since is None else instant(since)
    if start > anchor:
        raise ValueError("invalid_input")
    with recording_store.reading() as conn:
        cursor = conn.execute(
            """SELECT CASE WHEN telemetry IS NOT NULL OR EXISTS
                (SELECT 1 FROM agent_log WHERE agent_log.run_id=runs.run_id)
                THEN 'recorded' ELSE 'unrecorded' END,telemetry,accepted_at
            FROM runs WHERE purpose='chat' AND (? IS NULL OR session_id=?)""",
            (session_id, session_id),
        )
        rows = []
        for state, telemetry, accepted_at in cursor:
            at = instant(datetime.fromisoformat(accepted_at))
            if start <= at < anchor:
                rows.append({"recording_state": state, "telemetry": telemetry})
    return {
        **summarize_statistics(rows, since=start, until=anchor),
        "session_id": session_id,
    }
