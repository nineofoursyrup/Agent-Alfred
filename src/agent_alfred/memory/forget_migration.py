"""v6 body-free forgetting metadata. Applied in the schema owner's transaction."""

TABLES = (
    "forget_clock",
    "history_groups",
    "history_group_times",
    "forget_scope_versions",
    "forget_scope_insert",
    "forget_scope_update",
    "history_reads",
    "memory_uses",
    "memory_source_evidence",
    "forget_operations",
    "forget_projection_recovery",
    "forget_observations",
    "forget_limits",
    "forget_scopes",
    "forget_actions",
    "forget_cleanup",
    "forget_targets",
    "forget_projection_fences",
    "forget_projection_unknown",
)


def migrate_forgetting(conn):
    statements = (
        (
            "CREATE TABLE forget_clock (id INTEGER PRIMARY KEY "
            "CHECK(id=1), revision INTEGER NOT NULL)"
        ),
        "INSERT INTO forget_clock VALUES (1,0)",
        """CREATE TABLE history_groups (
        group_id TEXT PRIMARY KEY, kind TEXT NOT NULL, container_id TEXT,
        evidence TEXT NOT NULL CHECK(evidence IN ('unknown','complete')),
        evidence_id TEXT)""",
        """CREATE TABLE history_reads (
        source TEXT NOT NULL, consumer TEXT NOT NULL, attempt_id TEXT NOT NULL,
        purpose TEXT NOT NULL, PRIMARY KEY(source,consumer,attempt_id,purpose))""",
        """CREATE TABLE memory_uses (
        kind TEXT NOT NULL, memory_id TEXT NOT NULL, record_version INTEGER NOT NULL,
        consumer TEXT NOT NULL, attempt_id TEXT NOT NULL, purpose TEXT NOT NULL,
        PRIMARY KEY(kind,memory_id,record_version,consumer,attempt_id,purpose))""",
        """CREATE TABLE memory_source_evidence (
        kind TEXT NOT NULL, memory_id TEXT NOT NULL, record_version INTEGER NOT NULL,
        evidence_id TEXT NOT NULL, groups_json TEXT NOT NULL,
        PRIMARY KEY(kind,memory_id,record_version,evidence_id))""",
        """CREATE TABLE forget_operations (
        operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, memory_id TEXT NOT NULL,
        completeness TEXT NOT NULL, created_at TEXT NOT NULL)""",
        "CREATE TABLE forget_projection_recovery "
        "(operation_id TEXT PRIMARY KEY, evidence_id TEXT, error TEXT, failed_at TEXT)",
        """CREATE TABLE forget_observations (
        operation_id TEXT NOT NULL, progress_revision INTEGER NOT NULL,
        observed_at TEXT NOT NULL, state TEXT NOT NULL,
        PRIMARY KEY(operation_id,progress_revision))""",
        """CREATE TABLE forget_limits (
        operation_id TEXT NOT NULL, group_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('paused','isolated')),
        PRIMARY KEY(operation_id,group_id))""",
        """CREATE TABLE forget_scopes (
        scope_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL, container_id TEXT, members TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','confirmed','excluded')),
        evidence_id TEXT, confirmed_at TEXT, boundary TEXT NOT NULL,
        reason TEXT NOT NULL)""",
        """CREATE TABLE history_group_times (
        group_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL)""",
        """CREATE TABLE forget_scope_versions (
        scope_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
        PRIMARY KEY(scope_id,revision))""",
        """CREATE TABLE forget_actions (
        action_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, key_id TEXT NOT NULL,
        receipt TEXT NOT NULL)""",
        """CREATE TABLE forget_targets (
        target_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
        verified_revision INTEGER NOT NULL DEFAULT 0)""",
        "CREATE TABLE forget_projection_unknown (operation_id TEXT PRIMARY KEY)",
        """CREATE TABLE forget_projection_fences (
        operation_id TEXT NOT NULL, target_id TEXT NOT NULL,
        PRIMARY KEY(operation_id,target_id))""",
        """CREATE TABLE forget_cleanup (
        operation_id TEXT NOT NULL, target_id TEXT NOT NULL, revision INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','running','failed','complete')),
        error TEXT, PRIMARY KEY(operation_id,target_id))""",
    )
    for statement in statements:
        conn.execute(statement)
    for event in ("INSERT", "UPDATE"):
        conn.execute(f"""CREATE TRIGGER forget_scope_{event.lower()}
        AFTER {event} ON forget_scopes BEGIN
          INSERT INTO forget_scope_versions VALUES (
            NEW.scope_id, NEW.revision,
            json_object('scope_id', NEW.scope_id, 'revision', NEW.revision,
              'kind', NEW.kind, 'container_id', NEW.container_id,
              'members', json(NEW.members), 'state', NEW.state,
              'evidence_id', NEW.evidence_id, 'confirmed_at', NEW.confirmed_at,
              'boundary', json(NEW.boundary), 'reason', NEW.reason)
          );
        END""")
    from agent_alfred.memory.forget_graph import recover_legacy, sync_history

    sync_history(conn)
    recover_legacy(conn)
