"""Forward migration for recoverable local file actions."""


def migrate_files(conn):
    conn.execute("""CREATE TABLE file_operations (
        operation_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL,
        target TEXT NOT NULL, content TEXT NOT NULL, expected_digest TEXT,
        state TEXT NOT NULL CHECK(state IN ('prepared','complete','conflict')),
        call_id TEXT NOT NULL, run_id TEXT NOT NULL, session_id TEXT,
        created_at TEXT NOT NULL, receipt TEXT, request_digest TEXT NOT NULL,
        original_content TEXT, candidate_identity TEXT
    )""")
    conn.execute(
        "CREATE UNIQUE INDEX file_pending_target ON file_operations(target) "
        "WHERE state != 'complete'"
    )

    conn.execute("""CREATE TABLE skill_candidates (
        candidate_id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL,
        content_digest TEXT NOT NULL, builtin_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN (
            'awaiting_confirmation','confirmed','cancelled','invalidated')),
        run_id TEXT NOT NULL, step_index INTEGER NOT NULL, call_id TEXT NOT NULL,
        source TEXT NOT NULL, session_id TEXT
    )""")

    conn.execute("""CREATE TABLE local_tool_operations (
        operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL
    )""")
