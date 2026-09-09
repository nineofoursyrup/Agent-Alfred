"""Memory record metadata upgrade; invoked by a numbered schema migration."""

import sqlite3


def migrate_memory(conn: sqlite3.Connection) -> None:
    for table in ("facts", "episodes"):
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN record_version "
            "INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0)"
        )
        conn.execute(f"ALTER TABLE {table} ADD COLUMN modified_at TEXT")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN last_change_origin TEXT")
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN human_protected "
            "INTEGER NOT NULL DEFAULT 0 CHECK(human_protected IN (0,1))"
        )
        conn.execute(f"""UPDATE {table} SET modified_at=created_at,
            human_protected=CASE WHEN origin_kind IN ('manual','tool')
                THEN 1 ELSE 0 END,
            last_change_origin=CASE origin_kind
            WHEN 'manual' THEN json_object('type','manual','source',origin_source)
            WHEN 'tool' THEN json_object('type','tool','call_id',origin_call_id)
            WHEN 'consolidation' THEN json_object(
                'type','consolidation','batch_id',origin_batch_id)
            END""")
    conn.execute(
        "CREATE TABLE memory_revision "
        "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "revision INTEGER NOT NULL CHECK(revision >= 0))"
    )
    conn.execute("INSERT INTO memory_revision VALUES (1, 0)")
