"""Public consolidation batch lifecycle on real stores and forgetting seams."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from agent_alfred import schema
from agent_alfred.clock import format_instant
from agent_alfred.memory.audit import AuditKey
from agent_alfred.memory.commands import CommandContext, MemoryCommandService
from agent_alfred.memory.consolidation import ConsolidationLimits
from agent_alfred.memory.types import ConsolidationOrigin, ManualOrigin
from agent_alfred.model import ModelRef
from agent_alfred.runtime.recording import RecordingStore

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
T0 = format_instant(NOW)
T1 = format_instant(datetime(2026, 9, 9, 12, 1, tzinfo=timezone.utc))
MODEL = ModelRef("test-endpoint", "test-model")
CONTEXT = CommandContext(ManualOrigin("web"), "web")
PLAN = (
    '{"semantic":[{"action":"create","subject":"food","fact":"likes coriander"}],'
    '"episode_summary":"User mentioned coriander."}'
)


def _service(conn, *, threshold=2):
    schema.migrate(conn)
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: NOW,
    )
    memory.consolidation._limits = ConsolidationLimits(source_threshold=threshold)
    return memory


def _complete_chat(memory, conn, session, run, user, assistant, *, t0=T0, t1=T1):
    if conn.execute(
        "SELECT 1 FROM sessions WHERE session_id=?", (session,)
    ).fetchone() is None:
        schema.insert_session(conn, session_id=session, created_at=t0)
    schema.insert_accepted_run(
        conn,
        run_id=run,
        purpose="chat",
        session_id=session,
        gateway="web",
        accepted_at=t0,
        prompt_preview=user[:20],
    )
    schema.update_run_phase(
        conn,
        run_id=run,
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=t0,
    )
    schema.update_run_phase(
        conn,
        run_id=run,
        from_phase="running",
        to_phase="finished",
        activity_revision=schema.allocate_activity_revision(conn),
        outcome="completed",
        finished_at=t1,
    )
    conn.execute(
        "UPDATE runs SET admission_state='admitted' WHERE run_id=?", (run,)
    )
    for role, text, created in (("user", user, t0), ("assistant", assistant, t1)):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, telemetry, created_at, run_id
               ) VALUES (?, ?, ?, 'web', NULL, ?, ?)""",
            (
                session,
                role,
                json.dumps([{"type": "text", "text": text}], ensure_ascii=False),
                created,
                run,
            ),
        )
    conn.commit()
    assert "error" not in memory.forgetting.register_group(
        run,
        kind="run",
        container_id=session,
        evidence="complete",
        evidence_id="input-tracking:" + run,
        occurred_at=t0,
        context=CONTEXT,
    )


def test_separate_sessions_below_threshold_do_not_combine():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    _complete_chat(memory, conn, "s-a", "run-a", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s-b", "run-b", "I like coriander", "Noted.")
    first = memory.consolidation.submit(
        "s-a", PLAN, context=CONTEXT, operation_id="sub-a", model=MODEL
    )
    second = memory.consolidation.submit(
        "s-b", PLAN, context=CONTEXT, operation_id="sub-b", model=MODEL
    )
    assert first["status"] == "below_threshold"
    assert first["unprocessed_count"] == 1
    assert second["unprocessed_count"] == 1
    conn.close()


def test_failed_incomplete_and_unsafe_runs_are_not_sources():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    _complete_chat(memory, conn, "s1", "run-ok-1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s1", "run-ok-2", "Still coriander", "Ok.")
    schema.insert_accepted_run(
        conn,
        run_id="run-failed",
        purpose="chat",
        session_id="s1",
        gateway="web",
        accepted_at=T0,
    )
    schema.update_run_phase(
        conn,
        run_id="run-failed",
        from_phase="accepted",
        to_phase="running",
        activity_revision=schema.allocate_activity_revision(conn),
        started_at=T0,
    )
    schema.update_run_phase(
        conn,
        run_id="run-failed",
        from_phase="running",
        to_phase="finished",
        activity_revision=schema.allocate_activity_revision(conn),
        outcome="failed",
        finished_at=T1,
    )
    conn.commit()
    status = memory.consolidation.session_status("s1")
    assert status["unprocessed_count"] == 2
    conn.close()


def test_atomic_success_writes_facts_episode_and_source_markers():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    _complete_chat(memory, conn, "s1", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s1", "r2", "Still coriander", "Ok.")
    result = memory.consolidation.submit(
        "s1", PLAN, context=CONTEXT, operation_id="op-success", model=MODEL
    )
    assert result["status"] == "succeeded"
    assert result["products"]
    episode = [
        item for item in result["products"] if item["kind"] == "episodic"
    ][0]
    record = memory.get("episodic", episode["memory_id"])
    assert record.summary == "User mentioned coriander."
    assert record.origin == ConsolidationOrigin(result["batch_id"])
    fact = [item for item in result["products"] if item["kind"] == "semantic"][0]
    saved = memory.get("semantic", fact["memory_id"])
    assert saved.fact == "likes coriander"
    assert saved.origin == ConsolidationOrigin(result["batch_id"])
    assert conn.execute(
        "SELECT outcome FROM memory_consolidation_source_results ORDER BY run_id"
    ).fetchall() == [("succeeded",), ("succeeded",)]
    assert conn.execute("SELECT count(*) FROM tool_ledger").fetchone() == (0,)
    view = memory.consolidation.get_batch(result["batch_id"])
    assert "I like coriander" not in json.dumps(view)
    replay = memory.consolidation.submit(
        "s1", PLAN, context=CONTEXT, operation_id="op-success", model=MODEL
    )
    assert replay == result
    conn.close()


def test_rollback_after_intermediate_write_keeps_chat_and_drops_facts():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    _complete_chat(memory, conn, "s1", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s1", "r2", "Still coriander", "Ok.")
    conn.execute(
        """CREATE TRIGGER boom AFTER INSERT ON episodes BEGIN
           SELECT RAISE(ABORT, 'injected-consolidation-abort'); END"""
    )
    result = memory.consolidation.submit(
        "s1", PLAN, context=CONTEXT, operation_id="op-boom", model=MODEL
    )
    assert result["status"] == "failed"
    assert result["error"]["code"] == "storage_write_failed"
    assert conn.execute(
        "SELECT count(*) FROM memory_consolidation_plans WHERE plan_json IS NOT NULL"
    ).fetchone() == (1,)
    assert memory.consolidation.session_status("s1")["unprocessed_count"] == 2
    from agent_alfred.memory.types import FactQuery

    with memory.reading_stores() as (facts, episodes):
        assert facts.search(FactQuery(text="coriander")) == ()
        assert episodes.list_recent().records == ()
    assert conn.execute("SELECT content FROM agent_log WHERE role='user'").fetchone()[
        0
    ]
    conn.close()


def test_protected_target_waits_for_approval_and_grants_protection():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    seeded = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": "likes basil"},
        },
        CONTEXT,
    )
    _complete_chat(memory, conn, "s1", "r1", "I now like coriander not basil", "Ok.")
    _complete_chat(memory, conn, "s1", "r2", "Yes coriander", "Noted.")
    plan = (
        '{"semantic":[{"action":"update","id":"%s","subject":"food",'
        '"fact":"likes coriander"}],"episode_summary":"Diet changed."}'
        % seeded["memory_id"]
    )
    result = memory.consolidation.submit(
        "s1", plan, context=CONTEXT, operation_id="op-protect", model=MODEL
    )
    assert result["status"] == "awaiting_approval"
    assert memory.get("semantic", seeded["memory_id"]).fact == "likes basil"
    assert memory.consolidation.session_status("s1")["unprocessed_count"] == 2
    approved = memory.consolidation.approve(
        result["batch_id"],
        result["revision"],
        context=CONTEXT,
        operation_id="op-approve",
    )
    assert approved["status"] == "succeeded"
    record = memory.get("semantic", seeded["memory_id"])
    assert record.fact == "likes coriander"
    assert record.origin == ManualOrigin("web")
    assert record.last_change_origin == ConsolidationOrigin(result["batch_id"])
    assert record.human_protected is True
    conn.close()


def test_background_keep_edit_invalidates_whole_batch_omitted_hit_does_not():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    memory.consolidation._limits = ConsolidationLimits(
        source_threshold=2, candidate_count_limit=2
    )
    protected = memory.execute(
        {
            "operation_id": "prot-seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": "likes basil"},
        },
        CONTEXT,
    )
    kept = memory.execute(
        {
            "operation_id": "keep-seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "city", "fact": "lives in Beijing"},
        },
        CommandContext(ConsolidationOrigin("old"), "web"),
    )
    omitted = memory.execute(
        {
            "operation_id": "omit-seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "secret", "fact": "秘" * 80},
        },
        CommandContext(ConsolidationOrigin("old"), "web"),
    )
    _complete_chat(memory, conn, "s1", "r1", "I like basil in Beijing", "Ok.")
    _complete_chat(memory, conn, "s1", "r2", "Still basil", "Noted.")
    plan = (
        '{"semantic":[{"action":"update","id":"%s","subject":"food",'
        '"fact":"likes coriander"},{"action":"keep","id":"%s"}],'
        '"episode_summary":"User still likes basil."}'
        % (protected["memory_id"], kept["memory_id"])
    )
    result = memory.consolidation.submit(
        "s1", plan, context=CONTEXT, operation_id="op-keep", model=MODEL
    )
    assert result["status"] == "awaiting_approval", result
    reads = conn.execute(
        "SELECT memory_id, in_request FROM memory_consolidation_reads "
        "WHERE batch_id=?",
        (result["batch_id"],),
    ).fetchall()
    in_request = {row[0]: row[1] for row in reads}
    assert in_request.get(kept["memory_id"]) == 1
    assert in_request.get(omitted["memory_id"], 0) == 0
    memory.execute(
        {
            "operation_id": "edit-omit",
            "kind": "semantic",
            "action": "update",
            "expected_version": 1,
            "payload": {"id": omitted["memory_id"], "fact": "changed-omit"},
        },
        CONTEXT,
    )
    assert memory.consolidation.get_batch(result["batch_id"])["status"] == (
        "awaiting_approval"
    )
    memory.execute(
        {
            "operation_id": "edit-keep",
            "kind": "semantic",
            "action": "update",
            "expected_version": 1,
            "payload": {"id": kept["memory_id"], "fact": "changed-keep"},
        },
        CONTEXT,
    )
    gone = memory.consolidation.get_batch(result["batch_id"])
    assert gone["status"] == "invalidated"
    assert gone.get("plan") is None
    assert gone.get("episode_summary") is None
    stale = memory.consolidation.approve(
        result["batch_id"],
        result["revision"],
        context=CONTEXT,
        operation_id="stale-approve",
    )
    assert stale == {"error": {"code": "batch_invalidated"}}
    conn.close()


def test_reject_marks_sources_without_facts_and_survives_restart(tmp_path):
    path = tmp_path / "c.db"
    conn = sqlite3.connect(path)
    memory = _service(conn, threshold=2)
    seeded = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": "likes basil"},
        },
        CONTEXT,
    )
    _complete_chat(memory, conn, "s1", "r1", "I now like coriander", "Ok.")
    _complete_chat(memory, conn, "s1", "r2", "Yes coriander", "Noted.")
    plan = (
        '{"semantic":[{"action":"update","id":"%s","subject":"food",'
        '"fact":"likes coriander"}],"episode_summary":"Diet changed."}'
        % seeded["memory_id"]
    )
    result = memory.consolidation.submit(
        "s1", plan, context=CONTEXT, operation_id="op-wait", model=MODEL
    )
    assert result["status"] == "awaiting_approval"
    rejected = memory.consolidation.reject(
        result["batch_id"],
        result["revision"],
        context=CONTEXT,
        operation_id="op-reject",
    )
    assert rejected["status"] == "rejected"
    assert memory.get("semantic", seeded["memory_id"]).fact == "likes basil"
    conn.close()
    conn = sqlite3.connect(path)
    memory = _service(conn, threshold=2)
    assert memory.consolidation.get_batch(result["batch_id"])["status"] == "rejected"
    assert memory.consolidation.session_status("s1")["unprocessed_count"] == 0
    assert conn.execute(
        "SELECT outcome FROM memory_consolidation_source_results"
    ).fetchone() == ("user_rejected",)
    conn.close()


def test_skip_oversized_validates_oldest_run_and_recomputes_threshold():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=1)
    secret = "SECRET_CHAT_BODY"
    _complete_chat(memory, conn, "s1", "huge", secret * 5000, "ok")
    result = memory.consolidation.submit(
        "s1", PLAN, context=CONTEXT, operation_id="op-huge", model=MODEL
    )
    assert result["status"] == "source_too_large"
    assert result["run_id"] == "huge"
    assert secret not in json.dumps(result)
    skipped = memory.consolidation.skip_oversized(
        "s1", "huge", context=CONTEXT, operation_id="op-skip"
    )
    assert skipped["status"] == "skipped"
    assert skipped["unprocessed_count"] == 0
    assert memory.consolidation.skip_oversized(
        "s1", "huge", context=CONTEXT, operation_id="op-skip-again"
    ) == {"error": {"code": "source_mismatch"}}
    conn.close()


def test_invalid_plan_needs_new_generation_and_commit_retry_is_model_free():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    _complete_chat(memory, conn, "s1", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s1", "r2", "Still coriander", "Ok.")
    failed = memory.consolidation.submit(
        "s1", "not-json", context=CONTEXT, operation_id="op-bad", model=MODEL
    )
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "invalid_plan_output"
    retry = memory.consolidation.retry(
        failed["batch_id"],
        failed["revision"],
        context=CONTEXT,
        operation_id="op-retry-empty",
    )
    assert retry.get("error", {}).get("code") == "generation_required"
    conn.close()


def test_sql_abort_keeps_candidate_and_retry_commits_once(tmp_path):
    path = tmp_path / "c.db"
    conn = sqlite3.connect(path)
    memory = _service(conn, threshold=2)
    _complete_chat(memory, conn, "s1", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s1", "r2", "Still coriander", "Ok.")
    conn.execute(
        """CREATE TRIGGER injected AFTER INSERT ON episodes BEGIN
           SELECT RAISE(ABORT, 'test-only'); END"""
    )
    failed = memory.consolidation.submit(
        "s1", PLAN, context=CONTEXT, operation_id="op-abort", model=MODEL
    )
    assert failed["status"] == "failed"
    assert conn.execute(
        "SELECT status FROM memory_consolidation_batches"
    ).fetchone() == ("failed",)
    assert conn.execute(
        "SELECT count(*) FROM memory_consolidation_plans WHERE plan_json IS NOT NULL"
    ).fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM facts").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (0,)
    conn.execute("DROP TRIGGER injected")
    conn.commit()
    batch_id, revision = conn.execute(
        "SELECT batch_id, revision FROM memory_consolidation_batches"
    ).fetchone()
    conn.close()
    conn = sqlite3.connect(path)
    memory = _service(conn, threshold=2)
    committed = memory.consolidation.retry(
        batch_id, revision, context=CONTEXT, operation_id="op-retry-commit"
    )
    assert committed["status"] == "succeeded"
    replay = memory.consolidation.retry(
        batch_id, revision, context=CONTEXT, operation_id="op-retry-commit"
    )
    assert replay == committed
    assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (1,)
    conn.close()


def test_delete_then_approve_does_not_revive_and_approve_then_delete_stays_deleted():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    seeded = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": "likes basil"},
        },
        CONTEXT,
    )
    _complete_chat(memory, conn, "s1", "r1", "I now like coriander", "Ok.")
    _complete_chat(memory, conn, "s1", "r2", "Yes coriander", "Noted.")
    plan = (
        '{"semantic":[{"action":"update","id":"%s","subject":"food",'
        '"fact":"likes coriander"}],"episode_summary":"Diet changed."}'
        % seeded["memory_id"]
    )
    waiting = memory.consolidation.submit(
        "s1", plan, context=CONTEXT, operation_id="op-order", model=MODEL
    )
    assert waiting["status"] == "awaiting_approval"
    deleted = memory.execute(
        {
            "operation_id": "del-dep",
            "kind": "semantic",
            "action": "delete",
            "expected_version": 1,
            "payload": {"id": seeded["memory_id"]},
        },
        CONTEXT,
    )
    assert deleted["status"] == "deleted"
    waiting_status = memory.consolidation.get_batch(waiting["batch_id"])["status"]
    assert waiting_status == "invalidated"
    assert memory.consolidation.approve(
        waiting["batch_id"],
        waiting["revision"],
        context=CONTEXT,
        operation_id="approve-after-delete",
    ) == {"error": {"code": "batch_invalidated"}}
    conn.close()


def test_read_failure_is_not_empty_sources(monkeypatch):
    conn = sqlite3.connect(":memory:")
    memory = _service(conn, threshold=2)
    _complete_chat(memory, conn, "s1", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s1", "r2", "Still coriander", "Ok.")

    def fail(groups, *, purpose, transaction=None, connection=None):
        return {"error": {"code": "storage_read_failed"}}

    monkeypatch.setattr(memory.forgetting, "evaluate_history", fail)
    result = memory.consolidation.submit(
        "s1", PLAN, context=CONTEXT, operation_id="op-read-fail", model=MODEL
    )
    assert result == {"error": {"code": "storage_read_failed"}}
    conn.close()


def test_completed_approval_cannot_rewrite_a_later_protected_version():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn)
    saved = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "home", "fact": "Beijing"},
        },
        CONTEXT,
    )
    identifier = saved["memory_id"]
    for index in range(2):
        _complete_chat(
            memory, conn, "s", "r" + str(index), "I moved to Shanghai.", "Noted."
        )
    plan = json.dumps(
        {
            "semantic": [
                {
                    "action": "update",
                    "id": identifier,
                    "subject": "home",
                    "fact": "Shanghai",
                }
            ],
            "episode_summary": "Move discussed.",
        }
    )
    batch = memory.consolidation.submit(
        "s", plan, context=CONTEXT, operation_id="sub", model=MODEL
    )
    approved = memory.consolidation.approve(
        batch["batch_id"],
        batch["revision"],
        context=CONTEXT,
        operation_id="approve",
    )
    assert approved["status"] == "succeeded"
    replayed = memory.consolidation.approve(
        batch["batch_id"],
        batch["revision"],
        context=CONTEXT,
        operation_id="approve",
    )
    assert replayed == approved
    assert memory.get("semantic", identifier).fact == "Shanghai"
    assert memory.get("semantic", identifier).record_version == 2
    changed = memory.execute(
        {
            "operation_id": "manual-change",
            "kind": "semantic",
            "action": "update",
            "expected_version": 2,
            "payload": {"id": identifier, "fact": "Guangzhou"},
        },
        CONTEXT,
    )
    assert changed["status"] == "updated"
    from agent_alfred.memory.types import (
        ConsolidationApprovalProof,
        ProtectedMemoryError,
    )

    conn.execute("BEGIN")
    store, _ = memory._stores(conn)
    with pytest.raises(ProtectedMemoryError):
        store.apply_approved_consolidation(
            identifier,
            expected_version=3,
            origin=ConsolidationOrigin(batch["batch_id"]),
            proof=ConsolidationApprovalProof(batch["batch_id"], batch["revision"]),
            subject="home",
            fact="Shanghai",
        )
    record = store.get(identifier)
    conn.rollback()
    assert record.fact == "Guangzhou"
    assert record.record_version == 3
    conn.close()


def test_invalidated_submit_replay_does_not_resurrect_candidate_body(tmp_path):
    path = tmp_path / "replay.db"
    conn = sqlite3.connect(path)
    memory = _service(conn)
    saved = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "home", "fact": "Beijing"},
        },
        CONTEXT,
    )
    identifier = saved["memory_id"]
    for index in range(2):
        _complete_chat(
            memory, conn, "s", "r" + str(index), "I moved to Shanghai.", "Noted."
        )
    plan = json.dumps(
        {
            "semantic": [
                {
                    "action": "update",
                    "id": identifier,
                    "subject": "home",
                    "fact": "Shanghai-CANDIDATE-MARKER",
                }
            ],
            "episode_summary": "CANDIDATE-MARKER episode",
        }
    )
    batch = memory.consolidation.submit(
        "s", plan, context=CONTEXT, operation_id="sub", model=MODEL
    )
    assert batch["status"] == "awaiting_approval"
    assert "CANDIDATE-MARKER" in json.dumps(batch["plan"])
    live = memory.consolidation.submit(
        "s", plan, context=CONTEXT, operation_id="sub", model=MODEL
    )
    assert live["status"] == "awaiting_approval"
    assert live["plan"] == batch["plan"]
    mismatched = memory.consolidation.submit(
        "other-session", plan, context=CONTEXT, operation_id="sub", model=MODEL
    )
    assert mismatched == {"error": {"code": "operation_mismatch"}}
    assert "CANDIDATE-MARKER" not in json.dumps(mismatched)
    memory.execute(
        {
            "operation_id": "delete",
            "kind": "semantic",
            "action": "delete",
            "expected_version": 1,
            "payload": {"id": identifier},
        },
        CONTEXT,
    )
    current = memory.consolidation.get_batch(batch["batch_id"])
    assert current["status"] == "invalidated"
    assert "plan" not in current
    replayed = memory.consolidation.submit(
        "s", plan, context=CONTEXT, operation_id="sub", model=MODEL
    )
    encoded = json.dumps(replayed)
    assert "CANDIDATE-MARKER" not in encoded
    assert "plan" not in replayed
    receipts = conn.execute(
        "SELECT receipt FROM memory_consolidation_actions"
    ).fetchall()
    assert not any("CANDIDATE-MARKER" in row[0] for row in receipts)
    rejected = memory.consolidation.reject(
        batch["batch_id"],
        batch["revision"],
        context=CONTEXT,
        operation_id="reject-after",
    )
    assert rejected == {"error": {"code": "batch_invalidated"}}
    conn.close()
    conn = sqlite3.connect(path)
    memory = _service(conn)
    restarted = memory.consolidation.submit(
        "s", plan, context=CONTEXT, operation_id="sub", model=MODEL
    )
    assert "CANDIDATE-MARKER" not in json.dumps(restarted)
    assert "plan" not in restarted
    receipts = conn.execute(
        "SELECT receipt FROM memory_consolidation_actions"
    ).fetchall()
    assert not any("CANDIDATE-MARKER" in row[0] for row in receipts)
    conn.close()


def test_protection_only_promotion_keeps_candidate_and_retry_awaits_approval():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn)
    saved = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "home", "fact": "Beijing"},
        },
        CommandContext(ConsolidationOrigin("seed"), "web"),
    )
    identifier = saved["memory_id"]
    for index in range(2):
        _complete_chat(
            memory, conn, "s", "r" + str(index), "I moved to Shanghai.", "Noted."
        )
    plan = json.dumps(
        {
            "semantic": [
                {
                    "action": "update",
                    "id": identifier,
                    "subject": "home",
                    "fact": "Shanghai",
                }
            ],
            "episode_summary": "Move discussed.",
        }
    )
    conn.execute(
        "CREATE TRIGGER injected AFTER INSERT ON episodes BEGIN "
        "SELECT RAISE(ABORT,'test-only'); END"
    )
    batch = memory.consolidation.submit(
        "s", plan, context=CONTEXT, operation_id="sub", model=MODEL
    )
    assert batch["status"] == "failed"
    conn.execute("DROP TRIGGER injected")
    duplicate = memory.execute(
        {
            "operation_id": "promote",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "home", "fact": "Beijing"},
        },
        CONTEXT,
    )
    record = memory.get("semantic", identifier)
    assert duplicate["status"] == "already_exists"
    assert record.human_protected and record.record_version == 1
    before = memory.consolidation.get_batch(batch["batch_id"])
    assert before["status"] == "failed"
    assert before["plan"]["semantic"][0]["fact"] == "Shanghai"
    result = memory.consolidation.retry(
        batch["batch_id"],
        batch["revision"],
        context=CONTEXT,
        operation_id="retry",
    )
    assert result["status"] == "awaiting_approval"
    assert memory.get("semantic", identifier).fact == "Beijing"
    assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (0,)
    edited = memory.execute(
        {
            "operation_id": "edit-body",
            "kind": "semantic",
            "action": "update",
            "expected_version": 1,
            "payload": {"id": identifier, "fact": "Guangzhou"},
        },
        CONTEXT,
    )
    assert edited["status"] == "updated"
    gone = memory.consolidation.get_batch(batch["batch_id"])
    assert gone["status"] == "invalidated"
    assert "plan" not in gone
    conn.close()


def test_successful_batch_scrubs_plan_body_and_survives_product_delete(tmp_path):
    path = tmp_path / "c.db"
    conn = sqlite3.connect(path)
    memory = _service(conn)
    _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s", "r2", "Still coriander", "Ok.")
    result = memory.consolidation.submit(
        "s", PLAN, context=CONTEXT, operation_id="op-success-scrub", model=MODEL
    )
    assert result["status"] == "succeeded"
    fact = next(item for item in result["products"] if item["kind"] == "semantic")
    plan_row = conn.execute(
        "SELECT plan_json, episode_summary, candidate_text "
        "FROM memory_consolidation_plans WHERE batch_id=?",
        (result["batch_id"],),
    ).fetchone()
    assert plan_row == (None, None, None)
    deleted = memory.execute(
        {
            "operation_id": "delete-product",
            "kind": "semantic",
            "action": "delete",
            "expected_version": fact["record_version"],
            "payload": {"id": fact["memory_id"]},
        },
        CONTEXT,
    )
    assert deleted["status"] == "deleted"
    assert memory.get("semantic", fact["memory_id"]) is None
    conn.close()
    conn = sqlite3.connect(path)
    memory = _service(conn)
    stored = conn.execute(
        "SELECT plan_json FROM memory_consolidation_plans WHERE batch_id=?",
        (result["batch_id"],),
    ).fetchone()[0]
    assert stored is None
    assert memory.consolidation.get_batch(result["batch_id"])["status"] == "succeeded"
    conn.close()


def test_approve_sqlite_abort_persists_failed_and_retry_uses_bound_approval(
    tmp_path,
):
    path = tmp_path / "c.db"
    conn = sqlite3.connect(path)
    memory = _service(conn)
    seeded = memory.execute(
        {
            "operation_id": "seed",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": "likes basil"},
        },
        CONTEXT,
    )
    _complete_chat(memory, conn, "s", "r1", "I now like coriander", "Ok.")
    _complete_chat(memory, conn, "s", "r2", "Yes coriander", "Noted.")
    plan = (
        '{"semantic":[{"action":"update","id":"%s","subject":"food",'
        '"fact":"likes coriander"}],"episode_summary":"Diet changed."}'
        % seeded["memory_id"]
    )
    waiting = memory.consolidation.submit(
        "s", plan, context=CONTEXT, operation_id="op-wait", model=MODEL
    )
    assert waiting["status"] == "awaiting_approval"
    conn.execute(
        "CREATE TRIGGER boom AFTER INSERT ON episodes BEGIN "
        "SELECT RAISE(ABORT, 'failure'); END"
    )
    failed = memory.consolidation.approve(
        waiting["batch_id"],
        waiting["revision"],
        context=CONTEXT,
        operation_id="op-approve",
    )
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "storage_write_failed"
    assert memory.get("semantic", seeded["memory_id"]).fact == "likes basil"
    conn.close()
    conn = sqlite3.connect(path)
    memory = _service(conn)
    persisted = memory.consolidation.get_batch(waiting["batch_id"])
    assert persisted["status"] == "failed"
    assert persisted["error_code"] == "storage_write_failed"
    replay = memory.consolidation.approve(
        waiting["batch_id"],
        waiting["revision"],
        context=CONTEXT,
        operation_id="op-approve",
    )
    assert replay["status"] == "failed"
    conn.execute("DROP TRIGGER boom")
    retried = memory.consolidation.retry(
        waiting["batch_id"],
        waiting["revision"],
        context=CONTEXT,
        operation_id="op-retry",
    )
    assert retried["status"] == "succeeded"
    assert memory.get("semantic", seeded["memory_id"]).fact == "likes coriander"
    conn.close()


def test_identical_consolidation_update_does_not_invalidate_other_batch():
    conn = sqlite3.connect(":memory:")
    memory = _service(conn)
    auto = memory.execute(
        {
            "operation_id": "auto",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": "likes basil"},
        },
        CommandContext(ConsolidationOrigin("seed"), "web"),
    )
    protected = memory.execute(
        {
            "operation_id": "protected",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "city", "fact": "Beijing"},
        },
        CONTEXT,
    )
    _complete_chat(memory, conn, "c", "cr1", "I like basil", "Noted.")
    _complete_chat(memory, conn, "c", "cr2", "Still basil", "Noted.")
    pending_plan = json.dumps(
        {
            "semantic": [
                {
                    "action": "update",
                    "id": protected["memory_id"],
                    "subject": "city",
                    "fact": "Shanghai",
                },
                {"action": "keep", "id": auto["memory_id"]},
            ],
            "episode_summary": "City changed.",
        }
    )
    pending = memory.consolidation.submit(
        "c", pending_plan, context=CONTEXT, operation_id="pending", model=MODEL
    )
    assert pending["status"] == "awaiting_approval"
    _complete_chat(memory, conn, "d", "dr1", "I like basil", "Noted.")
    _complete_chat(memory, conn, "d", "dr2", "Still basil", "Noted.")
    noop = memory.consolidation.submit(
        "d",
        json.dumps(
            {
                "semantic": [
                    {
                        "action": "update",
                        "id": auto["memory_id"],
                        "subject": "food",
                        "fact": "likes basil",
                    }
                ],
                "episode_summary": "Diet changed.",
            }
        ),
        context=CONTEXT,
        operation_id="noop",
        model=MODEL,
    )
    assert noop["status"] == "succeeded"
    record = memory.get("semantic", auto["memory_id"])
    after = memory.consolidation.get_batch(pending["batch_id"])
    assert record.record_version == 1 and record.fact == "likes basil"
    assert after["status"] == "awaiting_approval" and "plan" in after
    conn.close()
