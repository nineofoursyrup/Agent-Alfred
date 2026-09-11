"""Durable, body-free mirror generations and current file ownership (v14)."""

SOURCE_TABLES = (
    "facts",
    "episodes",
    "memory_sources",
    "memory_provenance",
    "memory_source_evidence",
    "forget_limits",
)
OBJECTS = (
    "memory_mirrors",
    "memory_mirror_invalidations",
    "memory_mirror_actions",
    *(
        f"mirror_{table}_{event}"
        for table in SOURCE_TABLES
        for event in ("insert", "update", "delete")
    ),
)


def migrate_mirrors(conn):
    conn.execute("""CREATE TABLE memory_mirrors (
        target_id TEXT PRIMARY KEY,
        required_generation INTEGER NOT NULL DEFAULT 1,
        generated_generation INTEGER NOT NULL DEFAULT 0,
        verified_generation INTEGER NOT NULL DEFAULT 0,
        covered_cleanup_generation INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT, generated_at TEXT, verified_at TEXT,
        conflict INTEGER NOT NULL DEFAULT 0, error TEXT,
        intent TEXT
    )""")
    conn.execute(
        "CREATE TABLE memory_mirror_invalidations (signature TEXT PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TABLE memory_mirror_actions "
        "(operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
        "receipt TEXT NOT NULL)"
    )
    # The responsibility commits with the source write, even if its caller
    # loses notification/return. No rows means no configured managed mirrors.
    for table in SOURCE_TABLES:
        for event in ("insert", "update", "delete"):
            conn.execute(f"""CREATE TRIGGER mirror_{table}_{event}
                AFTER {event.upper()} ON {table} BEGIN
                UPDATE memory_mirrors
                SET required_generation=required_generation+1;
                END""")
