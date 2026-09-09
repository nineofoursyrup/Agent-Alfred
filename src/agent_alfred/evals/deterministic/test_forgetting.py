"""Public forgetting contracts on durable SQLite; no model or remote services."""

import sqlite3

import pytest

from agent_alfred.evals.deterministic.test_memory_commands import service
from agent_alfred.memory.commands import CommandContext
from agent_alfred.memory.types import ManualOrigin

CONTEXT = CommandContext(ManualOrigin("web"), "web")


def save(memory, operation="save", groups=()):
    return memory.execute(
        {
            "operation_id": operation,
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": operation, "fact": "private-body"},
        },
        CommandContext(ManualOrigin("web"), "web", source_groups=groups),
    )


def delete(memory, record, operation="delete"):
    return memory.execute(
        {
            "operation_id": operation,
            "kind": "semantic",
            "action": "delete",
            "expected_version": record["record_version"],
            "payload": {"id": record["memory_id"]},
        },
        CONTEXT,
    )


def test_known_closure_is_atomic_and_survives_restart(tmp_path):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as conn:
        memory = service(conn)
        f = memory.forgetting
        for group, session in [("R1", "S1"), ("R2", "S1"), ("R3", "S2"), ("R4", "S2")]:
            assert "error" not in f.register_group(
                group,
                kind="run",
                container_id=session,
                evidence="complete",
                evidence_id="fixture-proof",
                context=CONTEXT,
            )
        record = save(memory, groups=("R1",))
        f.register_read(
            "R2", sources=("R1",), attempt_id="a1", purpose="gate", context=CONTEXT
        )
        f.register_read(
            "R3", sources=("R2",), attempt_id="a2", purpose="answer", context=CONTEXT
        )
        result = delete(memory, record)
        assert result["status"] == "deleted"
        assert f.get_forgetting("delete")["state"] == "complete"
        assert f.evaluate_history(("R1", "R2", "R3", "R4"), purpose="working_window")[
            "allowed"
        ] == ["R4"]
    with sqlite3.connect(path) as conn:
        memory = service(conn)
        assert memory.forgetting.evaluate_history(
            ("R1", "R2", "R3", "R4"), purpose="gate_history"
        )["allowed"] == ["R4"]
        assert "private-body" not in str(memory.forgetting.get_forgetting("delete"))


def test_partial_scopes_freeze_exact_history_and_keep_unknown_after_restart(tmp_path):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as conn:
        memory = service(conn)
        f = memory.forgetting
        for g, s in [("U1", "S1"), ("U2", "S2")]:
            f.register_group(g, kind="run", container_id=s, context=CONTEXT)
        record = save(memory)
        delete(memory, record)
        progress = f.get_forgetting("delete")
        assert progress["state"] == "needs_scope"
        scopes = f.list_scopes("delete")
        assert {g for scope in scopes for g in scope["members"]} == {"U1", "U2"}
        f.register_group(
            "R5",
            kind="run",
            container_id="S1",
            evidence="complete",
            evidence_id="fixture-proof",
            context=CONTEXT,
        )
        first = scopes[0]
        action = {
            "operation_id": "delete",
            "action_id": "confirm-1",
            "scopes": [
                {"scope_id": first["scope_id"], "expected_revision": first["revision"]}
            ],
        }
        receipt = f.resolve_scope(action, CONTEXT)
        assert receipt["status"] == "confirmed"
        assert f.get_forgetting("delete")["state"] == "needs_scope"
        assert f.evaluate_history(("U1", "U2", "R5"), purpose="gate_history")[
            "allowed"
        ] == ["R5"]
        assert f.resolve_scope(action, CONTEXT) == receipt
    with sqlite3.connect(path) as conn:
        f = service(conn).forgetting
        assert f.resolve_scope(action, CONTEXT) == receipt
        remaining = [s for s in f.list_scopes("delete") if s["state"] == "pending"]
        assert len(remaining) == 1
        result = f.resolve_scope(
            {
                "operation_id": "delete",
                "action_id": "confirm-2",
                "scopes": [
                    {
                        "scope_id": remaining[0]["scope_id"],
                        "expected_revision": remaining[0]["revision"],
                    }
                ],
            },
            CONTEXT,
        )
        assert result["status"] == "confirmed"
        assert f.get_forgetting("delete")["state"] == "complete"
        assert f.list_scopes("delete")[0]["members"] == first["members"]


def test_delete_and_scope_invalidation_share_transaction_with_cleanup_intents(tmp_path):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    conn = sqlite3.connect(tmp_path / "memory.db")
    base = service(conn)
    conn.execute("CREATE TABLE candidate (body TEXT, status TEXT)")
    conn.execute("INSERT INTO candidate VALUES ('private-body','awaiting_approval')")
    conn.commit()

    class Projection:
        def invalidate(self, connection, *, memories, isolated):
            if memories or isolated:
                connection.execute(
                    "UPDATE candidate SET body=NULL,status='invalidated'"
                )
                return ("mirror",)
            return ()

    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=base._clock,
        projection_participants=(Projection(),),
    )
    record = save(memory)
    conn.execute(
        (
            "CREATE TRIGGER receipt_fault BEFORE INSERT ON "
            "memory_operations WHEN NEW.operation_id='delete' BEGIN SELECT "
            "RAISE(ABORT,'private-exception'); END"
        )
    )
    assert delete(memory, record) == {"error": {"code": "storage_write_failed"}}
    assert conn.execute("SELECT body,status FROM candidate").fetchone() == (
        "private-body",
        "awaiting_approval",
    )
    assert memory.forgetting.get_forgetting("delete") is None
    conn.execute("DROP TRIGGER receipt_fault")
    assert delete(memory, record)["status"] == "deleted"
    assert conn.execute("SELECT body,status FROM candidate").fetchone() == (
        None,
        "invalidated",
    )
    assert memory.forgetting.get_forgetting("delete")["state"] == "cleaning"
    assert memory.forgetting.managed_target_readable("mirror") is False
    conn.close()


def test_cleanup_failure_and_crash_after_replace_recover_same_generation(tmp_path):
    import threading
    from datetime import datetime, timezone

    import pytest

    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    path = tmp_path / "db"
    output = tmp_path / "mirror"

    class Projection:
        def invalidate(self, connection, *, memories, isolated):
            return ("mirror",) if memories else ()

    class FilePort:
        mode = "fail"
        writes = 0

        def verify(self, target_id, generation):
            return output.exists() and output.read_text() == str(generation)

        def rebuild(self, target_id, generation):
            if self.mode == "fail":
                raise OSError("private-file-error")
            output.write_text(str(generation))
            self.writes += 1
            if self.mode == "crash":
                raise KeyboardInterrupt

    port = FilePort()

    def opened(conn):
        service(conn)
        return MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=AuditKey("test", b"x" * 32),
            clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            projection_participants=(Projection(),),
            cleanup_port=port,
        )

    with sqlite3.connect(path) as conn:
        memory = opened(conn)
        record = save(memory)
        receipt = delete(memory, record)
        f = memory.forgetting
        assert f.get_forgetting("delete")["state"] == "cleaning"
        f.retry_cleanup("delete", CONTEXT)
        assert f.get_forgetting("delete")["state"] == "failed"
        assert f.managed_target_readable("mirror") is False
        port.mode = "crash"
        with pytest.raises(KeyboardInterrupt):
            f.retry_cleanup("delete", CONTEXT)
        assert f.get_forgetting("delete")["state"] == "cleaning"
    with sqlite3.connect(path) as conn:
        memory = opened(conn)
        f = memory.forgetting
        assert f.managed_target_readable("mirror") is False
        port.mode = "success"
        f.retry_cleanup("delete", CONTEXT)
        assert port.writes == 1
        assert f.get_forgetting("delete")["state"] == "complete"
        assert f.managed_target_readable("mirror") is True
        assert memory.get_operation("delete") == receipt
        assert memory.get("semantic", record["memory_id"]) is None


def test_progress_invalidates_old_projection_and_publishes_body_free_notice(tmp_path):
    import threading
    from datetime import datetime, timezone

    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    conn = sqlite3.connect(tmp_path / "db")
    service(conn)
    notices = []
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        memory_notifier=notices.append,
        process_instance_id="process",
    )
    f = memory.forgetting
    f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
    token = f.evaluate_history(("U1",), purpose="gate_history")
    record = save(memory)
    delete(memory, record)
    assert not f.projection_valid(token)
    scope = f.list_scopes("delete")[0]
    before = memory.memory_revision
    assert (
        f.resolve_scope(
            {
                "operation_id": "delete",
                "action_id": "confirm",
                "scopes": [
                    {
                        "scope_id": scope["scope_id"],
                        "expected_revision": scope["revision"],
                    }
                ],
            },
            CONTEXT,
        )["status"]
        == "confirmed"
    )
    assert memory.memory_revision > before
    assert notices[-1] == {
        "schema_version": 1,
        "process_instance_id": "process",
        "memory_revision": memory.memory_revision,
        "change": None,
    }
    assert not any(
        marker in str(notices)
        for marker in ("private-body", "subject", "seq", "checkpoint")
    )
    conn.close()


def test_v5_delete_receipt_and_ungrouped_history_recover_unknown(tmp_path, monkeypatch):
    import json

    from agent_alfred import schema

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    registry = schema.MIGRATIONS
    monkeypatch.setattr(schema, "MIGRATIONS", registry[:5])
    schema.migrate(conn)
    conn.execute(
        (
            "INSERT INTO agent_log "
            "(session_id,source,role,content,created_at) VALUES "
            "('S2','web','user',?,'2026-09-09T00:00:00Z')"
        ),
        (json.dumps("private-old-history"),),
    )
    receipt = {
        "operation_id": "old-delete",
        "kind": "semantic",
        "memory_id": "old-memory",
        "action": "delete",
        "status": "deleted",
        "committed_at": "2026-09-09T00:00:00Z",
    }
    conn.execute(
        "INSERT INTO memory_operations VALUES ('old-delete','fingerprint','key',?)",
        (json.dumps(receipt),),
    )
    conn.commit()
    monkeypatch.setattr(schema, "MIGRATIONS", registry)
    memory = service(conn)
    assert memory.get_operation("old-delete") == receipt
    f = memory.forgetting
    assert f.get_forgetting("old-delete")["state"] == "needs_scope"
    legacy = [s for s in f.list_scopes("old-delete") if s["kind"] == "legacy"]
    assert len(legacy) == 1 and len(legacy[0]["members"]) == 1
    assert (
        f.evaluate_history(legacy[0]["members"], purpose="working_window")["allowed"]
        == []
    )
    assert (
        json.loads(conn.execute("SELECT content FROM agent_log").fetchone()[0])
        == "private-old-history"
    )
    conn.close()


def test_late_memory_use_and_source_registration_isolate_old_versions(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    for group in ("R1", "R2", "R3"):
        f.register_group(
            group,
            kind="run",
            container_id="S1",
            evidence="complete",
            evidence_id="fixture-proof",
            context=CONTEXT,
        )
    record = save(memory)
    memory.execute(
        {
            "operation_id": "edit",
            "kind": "semantic",
            "action": "update",
            "expected_version": 1,
            "payload": {"id": record["memory_id"], "fact": "new-body"},
        },
        CONTEXT,
    )
    record["record_version"] = 2
    delete(memory, record)
    assert (
        f.register_sources(
            "semantic",
            record["memory_id"],
            1,
            groups=("R1",),
            evidence_id="archive-1",
            context=CONTEXT,
        )["status"]
        == "registered"
    )
    f.register_read(
        "R2",
        memories=(("semantic", record["memory_id"], 1),),
        attempt_id="old-attempt",
        purpose="gate",
        context=CONTEXT,
    )
    f.register_read(
        "R3",
        sources=("R2",),
        attempt_id="next-attempt",
        purpose="answer",
        context=CONTEXT,
    )
    assert (
        f.evaluate_history(("R1", "R2", "R3"), purpose="consolidation_source")[
            "allowed"
        ]
        == []
    )
    conn.close()


def test_four_purposes_keep_fresh_input_and_reject_stale_send(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
    f.register_group(
        "R4",
        kind="run",
        container_id="S2",
        evidence="complete",
        evidence_id="proof",
        context=CONTEXT,
    )
    raw = {"U1": "private-history", "R4": "safe-history"}
    tokens = []
    for purpose in (
        "working_window",
        "gate_history",
        "tool_summary",
        "consolidation_source",
    ):
        token = f.evaluate_history(("U1", "R4"), purpose=purpose)
        assert token["allowed"] == ["R4"]
        tokens.append(token)
        requests = []
        assert (
            f.consume_history(
                token,
                lambda groups: requests.append(
                    ["fresh-input", *[raw[g] for g in groups]]
                ),
                CONTEXT,
            )["status"]
            == "consumed"
        )
        assert requests == [["fresh-input", "safe-history"]]
    record = save(memory, groups=("R4",))
    delete(memory, record)
    for token in tokens:
        assert f.consume_history(
            token,
            lambda groups: (_ for _ in ()).throw(AssertionError("must not send")),
            CONTEXT,
        ) == {"error": {"code": "projection_stale"}}
    assert raw["U1"] == "private-history"  # independent human history remains
    conn.close()


def selection(f, operation="delete", action="confirm", scopes=None):
    scopes = f.list_scopes(operation) if scopes is None else scopes
    return {
        "operation_id": operation,
        "action_id": action,
        "scopes": [
            {"scope_id": s["scope_id"], "expected_revision": s["revision"]}
            for s in scopes
            if s["state"] == "pending"
        ],
    }


def test_confirm_replay_key_rotation_stale_and_invalid_choices(tmp_path):
    import threading
    from datetime import datetime, timezone

    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
    record = save(memory)
    delete(memory, record)
    action = selection(f)
    for invalid in (
        {**action, "scopes": []},
        {**action, "operation_id": "other"},
        {**action, "scopes": [{**action["scopes"][0], "members": ["fake"]}]},
    ):
        assert f.resolve_scope(invalid, CONTEXT) == {"error": {"code": "invalid_input"}}
    receipt = f.resolve_scope(action, CONTEXT)
    assert receipt["status"] == "confirmed"
    assert (
        f.resolve_scope({**action, "action_id": "late"}, CONTEXT)["error"]["code"]
        == "scope_stale"
    )
    assert f.resolve_scope({**action, "operation_id": "other"}, CONTEXT) == {
        "error": {"code": "operation_mismatch"}
    }
    rotated = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("new", b"y" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert rotated.forgetting.resolve_scope(action, CONTEXT) == {
        "error": {"code": "operation_unverifiable"}
    }
    assert rotated.forgetting.get_action("confirm") == receipt
    conn.close()


def test_scope_evidence_shrinks_only_unconfirmed_members_and_invalidates_revision(
    tmp_path,
):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    for group in ("U1", "U2"):
        f.register_group(group, kind="run", container_id="S1", context=CONTEXT)
    delete(memory, save(memory))
    old = selection(f)
    f.register_group(
        "U1",
        kind="run",
        container_id="S1",
        evidence="complete",
        evidence_id="archived-proof",
        context=CONTEXT,
    )
    assert f.resolve_scope(old, CONTEXT)["error"]["code"] == "scope_stale"
    current = f.list_scopes("delete")[0]
    assert current["members"] == ["U2"]
    assert current["evidence_id"] == "archived-proof"
    assert f.resolve_scope(selection(f, action="new"), CONTEXT)["status"] == "confirmed"
    f.register_group(
        "U2",
        kind="run",
        container_id="S1",
        evidence="complete",
        evidence_id="later-proof",
        context=CONTEXT,
    )
    assert f.evaluate_history(("U1", "U2"), purpose="tool_summary")["allowed"] == ["U1"]
    conn.close()


def test_confirmation_busy_is_not_queued_and_receipt_failure_rolls_back(tmp_path):
    import threading

    conn = sqlite3.connect(tmp_path / "db", check_same_thread=False)
    memory = service(conn)
    f = memory.forgetting
    f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
    delete(memory, save(memory))
    action = selection(f)
    entered, release = threading.Event(), threading.Event()

    # SQLite itself holds the first confirming transaction at its action insert.
    def pause():
        entered.set()
        assert release.wait(5)
        return 0

    conn.create_function("confirm_barrier", 0, pause)
    conn.execute(
        (
            "CREATE TRIGGER action_barrier BEFORE INSERT ON forget_actions "
            "BEGIN SELECT confirm_barrier(); END"
        )
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(f.resolve_scope(action, CONTEXT))
    )
    worker.start()
    try:
        assert entered.wait(5)
        assert f.resolve_scope({**action, "action_id": "second"}, CONTEXT) == {
            "error": {"code": "busy"}
        }
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0]["status"] == "confirmed"
    assert (
        f.resolve_scope({**action, "action_id": "second"}, CONTEXT)["error"]["code"]
        == "scope_stale"
    )
    conn.execute("DROP TRIGGER action_barrier")
    f.register_group("U2", kind="run", container_id="S2", context=CONTEXT)
    failed = selection(f, action="fails")
    conn.execute(
        (
            "CREATE TRIGGER action_fault BEFORE INSERT ON forget_actions "
            "BEGIN SELECT RAISE(ABORT,'private-error'); END"
        )
    )
    before = f.get_forgetting("delete")
    assert f.resolve_scope(failed, CONTEXT) == {
        "error": {"code": "storage_write_failed"}
    }
    assert f.get_forgetting("delete") == before
    assert f.get_action("fails") is None
    conn.close()


def test_successors_before_or_after_confirmation_and_multiple_operations(tmp_path):
    for first in (True, False):
        conn = sqlite3.connect(tmp_path / str(first))
        memory = service(conn)
        f = memory.forgetting
        f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
        for g in ("R2", "R3", "R4"):
            f.register_group(
                g,
                kind="run",
                container_id="S2",
                evidence="complete",
                evidence_id="proof",
                context=CONTEXT,
            )
        delete(memory, save(memory))
        action = selection(f)

        def edges():
            f.register_read(
                "R2",
                sources=("U1",),
                attempt_id="gate-1",
                purpose="gate",
                context=CONTEXT,
            )
            f.register_read(
                "R3",
                sources=("R2",),
                attempt_id="answer-2",
                purpose="answer",
                context=CONTEXT,
            )

        if first:
            edges()
            assert (
                f.evaluate_history(("R2", "R3"), purpose="gate_history")["allowed"]
                == []
            )
        frozen = f.resolve_scope(action, CONTEXT)["members"]
        if not first:
            edges()
        assert frozen == ["U1"]
        assert f.get_forgetting("delete")["state"] == "complete"
        assert f.evaluate_history(("U1", "R2", "R3", "R4"), purpose="working_window")[
            "allowed"
        ] == ["R4"]
        delete(memory, save(memory, "second", groups=("R2",)), "delete-2")
        f.register_group(
            "R2",
            kind="run",
            container_id="S2",
            evidence="complete",
            evidence_id="other-proof",
            context=CONTEXT,
        )
        assert f.evaluate_history(("R2",), purpose="working_window")["allowed"] == []
        assert (
            len(
                [
                    x
                    for x in f.get_forgetting("delete-2")["limits"]
                    if x["mode"] == "isolated"
                ]
            )
            >= 2
        )
        conn.close()


def test_batch_scope_can_cover_fixed_members_from_two_sessions(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    for group, session in (("U1", "S1"), ("U2", "S2")):
        f.register_group(group, kind="run", container_id=session, context=CONTEXT)
    f.register_group(
        "B2",
        kind="batch",
        container_id="B2",
        evidence="complete",
        evidence_id="proof",
        context=CONTEXT,
    )
    delete(memory, save(memory))
    offered = f.offer_scope(
        "delete",
        kind="batch",
        container_id="B1",
        groups=("U1", "U2"),
        evidence_id="batch-source-index",
        context=CONTEXT,
    )
    assert offered["status"] == "offered"
    scopes = [s for s in f.list_scopes("delete") if s["state"] == "pending"]
    assert len(scopes) == 1 and scopes[0]["members"] == ["U1", "U2"]
    assert scopes[0]["container_id"] == "B1"
    assert f.resolve_scope(selection(f), CONTEXT)["members"] == ["U1", "U2"]
    assert f.evaluate_history(("U1", "U2", "B2"), purpose="consolidation_source")[
        "allowed"
    ] == ["B2"]
    conn.close()


def test_scope_priority_and_last_confirmation_follow_cleanup_facts(tmp_path):
    import threading
    from datetime import datetime, timezone

    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    class Projection:
        def invalidate(self, connection, *, memories, isolated):
            return ("mirror",) if memories else ()

    class Port:
        fail = True
        generation = 0

        def verify(self, target, generation):
            return self.generation == generation

        def rebuild(self, target, generation):
            if self.fail:
                raise OSError("private-error")
            self.generation = generation

    for stage in ("pending", "failed", "complete"):
        conn = sqlite3.connect(tmp_path / stage)
        service(conn)
        port = Port()
        memory = MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=AuditKey("test", b"x" * 32),
            clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            projection_participants=(Projection(),),
            cleanup_port=port,
        )
        f = memory.forgetting
        f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
        receipt = delete(memory, save(memory))
        if stage != "pending":
            port.fail = stage == "failed"
            f.retry_cleanup("delete", CONTEXT)
        before = f.get_forgetting("delete")
        assert before["state"] == "needs_scope"
        assert before["cleanup"][0]["state"] == stage
        assert f.resolve_scope(selection(f), CONTEXT)["status"] == "confirmed"
        assert (
            f.get_forgetting("delete")["state"]
            == {"pending": "cleaning", "failed": "failed", "complete": "complete"}[
                stage
            ]
        )
        assert memory.get_operation("delete") == receipt
        conn.close()


def test_candidate_confirmation_failure_rolls_back_and_late_projection_reopens(
    tmp_path,
):
    import threading
    from datetime import datetime, timezone

    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    conn = sqlite3.connect(tmp_path / "db")
    service(conn)
    conn.execute("CREATE TABLE candidate (body TEXT, status TEXT)")
    conn.execute(
        "INSERT INTO candidate VALUES ('private-candidate','awaiting_approval')"
    )
    conn.commit()

    class Projection:
        late = False

        def invalidate(self, connection, *, memories, isolated):
            if "U1" in isolated:
                connection.execute(
                    "UPDATE candidate SET body=NULL,status='invalidated'"
                )
            return ("late-mirror",) if self.late else ()

    projection = Projection()
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        projection_participants=(projection,),
        projection_inventory_evidence_id="complete-late-projection-inventory",
    )
    f = memory.forgetting
    f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
    delete(memory, save(memory))
    action = selection(f)
    conn.execute(
        (
            "CREATE TRIGGER refuse_candidate BEFORE UPDATE ON candidate "
            "BEGIN SELECT RAISE(ABORT,'private-error'); END"
        )
    )
    assert f.resolve_scope(action, CONTEXT) == {
        "error": {"code": "storage_write_failed"}
    }
    assert f.get_action("confirm") is None
    assert f.list_scopes("delete")[0]["state"] == "pending"
    assert (
        conn.execute("SELECT body FROM candidate").fetchone()[0] == "private-candidate"
    )
    conn.execute("DROP TRIGGER refuse_candidate")
    original = f.resolve_scope(action, CONTEXT)
    assert f.get_forgetting("delete")["state"] == "complete"
    assert conn.execute("SELECT body,status FROM candidate").fetchone() == (
        None,
        "invalidated",
    )
    projection.late = True
    assert f.reconcile_projections(CONTEXT) == {"status": "reconciled"}
    assert f.get_forgetting("delete")["cleanup"][0]["target_id"] == "late-mirror"
    assert f.get_forgetting("delete")["state"] == "cleaning"
    assert f.managed_target_readable("late-mirror") is False
    assert f.resolve_scope(action, CONTEXT) == original
    conn.close()


def test_independent_same_content_and_raw_history_survive_projection_erasure(tmp_path):
    import json

    from agent_alfred.memory.types import FactQuery

    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    f.register_group(
        "R1",
        kind="run",
        container_id="S1",
        evidence="complete",
        evidence_id="proof",
        context=CONTEXT,
    )
    first = save(memory, "first", groups=("R1",))
    second = save(memory, "second")
    conn.execute(
        (
            "INSERT INTO agent_log "
            "(session_id,source,role,content,created_at,run_id) VALUES "
            "('S1','web','user',?,'2026-09-09T00:00:00Z','R1')"
        ),
        (json.dumps("private-body"),),
    )
    conn.execute(
        (
            "INSERT INTO tool_ledger "
            "(tool_name,fingerprint,effect,status,run_id,summary,created_at) "
            "VALUES ('external','f','local_write','succeeded','R1',"
            "'private-body','2026-09-09T00:00:00Z')"
        )
    )
    conn.commit()
    delete(memory, first)
    assert memory.get("semantic", first["memory_id"]) is None
    assert memory.get("semantic", second["memory_id"]).fact == "private-body"
    with memory.reading_stores() as (facts, _):
        assert [h.record.id for h in facts.search(FactQuery(text="private"))] == [
            second["memory_id"]
        ]
    assert (
        json.loads(conn.execute("SELECT content FROM agent_log").fetchone()[0])
        == "private-body"
    )
    assert (
        conn.execute(
            "SELECT summary FROM tool_ledger WHERE tool_name='external'"
        ).fetchone()[0]
        is None
    )
    conn.close()


def test_missing_source_identity_and_storage_evidence_failure_never_complete(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    delete(memory, save(memory, groups=("missing-old-run",)))
    assert f.get_forgetting("delete")["state"] == "needs_scope"
    assert (
        f.evaluate_history(("missing-old-run",), purpose="gate_history")["allowed"]
        == []
    )
    conn.set_authorizer(
        lambda action, name, column, db, source: (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and name == "forget_scopes"
            else sqlite3.SQLITE_OK
        )
    )
    assert f.list_scopes("delete") == {"error": {"code": "storage_read_failed"}}
    conn.set_authorizer(None)
    assert f.get_forgetting("delete")["state"] == "needs_scope"
    conn.close()


def test_projection_writer_rechecks_sources_and_target_in_shared_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    f.register_group(
        "R1",
        kind="run",
        container_id="S1",
        evidence="complete",
        evidence_id="proof",
        context=CONTEXT,
    )
    record = save(memory)
    token = f.evaluate_history(("R1",), purpose="consolidation_source")
    conn.execute("CREATE TABLE approved (id TEXT)")

    def approve(connection):
        connection.execute("INSERT INTO approved VALUES ('batch')")
        return {"status": "applied"}

    targets = (("semantic", record["memory_id"], 1),)
    assert f.write_projection(
        token, targets=targets, write=approve, context=CONTEXT
    ) == {"status": "applied"}
    token = f.evaluate_history(("R1",), purpose="consolidation_source")
    memory.execute(
        {
            "operation_id": "edit",
            "kind": "semantic",
            "action": "update",
            "expected_version": 1,
            "payload": {"id": record["memory_id"], "fact": "changed"},
        },
        CONTEXT,
    )
    assert f.write_projection(
        token, targets=targets, write=approve, context=CONTEXT
    ) == {"error": {"code": "batch_invalidated"}}
    assert conn.execute("SELECT count(*) FROM approved").fetchone()[0] == 1
    conn.close()


def test_absence_without_delete_evidence_does_not_invent_forgetting(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    result = delete(memory, {"memory_id": "never-existed", "record_version": 1})
    assert result["status"] == "already_absent"
    assert memory.forgetting.get_forgetting("delete") is None
    conn.close()


def test_confirmation_interruption_before_commit_recovers_pending_scope(tmp_path):
    import threading
    from datetime import datetime, timezone

    import pytest

    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    path = tmp_path / "db"

    class InterruptedProjection:
        def invalidate(self, connection, *, memories, isolated):
            if isolated:
                raise KeyboardInterrupt
            return ()

    with sqlite3.connect(path) as conn:
        service(conn)
        memory = MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=AuditKey("test", b"x" * 32),
            clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            projection_participants=(InterruptedProjection(),),
        )
        f = memory.forgetting
        f.register_group("U1", kind="run", container_id="S1", context=CONTEXT)
        delete(memory, save(memory))
        action = selection(f)
        with pytest.raises(KeyboardInterrupt):
            f.resolve_scope(action, CONTEXT)
    with sqlite3.connect(path) as conn:
        f = service(conn).forgetting
        assert f.get_action("confirm") is None
        assert f.get_forgetting("delete")["state"] == "needs_scope"
        assert f.resolve_scope(action, CONTEXT)["status"] == "confirmed"


def test_source_registration_borrows_transaction_without_commit_or_rollback(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    conn.execute("CREATE TABLE caller_work (id TEXT)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_work VALUES ('keep')")
    result = memory.forgetting.register_read(
        "R2",
        sources=("R1",),
        attempt_id="a",
        purpose="answer",
        context=CONTEXT,
        transaction=conn,
    )
    assert result == {"consumer": "R2"}
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM caller_work").fetchone()[0] == 0
    # Registration, graph propagation and caller work roll back together.
    assert conn.execute("SELECT count(*) FROM history_reads").fetchone()[0] == 0
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_work VALUES ('preserve')")
    assert memory.forgetting.register_read(
        "R2", sources=("R1",), attempt_id="a", purpose="answer", context=CONTEXT
    ) == {"error": {"code": "transaction_required"}}
    assert conn.in_transaction
    assert conn.execute("SELECT id FROM caller_work").fetchone()[0] == "preserve"
    conn.rollback()
    conn.close()


def test_existing_safe_evidence_is_order_independent_for_unknown_sources(tmp_path):
    from agent_alfred.memory.types import ConsolidationOrigin

    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    f.register_group(
        "R4",
        kind="run",
        container_id="S2",
        evidence="complete",
        evidence_id="safe-proof",
        context=CONTEXT,
    )
    record = memory.execute(
        {
            "operation_id": "unknown-save",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "unknown", "fact": "private-body"},
        },
        CommandContext(ConsolidationOrigin("old-batch"), "consolidation"),
    )
    delete(memory, record)
    assert f.evaluate_history(("R4",), purpose="gate_history")["allowed"] == ["R4"]
    assert f.get_forgetting("delete")["state"] == "needs_scope"
    f.register_group(
        "R4",
        kind="run",
        container_id="S2",
        evidence="complete",
        evidence_id="safe-proof",
        context=CONTEXT,
    )
    assert f.get_forgetting("delete")["state"] == "needs_scope"
    conn.close()


def test_scope_metadata_has_frozen_time_bounds_reason_and_old_revision(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    f.register_group(
        "U1",
        kind="run",
        container_id="S1",
        occurred_at="2026-09-08T08:00:00+08:00",
        context=CONTEXT,
    )
    f.register_group(
        "U2",
        kind="run",
        container_id="S1",
        occurred_at="2026-09-08T01:00:00Z",
        context=CONTEXT,
    )
    delete(memory, save(memory))
    scope = f.list_scopes("delete")[0]
    assert scope["boundary"] == {
        "member_count": 2,
        "earliest_at": "2026-09-08T00:00:00+00:00",
        "latest_at": "2026-09-08T01:00:00+00:00",
        "time_complete": True,
    }
    assert scope["reason"] == "association_unknown"
    f.register_group(
        "U1",
        kind="run",
        container_id="S1",
        evidence="complete",
        evidence_id="safe-proof",
        context=CONTEXT,
    )
    assert f.get_scope(scope["scope_id"], scope["revision"]) == scope
    assert f.list_scopes("delete")[0]["members"] == ["U2"]
    conn.close()


def test_borrowed_registration_inside_projection_owner_never_relocks(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    f.register_group(
        "R1",
        kind="run",
        container_id="S1",
        evidence="complete",
        evidence_id="proof",
        context=CONTEXT,
    )
    token = f.evaluate_history(("R1",), purpose="consolidation_source")
    result = f.write_projection(
        token,
        write=lambda tx: f.register_read(
            "B1",
            sources=("R1",),
            attempt_id="attempt",
            purpose="consolidation",
            context=CONTEXT,
            transaction=tx,
        ),
        context=CONTEXT,
    )
    assert result == {"consumer": "B1"}
    assert not conn.in_transaction
    delete(memory, save(memory, groups=("R1",)))
    assert f.evaluate_history(("B1",), purpose="consolidation_source")["allowed"] == []
    conn.close()


def test_complete_observation_survives_reopening_and_restart(tmp_path):
    path = tmp_path / "db"
    with sqlite3.connect(path) as conn:
        memory = service(conn)
        delete(memory, save(memory))
        completed = memory.forgetting.get_forgetting("delete")
        observation = completed["observations"][-1]
        assert observation["state"] == "complete"
        assert observation["progress_revision"] == completed["progress_revision"]
        memory.forgetting.register_group(
            "late", kind="run", container_id="S2", context=CONTEXT
        )
        assert memory.forgetting.get_forgetting("delete")["state"] == "needs_scope"
    with sqlite3.connect(path) as conn:
        current = service(conn).forgetting.get_forgetting("delete")
        assert current["state"] == "needs_scope"
        assert current["observations"][0] == observation
        assert current["observations"][-1]["state"] == "needs_scope"


def test_independent_mutations_preserve_caller_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    # Durable target comes from the same public participant used in production.
    import threading
    from datetime import datetime, timezone

    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    class Projection:
        def invalidate(self, connection, *, memories, isolated):
            return ("mirror",) if memories else ()

    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        projection_participants=(Projection(),),
    )
    record = save(memory)
    delete(memory, record)
    conn.execute("CREATE TABLE caller_work (value TEXT)")
    operations = (
        lambda: memory.forgetting.retry_cleanup("delete", CONTEXT),
        lambda: save(memory, "nested-save"),
        lambda: memory.forgetting.reconcile_projections(CONTEXT),
    )
    for operation in operations:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO caller_work VALUES ('preserve-me')")
        assert operation() == {"error": {"code": "transaction_required"}}
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM caller_work").fetchall() == [
            ("preserve-me",)
        ]
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM caller_work").fetchone()[0] == 0
    assert save(memory, "after-caller-rollback")["status"] == "saved"
    conn.close()


def test_v5_upgrade_restores_projection_cleanup_before_complete(tmp_path, monkeypatch):
    import json
    import threading
    from datetime import datetime, timezone

    from agent_alfred import schema
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    path = tmp_path / "v5.db"
    conn = sqlite3.connect(path)
    registry = schema.MIGRATIONS
    monkeypatch.setattr(schema, "MIGRATIONS", registry[:5])
    schema.migrate(conn)
    receipt = {
        "operation_id": "old-delete",
        "kind": "semantic",
        "memory_id": "old-id",
        "action": "delete",
        "status": "deleted",
        "committed_at": "2026-09-09T00:00:00Z",
    }
    conn.execute(
        "INSERT INTO memory_operations VALUES (?,?,?,?)",
        ("old-delete", "fp", "key", json.dumps(receipt)),
    )
    conn.execute("INSERT INTO memory_sources VALUES ('semantic','old-id',1,'R1')")
    schema.insert_session(conn, session_id="S1", created_at=receipt["committed_at"])
    schema.insert_accepted_run(
        conn,
        run_id="R1",
        purpose="chat",
        session_id="S1",
        gateway="web",
        accepted_at=receipt["committed_at"],
    )
    conn.execute(
        "INSERT INTO agent_log (session_id,source,role,content,created_at,run_id) "
        "VALUES ('S1','web','user',?,?,'R1')",
        (json.dumps("original-history"), receipt["committed_at"]),
    )
    conn.execute(
        "INSERT INTO tool_ledger "
        "(tool_name,fingerprint,effect,status,run_id,summary,created_at) "
        "VALUES ('fixture','f','local_write','succeeded','R1','private-summary',?)",
        (receipt["committed_at"],),
    )
    conn.execute("CREATE TABLE candidate (body TEXT)")
    conn.execute("INSERT INTO candidate VALUES ('private-projection')")
    conn.commit()
    monkeypatch.setattr(schema, "MIGRATIONS", registry)
    schema.migrate(conn)
    memory = service(conn)
    assert conn.execute("SELECT summary FROM tool_ledger").fetchone() == (None,)
    assert memory.forgetting.get_forgetting("old-delete")["state"] == "cleaning"
    assert memory.get_operation("old-delete") == receipt
    for operation in (
        lambda: memory.forgetting.retry_cleanup("old-delete", CONTEXT),
        lambda: memory.forgetting.reconcile_projections(CONTEXT),
    ):
        assert operation() == {"error": {"code": "projection_inventory_required"}}
        assert memory.forgetting.get_forgetting("old-delete")["state"] == "cleaning"
        assert conn.execute("SELECT body FROM candidate").fetchone() == (
            "private-projection",
        )
    conn.close()

    class Projection:
        def invalidate(self, connection, *, memories, isolated):
            if ("semantic", "old-id") in memories:
                connection.execute("UPDATE candidate SET body=NULL")
                return ("mirror",)
            return ()

    class Port:
        generation = 0

        def verify(self, target, generation):
            return self.generation == generation

        def rebuild(self, target, generation):
            self.generation = generation

    conn = sqlite3.connect(path)
    port = Port()
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=AuditKey("test", b"x" * 32),
        clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
        projection_participants=(Projection(),),
        projection_inventory_evidence_id="trusted-local-managed-output-inventory",
        cleanup_port=port,
    )
    independent = save(memory, "independent")
    # Failure while materializing obligations rolls back the whole recovery.
    conn.execute(
        "CREATE TRIGGER refuse BEFORE UPDATE ON candidate "
        "BEGIN SELECT RAISE(ABORT,'failure'); END"
    )
    assert memory.forgetting.retry_cleanup("old-delete", CONTEXT) == {
        "error": {"code": "storage_write_failed", "failure_recorded": True}
    }
    assert memory.forgetting.get_forgetting("old-delete")["state"] == "failed"
    assert not memory.forgetting.managed_target_readable("mirror")
    conn.execute("DROP TRIGGER refuse")
    assert memory.forgetting.retry_cleanup("old-delete", CONTEXT)["state"] == "complete"
    assert conn.execute("SELECT body FROM candidate").fetchone() == (None,)
    assert (
        json.loads(conn.execute("SELECT content FROM agent_log").fetchone()[0])
        == "original-history"
    )
    assert memory.get("semantic", independent["memory_id"]).fact == "private-body"
    assert memory.get_operation("old-delete") == receipt
    conn.close()


def test_control_exception_after_mutation_acquire_releases_lease(tmp_path):
    import sys

    import pytest

    from agent_alfred.memory.commands import MemoryCommandService

    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    interruption = KeyboardInterrupt("post-acquire control exception")
    code = MemoryCommandService._run_mutation.__code__

    def interrupt(frame, event, arg):
        # Include the native acquire return before STORE_FAST captures it.
        if frame.f_code is code:
            frame.f_trace_opcodes = True
            if event == "opcode" and memory._mutation_lock.locked():
                raise interruption
        return interrupt

    previous = sys.gettrace()
    try:
        sys.settrace(interrupt)
        with pytest.raises(KeyboardInterrupt) as raised:
            memory.forgetting.register_group(
                "R1", kind="run", container_id="S1", context=CONTEXT
            )
        assert raised.value is interruption
    finally:
        sys.settrace(previous)
    assert memory.forgetting.register_group(
        "R1", kind="run", container_id="S1", context=CONTEXT
    ) == {"group_id": "R1"}
    assert not conn.in_transaction
    assert save(memory, "after-interruption")["status"] == "saved"
    conn.close()


def test_late_sources_and_reads_revise_only_pending_obligations(tmp_path):
    for late_kind in ("source", "memory_use", "history_read"):
        conn = sqlite3.connect(tmp_path / late_kind)
        memory = service(conn)
        f = memory.forgetting
        for group, session in (("U1", "S1"), ("U2", "S1"), ("U3", "S2")):
            f.register_group(group, kind="run", container_id=session, context=CONTEXT)
        record = save(memory)
        delete(memory, record)
        old = next(s for s in f.list_scopes("delete") if s["container_id"] == "S1")
        other = next(s for s in f.list_scopes("delete") if s["container_id"] == "S2")

        def action(scope, action_id):
            return {
                "operation_id": "delete",
                "action_id": action_id,
                "scopes": [
                    {
                        "scope_id": scope["scope_id"],
                        "expected_revision": scope["revision"],
                    }
                ],
            }

        confirmed_action = action(other, "already-confirmed")
        original = f.resolve_scope(confirmed_action, CONTEXT)
        assert original["status"] == "confirmed"

        def register():
            if late_kind == "source":
                return f.register_sources(
                    "semantic",
                    record["memory_id"],
                    1,
                    groups=("U1",),
                    evidence_id="late-proof",
                    context=CONTEXT,
                )
            if late_kind == "memory_use":
                return f.register_read(
                    "U1",
                    memories=(("semantic", record["memory_id"], 1),),
                    attempt_id="late",
                    purpose="answer",
                    context=CONTEXT,
                )
            return f.register_read(
                "U1",
                sources=("U3",),
                attempt_id="late",
                purpose="answer",
                context=CONTEXT,
            )

        conn.execute(
            "CREATE TRIGGER refuse_scope BEFORE UPDATE ON forget_scopes "
            "BEGIN SELECT RAISE(ABORT,'failure'); END"
        )
        assert register() == {"error": {"code": "storage_write_failed"}}
        assert f.get_scope(old["scope_id"], old["revision"]) == old
        assert {x["group_id"]: x["mode"] for x in f.get_forgetting("delete")["limits"]}[
            "U1"
        ] == "paused"
        conn.execute("DROP TRIGGER refuse_scope")
        assert "error" not in register()
        current = next(
            s for s in f.list_scopes("delete") if s["scope_id"] == old["scope_id"]
        )
        assert current["members"] == ["U2"]
        assert current["revision"] == old["revision"] + 1
        assert f.resolve_scope(action(old, "stale-first"), CONTEXT)["error"] == {
            "code": "scope_stale",
            "scope_id": old["scope_id"],
            "current_revision": current["revision"],
        }
        assert f.get_action("stale-first") is None
        assert f.resolve_scope(confirmed_action, CONTEXT) == original
        assert f.get_scope(old["scope_id"], old["revision"]) == old
        assert f.get_forgetting("delete")["state"] == "needs_scope"
        # Source registration does not reclassify the original coverage evidence.
        assert f.get_forgetting("delete")["completeness"] == "known"
        assert (
            "error" not in register()
        )  # unchanged obligations do not bump scope revision
        assert (
            next(
                s for s in f.list_scopes("delete") if s["scope_id"] == old["scope_id"]
            )["revision"]
            == current["revision"]
        )
        conn.close()
        conn = sqlite3.connect(tmp_path / late_kind)
        f = service(conn).forgetting
        assert f.resolve_scope(confirmed_action, CONTEXT) == original
        assert f.resolve_scope(action(current, "remaining"), CONTEXT)["members"] == [
            "U2"
        ]
        assert f.get_forgetting("delete")["state"] == "complete"
        conn.close()


def test_control_exception_after_admission_releases_both_leases(tmp_path):
    import sys

    import pytest

    from agent_alfred.evals.deterministic.test_host_lifecycle import _host
    from agent_alfred.memory.commands import MemoryCommandService

    host, conn, _, _ = _host()
    memory = host.memory_service
    interruption = KeyboardInterrupt("post-admission boundary")

    def interrupt(frame, event, arg):
        if frame.f_code is MemoryCommandService._run_mutation.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and host._mutating:
                raise interruption
        return interrupt

    previous = sys.gettrace()
    try:
        sys.settrace(interrupt)
        with pytest.raises(KeyboardInterrupt) as raised:
            save(memory)
        assert raised.value is interruption
    finally:
        sys.settrace(previous)
    try:
        assert save(memory, "next")["status"] == "saved"
        # A denied memory mutation must never release another door's admission.
        assert host.try_begin_mutation() is None
        assert save(memory, "blocked") == {"error": {"code": "busy"}}
        assert host.try_begin_mutation() == "mutation_in_flight"
        host.end_mutation()
        assert save(memory, "after-other-owner")["status"] == "saved"
    finally:
        host.close()
        conn.close()


def test_resolved_identity_and_time_evidence_invalidate_old_scope(tmp_path):
    conn = sqlite3.connect(tmp_path / "db")
    memory = service(conn)
    f = memory.forgetting
    # Unknown source identities cannot be confirmed until their boundary is known.
    f.register_read(
        "reader",
        sources=("missing",),
        attempt_id="old-attempt",
        purpose="answer",
        context=CONTEXT,
    )
    delete(memory, save(memory))
    old = f.list_scopes("delete")[0]
    old_action = {
        "operation_id": "delete",
        "action_id": "first",
        "scopes": [{"scope_id": old["scope_id"], "expected_revision": old["revision"]}],
    }
    assert f.resolve_scope(old_action, CONTEXT)["error"]["code"] == "invalid_input"
    f.register_group("missing", kind="run", container_id="S1", context=CONTEXT)
    assert f.resolve_scope(old_action, CONTEXT)["error"]["code"] == "scope_stale"
    assert f.get_scope(old["scope_id"], old["revision"]) == old
    identified = next(s for s in f.list_scopes("delete") if s["container_id"] == "S1")
    assert identified["kind"] == "run" and identified["members"] == ["missing"]
    before_time = {
        "operation_id": "delete",
        "action_id": "with-time",
        "scopes": [
            {
                "scope_id": identified["scope_id"],
                "expected_revision": identified["revision"],
            }
        ],
    }
    f.register_group(
        "missing",
        kind="run",
        container_id="S1",
        occurred_at="2026-09-09T00:00:00Z",
        context=CONTEXT,
    )
    assert f.resolve_scope(before_time, CONTEXT)["error"]["code"] == "scope_stale"
    timed = next(
        s for s in f.list_scopes("delete") if s["scope_id"] == identified["scope_id"]
    )
    assert timed["revision"] == identified["revision"] + 1
    assert timed["boundary"]["time_complete"] is True
    assert timed["boundary"]["earliest_at"] == "2026-09-09T00:00:00+00:00"
    assert f.get_forgetting("delete")["state"] == "needs_scope"
    conn.close()


def test_control_exception_during_host_release_completes_cleanup(tmp_path):
    import sys

    import pytest

    from agent_alfred.evals.deterministic.test_host_lifecycle import _host
    from agent_alfred.runtime.host import RuntimeHost

    for boundary in ("entry", "owner_cleared", "slot_cleared"):
        host, conn, _, _ = _host()
        memory = host.memory_service
        interruption = KeyboardInterrupt(boundary)

        def interrupt(frame, event, arg):
            if frame.f_code is RuntimeHost.end_mutation.__code__:
                frame.f_trace_opcodes = True
                if event == "opcode" and (
                    boundary == "entry"
                    or boundary == "owner_cleared"
                    and host._mutation_owner is None
                    or boundary == "slot_cleared"
                    and not host._mutating
                ):
                    raise interruption
            return interrupt

        previous = sys.gettrace()
        try:
            sys.settrace(interrupt)
            with pytest.raises(KeyboardInterrupt) as raised:
                save(memory)
            assert raised.value is interruption
        finally:
            sys.settrace(previous)
        try:
            assert save(memory, "after-release-interruption")["status"] == "saved"
        finally:
            host.close()
            conn.close()


def test_regrouped_unresolved_member_stales_when_identity_is_resolved(tmp_path):
    for kind in ("session", "batch"):
        conn = sqlite3.connect(tmp_path / kind)
        memory = service(conn)
        f = memory.forgetting
        f.register_read(
            "reader",
            sources=("missing",),
            attempt_id="a",
            purpose="answer",
            context=CONTEXT,
        )
        delete(memory, save(memory))
        offered = f.offer_scope(
            "delete",
            kind=kind,
            container_id="container",
            groups=("missing",),
            evidence_id="container-proof",
            context=CONTEXT,
        )
        scope = next(
            s for s in f.list_scopes("delete") if s["scope_id"] == offered["scope_id"]
        )
        action = {
            "operation_id": "delete",
            "action_id": "first",
            "scopes": [
                {"scope_id": scope["scope_id"], "expected_revision": scope["revision"]}
            ],
        }
        assert f.resolve_scope(action, CONTEXT)["error"]["code"] == "invalid_input"
        f.register_group("missing", kind="run", container_id="S1", context=CONTEXT)
        assert f.resolve_scope(action, CONTEXT)["error"]["code"] == "scope_stale"
        assert f.get_action("first") is None
        assert f.get_scope(scope["scope_id"], scope["revision"]) == scope
        assert f.get_forgetting("delete")["state"] == "needs_scope"
        conn.close()


def test_control_exception_inside_host_acquisition_keeps_owner_recoverable(tmp_path):
    import sys

    import pytest

    from agent_alfred.evals.deterministic.test_host_lifecycle import _host
    from agent_alfred.runtime.host import RuntimeHost

    for boundary in ("lock", "owner", "slot"):
        host, conn, _, _ = _host()
        interruption = KeyboardInterrupt(boundary)

        def interrupt(frame, event, arg):
            if frame.f_code is RuntimeHost.try_begin_mutation.__code__:
                frame.f_trace_opcodes = True
                if event == "opcode" and (
                    boundary == "lock"
                    and host._lock.locked()
                    or boundary == "owner"
                    and host._mutation_owner is not None
                    or boundary == "slot"
                    and host._mutating
                ):
                    raise interruption
            return interrupt

        previous = sys.gettrace()
        try:
            sys.settrace(interrupt)
            with pytest.raises(KeyboardInterrupt) as raised:
                save(host.memory_service)
            assert raised.value is interruption
        finally:
            sys.settrace(previous)
        try:
            assert save(host.memory_service, "after-acquisition")["status"] == "saved"
        finally:
            host.close()
            conn.close()


def test_native_lock_release_return_has_durable_completion(tmp_path):
    import sys

    import pytest

    from agent_alfred.evals.deterministic.test_host_lifecycle import _host
    from agent_alfred.resource_rollback import OwnedLock

    for phase in ("acquire", "release"):
        host, conn, _, _ = _host()
        interruption = KeyboardInterrupt("released-before-wrapper-return")

        def interrupt(frame, event, arg):
            if (
                event == "return"
                and frame.f_code is OwnedLock.close.__code__
                and host._mutating == (phase == "acquire")
            ):
                raise interruption
            return interrupt

        previous = sys.gettrace()
        try:
            sys.settrace(interrupt)
            with pytest.raises(KeyboardInterrupt) as raised:
                save(host.memory_service)
            assert raised.value is interruption
        finally:
            sys.settrace(previous)
        try:
            assert (
                save(host.memory_service, "after-release-return")["status"] == "saved"
            )
        finally:
            host.close()
            conn.close()


def test_ownerless_mutation_gate_interruptions_keep_host_usable(tmp_path):
    import sys

    import pytest

    from agent_alfred.evals.deterministic.test_host_lifecycle import _host
    from agent_alfred.gateway.web.api import MutationGate
    from agent_alfred.resource_rollback import OwnedLock
    from agent_alfred.runtime.host import RuntimeHost

    for phase in ("begin", "end"):
        for boundary in ("close_entry", "return"):
            host, conn, _, _ = _host()
            target = (
                RuntimeHost.try_begin_mutation
                if phase == "begin"
                else RuntimeHost.end_mutation
            ).__code__
            signal = KeyboardInterrupt(f"{phase}-{boundary}")
            captured = []

            def interrupt(frame, event, arg):
                match = (
                    boundary == "return"
                    and event == "return"
                    and frame.f_code is target
                )
                if (
                    boundary == "close_entry"
                    and event == "call"
                    and frame.f_code is OwnedLock.close.__code__
                    and frame.f_back.f_code is target
                ):
                    captured.append(frame.f_locals["self"])
                    match = True
                if match:
                    raise signal
                return interrupt

            previous = sys.gettrace()
            try:
                sys.settrace(interrupt)
                with pytest.raises(KeyboardInterrupt) as raised:
                    MutationGate(host).create_session()
                assert raised.value is signal
            finally:
                sys.settrace(previous)
            available = host._lock.acquire(blocking=False)
            if available:
                host._lock.release()
            try:
                assert available
                assert not host._mutating
                sid, refusal = MutationGate(host).create_session()
                assert sid and refusal is None
                expected = 1 if phase == "begin" else 2
                assert (
                    conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
                    == expected
                )
            finally:
                # Only a failing pre-fix case needs fixture rescue.
                # Never mask the assertions above.
                for lock in captured:
                    lock.close()
                host.end_mutation()
                host.close()
                conn.close()


@pytest.mark.parametrize("failure_type", ("sqlite", "value", "runtime"))
def test_projection_recovery_failure_is_durable_and_retryable(
    tmp_path, monkeypatch, failure_type
):
    import json
    import threading
    from datetime import datetime, timezone

    from agent_alfred import schema
    from agent_alfred.memory.audit import AuditKey
    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    participant_error = [None]

    class Projection:
        def invalidate(self, connection, *, memories, isolated):
            connection.execute("UPDATE candidate SET body=NULL")
            if participant_error[0] is not None:
                raise participant_error[0]("private-participant-error")
            return ()

    def opened(path):
        conn = sqlite3.connect(path)
        schema.migrate(conn)
        memory = MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=AuditKey("test", b"x" * 32),
            clock=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            projection_participants=(Projection(),),
            projection_inventory_evidence_id="managed-inventory",
        )
        return conn, memory

    for method in ("retry", "reconcile"):
        for unknown in (False, True):
            path = tmp_path / f"{method}-{unknown}.db"
            conn = sqlite3.connect(path)
            registry = schema.MIGRATIONS
            monkeypatch.setattr(schema, "MIGRATIONS", registry[:5])
            schema.migrate(conn)
            receipt = {
                "operation_id": "legacy",
                "kind": "semantic",
                "memory_id": "old",
                "action": "delete",
                "status": "deleted",
                "committed_at": "2026-09-09T00:00:00Z",
            }
            conn.execute(
                "INSERT INTO memory_operations VALUES ('legacy','fp','key',?)",
                (json.dumps(receipt),),
            )
            conn.execute("INSERT INTO memory_sources VALUES ('semantic','old',1,'R1')")
            schema.insert_session(
                conn, session_id="S1", created_at=receipt["committed_at"]
            )
            schema.insert_accepted_run(
                conn,
                run_id="R1",
                purpose="chat",
                session_id="S1",
                gateway="web",
                accepted_at=receipt["committed_at"],
            )
            conn.execute("CREATE TABLE candidate (body TEXT)")
            conn.execute("INSERT INTO candidate VALUES ('private-projection')")
            conn.commit()
            conn.close()
            monkeypatch.setattr(schema, "MIGRATIONS", registry)
            conn, memory = opened(path)
            f = memory.forgetting
            if unknown:
                f.register_group("U2", kind="run", container_id="S2", context=CONTEXT)
            before = f.get_forgetting("legacy")
            conn.execute("PRAGMA query_only=ON")
            rejected = (
                f.retry_cleanup("legacy", CONTEXT)
                if method == "retry"
                else f.reconcile_projections(CONTEXT)
            )
            assert rejected == {
                "error": {"code": "storage_write_failed", "failure_recorded": False}
            }
            assert f.get_forgetting("legacy") == before
            conn.execute("PRAGMA query_only=OFF")
            if failure_type == "sqlite":
                conn.execute(
                    "CREATE TRIGGER refuse_projection BEFORE UPDATE ON candidate "
                    "BEGIN SELECT RAISE(ABORT,'private-sql-error'); END"
                )
            else:
                participant_error[0] = (
                    ValueError if failure_type == "value" else RuntimeError
                )
            result = (
                f.retry_cleanup("legacy", CONTEXT)
                if method == "retry"
                else f.reconcile_projections(CONTEXT)
            )
            assert result["error"]["code"] == "storage_write_failed"
            state = f.get_forgetting("legacy")
            assert state["state"] == ("needs_scope" if unknown else "failed")
            assert (
                state["projection_recovery_error"] == "projection_invalidation_failed"
            )
            assert result["error"]["failure_recorded"] is True
            assert "private-" not in str(state)
            assert memory.get_operation("legacy") == receipt
            assert conn.execute("SELECT body FROM candidate").fetchone() == (
                "private-projection",
            )
            conn.close()
            conn, memory = opened(path)
            f = memory.forgetting
            assert f.get_forgetting("legacy") == state
            if failure_type == "sqlite":
                conn.execute("DROP TRIGGER refuse_projection")
            participant_error[0] = None
            if method == "retry":
                f.retry_cleanup("legacy", CONTEXT)
            else:
                f.reconcile_projections(CONTEXT)
            after = f.get_forgetting("legacy")
            assert after["projection_recovery_error"] is None
            assert after["state"] == ("needs_scope" if unknown else "complete")
            assert memory.get_operation("legacy") == receipt
            assert conn.execute("SELECT body FROM candidate").fetchone() == (None,)
            conn.close()


def test_direct_legacy_host_cleanup_and_foreign_claim_are_safe(tmp_path):
    import sys

    import pytest

    from agent_alfred.evals.deterministic.test_host_lifecycle import _host
    from agent_alfred.resource_rollback import OwnedLock, ResumableRollback
    from agent_alfred.runtime.host import RuntimeHost

    for phase in ("begin", "end"):
        host, conn, _, _ = _host()
        if phase == "end":
            assert host.try_begin_mutation() is None
        target = (
            RuntimeHost.try_begin_mutation
            if phase == "begin"
            else RuntimeHost.end_mutation
        ).__code__
        signal = KeyboardInterrupt("legacy-close-entry")

        def interrupt(frame, event, arg):
            if (
                event == "call"
                and frame.f_code is OwnedLock.close.__code__
                and frame.f_back.f_code is target
            ):
                raise signal
            return interrupt

        previous = sys.gettrace()
        try:
            sys.settrace(interrupt)
            with pytest.raises(KeyboardInterrupt) as raised:
                (host.try_begin_mutation if phase == "begin" else host.end_mutation)()
            assert raised.value is signal
        finally:
            sys.settrace(previous)
        try:
            available = host._lock.acquire(blocking=False)
            if available:
                host._lock.release()
            assert available and not host._mutating
            owner = ResumableRollback()
            assert host.try_begin_mutation(owner=owner) is None
            host.end_mutation()  # Stale/no implicit claim cannot release this owner.
            assert host.try_begin_mutation() == "mutation_in_flight"
            host.end_mutation(owner=owner)
            assert host.execute_mutation(lambda: "next") == ("next", None)
        finally:
            host.close()
            conn.close()


def test_related_dashboard_mutations_recover_host_return_interruptions(tmp_path):
    import sys

    import pytest

    from agent_alfred.evals.deterministic.test_host_lifecycle import _host
    from agent_alfred.gateway.web.api import DashboardApi
    from agent_alfred.runtime.host import RuntimeHost

    for route in ("settings", "env", "probe"):
        for phase in ("begin", "end"):
            host, conn, _, _ = _host()
            api = DashboardApi(facade=host)
            target = (
                RuntimeHost.try_begin_mutation
                if phase == "begin"
                else RuntimeHost.end_mutation
            ).__code__
            signal = KeyboardInterrupt(f"{route}-{phase}")

            def interrupt(frame, event, arg):
                if event == "return" and frame.f_code is target:
                    raise signal
                return interrupt

            previous = sys.gettrace()
            try:
                sys.settrace(interrupt)
                with pytest.raises(KeyboardInterrupt) as raised:
                    if route == "settings":
                        api.mutate_settings({})
                    elif route == "env":
                        api.reread_env()
                    else:
                        # Unknown endpoint is rejected locally; no credential probe IO.
                        api.probe_auth({"endpoint_id": "unconfigured-test-endpoint"})
                assert raised.value is signal
            finally:
                sys.settrace(previous)
            try:
                sid, refusal = api._gate.create_session()
                assert sid and refusal is None
            finally:
                host.close()
                conn.close()


@pytest.mark.parametrize(
    "scope_failure",
    (None, "runtime", "generator", "control", "control_insert", "control_commit"),
)
def test_completed_target_reconcile_rollback_stays_unreadable_after_restart(
    tmp_path, scope_failure
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    class Projection:
        failure = None
        scope_error = None

        def reconciliation_targets(self, conn, *, memories, isolated):
            if scope_failure == "generator":
                yield "mirror"
            if self.scope_error:
                raise self.scope_error
            yield "mirror"

        def invalidate(self, conn, *, memories, isolated):
            conn.execute("UPDATE candidate SET body=NULL")
            if self.failure:
                raise self.failure
            return ("mirror",)

    class FilePort:
        def verify(self, target, generation):
            path = tmp_path / target
            return path.exists() and path.read_text() == str(generation)

        def rebuild(self, target, generation):
            (tmp_path / target).write_text(str(generation))

    projection = Projection()

    def opened(conn):
        base = service(conn)
        return MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=base._key,
            clock=base._clock,
            projection_participants=(projection,),
            cleanup_port=FilePort(),
            projection_inventory_evidence_id="complete-inventory",
        )

    path = tmp_path / "memory.db"
    conn = sqlite3.connect(path)
    memory = opened(conn)
    conn.execute("CREATE TABLE candidate (body TEXT)")
    conn.execute("INSERT INTO candidate VALUES ('private-body')")
    conn.commit()
    receipt = delete(memory, save(memory))
    f = memory.forgetting
    assert f.retry_cleanup("delete", CONTEXT)["state"] == "complete"
    assert f.managed_target_readable("mirror")
    conn.execute("UPDATE candidate SET body='rediscovered-body'")
    conn.commit()
    if scope_failure:
        projection.scope_error = (
            KeyboardInterrupt("private-control")
            if scope_failure.startswith("control")
            else RuntimeError("private-scope-error")
        )
    else:
        projection.failure = RuntimeError("private-execution-error")
    if scope_failure in ("control_insert", "control_commit"):

        def deny_intent(action, table, *args):
            denied = (
                action == sqlite3.SQLITE_INSERT and table == "forget_projection_fences"
                if scope_failure == "control_insert"
                else action == sqlite3.SQLITE_TRANSACTION and table == "COMMIT"
            )
            return sqlite3.SQLITE_DENY if denied else sqlite3.SQLITE_OK

        conn.set_authorizer(deny_intent)
        with pytest.raises(KeyboardInterrupt) as caught:
            f.reconcile_projections(CONTEXT)
        assert caught.value is projection.scope_error
        conn.set_authorizer(None)
        assert not conn.in_transaction
    if scope_failure and scope_failure.startswith("control"):
        with pytest.raises(KeyboardInterrupt) as caught:
            f.reconcile_projections(CONTEXT)
        assert caught.value is projection.scope_error
        assert f.get_forgetting("delete")["state"] == "cleaning"
    else:
        assert f.reconcile_projections(CONTEXT)["error"]["failure_recorded"]
        assert f.get_forgetting("delete")["state"] == "failed"
    assert not f.managed_target_readable("mirror")
    assert conn.execute("SELECT body FROM candidate").fetchone() == (
        "rediscovered-body",
    )
    conn.close()
    conn = sqlite3.connect(path)
    memory = opened(conn)
    f = memory.forgetting
    assert not f.managed_target_readable("mirror")
    projection.failure = None
    projection.scope_error = None
    assert f.reconcile_projections(CONTEXT) == {"status": "reconciled"}
    assert not f.managed_target_readable("mirror")
    assert f.retry_cleanup("delete", CONTEXT)["state"] == "complete"
    assert f.managed_target_readable("mirror")
    assert memory.get_operation("delete") == receipt
    conn.close()


@pytest.mark.parametrize("failure_mode", ("runtime", "control", "record_denied"))
@pytest.mark.parametrize("unknown", (False, True))
def test_reconcile_fences_shared_targets_across_failure_and_restart(
    tmp_path, failure_mode, unknown
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    class Projection:
        targets = {}
        rediscovered = False
        failure = None
        fail_memory = None

        def reconciliation_targets(self, conn, *, memories, isolated):
            return self.affected(memories)

        def affected(self, memories):
            result = set()
            for _, memory_id in memories:
                if not self.rediscovered or memory_id != records[2]["memory_id"]:
                    result.update(self.targets.get(memory_id, ()))
            return result

        def invalidate(self, conn, *, memories, isolated):
            for _, memory_id in memories:
                conn.execute(
                    "UPDATE candidate SET body=NULL WHERE memory_id=?", (memory_id,)
                )
                if self.failure and memory_id == self.fail_memory:
                    raise self.failure
            return self.affected(memories)

    class FilePort:
        fail = False

        def verify(self, target, generation):
            path = tmp_path / target
            return path.exists() and path.read_text() == str(generation)

        def rebuild(self, target, generation):
            if self.fail:
                raise OSError("private-file-fault")
            (tmp_path / target).write_text(str(generation))

    projection, port = Projection(), FilePort()
    records = []

    def opened(conn):
        base = service(conn)
        return MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=base._key,
            clock=base._clock,
            projection_participants=(projection,),
            cleanup_port=port,
            projection_inventory_evidence_id="trusted-complete-inventory",
        )

    path = tmp_path / "database"
    conn = sqlite3.connect(path)
    memory = opened(conn)
    conn.execute("CREATE TABLE candidate (memory_id TEXT, body TEXT)")
    conn.commit()
    receipts = []
    for number, targets in enumerate(
        (("shared", "a-only"), ("shared", "b-only"), ("safe",))
    ):
        record = save(memory, f"save-{number}")
        records.append(record)
        projection.targets[record["memory_id"]] = targets
        receipts.append(delete(memory, record, f"delete-{number}"))
        assert (
            memory.forgetting.retry_cleanup(f"delete-{number}", CONTEXT)["state"]
            == "complete"
        )
    f = memory.forgetting
    assert all(
        f.managed_target_readable(t) for t in ("shared", "a-only", "b-only", "safe")
    )
    if unknown:
        f.register_group(
            "unknown", kind="run", container_id="other-session", context=CONTEXT
        )
    projection.rediscovered = True
    projection.fail_memory = records[1]["memory_id"]
    projection.failure = (
        KeyboardInterrupt("control")
        if failure_mode == "control"
        else RuntimeError("private-error")
    )
    conn.executemany(
        "INSERT INTO candidate VALUES (?,'rediscovered')",
        [(r["memory_id"],) for r in records],
    )
    conn.commit()
    if failure_mode == "record_denied":
        conn.set_authorizer(
            lambda action, table, *args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT
                and table == "forget_projection_recovery"
                else sqlite3.SQLITE_OK
            )
        )
    if failure_mode == "control":
        with pytest.raises(KeyboardInterrupt) as caught:
            f.reconcile_projections(CONTEXT)
        assert caught.value is projection.failure
    else:
        result = f.reconcile_projections(CONTEXT)
        assert result["error"]["failure_recorded"] == (failure_mode != "record_denied")
    conn.set_authorizer(None)
    assert not conn.in_transaction
    assert (
        conn.execute("SELECT body FROM candidate").fetchall() == [("rediscovered",)] * 3
    )
    for target in ("shared", "a-only", "b-only"):
        assert not f.managed_target_readable(target)
    assert f.managed_target_readable("safe")
    expected = (
        "needs_scope"
        if unknown
        else ("failed" if failure_mode == "runtime" else "cleaning")
    )
    assert f.get_forgetting("delete-1")["state"] == expected
    assert [memory.get_operation(f"delete-{n}") for n in range(3)] == receipts
    conn.close()
    conn = sqlite3.connect(path)
    memory = opened(conn)
    f = memory.forgetting
    assert not f.managed_target_readable("shared")
    assert f.managed_target_readable("safe")
    assert f.get_forgetting("delete-1")["state"] == expected
    # Restoring A cannot clear B's independent obligation on the shared output.
    f.retry_cleanup("delete-0", CONTEXT)
    assert not f.managed_target_readable("shared")
    assert f.managed_target_readable("a-only")
    projection.failure = None
    port.fail = True
    f.retry_cleanup("delete-1", CONTEXT)
    assert not f.managed_target_readable("shared")
    port.fail = False
    f.retry_cleanup("delete-1", CONTEXT)
    assert all(
        f.managed_target_readable(t) for t in ("shared", "a-only", "b-only", "safe")
    )
    assert [memory.get_operation(f"delete-{n}") for n in range(3)] == receipts
    assert f.get_forgetting("delete-1")["state"] == (
        "needs_scope" if unknown else "complete"
    )
    conn.close()


@pytest.mark.parametrize("order", ("first", "last", "partial", "legacy", "opaque"))
def test_incomplete_declarations_fence_same_round_new_shared_target(tmp_path, order):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    class Projection:
        discovery = False
        failed_memory = None
        fail = True

        def reconciliation_targets(self, conn, *, memories, isolated):
            if not self.discovery:
                return
            if order == "partial":
                yield "partial-target"
            if memories[0][1] == self.failed_memory and self.fail:
                raise RuntimeError("private-scope-failure")
            yield "new-shared"

        def invalidate(self, conn, *, memories, isolated):
            if (
                self.discovery
                and order in ("legacy", "opaque")
                and self.fail
                and memories[0][1] == self.failed_memory
            ):
                raise RuntimeError("private-execution-failure")
            return ("new-shared",) if self.discovery else ()

    class Opaque:
        def invalidate(self, conn, *, memories, isolated):
            return projection.invalidate(conn, memories=memories, isolated=isolated)

    class Legacy:
        def invalidate(self, conn, *, memories, isolated):
            return ()

    class Port:
        def verify(self, target, generation):
            file = tmp_path / target
            return file.exists() and file.read_text() == str(generation)

        def rebuild(self, target, generation):
            (tmp_path / target).write_text(str(generation))

    conn = sqlite3.connect(tmp_path / "db")
    base = service(conn)
    projection = Projection()
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=base._clock,
        projection_participants=(
            (Opaque(),)
            if order == "opaque"
            else (projection, Legacy())
            if order == "legacy"
            else (projection,)
        ),
        projection_inventory_evidence_id="trusted-inventory",
        cleanup_port=Port(),
    )
    records = [save(memory, f"save-{n}") for n in range(2)]
    for n, record in enumerate(records):
        delete(memory, record, f"D{n}")
    failed = 0 if order == "first" else 1
    projection.failed_memory = records[failed]["memory_id"]
    projection.discovery = True
    f = memory.forgetting
    assert f.reconcile_projections(CONTEXT)["error"]["failure_recorded"]
    assert f.get_forgetting(f"D{1 - failed}")["projection_recovery_pending"]
    f.retry_cleanup(f"D{1 - failed}", CONTEXT)
    assert not f.managed_target_readable("new-shared")
    assert f.get_forgetting(f"D{failed}")["state"] == "failed"
    projection.fail = False
    f.retry_cleanup(f"D{failed}", CONTEXT)
    assert f.managed_target_readable("new-shared")
    conn.close()


def test_restart_without_inventory_cannot_clear_new_recovery_obligations(tmp_path):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    class Interrupted:
        def invalidate(self, conn, *, memories, isolated):
            raise KeyboardInterrupt("private-control")

    path = tmp_path / "db"
    conn = sqlite3.connect(path)
    base = service(conn)
    receipt = delete(base, save(base))
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=base._clock,
        projection_participants=(Interrupted(),),
        projection_inventory_evidence_id="full-inventory",
    )
    with pytest.raises(KeyboardInterrupt):
        memory.forgetting.reconcile_projections(CONTEXT)
    before = memory.forgetting.get_forgetting("delete")
    assert before["projection_recovery_pending"]
    assert before["state"] == "cleaning"
    conn.close()
    conn = sqlite3.connect(path)
    memory = service(conn)
    for invoke in (
        lambda: memory.forgetting.retry_cleanup("delete", CONTEXT),
        lambda: memory.forgetting.reconcile_projections(CONTEXT),
    ):
        assert invoke() == {"error": {"code": "projection_inventory_required"}}
        assert memory.forgetting.get_forgetting("delete") == before
        assert memory.get_operation("delete") == receipt
    conn.close()


@pytest.mark.parametrize("deny_commit", (False, True))
@pytest.mark.parametrize("participant_count", (1, 2))
@pytest.mark.parametrize(
    "signal_type", (KeyboardInterrupt, RuntimeError, ValueError, sqlite3.DatabaseError)
)
@pytest.mark.parametrize("entry", ("reconcile", "retry"))
@pytest.mark.parametrize("eventual_release", (False, True))
def test_declaration_cleanup_owner_survives_secondary_database_failure(
    tmp_path, deny_commit, participant_count, signal_type, entry, eventual_release
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback
    from agent_alfred.runtime.recording import RecordingStore

    def exception_graph(error):
        seen, pending, found = set(), [error], []
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            found.append(current)
            pending.extend((current.__cause__, current.__context__))
        return found

    class Declaration:
        signal = KeyboardInterrupt("private-control")
        allow_release = False
        releases = 0
        effects = 0
        release_after = None
        attempts = 0

        def __init__(self):
            self.lock = threading.Lock()

        def reconciliation_targets(self, conn, *, memories, isolated):
            assert self.lock.acquire(blocking=False)
            cleanup = ResumableRollback()

            def release():
                self.attempts += 1
                if not self.allow_release and (
                    self.release_after is None or self.attempts < self.release_after
                ):
                    return False
                self.lock.release()
                self.releases += 1

            cleanup.own(self.lock, release)
            yield "mirror"
            cleanup.raise_failure(self.signal)

        def invalidate(self, conn, *, memories, isolated):
            self.effects += 1
            return ()

    path = tmp_path / "db"
    conn = sqlite3.connect(path)
    base = service(conn)
    receipt = delete(base, save(base))
    declaration = Declaration()
    later = Declaration()
    later.signal = KeyboardInterrupt("private-later-control")
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=base._clock,
        projection_participants=(
            (declaration,) if participant_count == 1 else (declaration, later)
        ),
        projection_inventory_evidence_id="trusted-inventory",
    )
    f = memory.forgetting
    # Establish a real, durable restriction before the secondary failure case.
    with pytest.raises(KeyboardInterrupt) as first:
        f.reconcile_projections(CONTEXT)
    handles = [
        e for e in exception_graph(first.value) if isinstance(e, IncompleteRollback)
    ]
    assert len(handles) == 1
    assert not later.lock.locked()
    declaration.allow_release = True
    assert handles[0].retry()
    assert handles[0].retry()
    assert declaration.releases == 1
    assert not declaration.lock.locked()
    before = f.get_forgetting("delete")
    assert not f.managed_target_readable("mirror")

    declaration.allow_release = False
    declaration.signal = signal_type("private-second-control")
    declaration.attempts = 0
    declaration.release_after = 3 if eventual_release else None
    if deny_commit:
        conn.set_authorizer(
            lambda action, name, *args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and name == "COMMIT"
                else sqlite3.SQLITE_OK
            )
        )
    with pytest.raises(signal_type) as caught:
        if entry == "reconcile":
            f.reconcile_projections(CONTEXT)
        else:
            f.retry_cleanup("delete", CONTEXT)
    conn.set_authorizer(None)
    assert caught.value is declaration.signal
    graph = exception_graph(caught.value)
    handles = [e for e in graph if isinstance(e, IncompleteRollback)]
    assert len(handles) == 1
    assert (
        any(
            isinstance(e, sqlite3.DatabaseError) and e is not caught.value
            for e in graph
        )
        == deny_commit
    )
    assert declaration.effects == 0
    assert later.effects == 0
    assert not later.lock.locked()
    assert not conn.in_transaction
    state = f.get_forgetting("delete")
    if signal_type is KeyboardInterrupt or deny_commit:
        assert state == before
    else:
        assert state["state"] == "failed"
        assert state["projection_recovery_error"] == "projection_invalidation_failed"
    assert memory.get_operation("delete") == receipt
    assert not f.managed_target_readable("mirror")
    # The only recovery handle comes from the public exception graph above.
    if eventual_release:
        assert declaration.attempts == 1
        assert not handles[0].retry()
    else:
        declaration.allow_release = True
    assert handles[0].retry()
    assert handles[0].retry()
    assert not declaration.lock.locked()
    assert declaration.releases == 2
    assert (
        any(
            isinstance(e, sqlite3.DatabaseError) and e is not caught.value
            for e in exception_graph(caught.value)
        )
        == deny_commit
    )
    conn.close()
    conn = sqlite3.connect(path)
    reopened = service(conn)
    assert not reopened.forgetting.managed_target_readable("mirror")
    assert reopened.get_operation("delete") == receipt
    assert reopened.forgetting.get_forgetting("delete") == state
    assert save(reopened, "after-recovery")["status"] == "saved"
    conn.close()


@pytest.mark.parametrize("phase", ("verify", "rebuild"))
@pytest.mark.parametrize("unknown", (False, True))
@pytest.mark.parametrize("deny_commit", (False, True))
@pytest.mark.parametrize(
    "signal_type", (RuntimeError, ValueError, sqlite3.DatabaseError, KeyboardInterrupt)
)
def test_file_failure_is_recorded_before_returning_cleanup_owner(
    tmp_path, phase, unknown, deny_commit, signal_type
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback
    from agent_alfred.runtime.recording import RecordingStore

    class Projection:
        def invalidate(self, conn, *, memories, isolated):
            return ("mirror",)

    class Port:
        armed = True
        attempts = 0
        releases = 0
        lock = threading.Lock()

        def fail(self):
            assert self.lock.acquire(blocking=False)
            owner = ResumableRollback()

            def release():
                self.attempts += 1
                if self.attempts < 3:
                    return False
                self.lock.release()
                self.releases += 1

            owner.own(self.lock, release)
            if deny_commit:
                conn.set_authorizer(
                    lambda action, name, *args: (
                        sqlite3.SQLITE_DENY
                        if action == sqlite3.SQLITE_TRANSACTION and name == "COMMIT"
                        else sqlite3.SQLITE_OK
                    )
                )
            owner.raise_failure(signal)

        def verify(self, target, generation):
            if self.armed and phase == "verify":
                self.fail()
            return not self.armed

        def rebuild(self, target, generation):
            if self.armed:
                self.fail()

    path = tmp_path / "db"
    conn = sqlite3.connect(path)
    base = service(conn)
    port = Port()
    signal = signal_type("private-file-failure")

    def make():
        return MemoryCommandService(
            recording_store=RecordingStore(conn, threading.Lock()),
            audit_key=base._key,
            clock=base._clock,
            projection_participants=(Projection(),),
            cleanup_port=port,
            projection_inventory_evidence_id="trusted",
        )

    memory = make()
    if unknown:
        memory.forgetting.register_group(
            "U1", kind="run", container_id="S1", context=CONTEXT
        )
    receipt = delete(memory, save(memory))
    with pytest.raises(signal_type) as caught:
        memory.forgetting.retry_cleanup("delete", CONTEXT)
    conn.set_authorizer(None)
    assert caught.value is signal
    seen, todo, graph = set(), [caught.value], []
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        todo.extend((current.__cause__, current.__context__))
    handles = [e for e in graph if isinstance(e, IncompleteRollback)]
    assert len(handles) == 1
    control = signal_type is KeyboardInterrupt
    assert any(isinstance(e, sqlite3.Error) and e is not signal for e in graph) == (
        deny_commit and not control
    )
    assert not conn.in_transaction
    state = memory.forgetting.get_forgetting("delete")
    persisted_failure = not control and not deny_commit
    assert state["state"] == (
        "needs_scope" if unknown else "failed" if persisted_failure else "cleaning"
    )
    assert state["cleanup"][0]["state"] == (
        "failed" if persisted_failure else "running"
    )
    assert state["cleanup"][0]["error"] == (
        "cleanup_unconfirmed" if persisted_failure else None
    )
    assert not memory.forgetting.managed_target_readable("mirror")
    assert port.attempts == 1
    assert not handles[0].retry()
    assert handles[0].retry()
    assert handles[0].retry()
    assert port.releases == 1 and not port.lock.locked()
    assert memory.forgetting.get_forgetting("delete") == state
    conn.close()
    conn = sqlite3.connect(path)
    memory = make()
    assert memory.forgetting.get_forgetting("delete") == state
    assert not memory.forgetting.managed_target_readable("mirror")
    assert memory.get_operation("delete") == receipt
    port.armed = False
    result = memory.forgetting.retry_cleanup("delete", CONTEXT)
    assert result["state"] == ("needs_scope" if unknown else "complete")
    assert result["cleanup"][0]["state"] == "complete"
    assert memory.forgetting.managed_target_readable("mirror")
    assert memory.get_operation("delete") == receipt
    assert save(memory, "after-recovery")["status"] == "saved"
    conn.close()


@pytest.mark.parametrize("signal_type", (ValueError, RuntimeError))
@pytest.mark.parametrize("stage", ("UPDATE", "COMMIT"))
def test_plain_file_failure_with_unwritable_result_is_storage_failure(
    tmp_path, signal_type, stage
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.runtime.recording import RecordingStore

    class Projection:
        def invalidate(self, conn, *, memories, isolated):
            return ("mirror",)

    class Port:
        def verify(self, target, generation):
            conn.set_authorizer(
                lambda action, name, *args: (
                    sqlite3.SQLITE_DENY
                    if (
                        stage == "COMMIT"
                        and action == sqlite3.SQLITE_TRANSACTION
                        and name == "COMMIT"
                    )
                    or (
                        stage == "UPDATE"
                        and action == sqlite3.SQLITE_UPDATE
                        and name == "forget_cleanup"
                    )
                    else sqlite3.SQLITE_OK
                )
            )
            raise signal_type("private-file-failure")

    conn = sqlite3.connect(tmp_path / "db")
    base = service(conn)
    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=base._clock,
        projection_participants=(Projection(),),
        cleanup_port=Port(),
    )
    receipt = delete(memory, save(memory))
    try:
        result = memory.forgetting.retry_cleanup("delete", CONTEXT)
    finally:
        conn.set_authorizer(None)
    assert result == {"error": {"code": "storage_write_failed"}}
    assert not conn.in_transaction
    assert memory.forgetting.get_forgetting("delete")["state"] == "cleaning"
    assert not memory.forgetting.managed_target_readable("mirror")
    assert memory.get_operation("delete") == receipt
    conn.close()


@pytest.mark.parametrize("route", ("file", "projection_retry", "projection_reconcile"))
@pytest.mark.parametrize("secondary_type", (sqlite3.DatabaseError, KeyboardInterrupt))
def test_file_cleanup_owner_survives_failure_bookkeeping_control(
    tmp_path, route, secondary_type
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback
    from agent_alfred.runtime.recording import RecordingStore

    armed = False
    lock = threading.Lock()
    context_lock = threading.Lock()
    enabled = False
    seed_recovery = False
    release_allowed = False
    releases = []
    original = ValueError("private-file-failure")
    control = secondary_type("private-bookkeeping-control")

    class Projection:
        def reconciliation_targets(self, conn, *, memories, isolated):
            yield "mirror"
            if seed_recovery:
                raise KeyboardInterrupt("seed durable recovery")

        def invalidate(self, conn, *, memories, isolated):
            if enabled and route != "file":
                Port().rebuild("mirror", 1)
            return ("mirror",)

    class Port:
        def verify(self, target, generation):
            return False

        def rebuild(self, target, generation):
            nonlocal armed
            assert lock.acquire(blocking=False)
            owner = ResumableRollback()

            def release():
                if not release_allowed:
                    return False
                lock.release()
                releases.append(True)

            owner.own(lock, release)
            armed = True
            assert context_lock.acquire(blocking=False)
            dependency = ResumableRollback()

            def release_dependency():
                if not release_allowed:
                    return False
                context_lock.release()
                releases.append("dependency")

            dependency.own(context_lock, release_dependency)
            try:
                dependency.raise_failure(RuntimeError("private-dependency"))
            except RuntimeError:
                owner.raise_failure(original)

    conn = sqlite3.connect(tmp_path / "db")
    base = service(conn)

    def clock():
        if armed:
            raise control
        return base._clock()

    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=clock,
        projection_participants=(Projection(),),
        cleanup_port=Port(),
        projection_inventory_evidence_id="trusted",
    )
    receipt = delete(memory, save(memory))
    if route != "file":
        seed_recovery = True
        with pytest.raises(KeyboardInterrupt):
            memory.forgetting.reconcile_projections(CONTEXT)
        seed_recovery = False
    enabled = True
    expected = control if secondary_type is KeyboardInterrupt else original
    with pytest.raises(type(expected)) as caught:
        if route == "projection_reconcile":
            memory.forgetting.reconcile_projections(CONTEXT)
        else:
            memory.forgetting.retry_cleanup("delete", CONTEXT)
    armed = False
    assert caught.value is expected
    seen, todo, graph = set(), [caught.value], []
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        todo.extend((current.__cause__, current.__context__))
    assert original in graph and control in graph
    handles = [e for e in graph if isinstance(e, IncompleteRollback)]
    assert len(handles) == 2
    release_allowed = True
    for handle in handles:
        assert handle.retry() and handle.retry()
    assert len(releases) == 2 and True in releases and "dependency" in releases
    assert not lock.locked() and not context_lock.locked()
    assert not conn.in_transaction
    assert memory.forgetting.get_forgetting("delete")["state"] == "cleaning"
    assert not memory.forgetting.managed_target_readable("mirror")
    assert memory.get_operation("delete") == receipt
    assert save(memory, "after-control")["status"] == "saved"
    conn.close()


@pytest.mark.parametrize("nested", (False, True))
@pytest.mark.parametrize("entry", ("reconcile", "retry"))
@pytest.mark.parametrize("phase", ("clock", "notify"))
@pytest.mark.parametrize("original_type", (ValueError, KeyboardInterrupt))
@pytest.mark.parametrize(
    "secondary_type",
    (RuntimeError, sqlite3.DatabaseError, KeyboardInterrupt, SystemExit),
)
def test_declaration_owners_survive_later_recovery_failure(
    tmp_path, entry, phase, original_type, secondary_type, nested
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback
    from agent_alfred.runtime.recording import RecordingStore

    class Resource:
        def __init__(self):
            self.lock = threading.Lock()
            self.allowed = False
            self.releases = 0
            self.attempts = 0

        def fail(self, signal):
            assert self.lock.acquire(blocking=False)
            owner = ResumableRollback()

            def release():
                self.attempts += 1
                if not self.allowed:
                    return False
                self.lock.release()
                self.releases += 1

            owner.own(self.lock, release)
            owner.raise_failure(signal)

    def graph(error):
        seen, pending, found = set(), [error], []
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            found.append(current)
            pending.extend((current.__cause__, current.__context__))
        return found

    declaration = Resource()
    bookkeeping = Resource()
    declaration_context = Resource()
    bookkeeping_context = Resource()

    def fail(resource, dependency, signal):
        if nested:
            try:
                dependency.fail(RuntimeError("private-dependency-failure"))
            except RuntimeError:
                resource.fail(signal)
        else:
            resource.fail(signal)

    original = original_type("private-declaration-failure")
    secondary = secondary_type("private-recovery-failure")
    inject = False
    target = "mirror-1"
    effects = []

    class Projection:
        def reconciliation_targets(self, conn, *, memories, isolated):
            yield target
            fail(declaration, declaration_context, original)

        def invalidate(self, conn, *, memories, isolated):
            effects.append(True)
            return ()

    def fault():
        nonlocal inject
        if inject:
            inject = False
            fail(bookkeeping, bookkeeping_context, secondary)

    path = tmp_path / "db"
    conn = sqlite3.connect(path)
    base = service(conn)
    receipt = delete(base, save(base))

    def clock():
        if phase == "clock":
            fault()
        return base._clock()

    def notify(payload):
        if phase == "notify":
            fault()

    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=clock,
        memory_notifier=notify,
        projection_participants=(Projection(),),
        projection_inventory_evidence_id="trusted",
    )
    with pytest.raises(original_type) as first:
        memory.forgetting.reconcile_projections(CONTEXT)
    handles = [e for e in graph(first.value) if isinstance(e, IncompleteRollback)]
    assert len(handles) == (2 if nested else 1)
    declaration.allowed = declaration_context.allowed = True
    for handle in handles:
        assert handle.retry() and handle.retry()
    declaration.allowed = declaration_context.allowed = False
    original = original_type("private-next-declaration-failure")
    target = "mirror-2"
    inject = True
    expected = (
        original
        if original_type is KeyboardInterrupt or issubclass(secondary_type, Exception)
        else secondary
    )
    with pytest.raises(type(expected)) as caught:
        if entry == "reconcile":
            memory.forgetting.reconcile_projections(CONTEXT)
        else:
            memory.forgetting.retry_cleanup("delete", CONTEXT)
    assert caught.value is expected
    found = graph(caught.value)
    assert original in found and secondary in found
    handles = [e for e in found if isinstance(e, IncompleteRollback)]
    assert len(handles) == (4 if nested else 2)
    assert declaration.attempts == 3 and bookkeeping.attempts == 1
    declaration.allowed = bookkeeping.allowed = True
    declaration_context.allowed = bookkeeping_context.allowed = True
    for handle in handles:
        assert handle.retry() and handle.retry()
    assert declaration.releases == 2 and bookkeeping.releases == 1
    if nested:
        assert declaration_context.releases == 2
        assert bookkeeping_context.releases == 1
        assert not declaration_context.lock.locked()
        assert not bookkeeping_context.lock.locked()
    assert not declaration.lock.locked() and not bookkeeping.lock.locked()
    assert not effects and not conn.in_transaction
    assert not memory.forgetting.managed_target_readable("mirror-1")
    assert not memory.forgetting.managed_target_readable("mirror-2")
    assert memory.get_operation("delete") == receipt
    before = memory.forgetting.get_forgetting("delete")
    conn.close()
    conn = sqlite3.connect(path)
    reopened = service(conn)
    assert reopened.forgetting.get_forgetting("delete") == before
    assert not reopened.forgetting.managed_target_readable("mirror-1")
    assert reopened.get_operation("delete") == receipt
    assert save(reopened, "after-failure")["status"] == "saved"
    conn.close()


@pytest.mark.parametrize("entry", ("reconcile", "retry"))
@pytest.mark.parametrize("unknown", (False, True))
@pytest.mark.parametrize("multiple", (False, True))
@pytest.mark.parametrize("secondary_type", (RuntimeError, KeyboardInterrupt))
@pytest.mark.parametrize("deny_record", (False, True))
def test_declaration_failure_keeps_its_operation_before_notification(
    tmp_path, entry, unknown, multiple, secondary_type, deny_record
):
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback
    from agent_alfred.runtime.recording import RecordingStore

    class Resource:
        def __init__(self):
            self.lock = threading.Lock()
            self.allowed = False
            self.releases = 0

        def fail(self, error):
            assert self.lock.acquire(blocking=False)
            owner = ResumableRollback()

            def release():
                if not self.allowed:
                    return False
                self.lock.release()
                self.releases += 1

            owner.own(self.lock, release)
            owner.raise_failure(error)

    first, second = Resource(), Resource()
    original = ValueError("private-declaration")
    secondary = secondary_type("private-notify")
    seed = True
    notified = False
    effects = []

    class Projection:
        def reconciliation_targets(self, conn, *, memories, isolated):
            yield "seed" if seed else "mirror"
            if seed:
                raise KeyboardInterrupt("seed recovery")
            first.fail(original)

        def invalidate(self, conn, *, memories, isolated):
            effects.append(True)
            return ()

    path = tmp_path / "db"
    conn = sqlite3.connect(path)
    base = service(conn)
    if unknown:
        base.forgetting.register_group(
            "U1", kind="run", container_id="S1", context=CONTEXT
        )
    receipt = delete(base, save(base))
    other_receipt = (
        delete(base, save(base, "other-save"), "other-delete") if multiple else None
    )

    def notify(payload):
        nonlocal notified
        if seed or notified:
            return
        notified = True
        if deny_record:
            conn.set_authorizer(
                lambda action, name, *args: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_INSERT
                    and name == "forget_projection_recovery"
                    else sqlite3.SQLITE_OK
                )
            )
        second.fail(secondary)

    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=base._clock,
        memory_notifier=notify,
        projection_participants=(Projection(),),
        projection_inventory_evidence_id="trusted",
    )
    with pytest.raises(KeyboardInterrupt):
        memory.forgetting.reconcile_projections(CONTEXT)
    seed = False
    expected = secondary if secondary_type is KeyboardInterrupt else original
    with pytest.raises(type(expected)) as caught:
        if entry == "reconcile":
            memory.forgetting.reconcile_projections(CONTEXT)
        else:
            memory.forgetting.retry_cleanup("delete", CONTEXT)
    conn.set_authorizer(None)
    assert caught.value is expected and notified
    seen, todo, graph = set(), [caught.value], []
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        todo.extend((current.__cause__, current.__context__))
    handles = [e for e in graph if isinstance(e, IncompleteRollback)]
    assert len(handles) == 2 and original in graph and secondary in graph
    first.allowed = second.allowed = True
    for handle in handles:
        assert handle.retry() and handle.retry()
    assert first.releases == second.releases == 1
    state = memory.forgetting.get_forgetting("delete")
    recorded = secondary_type is RuntimeError and not deny_record
    assert state["state"] == (
        "needs_scope" if unknown else "failed" if recorded else "cleaning"
    )
    assert state["projection_recovery_error"] == (
        "projection_invalidation_failed" if recorded else None
    )
    if multiple:
        assert (
            memory.forgetting.get_forgetting("other-delete")[
                "projection_recovery_error"
            ]
            is None
        )
        assert memory.get_operation("other-delete") == other_receipt
    assert not effects and not conn.in_transaction
    assert memory.get_operation("delete") == receipt
    assert not memory.forgetting.managed_target_readable("mirror")
    conn.close()
    conn = sqlite3.connect(path)
    reopened = service(conn)
    assert reopened.forgetting.get_forgetting("delete") == state
    assert reopened.get_operation("delete") == receipt
    assert not reopened.forgetting.managed_target_readable("mirror")
    conn.close()


def _recovery_sigint_scenario(edge, original_control, secondary_control=False):
    """Child-only real signal experiment at the exception publication boundary."""
    import inspect
    import signal
    import sys
    import threading

    from agent_alfred.memory.commands import MemoryCommandService
    from agent_alfred.resource_rollback import (
        IncompleteRollback,
        ResumableRollback,
        reraise_failure,
        retain_failure_context,
    )
    from agent_alfred.runtime.recording import RecordingStore

    locks = [threading.Lock() for _ in range(3)]
    allowed = False
    releases = [0, 0, 0]
    armed = False
    primary = (
        KeyboardInterrupt("declaration")
        if original_control
        else ValueError("declaration")
    )
    secondary = (KeyboardInterrupt if secondary_control else sqlite3.DatabaseError)(
        "bookkeeping"
    )

    def fail(index, error):
        assert locks[index].acquire(blocking=False)
        owner = ResumableRollback()

        def release():
            if not allowed:
                return False
            locks[index].release()
            releases[index] += 1

        owner.own(locks[index], release)
        owner.raise_failure(error)

    class Projection:
        def reconciliation_targets(self, conn, *, memories, isolated):
            nonlocal armed
            yield "mirror"
            armed = True
            try:
                fail(0, RuntimeError("dependency"))
            except RuntimeError:
                fail(1, primary)

        def invalidate(self, conn, *, memories, isolated):
            raise AssertionError("effects must not run")

    conn = sqlite3.connect(":memory:")
    base = service(conn)
    receipt = delete(base, save(base))

    def clock():
        nonlocal armed
        if armed:
            armed = False
            fail(2, secondary)
        return base._clock()

    memory = MemoryCommandService(
        recording_store=RecordingStore(conn, threading.Lock()),
        audit_key=base._key,
        clock=clock,
        projection_participants=(Projection(),),
        projection_inventory_evidence_id="trusted",
    )
    if edge.startswith("caller_"):
        trace_function = getattr(
            memory.forgetting,
            "_prepare_projection_recovery_attempt",
            memory.forgetting._prepare_projection_recovery,
        ).__func__
    else:
        trace_function = retain_failure_context if edge == "retain" else reraise_failure
    source, start = inspect.getsourcelines(trace_function)
    markers = {
        "caller_handler": "primary = dominant_error(scope_failure, error)",
        "caller_call": "primary, earlier=scope_failure if primary is error else error",
        "retain": "if earlier is None",
        "snapshot": "previous = failure.__context__",
        "build": "bridge = RuntimeError",
        "cause": "bridge.__cause__ =",
        "context": "bridge.__context__ =",
        "publish": "failure.__cause__ = bridge",
        "raise": "raise failure",
    }
    # The v24 red fixture has only the post-destruction retention gap. New
    # candidates must exercise their actual publication edges, never skip them.
    legacy = any("retain_failure_context(failure, previous)" in line for line in source)
    marker = (
        "retain_failure_context(failure, previous)" if legacy else markers.get(edge)
    )
    line = (
        start + next(i for i, text in enumerate(source) if marker in text)
        if marker
        else None
    )
    fired = []

    def interrupt(frame, event, arg):
        if frame.f_code is trace_function.__code__ and not fired:
            hit = (
                event == "call"
                if edge == "call"
                else event == "line" and frame.f_lineno == line
            )
            if edge == "raised" and not legacy:
                hit = event == "exception" and arg[1] is (
                    secondary if secondary_control and not original_control else primary
                )
            if hit:
                fired.append(True)
                signal.raise_signal(signal.SIGINT)
        return interrupt

    sys.settrace(interrupt)
    try:
        memory.forgetting.reconcile_projections(CONTEXT)
    except BaseException as error:
        caught = error
    else:
        raise AssertionError("missing public failure")
    finally:
        sys.settrace(None)
    assert fired == [True]
    assert isinstance(caught, KeyboardInterrupt)
    if original_control:
        assert caught is primary
    todo, seen, graph = [caught], set(), []
    while todo:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        todo.extend((current.__cause__, current.__context__))
    assert primary in graph and secondary in graph
    handles = [e for e in graph if isinstance(e, IncompleteRollback)]
    assert len(handles) == 3
    allowed = True
    for handle in handles:
        assert handle.retry() and handle.retry()
    assert releases == [1, 1, 1] and not any(lock.locked() for lock in locks)
    assert not conn.in_transaction
    assert memory.get_operation("delete") == receipt
    assert not memory.forgetting.managed_target_readable("mirror")
    assert save(memory, "after-signal")["status"] == "saved"
    conn.close()
    print("injected=1 public_owners=3 releases=1,1,1 effects=0 transaction=0")


@pytest.mark.parametrize(
    "edge",
    ("snapshot", "build", "cause", "context", "publish", "raise", "raised", "call"),
)
@pytest.mark.parametrize("original_control", (False, True))
def test_public_recovery_survives_sigint_publication_edges(edge, original_control):
    import subprocess
    import sys

    command = (
        "from agent_alfred.evals.deterministic.test_forgetting "
        "import _recovery_sigint_scenario; "
        f"_recovery_sigint_scenario({edge!r}, {original_control!r})"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", command],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "injected=1 public_owners=3 releases=1,1,1" in result.stdout


@pytest.mark.parametrize("edge", ("retain", "raise", "raised", "call"))
def test_public_recovery_protects_earlier_graph_from_sigint(edge):
    import subprocess
    import sys

    command = (
        "from agent_alfred.evals.deterministic.test_forgetting "
        "import _recovery_sigint_scenario; "
        f"_recovery_sigint_scenario({edge!r}, False, True)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", command],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "injected=1 public_owners=3 releases=1,1,1" in result.stdout


@pytest.mark.parametrize("edge", ("caller_handler", "caller_call"))
@pytest.mark.parametrize("secondary_control", (False, True))
def test_public_recovery_caller_holds_graph_across_sigint(edge, secondary_control):
    import subprocess
    import sys

    command = (
        "from agent_alfred.evals.deterministic.test_forgetting "
        "import _recovery_sigint_scenario; "
        f"_recovery_sigint_scenario({edge!r}, False, {secondary_control!r})"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", command],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "injected=1 public_owners=3 releases=1,1,1" in result.stdout
