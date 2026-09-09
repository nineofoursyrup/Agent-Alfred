"""Forward migration for trusted external-operation identity and receipts."""


def migrate_external_operations(conn):
    conn.execute("""CREATE TABLE external_tool_operations (
        run_id TEXT NOT NULL, step_index INTEGER NOT NULL, call_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, ledger_id INTEGER NOT NULL UNIQUE
            REFERENCES tool_ledger(id),
        receipt TEXT,
        PRIMARY KEY(run_id, step_index, call_id)
    )""")
