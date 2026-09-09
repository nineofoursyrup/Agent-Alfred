"""Body-free operation receipts and provenance for the shared command service."""

import sqlite3


def migrate_commands(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE memory_operations (
        operation_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        key_id TEXT NOT NULL,
        receipt TEXT NOT NULL CHECK(json_valid(receipt))
    )""")
    conn.execute("""CREATE TABLE memory_sources (
        kind TEXT NOT NULL CHECK(kind IN ('semantic','episodic')),
        memory_id TEXT NOT NULL,
        record_version INTEGER NOT NULL,
        source_group_id TEXT NOT NULL,
        PRIMARY KEY(kind,memory_id,record_version,source_group_id)
    )""")
    conn.execute("""CREATE TABLE memory_provenance (
        kind TEXT NOT NULL CHECK(kind IN ('semantic','episodic')),
        memory_id TEXT NOT NULL,
        record_version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('known','known_none','unknown')),
        PRIMARY KEY(kind,memory_id,record_version)
    )""")
    for kind, table in (("semantic", "facts"), ("episodic", "episodes")):
        conn.execute(
            f"INSERT INTO memory_provenance SELECT ?,id,record_version,'unknown' "
            f"FROM {table}",
            (kind,),
        )
