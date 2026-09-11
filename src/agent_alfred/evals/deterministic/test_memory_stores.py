"""Store contracts through caller-owned transactions and real SQLite FTS5."""

import hashlib
import hmac
import sqlite3
from datetime import UTC, datetime

import pytest

from agent_alfred.memory.semantic import SQLiteSemanticStore
from agent_alfred.memory.types import FactQuery, ManualOrigin
from agent_alfred.schema import migrate

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def fingerprint(value):
    return hmac.new(b"test-key", value, hashlib.sha256).hexdigest(), "test-key-id"


@pytest.fixture
def database():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    yield conn
    conn.close()


def test_save_is_searchable_and_preserves_first_spelling(database):
    store = SQLiteSemanticStore(database, fingerprint=fingerprint, clock=lambda: NOW)
    database.execute("BEGIN")
    first = store.save("  Ａlice ", "likes   coriander", ManualOrigin("web"))
    assert store.save("Alice", "likes coriander", ManualOrigin("cli")) == first
    record = store.get(first)
    assert record.subject == "  Ａlice "
    assert record.fact == "likes   coriander"
    assert record.origin == ManualOrigin("web")
    assert record.record_version == 1
    assert record.modified_at == NOW
    assert record.human_protected is True
    assert [hit.record.id for hit in store.search(FactQuery(text="coriander"))] == [
        first
    ]
    database.rollback()
    assert store.get(first) is None
    assert store.search(FactQuery(text="coriander")) == ()


def test_edit_compares_version_preserves_origin_and_does_not_merge_duplicates(database):
    from agent_alfred.memory.types import (
        ConsolidationOrigin,
        DuplicateConflict,
        UpdateApplied,
        VersionConflict,
    )

    store = SQLiteSemanticStore(database, fingerprint=fingerprint, clock=lambda: NOW)
    database.execute("BEGIN")
    original = store.save("Alice", "likes coriander", ConsolidationOrigin("batch"))
    other = store.save("Alice", "likes basil", ManualOrigin("web"))
    assert store.update(
        original, expected_version=1, origin=ManualOrigin("web"), fact="likes basil"
    ) == DuplicateConflict(other)
    assert store.get(original).fact == "likes coriander"
    assert store.update(
        original, expected_version=1, origin=ManualOrigin("web"), fact="likes mint"
    ) == UpdateApplied(original, True, 2)
    assert store.update(
        original, expected_version=1, origin=ManualOrigin("web"), fact="stale overwrite"
    ) == VersionConflict(2)
    assert store.update(
        original, expected_version=2, origin=ManualOrigin("web"), fact="likes mint"
    ) == UpdateApplied(original, False, 2)
    record = store.get(original)
    assert record.origin == ConsolidationOrigin("batch")
    assert record.last_change_origin == ManualOrigin("web")
    assert record.human_protected
    assert store.search(FactQuery(text="coriander")) == ()
    assert store.save("Alice", "likes mint", ManualOrigin("cli")) == original


def test_episode_half_open_intersections_and_utc_order(database):
    from agent_alfred.memory.episodic import SQLiteEpisodicStore
    from agent_alfred.memory.types import EpisodeQuery

    store = SQLiteEpisodicStore(database, fingerprint=fingerprint, clock=lambda: NOW)
    dt = datetime.fromisoformat
    database.execute("BEGIN")
    crossing = store.save(
        "conference",
        dt("2026-09-08T23:00:00+00:00"),
        dt("2026-09-09T02:00:00+00:00"),
        ManualOrigin("web"),
    )
    instant_id = store.save(
        "conference", dt("2026-09-09T08:00:00+08:00"), None, ManualOrigin("cli")
    )
    store.save("conference", dt("2026-09-10T00:00:00+00:00"), None, ManualOrigin("cli"))
    store.save("empty conference", NOW, NOW, ManualOrigin("web"))
    query = EpisodeQuery(since=NOW, until=dt("2026-09-10T00:00:00+00:00"))
    assert [hit.record.id for hit in store.search(query)] == [instant_id, crossing]
    assert store.search(EpisodeQuery(since=NOW, until=NOW)) == ()
    with pytest.raises(ValueError):
        store.save("naive", datetime(2026, 9, 9), None, ManualOrigin("web"))
    with pytest.raises(ValueError):
        store.save(
            "reversed", NOW, dt("2026-09-08T00:00:00+00:00"), ManualOrigin("web")
        )


def test_explicit_repeat_protects_without_content_version_and_blocks_automatic_update(
    database,
):
    from agent_alfred.memory.types import ConsolidationOrigin, ProtectedMemoryError

    store = SQLiteSemanticStore(database, fingerprint=fingerprint, clock=lambda: NOW)
    database.execute("BEGIN")
    id = store.save("Alice", "likes mint", ConsolidationOrigin("batch"))
    revision = store.memory_revision
    result = store.save_with_result("Alice", "likes mint", ManualOrigin("web"))
    assert (result.id, result.created, result.version) == (id, False, 1)
    assert store.memory_revision == revision + 1
    assert store.get(id).origin == ConsolidationOrigin("batch")
    with pytest.raises(ProtectedMemoryError):
        store.update(
            id,
            expected_version=1,
            origin=ConsolidationOrigin("next"),
            fact="likes pepper",
        )
    assert store.get(id).fact == "likes mint"
    from agent_alfred.memory.types import (
        ConsolidationApprovalProof,
        UpdateApplied,
    )

    proof = ConsolidationApprovalProof("approved-batch", 1)
    with pytest.raises(ProtectedMemoryError):
        store.apply_approved_consolidation(
            id,
            expected_version=1,
            origin=ConsolidationOrigin("approved-batch"),
            proof=proof,
            fact="likes pepper",
        )
    now = "2026-09-09T00:00:00+00:00"
    database.execute(
        """INSERT INTO memory_consolidation_batches (
             batch_id, session_id, revision, status, created_at, updated_at
           ) VALUES ('approved-batch', 's1', 1, 'awaiting_approval', ?, ?)""",
        (now, now),
    )
    database.execute(
        """INSERT INTO memory_consolidation_plans (
             batch_id, revision, plan_json, episode_summary, occurred_at,
             occurred_until, candidate_text
           ) VALUES ('approved-batch', 1, ?, NULL, NULL, NULL, NULL)""",
        (
            '{"semantic":[{"action":"update","id":"%s","subject":"Alice",'
            '"fact":"likes pepper","expected_version":1}],'
            '"episode_summary":"x"}' % id,
        ),
    )
    database.execute(
        "INSERT INTO memory_consolidation_approvals VALUES (?,?,?,?)",
        ("approved-batch", 1, now, "op-approve"),
    )
    with pytest.raises(ProtectedMemoryError):
        store.apply_approved_consolidation(
            id,
            expected_version=1,
            origin=ConsolidationOrigin("approved-batch"),
            proof=ConsolidationApprovalProof("approved-batch", 99),
            fact="likes pepper",
        )
    with pytest.raises(ProtectedMemoryError):
        store.apply_approved_consolidation(
            id,
            expected_version=1,
            origin=ConsolidationOrigin("approved-batch"),
            proof=proof,
            fact="likes thyme",
        )
    approved = store.apply_approved_consolidation(
        id,
        expected_version=1,
        origin=ConsolidationOrigin("approved-batch"),
        proof=proof,
        fact="likes pepper",
    )
    assert approved == UpdateApplied(id, True, 2)
    record = store.get(id)
    assert record.fact == "likes pepper"
    assert record.origin == ConsolidationOrigin("batch")
    assert record.last_change_origin == ConsolidationOrigin("approved-batch")
    assert record.human_protected is True


def test_approval_proof_binds_frozen_version_and_live_batch_lifecycle(database):
    from agent_alfred.memory.types import (
        ConsolidationApprovalProof,
        ConsolidationOrigin,
        ProtectedMemoryError,
    )

    store = SQLiteSemanticStore(database, fingerprint=fingerprint, clock=lambda: NOW)
    database.execute("BEGIN")
    identifier = store.save("home", "Beijing", ManualOrigin("web"))
    store.update(
        identifier,
        expected_version=1,
        origin=ManualOrigin("web"),
        fact="Guangzhou",
    )
    assert store.get(identifier).record_version == 2
    now = "2026-09-09T00:00:00+00:00"
    database.execute(
        """INSERT INTO memory_consolidation_batches (
             batch_id, session_id, revision, status, created_at, updated_at
           ) VALUES ('old-batch', 's1', 1, 'awaiting_approval', ?, ?)""",
        (now, now),
    )
    database.execute(
        """INSERT INTO memory_consolidation_plans (
             batch_id, revision, plan_json, episode_summary, occurred_at,
             occurred_until, candidate_text
           ) VALUES ('old-batch', 1, ?, NULL, NULL, NULL, NULL)""",
        (
            '{"semantic":[{"action":"update","id":"%s","subject":"home",'
            '"fact":"Shanghai","expected_version":1}],'
            '"episode_summary":"x"}' % identifier,
        ),
    )
    database.execute(
        "INSERT INTO memory_consolidation_approvals VALUES (?,?,?,?)",
        ("old-batch", 1, now, "op-stale"),
    )
    with pytest.raises(ProtectedMemoryError):
        store.apply_approved_consolidation(
            identifier,
            expected_version=2,
            origin=ConsolidationOrigin("old-batch"),
            proof=ConsolidationApprovalProof("old-batch", 1),
            subject="home",
            fact="Shanghai",
        )
    database.execute(
        """INSERT INTO memory_consolidation_batches (
             batch_id, session_id, revision, status, created_at, updated_at
           ) VALUES ('done-batch', 's2', 1, 'succeeded', ?, ?)""",
        (now, now),
    )
    database.execute(
        """INSERT INTO memory_consolidation_plans (
             batch_id, revision, plan_json, episode_summary, occurred_at,
             occurred_until, candidate_text
           ) VALUES ('done-batch', 1, ?, NULL, NULL, NULL, NULL)""",
        (
            '{"semantic":[{"action":"update","id":"%s","subject":"home",'
            '"fact":"Shanghai","expected_version":2}],'
            '"episode_summary":"x"}' % identifier,
        ),
    )
    database.execute(
        "INSERT INTO memory_consolidation_approvals VALUES (?,?,?,?)",
        ("done-batch", 1, now, "op-done"),
    )
    with pytest.raises(ProtectedMemoryError):
        store.apply_approved_consolidation(
            identifier,
            expected_version=2,
            origin=ConsolidationOrigin("done-batch"),
            proof=ConsolidationApprovalProof("done-batch", 1),
            subject="home",
            fact="Shanghai",
        )
    record = store.get(identifier)
    assert record.fact == "Guangzhou"
    assert record.record_version == 2


def test_episode_update_preserves_identity_and_clears_end_explicitly(database):
    from datetime import timedelta

    from agent_alfred.memory.episodic import SQLiteEpisodicStore
    from agent_alfred.memory.types import EpisodeQuery, UpdateApplied

    store = SQLiteEpisodicStore(database, fingerprint=fingerprint, clock=lambda: NOW)
    database.execute("BEGIN")
    id = store.save("conference", NOW, NOW + timedelta(hours=2), ManualOrigin("web"))
    assert store.update(
        id, expected_version=1, origin=ManualOrigin("web"), occurred_until=None
    ) == UpdateApplied(id, True, 2)
    assert store.get(id).occurred_until is None
    assert [hit.record.id for hit in store.search(EpisodeQuery(text="conference"))] == [
        id
    ]


@pytest.mark.parametrize("family", ["semantic", "episodic"])
def test_store_delete_is_versioned_hard_delete_and_faults_remain_faults(
    database, family
):
    from agent_alfred.memory.episodic import SQLiteEpisodicStore
    from agent_alfred.memory.types import (
        AlreadyAbsent,
        Deleted,
        EpisodeQuery,
        VersionConflict,
    )

    cls = SQLiteSemanticStore if family == "semantic" else SQLiteEpisodicStore
    store = cls(database, fingerprint=fingerprint, clock=lambda: NOW)
    database.execute("BEGIN")
    if family == "semantic":
        id = store.save("Alice", "coriander", ManualOrigin("web"))
        other = store.save("Bob", "basil", ManualOrigin("web"))
        query = FactQuery(text="coriander")
    else:
        id = store.save("coriander", NOW, None, ManualOrigin("web"))
        other = store.save("basil", NOW, None, ManualOrigin("web"))
        query = EpisodeQuery(text="coriander")
    database.commit()
    database.execute("BEGIN")
    assert store.delete(id, expected_version=2) == VersionConflict(1)
    deleted = store.delete(id, expected_version=1)
    assert isinstance(deleted, Deleted)
    assert deleted.id == id
    assert deleted.fingerprint and deleted.key_id
    assert store.get(id) is None
    assert store.search(query) == ()
    database.rollback()
    assert store.get(id) is not None
    assert len(store.search(query)) == 1
    database.execute("BEGIN")
    deleted = store.delete(id, expected_version=1)
    assert isinstance(deleted, Deleted)
    assert deleted.id == id
    assert deleted.fingerprint and deleted.key_id
    assert store.delete(id, expected_version=1) == AlreadyAbsent(id)
    assert store.get(other) is not None
    database.commit()
    database.close()
    with pytest.raises(sqlite3.Error):
        store.search(query)


def test_idempotency_survives_reopen_and_keeps_case_and_punctuation(tmp_path):
    path = tmp_path / "memory.db"
    conn = sqlite3.connect(path)
    migrate(conn)
    store = SQLiteSemanticStore(conn, fingerprint=fingerprint, clock=lambda: NOW)
    conn.execute("BEGIN")
    id = store.save("Alice", "iOS", ManualOrigin("web"))
    assert store.save("Alice", "ios", ManualOrigin("web")) != id
    assert store.save("Alice", "iOS.", ManualOrigin("web")) != id
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    store = SQLiteSemanticStore(conn, fingerprint=fingerprint, clock=lambda: NOW)
    conn.execute("BEGIN")
    assert store.save(" Alice ", " iOS ", ManualOrigin("cli")) == id
    assert store.search(FactQuery(text="iOS", subject="Bob")) == ()
    conn.rollback()
    conn.close()


def test_recent_list_has_stable_same_time_order_and_rejects_stale_cursor(database):
    from agent_alfred.memory.types import CursorStaleError

    store = SQLiteSemanticStore(database, fingerprint=fingerprint, clock=lambda: NOW)
    database.execute("BEGIN")
    first = store.save("Alice", "mint", ManualOrigin("web"))
    second = store.save("Bob", "basil", ManualOrigin("web"))
    page = store.list_recent(limit=1)
    assert [record.id for record in page.records] == [second]
    assert [
        record.id
        for record in store.list_recent(limit=1, cursor=page.next_cursor).records
    ] == [first]
    store.save("Cindy", "coriander", ManualOrigin("web"))
    with pytest.raises(CursorStaleError):
        store.list_recent(limit=1, cursor=page.next_cursor)
