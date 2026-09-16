"""Extract protected diagnostic rows from one source snapshot transaction."""

from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from agent_alfred.database_console.budget import (
    PROTECTED_INPUT_LIMIT,
    SOURCE_ROW_LIMIT,
    SOURCE_VALUE_LIMIT,
    object_meta_bytes,
    row_bytes,
    value_bytes,
)
from agent_alfred.database_console.catalog import (
    OBJECT_BY_NAME,
    OBJECTS,
    Column,
    DiagObject,
    manifest,
)
from agent_alfred.database_console.errors import ConsoleError
from agent_alfred.database_console.sqlite_limits import memory_temp_store
from agent_alfred.redact import Redactor

METERING_REASONS = frozenset({"control_interrupted", "interrupted_before_dispatch"})
COST_REASONS = frozenset(
    {
        "http_not_sent",
        "transport_not_sent",
        "not_reported",
        "undeclared_metering",
        "invalid_units",
        "invalid_persisted_metering",
    }
)


class Budget:
    def __init__(self) -> None:
        self.used = 0

    def add(self, amount: int) -> None:
        if amount < 0 or self.used + amount > PROTECTED_INPUT_LIMIT:
            raise ConsoleError("input_too_large")
        self.used += amount


def _utf8(value: str) -> str:
    value.encode("utf-8").decode("utf-8")
    return value


def _protect(redactor: Redactor, value: object, *, identity: bool) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    _utf8(value)
    if len(value.encode("utf-8")) > SOURCE_VALUE_LIMIT:
        raise ConsoleError("input_too_large")
    protected = redactor.redact_text(value)
    if identity and protected != value:
        raise ConsoleError("protected_identity")
    return protected


def _map_reason(value: object, allowed: frozenset[str], other: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ConsoleError("data_invalid")
    if value == "":
        return None
    if value in allowed:
        return value
    return other


def _origin(raw: object, redactor: Redactor) -> tuple[object, object, object, object]:
    if raw is None:
        return None, None, None, None
    if type(raw) is not str:
        raise ConsoleError("data_invalid")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConsoleError("data_invalid") from None
    if not isinstance(data, dict) or type(data.get("type")) is not str:
        raise ConsoleError("data_invalid")
    kind = data["type"]
    if kind == "manual":
        source = data.get("source")
        if source not in ("cli", "web"):
            raise ConsoleError("data_invalid")
        return "manual", _protect(redactor, source, identity=False), None, None
    if kind == "tool":
        call_id = data.get("call_id")
        if type(call_id) is not str:
            raise ConsoleError("data_invalid")
        return "tool", None, _protect(redactor, call_id, identity=True), None
    if kind == "consolidation":
        batch_id = data.get("batch_id")
        if type(batch_id) is not str:
            raise ConsoleError("data_invalid")
        return "consolidation", None, None, _protect(redactor, batch_id, identity=True)
    raise ConsoleError("data_invalid")


def _provenance(
    conn: sqlite3.Connection, kind: str, memory_id: str, version: int
) -> str:
    row = next(
        _source_rows(
            conn,
            "SELECT state FROM memory_provenance "
            "WHERE kind=? AND memory_id=? AND record_version=?",
            (kind, memory_id, version),
        ),
        None,
    )
    if row is None:
        return "unknown"
    if row[0] not in ("known", "known_none", "unknown"):
        raise ConsoleError("data_invalid")
    return row[0]


def _message_text(content: object, redactor: Redactor) -> str:
    if type(content) is not str:
        raise ConsoleError("data_invalid")
    try:
        parsed = json.loads(content)
    except ValueError:
        raise ConsoleError("data_invalid") from None
    if not isinstance(parsed, list):
        raise ConsoleError("data_invalid")
    parts: list[str] = []
    for item in parsed:
        if not isinstance(item, dict) or type(item.get("type")) is not str:
            raise ConsoleError("data_invalid")
        kind = item["type"]
        if kind == "text":
            if type(item.get("text")) is not str:
                raise ConsoleError("data_invalid")
            parts.append(str(_protect(redactor, item["text"], identity=False)))
        elif kind == "thinking":
            if type(item.get("text")) is not str:
                raise ConsoleError("data_invalid")
            signature = item.get("signature")
            if signature is not None and type(signature) is not str:
                raise ConsoleError("data_invalid")
            _utf8(item["text"])
            if signature is not None:
                _utf8(signature)
            continue
        elif kind == "tool_call":
            if any(type(item.get(key)) is not str for key in ("id", "name")):
                raise ConsoleError("data_invalid")
            _utf8(item["id"])
            _utf8(item["name"])
            raw_input = item.get("input")
            if not isinstance(raw_input, dict):
                raise ConsoleError("data_invalid")
            continue
        elif kind == "tool_result":
            if type(item.get("call_id")) is not str:
                raise ConsoleError("data_invalid")
            _utf8(item["call_id"])
            content = item.get("content")
            if not isinstance(content, list) or any(
                not isinstance(part, dict)
                or part.get("type") != "text"
                or type(part.get("text")) is not str
                for part in content
            ):
                raise ConsoleError("data_invalid")
            for part in content:
                _utf8(part["text"])
            if "is_error" in item and type(item["is_error"]) is not bool:
                raise ConsoleError("data_invalid")
            continue
        else:
            raise ConsoleError("data_invalid")
    return "".join(parts)


def _token(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or type(value) is not int or value < 0:
        raise ConsoleError("data_invalid")
    return value


def _money(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConsoleError("data_invalid")
    try:
        if type(value) is int:
            amount = Decimal(value)
        elif type(value) is str:
            amount = Decimal(value)
        else:
            raise ConsoleError("data_invalid")
        if not amount.is_finite() or amount < 0:
            raise ConsoleError("data_invalid")
    except InvalidOperation, ValueError:
        raise ConsoleError("data_invalid") from None
    if max(amount.adjusted() + 1, -amount.as_tuple().exponent) > SOURCE_VALUE_LIMIT:
        raise ConsoleError("input_too_large")
    return format(amount, "f")


def _cost_projection(
    raw: object, source_id: object
) -> tuple[str, object, object, object, object, object]:
    if type(raw) is not str:
        raise ConsoleError("data_invalid")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConsoleError("data_invalid") from None
    if not isinstance(data, dict) or type(data.get("kind")) is not str:
        raise ConsoleError("data_invalid")
    kind = data["kind"]
    if kind in {"not_billable", "unknown", "unrecorded"}:
        reason = _map_reason(data.get("reason"), COST_REASONS, "other_recorded_reason")
        return kind, None, None, None, None, reason
    if kind != "reported":
        raise ConsoleError("data_invalid")
    units = _money(data.get("units"))
    unit = data.get("unit")
    source = data.get("source")
    service = data.get("service")
    if (
        units is None
        or type(unit) is not str
        or not unit
        or type(source) is not str
        or not source
        or type(service) is not str
        or not service
        or service != source_id
    ):
        raise ConsoleError("data_invalid")
    return "reported", units, unit, source, service, None


def _source_rows(source, sql, params=()):
    """Check every raw value and framed row before transferring content to Python.

    Both passes run inside the same source transaction. Only trusted extraction
    SQL reaches this helper; user SQL is confined to the independent dataset.
    """
    cursor = source.execute(f"SELECT * FROM ({sql}) LIMIT 0", params)
    names = [item[0] for item in cursor.description]
    cursor.close()
    sizes = []
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        sizes.append(
            f"CASE typeof({quoted}) WHEN 'null' THEN 0 "
            f"WHEN 'integer' THEN 8 WHEN 'real' THEN 8 "
            f"ELSE length(CAST({quoted} AS BLOB)) END"
        )
    for lengths in source.execute(f"SELECT {', '.join(sizes)} FROM ({sql})", params):
        if any(size > SOURCE_VALUE_LIMIT for size in lengths):
            raise ConsoleError("input_too_large")
        if 16 + 8 * len(lengths) + sum(lengths) > SOURCE_ROW_LIMIT:
            raise ConsoleError("input_too_large")
    yield from source.execute(sql, params)


def _typed(column: Column, value: object, redactor: Redactor) -> object:
    if value is None:
        if not column.nullable:
            raise ConsoleError("data_invalid")
        return None
    if isinstance(value, bool):
        raise ConsoleError("data_invalid")
    expected = column.sql_type
    if expected == "INTEGER":
        if type(value) is not int or (column.flag and value not in (0, 1)):
            raise ConsoleError("data_invalid")
        return value
    if expected == "REAL":
        if type(value) is not float:
            raise ConsoleError("data_invalid")
        return value
    if expected == "BLOB":
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ConsoleError("data_invalid")
        raw = bytes(value)
        if len(raw) > SOURCE_VALUE_LIMIT:
            raise ConsoleError("input_too_large")
        return raw
    if not isinstance(value, str):
        raise ConsoleError("data_invalid")
    _utf8(value)
    return _protect(redactor, value, identity=column.identity)


def _insert(
    dataset: sqlite3.Connection,
    spec: DiagObject,
    values: tuple[object, ...],
    budget: Budget,
    redactor: Redactor,
) -> None:
    if len(values) != len(spec.columns):
        raise ConsoleError("data_invalid")
    checked: list[object] = []
    for column, value in zip(spec.columns, values, strict=True):
        checked.append(_typed(column, value, redactor))
    payload = tuple(checked)
    size = row_bytes(payload)
    if size > SOURCE_ROW_LIMIT:
        raise ConsoleError("input_too_large")
    budget.add(size)
    placeholders = ",".join("?" * len(payload))
    dataset.execute(
        f"INSERT INTO {spec.name} ({','.join(spec.column_names)}) "
        f"VALUES ({placeholders})",
        payload,
    )


def _protect_row(
    spec: DiagObject, raw: tuple[object, ...], redactor: Redactor
) -> tuple[object, ...]:
    out = []
    for column, value in zip(spec.columns, raw, strict=True):
        if isinstance(value, str):
            out.append(_protect(redactor, value, identity=column.identity))
        else:
            out.append(value)
    return tuple(out)


def _copy_simple(
    source: sqlite3.Connection,
    dataset: sqlite3.Connection,
    spec: DiagObject,
    sql: str,
    redactor: Redactor,
    budget: Budget,
    params: tuple[object, ...] = (),
) -> None:
    budget.add(object_meta_bytes(spec.name, spec.column_names))
    for row in _source_rows(source, sql, params):
        _insert(dataset, spec, tuple(row), budget, redactor)


def _sessions(source, dataset, spec, redactor, budget):
    _copy_simple(
        source,
        dataset,
        spec,
        "SELECT session_id, created_at, activity_revision FROM sessions",
        redactor,
        budget,
    )


def _runs(source, dataset, spec, redactor, budget):
    _copy_simple(
        source,
        dataset,
        spec,
        "SELECT run_id, purpose, session_id, gateway, "
        "entry_surface_id, phase, outcome, "
        "accepted_at, started_at, finished_at, "
        "activity_revision, admission_state FROM runs",
        redactor,
        budget,
    )


def _messages(source, dataset, spec, redactor, budget):
    budget.add(object_meta_bytes(spec.name, spec.column_names))
    for row in _source_rows(
        source,
        "SELECT id, session_id, run_id, role, source, created_at, "
        "consolidated, content "
        "FROM agent_log WHERE role IN ('user','assistant')",
    ):
        text = _message_text(row[7], redactor)
        values = (
            row[0],
            _protect(redactor, row[1], identity=True),
            _protect(redactor, row[2], identity=True),
            row[3],
            row[4],
            row[5],
            row[6],
            text,
        )
        _insert(dataset, spec, values, budget, redactor)


def _attempts_and_coverage(
    source, dataset, attempts_spec, coverage_spec, redactor, budget, wanted
):
    if attempts_spec is not None:
        budget.add(object_meta_bytes(attempts_spec.name, attempts_spec.column_names))
    if coverage_spec is not None:
        budget.add(object_meta_bytes(coverage_spec.name, coverage_spec.column_names))
    in_progress = 0
    summary = {
        "recorded": 0,
        "recorded_empty": 0,
        "unrecorded": 0,
        "in_progress_runs": 0,
    }
    for run_id, phase, telemetry in _source_rows(
        source, "SELECT run_id, phase, telemetry FROM runs"
    ):
        if isinstance(telemetry, str) and value_bytes(telemetry) > SOURCE_VALUE_LIMIT:
            raise ConsoleError("input_too_large")
        if isinstance(telemetry, (bytes, bytearray)):
            raise ConsoleError("data_invalid")
        if phase not in {"accepted", "running", "finished"}:
            raise ConsoleError("data_invalid")
        run_id = _protect(redactor, run_id, identity=True)
        finished = 1 if phase == "finished" else 0
        if phase in {"accepted", "running"}:
            in_progress += 1
        state, count, items = _attempt_rows(telemetry)
        summary[state] += 1
        if coverage_spec is not None:
            _insert(
                dataset,
                coverage_spec,
                (run_id, state, count, finished),
                budget,
                redactor,
            )
        if attempts_spec is not None:
            for item in items:
                _insert(
                    dataset,
                    attempts_spec,
                    (
                        run_id,
                        _protect(redactor, item["attempt_id"], identity=True),
                        item["outcome"],
                        _protect(redactor, item["endpoint_id"], identity=True)
                        if item["endpoint_id"] is not None
                        else None,
                        _protect(redactor, item["model_id"], identity=False)
                        if item["model_id"] is not None
                        else None,
                        item["total_input_tokens"],
                        item["uncached_input_tokens"],
                        item["cache_read_tokens"],
                        item["cache_write_tokens"],
                        item["output_tokens"],
                        item["reasoning_tokens"],
                        item["endpoint_reported_cost_usd"],
                    ),
                    budget,
                    redactor,
                )
    summary["in_progress_runs"] = in_progress
    del wanted
    return summary


def _attempt_rows(telemetry: object) -> tuple[str, int | None, list[dict[str, Any]]]:
    if telemetry is None:
        return "unrecorded", None, []
    if type(telemetry) is not str:
        raise ConsoleError("data_invalid")
    try:
        data = json.loads(telemetry)
    except ValueError:
        raise ConsoleError("data_invalid") from None
    if not isinstance(data, dict):
        raise ConsoleError("data_invalid")
    if "attempts" not in data:
        return "unrecorded", None, []
    attempts = data["attempts"]
    if attempts is None:
        raise ConsoleError("data_invalid")
    if not isinstance(attempts, list):
        raise ConsoleError("data_invalid")
    if attempts == []:
        return "recorded_empty", 0, []
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in attempts:
        if (
            not isinstance(item, dict)
            or type(item.get("attempt_id")) is not str
            or not item["attempt_id"]
        ):
            raise ConsoleError("data_invalid")
        attempt_id = item["attempt_id"]
        if attempt_id in seen:
            raise ConsoleError("data_invalid")
        seen.add(attempt_id)
        model = item.get("model")
        if model is None:
            endpoint_id = None
            model_id = None
        elif isinstance(model, dict):
            endpoint_id = model.get("endpoint_id")
            model_id = model.get("model_id")
            if (endpoint_id is None) != (model_id is None):
                raise ConsoleError("data_invalid")
            if endpoint_id is not None and (
                type(endpoint_id) is not str or type(model_id) is not str
            ):
                raise ConsoleError("data_invalid")
        else:
            raise ConsoleError("data_invalid")
        outcome = item.get("outcome")
        if outcome not in (None, "committed", "aborted"):
            raise ConsoleError("data_invalid")
        usage = item.get("usage")
        if usage is None:
            tokens = {
                name: None
                for name in (
                    "total_input_tokens",
                    "uncached_input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                )
            }
            cost = None
        elif isinstance(usage, dict):
            tokens = {
                name: _token(usage.get(name))
                for name in (
                    "total_input_tokens",
                    "uncached_input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                )
            }
            cost = _money(usage.get("endpoint_reported_cost_usd"))
        else:
            raise ConsoleError("data_invalid")
        rows.append(
            {
                "attempt_id": attempt_id,
                "outcome": outcome,
                "endpoint_id": endpoint_id,
                "model_id": model_id,
                **tokens,
                "endpoint_reported_cost_usd": cost,
            }
        )
    return "recorded", len(rows), rows


def _memory_kind(
    source, dataset, spec, redactor, budget, table, kind, extra_cols, extra_select
):
    budget.add(object_meta_bytes(spec.name, spec.column_names))
    sql = (
        f"SELECT id, record_version, {extra_select}, origin_kind, "
        f"origin_batch_id, origin_source, "
        f"origin_call_id, created_at, modified_at, human_protected, last_change_origin "
        f"FROM {table}"
    )
    for row in _source_rows(source, sql):
        origin = _origin(row[-1], redactor)
        head = [
            _protect(redactor, row[0], identity=True),
            row[1],
        ]
        extras = []
        for column, value in zip(extra_cols, row[2 : 2 + len(extra_cols)], strict=True):
            identity = OBJECT_BY_NAME[spec.name].identity_columns
            extras.append(
                _protect(redactor, value, identity=column in identity)
                if isinstance(value, str)
                else value
            )
        rest = row[2 + len(extra_cols) : -1]
        values = (
            *head,
            *extras,
            rest[0],
            _protect(redactor, rest[1], identity=True),
            rest[2],
            _protect(redactor, rest[3], identity=True),
            rest[4],
            rest[5],
            rest[6],
            *origin,
            _provenance(source, kind, row[0], row[1]),
        )
        _insert(dataset, spec, values, budget, redactor)


def _metering(source, dataset, spec, redactor, budget):
    budget.add(object_meta_bytes(spec.name, spec.column_names))
    for row in _source_rows(
        source,
        "SELECT run_id, step_index, call_id, ordinal, tool_name, "
        "source_id, capability_id, "
        "effect, requested_at, start_confirmation, result, reason, "
        "finished_at, operation_id, "
        "model_delivery, cost FROM tool_metering",
    ):
        cost = _cost_projection(row[15], row[5])
        values = (
            _protect(redactor, row[0], identity=True),
            row[1],
            _protect(redactor, row[2], identity=True),
            row[3],
            _protect(redactor, row[4], identity=False),
            _protect(redactor, row[5], identity=True),
            _protect(redactor, row[6], identity=True),
            row[7],
            row[8],
            row[9],
            row[10],
            _map_reason(row[11], METERING_REASONS, "other_recorded_reason"),
            row[12],
            _protect(redactor, row[13], identity=True),
            row[14],
            *cost,
        )
        _insert(dataset, spec, values, budget, redactor)


def _operation_state(source, operation_id):
    pending_scope = failed = pending_cleanup = False
    for (state,) in _source_rows(
        source, "SELECT state FROM forget_scopes WHERE operation_id=?", (operation_id,)
    ):
        if state not in {"pending", "confirmed", "excluded"}:
            raise ConsoleError("data_invalid")
        pending_scope |= state == "pending"
    for (state,) in _source_rows(
        source, "SELECT state FROM forget_cleanup WHERE operation_id=?", (operation_id,)
    ):
        if state not in {"pending", "running", "failed", "complete"}:
            raise ConsoleError("data_invalid")
        failed |= state == "failed"
        pending_cleanup |= state != "complete"
    for evidence, error in _source_rows(
        source,
        "SELECT evidence_id,error FROM forget_projection_recovery WHERE operation_id=?",
        (operation_id,),
    ):
        if any(
            value is not None and type(value) is not str for value in (evidence, error)
        ):
            raise ConsoleError("data_invalid")
        failed |= error is not None
        pending_cleanup |= evidence is None
    for _row in _source_rows(
        source,
        "SELECT 1 FROM forget_projection_unknown WHERE operation_id=?",
        (operation_id,),
    ):
        pending_cleanup = True
    if pending_scope:
        return "needs_scope"
    if failed:
        return "failed"
    return "cleaning" if pending_cleanup else "complete"


def _forget_ops(source, dataset, spec, redactor, budget):
    budget.add(object_meta_bytes(spec.name, spec.column_names))
    for row in _source_rows(
        source,
        "SELECT operation_id, kind, memory_id, completeness, "
        "created_at FROM forget_operations",
    ):
        state = _operation_state(source, row[0])
        _insert(
            dataset,
            spec,
            (
                _protect(redactor, row[0], identity=True),
                row[1],
                _protect(redactor, row[2], identity=True),
                row[3],
                row[4],
                state,
            ),
            budget,
            redactor,
        )


def _mapped_error(source, dataset, spec, redactor, budget, sql, error_index, code):
    budget.add(object_meta_bytes(spec.name, spec.column_names))
    for row in _source_rows(source, sql):
        values = []
        for index, value in enumerate(row):
            column = spec.columns[index]
            if index == error_index:
                values.append(_map_reason(value, frozenset(), code))
            elif isinstance(value, str):
                values.append(_protect(redactor, value, identity=column.identity))
            else:
                values.append(value)
        _insert(dataset, spec, tuple(values), budget, redactor)


EXTRACTORS: dict[str, Callable[..., Any]] = {}


def _mid_extract(barriers: dict[str, str] | None, extracted: list[int]) -> None:
    extracted[0] += 1
    if extracted[0] != 1 or not barriers:
        return
    path = barriers.get("mid_extract")
    if not path:
        return
    ready = path + ".ready"
    if os.path.exists(ready):
        os.close(os.open(ready, os.O_WRONLY))
    os.close(os.open(path, os.O_RDONLY))


def fill(
    source: sqlite3.Connection,
    dataset: sqlite3.Connection,
    names: tuple[str, ...],
    redactor: Redactor,
    barriers: dict[str, str] | None = None,
) -> dict[str, Any]:
    budget = Budget()
    coverage = {
        "attempts": {
            "recorded": 0,
            "recorded_empty": 0,
            "unrecorded": 0,
            "in_progress_runs": 0,
        },
        "notice": "缺行不证明未发生请求",
    }
    wanted = set(names)
    budget.add(
        len(
            json.dumps(
                [item for item in manifest() if item["name"] in wanted],
                ensure_ascii=False,
            ).encode("utf-8")
        )
    )
    extracted = [0]
    if "diag_attempts" in wanted or "diag_attempt_coverage" in wanted:
        summary = _attempts_and_coverage(
            source,
            dataset,
            OBJECT_BY_NAME["diag_attempts"] if "diag_attempts" in wanted else None,
            OBJECT_BY_NAME["diag_attempt_coverage"]
            if "diag_attempt_coverage" in wanted
            else None,
            redactor,
            budget,
            wanted,
        )
        coverage["attempts"] = summary
        wanted -= {"diag_attempts", "diag_attempt_coverage"}
        _mid_extract(barriers, extracted)
    for name in sorted(wanted):
        spec = OBJECT_BY_NAME[name]
        if name == "diag_sessions":
            _sessions(source, dataset, spec, redactor, budget)
        elif name == "diag_runs":
            _runs(source, dataset, spec, redactor, budget)
        elif name == "diag_messages":
            _messages(source, dataset, spec, redactor, budget)
        elif name == "diag_facts":
            _memory_kind(
                source,
                dataset,
                spec,
                redactor,
                budget,
                "facts",
                "semantic",
                ("subject", "fact"),
                "subject, fact",
            )
        elif name == "diag_episodes":
            _memory_kind(
                source,
                dataset,
                spec,
                redactor,
                budget,
                "episodes",
                "episodic",
                ("summary", "occurred_at", "occurred_until"),
                "summary, occurred_at, occurred_until",
            )
        elif name == "diag_memory_sources":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT kind, memory_id, record_version, "
                "source_group_id FROM memory_sources",
                redactor,
                budget,
            )
        elif name == "diag_history_groups":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT g.group_id, g.kind, g.container_id, g.evidence, t.occurred_at "
                "FROM history_groups g "
                "LEFT JOIN history_group_times t ON t.group_id=g.group_id",
                redactor,
                budget,
            )
        elif name == "diag_memory_uses":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT kind, memory_id, record_version, consumer, "
                "attempt_id, purpose FROM memory_uses",
                redactor,
                budget,
            )
        elif name == "diag_history_reads":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT source, consumer, attempt_id, purpose FROM history_reads",
                redactor,
                budget,
            )
        elif name == "diag_tool_metering":
            _metering(source, dataset, spec, redactor, budget)
        elif name == "diag_tool_ledger":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT id, tool_name, effect, status, call_id, "
                "run_id, session_id, created_at, updated_at "
                "FROM tool_ledger",
                redactor,
                budget,
            )
        elif name == "diag_tool_operation_links":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT run_id, step_index, call_id, ledger_id "
                "FROM external_tool_operations",
                redactor,
                budget,
            )
        elif name == "diag_calendar_entries":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT id, title, starts_at, ends_at, iana_time_zone, "
                "participants, notes, created_at "
                "FROM calendar_entries",
                redactor,
                budget,
            )
        elif name == "diag_forget_operations":
            _forget_ops(source, dataset, spec, redactor, budget)
        elif name == "diag_forget_limits":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT operation_id, group_id, mode FROM forget_limits",
                redactor,
                budget,
            )
        elif name == "diag_forget_cleanup":
            _mapped_error(
                source,
                dataset,
                spec,
                redactor,
                budget,
                "SELECT operation_id, target_id, revision, state, error "
                "FROM forget_cleanup",
                4,
                "cleanup_error",
            )
        elif name == "diag_consolidation_batches":
            _mapped_error(
                source,
                dataset,
                spec,
                redactor,
                budget,
                "SELECT batch_id, session_id, revision, status, "
                "created_at, updated_at, finished_at, "
                "error_code, generation_run_id FROM memory_consolidation_batches",
                7,
                "consolidation_error",
            )
        elif name == "diag_consolidation_sources":
            _copy_simple(
                source,
                dataset,
                spec,
                "SELECT batch_id, revision, run_id, session_id, "
                "ordinal, accepted_at, finished_at "
                "FROM memory_consolidation_sources",
                redactor,
                budget,
            )
        elif name == "diag_memory_mirrors":
            _mapped_error(
                source,
                dataset,
                spec,
                redactor,
                budget,
                "SELECT target_id, required_generation, "
                "generated_generation, verified_generation, "
                "covered_cleanup_generation, generated_at, "
                "verified_at, conflict, error "
                "FROM memory_mirrors",
                8,
                "mirror_error",
            )
        else:
            raise ConsoleError("invalid_request")
        _mid_extract(barriers, extracted)
    budget.add(len(json.dumps(coverage, ensure_ascii=False).encode("utf-8")))
    return coverage


def open_source(path: str, identity: tuple[int, int]) -> sqlite3.Connection:
    stat = os.stat(path)
    if (stat.st_dev, stat.st_ino) != identity:
        raise ConsoleError("database_unavailable")
    conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise ConsoleError("database_unavailable")
        memory_temp_store(conn)
        journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal not in {"delete", "truncate", "persist", "memory", "wal", "off"}:
            raise ConsoleError("database_unavailable")
        conn.execute("PRAGMA busy_timeout=0")
        return conn
    except OSError, sqlite3.Error, ConsoleError:
        conn.close()
        raise ConsoleError("database_unavailable") from None


def open_dataset(uri: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(uri, uri=True)
    try:
        memory_temp_store(conn)
    except OSError, sqlite3.Error:
        conn.close()
        raise ConsoleError("database_unavailable") from None
    conn.execute("PRAGMA busy_timeout=0")
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    except AttributeError, sqlite3.Error:
        conn.close()
        raise ConsoleError("database_unavailable") from None
    for spec in OBJECTS:
        conn.execute(spec.create_sql)
    return conn
