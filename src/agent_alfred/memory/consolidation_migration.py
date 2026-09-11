"""v11 consolidation batch lifecycle. Applied in the schema owner's transaction."""

TABLES = (
    "memory_consolidation_batches",
    "memory_consolidation_sources",
    "memory_consolidation_source_results",
    "memory_consolidation_reads",
    "memory_consolidation_plans",
    "memory_consolidation_approvals",
    "memory_consolidation_blocks",
    "memory_consolidation_actions",
    "memory_consolidation_one_unresolved",
    "memory_consolidation_session_idx",
)

_STATUSES = (
    "queued",
    "running",
    "awaiting_approval",
    "succeeded",
    "rejected",
    "failed",
    "invalidated",
)
_STATUS_SQL = ", ".join(f"'{status}'" for status in _STATUSES)
_UNRESOLVED = (
    "queued",
    "running",
    "awaiting_approval",
    "failed",
)
_UNRESOLVED_SQL = ", ".join(f"'{status}'" for status in _UNRESOLVED)
_RESULTS = ("succeeded", "user_rejected", "user_skipped")
_RESULT_SQL = ", ".join(f"'{result}'" for result in _RESULTS)


def migrate_consolidation(conn) -> None:
    conn.execute(
        f"""CREATE TABLE memory_consolidation_batches (
        batch_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        status TEXT NOT NULL CHECK(status IN ({_STATUS_SQL})),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        error_code TEXT,
        generation_run_id TEXT,
        receipt TEXT CHECK(receipt IS NULL OR json_valid(receipt))
        )"""
    )
    conn.execute(
        """CREATE INDEX memory_consolidation_session_idx
        ON memory_consolidation_batches(session_id, revision)"""
    )
    conn.execute(
        f"""CREATE UNIQUE INDEX memory_consolidation_one_unresolved
        ON memory_consolidation_batches(session_id)
        WHERE status IN ({_UNRESOLVED_SQL})"""
    )
    conn.execute(
        """CREATE TABLE memory_consolidation_sources (
        batch_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        accepted_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        PRIMARY KEY(batch_id, revision, run_id)
        )"""
    )
    conn.execute(
        f"""CREATE TABLE memory_consolidation_source_results (
        run_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ({_RESULT_SQL})),
        batch_id TEXT,
        recorded_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE memory_consolidation_reads (
        batch_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('semantic','episodic')),
        memory_id TEXT NOT NULL,
        record_version INTEGER NOT NULL,
        human_protected INTEGER NOT NULL CHECK(human_protected IN (0,1)),
        in_request INTEGER NOT NULL CHECK(in_request IN (0,1)),
        PRIMARY KEY(batch_id, revision, kind, memory_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE memory_consolidation_plans (
        batch_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        plan_json TEXT CHECK(plan_json IS NULL OR json_valid(plan_json)),
        episode_summary TEXT,
        occurred_at TEXT,
        occurred_until TEXT,
        candidate_text TEXT,
        PRIMARY KEY(batch_id, revision)
        )"""
    )
    conn.execute(
        """CREATE TABLE memory_consolidation_approvals (
        batch_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        approved_at TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        PRIMARY KEY(batch_id, revision)
        )"""
    )
    conn.execute(
        """CREATE TABLE memory_consolidation_blocks (
        session_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN ('source_too_large')),
        characters INTEGER NOT NULL,
        "limit" INTEGER NOT NULL,
        created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE memory_consolidation_actions (
        operation_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        key_id TEXT NOT NULL,
        receipt TEXT NOT NULL CHECK(json_valid(receipt))
        )"""
    )
