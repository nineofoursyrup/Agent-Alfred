"""Required source dependencies, including columns used only by derived projections.

Only these structural declarations are checked. Business rows are checked when
and only when their diagnostic object is selected. Never migrate or repair.
"""

REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "sessions": {
        "session_id": "TEXT",
        "created_at": "TEXT",
        "activity_revision": "INTEGER",
    },
    "runs": {
        "run_id": "TEXT",
        "purpose": "TEXT",
        "session_id": "TEXT",
        "gateway": "TEXT",
        "entry_surface_id": "TEXT",
        "phase": "TEXT",
        "outcome": "TEXT",
        "accepted_at": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "activity_revision": "INTEGER",
        "telemetry": "TEXT",
        "admission_state": "TEXT",
    },
    "agent_log": {
        "id": "INTEGER",
        "session_id": "TEXT",
        "role": "TEXT",
        "content": "TEXT",
        "consolidated": "INTEGER",
        "source": "TEXT",
        "created_at": "TEXT",
        "run_id": "TEXT",
    },
    "facts": {
        "id": "TEXT",
        "record_version": "INTEGER",
        "origin_kind": "TEXT",
        "origin_batch_id": "TEXT",
        "origin_source": "TEXT",
        "origin_call_id": "TEXT",
        "created_at": "TEXT",
        "modified_at": "TEXT",
        "human_protected": "INTEGER",
        "last_change_origin": "TEXT",
        "subject": "TEXT",
        "fact": "TEXT",
    },
    "episodes": {
        "id": "TEXT",
        "record_version": "INTEGER",
        "origin_kind": "TEXT",
        "origin_batch_id": "TEXT",
        "origin_source": "TEXT",
        "origin_call_id": "TEXT",
        "created_at": "TEXT",
        "modified_at": "TEXT",
        "human_protected": "INTEGER",
        "last_change_origin": "TEXT",
        "summary": "TEXT",
        "occurred_at": "TEXT",
        "occurred_until": "TEXT",
    },
    "memory_sources": {
        "kind": "TEXT",
        "memory_id": "TEXT",
        "record_version": "INTEGER",
        "source_group_id": "TEXT",
    },
    "memory_provenance": {
        "kind": "TEXT",
        "memory_id": "TEXT",
        "record_version": "INTEGER",
        "state": "TEXT",
    },
    "history_groups": {
        "group_id": "TEXT",
        "kind": "TEXT",
        "container_id": "TEXT",
        "evidence": "TEXT",
    },
    "history_group_times": {"group_id": "TEXT", "occurred_at": "TEXT"},
    "memory_uses": {
        "kind": "TEXT",
        "memory_id": "TEXT",
        "record_version": "INTEGER",
        "consumer": "TEXT",
        "attempt_id": "TEXT",
        "purpose": "TEXT",
    },
    "history_reads": {
        "source": "TEXT",
        "consumer": "TEXT",
        "attempt_id": "TEXT",
        "purpose": "TEXT",
    },
    "tool_metering": {
        "run_id": "TEXT",
        "step_index": "INTEGER",
        "call_id": "TEXT",
        "ordinal": "INTEGER",
        "tool_name": "TEXT",
        "source_id": "TEXT",
        "capability_id": "TEXT",
        "effect": "TEXT",
        "requested_at": "TEXT",
        "start_confirmation": "TEXT",
        "result": "TEXT",
        "reason": "TEXT",
        "finished_at": "TEXT",
        "cost": "TEXT",
        "operation_id": "TEXT",
        "model_delivery": "TEXT",
    },
    "tool_ledger": {
        "id": "INTEGER",
        "tool_name": "TEXT",
        "effect": "TEXT",
        "status": "TEXT",
        "call_id": "TEXT",
        "run_id": "TEXT",
        "session_id": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    },
    "external_tool_operations": {
        "run_id": "TEXT",
        "step_index": "INTEGER",
        "call_id": "TEXT",
        "ledger_id": "INTEGER",
    },
    "calendar_entries": {
        "id": "INTEGER",
        "title": "TEXT",
        "starts_at": "TEXT",
        "ends_at": "TEXT",
        "iana_time_zone": "TEXT",
        "participants": "TEXT",
        "notes": "TEXT",
        "created_at": "TEXT",
    },
    "forget_operations": {
        "operation_id": "TEXT",
        "kind": "TEXT",
        "memory_id": "TEXT",
        "completeness": "TEXT",
        "created_at": "TEXT",
    },
    "forget_limits": {"operation_id": "TEXT", "group_id": "TEXT", "mode": "TEXT"},
    "forget_cleanup": {
        "operation_id": "TEXT",
        "target_id": "TEXT",
        "revision": "INTEGER",
        "state": "TEXT",
        "error": "TEXT",
    },
    "memory_consolidation_batches": {
        "batch_id": "TEXT",
        "session_id": "TEXT",
        "revision": "INTEGER",
        "status": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "finished_at": "TEXT",
        "error_code": "TEXT",
        "generation_run_id": "TEXT",
    },
    "memory_consolidation_sources": {
        "batch_id": "TEXT",
        "revision": "INTEGER",
        "run_id": "TEXT",
        "session_id": "TEXT",
        "ordinal": "INTEGER",
        "accepted_at": "TEXT",
        "finished_at": "TEXT",
    },
    "memory_mirrors": {
        "target_id": "TEXT",
        "required_generation": "INTEGER",
        "generated_generation": "INTEGER",
        "verified_generation": "INTEGER",
        "covered_cleanup_generation": "INTEGER",
        "generated_at": "TEXT",
        "verified_at": "TEXT",
        "conflict": "INTEGER",
        "error": "TEXT",
    },
    "schema_migrations": {"version": "INTEGER", "applied_at": "TEXT"},
    "memory_revision": {"singleton": "INTEGER", "revision": "INTEGER"},
    "forget_scopes": {"operation_id": "TEXT", "state": "TEXT"},
    "forget_projection_recovery": {
        "operation_id": "TEXT",
        "evidence_id": "TEXT",
        "error": "TEXT",
    },
    "forget_projection_unknown": {"operation_id": "TEXT"},
}


def compatible_schema(conn) -> bool:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    if not REQUIRED_COLUMNS.keys() <= tables:
        return False
    for table, required in REQUIRED_COLUMNS.items():
        present = {
            row[1]: row[2].upper()
            for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        if any(present.get(name) != kind for name, kind in required.items()):
            return False
    return True
