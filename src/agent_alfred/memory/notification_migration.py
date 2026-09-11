"""v15: auxiliary memory state shares the persisted readback revision."""

TABLES = (
    "memory_mirrors",
    "memory_mirror_actions",
    "memory_consolidation_batches",
    "memory_consolidation_plans",
    "memory_consolidation_actions",
    "memory_consolidation_blocks",
    "memory_consolidation_ready",
    "memory_consolidation_triggers",
)
OBJECTS = tuple(
    f"memory_patch_{table}_{event}"
    for table in TABLES
    for event in ("insert", "update", "delete")
)


def migrate_notifications(conn):
    for table in TABLES:
        for event in ("insert", "update", "delete"):
            conn.execute(f"""CREATE TRIGGER memory_patch_{table}_{event}
                AFTER {event.upper()} ON {table} BEGIN
                UPDATE memory_revision SET revision=revision+1 WHERE singleton=1;
                END""")


# v15 is frozen. v16 adds the Run evidence exposed by the consolidation read API.
RUN_OBJECTS = ("memory_patch_consolidation_run_update",)


def migrate_run_notifications(conn):
    conn.execute("""CREATE TRIGGER memory_patch_consolidation_run_update
        AFTER UPDATE OF phase,outcome,started_at,finished_at,telemetry,admission_state
        ON runs WHEN NEW.purpose='consolidation' AND (
            OLD.phase IS NOT NEW.phase OR OLD.outcome IS NOT NEW.outcome
            OR OLD.started_at IS NOT NEW.started_at
            OR OLD.finished_at IS NOT NEW.finished_at
            OR OLD.telemetry IS NOT NEW.telemetry
            OR OLD.admission_state IS NOT NEW.admission_state
        ) BEGIN
        UPDATE memory_revision SET revision=revision+1 WHERE singleton=1;
        END""")
