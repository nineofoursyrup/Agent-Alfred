"""Field-by-field diagnostic projections from real source fixtures."""

import json

from agent_alfred.evals.deterministic.test_database_http import (
    _dashboard,
    _execute,
    _write,
)
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin
from agent_alfred.runtime.work import SubmitRequest


def _cell(cell):
    if cell["type"] == "null":
        return None
    if cell["type"] == "integer":
        return int(cell["value"])
    return cell.get("value")


def _table(body):
    return [
        dict(zip(body["columns"], (_cell(item) for item in row), strict=True))
        for row in body["rows"]
    ]


def test_twenty_one_objects_field_rules_from_real_fixtures(tmp_path):
    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    try:
        submitted = host.submit(SubmitRequest(message="hello"))
        host.wait(submitted.run_id)
        context = CommandContext(origin=ManualOrigin("web"), source="web")
        fact = host._memory_service.execute(
            {
                "operation_id": "ac03-fact",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "me", "fact": "likes-tea"},
            },
            context,
        )
        episode = host._memory_service.execute(
            {
                "operation_id": "ac03-episode",
                "kind": "episodic",
                "action": "save",
                "payload": {
                    "summary": "boiled water",
                    "occurred_at": "2026-09-01T23:30:00+08:00",
                    "occurred_until": None,
                },
            },
            context,
        )
        assert fact["status"] == "saved"
        assert episode["status"] == "saved"
        session_id = submitted.session_id
        run_id = submitted.run_id
        fact_id = fact["memory_id"]

        def load(conn):
            conn.execute(
                "UPDATE agent_log SET content=? WHERE run_id=? AND role='assistant'",
                (
                    json.dumps(
                        [
                            {"type": "thinking", "text": "hidden-thought"},
                            {"type": "text", "text": "pong"},
                        ]
                    ),
                    run_id,
                ),
            )
            conn.execute(
                "UPDATE runs SET telemetry=? WHERE run_id=?",
                (
                    json.dumps(
                        {
                            "attempts": [
                                {
                                    "attempt_id": "att-1",
                                    "outcome": "committed",
                                    "model": {
                                        "endpoint_id": "ep-1",
                                        "model_id": "m-1",
                                    },
                                    "usage": {
                                        "total_input_tokens": None,
                                        "output_tokens": 4,
                                    },
                                }
                            ]
                        }
                    ),
                    run_id,
                ),
            )
            for run_name, telemetry in (
                ("run-empty", json.dumps({"attempts": []})),
                ("run-none", None),
            ):
                conn.execute(
                    "INSERT INTO runs ("
                    "run_id, purpose, session_id, gateway, phase, outcome, "
                    "accepted_at, activity_revision, telemetry, admission_state"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_name,
                        "chat",
                        session_id,
                        "web",
                        "finished",
                        "completed",
                        "2026-09-16T00:00:00Z",
                        1,
                        telemetry,
                        "admitted",
                    ),
                )
            conn.execute(
                "INSERT OR IGNORE INTO memory_sources VALUES (?,?,?,?)",
                ("semantic", fact_id, 1, "grp-fact"),
            )
            conn.execute(
                "UPDATE memory_provenance SET state='known' "
                "WHERE kind='semantic' AND memory_id=?",
                (fact_id,),
            )
            conn.execute(
                "INSERT INTO history_groups VALUES (?,?,?,?,?)",
                ("grp-timed", "run", "run-box", "complete", None),
            )
            conn.execute(
                "INSERT INTO history_groups VALUES (?,?,?,?,?)",
                ("grp-open", "run", None, "unknown", None),
            )
            conn.execute(
                "INSERT INTO history_group_times VALUES (?,?)",
                ("grp-timed", "2026-09-16T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO memory_uses VALUES (?,?,?,?,?,?)",
                ("semantic", fact_id, 1, "consumer-a", "att-1", "retrieve"),
            )
            conn.execute(
                "INSERT INTO history_reads VALUES (?,?,?,?)",
                ("grp-timed", "grp-open", "att-1", "retrieve"),
            )
            conn.execute(
                "INSERT INTO tool_ledger ("
                "tool_name, fingerprint, effect, status, call_id, run_id, "
                "session_id, summary, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "demo",
                    "fp-1",
                    "local_write",
                    "succeeded",
                    "call-1",
                    run_id,
                    session_id,
                    "hidden-summary",
                    "2026-09-16T00:00:00Z",
                    "2026-09-16T00:00:01Z",
                ),
            )
            ledger_id = conn.execute("SELECT id FROM tool_ledger").fetchone()[0]
            conn.execute(
                "INSERT INTO external_tool_operations VALUES (?,?,?,?,?,?)",
                (run_id, 0, "call-1", "fp-link", ledger_id, None),
            )
            conn.execute(
                "INSERT INTO calendar_entries ("
                "title, starts_at, ends_at, iana_time_zone, created_at"
                ") VALUES (?,?,?,?,?)",
                (
                    "tea",
                    "2026-09-01T15:30:00+00:00",
                    None,
                    "UTC",
                    "2026-09-01T15:00:00+00:00",
                ),
            )
            conn.execute(
                "INSERT INTO forget_operations VALUES (?,?,?,?,?)",
                ("op-complete", "semantic", fact_id, "full", "2026-09-16T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO forget_cleanup VALUES (?,?,?,?,?)",
                ("op-complete", "tgt-complete", 1, "complete", None),
            )
            conn.execute(
                "INSERT INTO forget_operations VALUES (?,?,?,?,?)",
                ("op-scope", "semantic", fact_id, "full", "2026-09-16T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO forget_scopes ("
                "scope_id, operation_id, revision, kind, members, state, "
                "boundary, reason"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (
                    "scope-1",
                    "op-scope",
                    1,
                    "group",
                    "[]",
                    "pending",
                    "{}",
                    "wait",
                ),
            )
            conn.execute(
                "INSERT INTO forget_operations VALUES (?,?,?,?,?)",
                ("op-failed", "semantic", fact_id, "full", "2026-09-16T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO forget_cleanup VALUES (?,?,?,?,?)",
                ("op-failed", "tgt-failed", 1, "failed", "disk-full"),
            )
            conn.execute(
                "INSERT INTO forget_operations VALUES (?,?,?,?,?)",
                ("op-clean", "semantic", fact_id, "full", "2026-09-16T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO forget_cleanup VALUES (?,?,?,?,?)",
                ("op-clean", "tgt-clean", 1, "pending", None),
            )
            conn.execute(
                "INSERT INTO forget_limits VALUES (?,?,?)",
                ("op-complete", "grp-timed", "paused"),
            )
            conn.execute(
                "INSERT INTO memory_consolidation_batches ("
                "batch_id, session_id, revision, status, created_at, "
                "updated_at, finished_at, error_code, generation_run_id, receipt"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "batch-1",
                    session_id,
                    1,
                    "failed",
                    "2026-09-16T00:00:00Z",
                    "2026-09-16T00:00:01Z",
                    "2026-09-16T00:00:02Z",
                    "model-timeout",
                    "gen-run",
                    None,
                ),
            )
            conn.execute(
                "INSERT INTO memory_consolidation_sources VALUES (?,?,?,?,?,?,?)",
                (
                    "batch-1",
                    1,
                    run_id,
                    session_id,
                    0,
                    "2026-09-16T00:00:00Z",
                    "2026-09-16T00:00:01Z",
                ),
            )
            conn.execute(
                "UPDATE memory_mirrors SET error=? WHERE target_id LIKE 'memory/%'",
                ("read-failed",),
            )

        _write(host, load)

        def query(sql):
            status, body, _head = _execute(dashboard, sql)
            assert status == 200, (sql, body)
            return _table(body)

        sessions = query(
            "SELECT session_id, created_at, activity_revision FROM diag_sessions"
        )
        assert any(row["session_id"] == session_id for row in sessions)

        runs = query(
            "SELECT run_id, purpose, session_id, gateway, phase, outcome, "
            "admission_state FROM diag_runs"
        )
        by_run = {row["run_id"]: row for row in runs}
        assert by_run[run_id]["phase"] == "finished"
        assert by_run[run_id]["outcome"] == "completed"
        dumped_runs = json.dumps(_execute(dashboard, "SELECT * FROM diag_runs")[1])
        assert "prompt_preview" not in dumped_runs
        assert "telemetry" not in dumped_runs

        messages = query(
            "SELECT message_id, session_id, run_id, role, text FROM diag_messages"
        )
        assistant = [row for row in messages if row["role"] == "assistant"]
        assert all("hidden-thought" not in (row["text"] or "") for row in assistant)
        assert any(row["text"] == "pong" for row in assistant)
        assert all(row["role"] in {"user", "assistant"} for row in messages)

        coverage = query(
            "SELECT run_id, state, recorded_count, run_finished "
            "FROM diag_attempt_coverage"
        )
        cov = {row["run_id"]: row for row in coverage}
        assert cov[run_id]["state"] == "recorded"
        assert cov[run_id]["recorded_count"] == 1
        assert cov[run_id]["run_finished"] == 1
        assert cov["run-empty"]["state"] == "recorded_empty"
        assert cov["run-empty"]["recorded_count"] == 0
        assert cov["run-none"]["state"] == "unrecorded"
        assert cov["run-none"]["recorded_count"] is None

        attempts = query(
            "SELECT run_id, attempt_id, outcome, endpoint_id, model_id, "
            "total_input_tokens, output_tokens FROM diag_attempts"
        )
        att = attempts[0]
        assert att["attempt_id"] == "att-1"
        assert att["outcome"] == "committed"
        assert att["endpoint_id"] == "ep-1"
        assert att["total_input_tokens"] is None
        assert att["output_tokens"] == 4

        facts = query(
            "SELECT id, subject, fact, origin_kind, last_change_origin_type, "
            "last_change_origin_source, last_change_origin_call_id, "
            "last_change_origin_batch_id, provenance_state FROM diag_facts"
        )
        tea = next(row for row in facts if row["fact"] == "likes-tea")
        assert tea["origin_kind"] == "manual"
        assert tea["last_change_origin_type"] == "manual"
        assert tea["last_change_origin_source"] == "web"
        assert tea["last_change_origin_call_id"] is None
        assert tea["last_change_origin_batch_id"] is None
        assert tea["provenance_state"] == "known"

        episodes = query(
            "SELECT id, summary, occurred_until, provenance_state FROM diag_episodes"
        )
        boiled = next(row for row in episodes if row["summary"] == "boiled water")
        assert boiled["occurred_until"] is None

        sources = query(
            "SELECT kind, memory_id, record_version, source_group_id "
            "FROM diag_memory_sources"
        )
        assert {
            "kind": "semantic",
            "memory_id": fact_id,
            "record_version": 1,
            "source_group_id": "grp-fact",
        } in sources

        groups = query(
            "SELECT group_id, kind, container_id, evidence, occurred_at "
            "FROM diag_history_groups"
        )
        grouped = {row["group_id"]: row for row in groups}
        assert grouped["grp-timed"]["occurred_at"] == "2026-09-16T00:00:00Z"
        assert grouped["grp-open"]["occurred_at"] is None
        dumped_groups = json.dumps(
            _execute(dashboard, "SELECT * FROM diag_history_groups")[1]
        )
        assert "evidence_id" not in dumped_groups

        uses = query(
            "SELECT kind, memory_id, consumer, attempt_id, purpose "
            "FROM diag_memory_uses"
        )
        assert uses[0]["consumer"] == "consumer-a"
        assert "run_id" not in uses[0]

        reads = query(
            "SELECT source, consumer, attempt_id, purpose FROM diag_history_reads"
        )
        assert reads[0] == {
            "source": "grp-timed",
            "consumer": "grp-open",
            "attempt_id": "att-1",
            "purpose": "retrieve",
        }

        ledger = query(
            "SELECT tool_name, effect, status, call_id, run_id, session_id "
            "FROM diag_tool_ledger"
        )
        assert ledger[0]["effect"] == "local_write"
        dumped_ledger = json.dumps(
            _execute(dashboard, "SELECT * FROM diag_tool_ledger")[1]
        )
        assert "hidden-summary" not in dumped_ledger
        assert "fingerprint" not in dumped_ledger

        links = query(
            "SELECT run_id, step_index, call_id, ledger_id "
            "FROM diag_tool_operation_links"
        )
        assert links[0]["run_id"] == run_id
        assert links[0]["step_index"] == 0
        dumped_links = json.dumps(
            _execute(dashboard, "SELECT * FROM diag_tool_operation_links")[1]
        )
        assert "receipt" not in dumped_links
        assert "fingerprint" not in dumped_links

        calendar = query(
            "SELECT title, ends_at, iana_time_zone FROM diag_calendar_entries"
        )
        assert calendar[0]["title"] == "tea"
        assert calendar[0]["ends_at"] is None

        forgets = query(
            "SELECT operation_id, current_state FROM diag_forget_operations"
        )
        states = {row["operation_id"]: row["current_state"] for row in forgets}
        assert states["op-complete"] == "complete"
        assert states["op-scope"] == "needs_scope"
        assert states["op-failed"] == "failed"
        assert states["op-clean"] == "cleaning"

        limits = query("SELECT operation_id, group_id, mode FROM diag_forget_limits")
        assert limits[0]["mode"] == "paused"

        cleanup = query(
            "SELECT operation_id, state, error_code FROM diag_forget_cleanup"
        )
        by_op = {row["operation_id"]: row for row in cleanup}
        assert by_op["op-complete"]["error_code"] is None
        assert by_op["op-failed"]["error_code"] == "cleanup_error"
        assert "disk-full" not in json.dumps(cleanup)

        batches = query(
            "SELECT batch_id, session_id, revision, status, error_code, "
            "generation_run_id FROM diag_consolidation_batches"
        )
        assert batches[0]["batch_id"] == "batch-1"
        assert batches[0]["error_code"] == "consolidation_error"
        dumped_batch = json.dumps(
            _execute(dashboard, "SELECT * FROM diag_consolidation_batches")[1]
        )
        assert "receipt" not in dumped_batch
        assert "model-timeout" not in dumped_batch

        cons_sources = query(
            "SELECT batch_id, revision, run_id, ordinal "
            "FROM diag_consolidation_sources"
        )
        assert cons_sources[0]["run_id"] == run_id
        assert cons_sources[0]["ordinal"] == 0

        mirrors = query(
            "SELECT target_id, conflict, error_code FROM diag_memory_mirrors"
        )
        assert mirrors
        assert all(row["error_code"] == "mirror_error" for row in mirrors)
        dumped_mirrors = json.dumps(
            _execute(dashboard, "SELECT * FROM diag_memory_mirrors")[1]
        )
        assert "ready" not in dumped_mirrors
        assert "intent" not in dumped_mirrors
        assert "fingerprint" not in dumped_mirrors
        assert "read-failed" not in dumped_mirrors
    finally:
        assert dashboard.close()
