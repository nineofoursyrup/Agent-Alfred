"""Body-free graph and scope operations inside the caller's transaction."""

import json
from datetime import datetime, timezone
from uuid import uuid4


def bump(conn):
    conn.execute("UPDATE memory_revision SET revision=revision+1 WHERE singleton=1")
    return conn.execute(
        "UPDATE forget_clock SET revision=revision+1 WHERE id=1 RETURNING revision"
    ).fetchone()[0]


def revision(conn):
    return conn.execute("SELECT revision FROM forget_clock WHERE id=1").fetchone()[0]


def operation_state(conn, operation_id):
    pending = conn.execute(
        "SELECT count(*) FROM forget_scopes WHERE operation_id=? AND state='pending'",
        (operation_id,),
    ).fetchone()[0]
    cleanup = [
        row[0]
        for row in conn.execute(
            "SELECT state FROM forget_cleanup WHERE operation_id=?",
            (operation_id,),
        )
    ]
    if pending:
        return "needs_scope"
    recovery_failed = (
        conn.execute(
            "SELECT 1 FROM forget_projection_recovery WHERE operation_id=? "
            "AND error IS NOT NULL",
            (operation_id,),
        ).fetchone()
        is not None
    )
    if "failed" in cleanup or recovery_failed:
        return "failed"
    recovery_pending = (
        conn.execute(
            "SELECT 1 FROM forget_projection_recovery WHERE operation_id=? "
            "AND evidence_id IS NULL",
            (operation_id,),
        ).fetchone()
        is not None
    )
    unknown_targets = (
        conn.execute(
            "SELECT 1 FROM forget_projection_unknown WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        is not None
    )
    if (
        recovery_pending
        or unknown_targets
        or any(state != "complete" for state in cleanup)
    ):
        return "cleaning"
    return "complete"


def record_observations(conn, observed_at):
    for (operation,) in conn.execute(
        "SELECT operation_id FROM forget_operations"
    ).fetchall():
        state = operation_state(conn, operation)
        prior = conn.execute(
            "SELECT state FROM forget_observations WHERE operation_id=? "
            "ORDER BY progress_revision DESC LIMIT 1",
            (operation,),
        ).fetchone()
        if prior is None or prior[0] != state:
            conn.execute(
                "INSERT INTO forget_observations VALUES (?,?,?,?)",
                (operation, revision(conn), observed_at, state),
            )


def ensure_group(conn, group_id):
    conn.execute(
        (
            "INSERT OR IGNORE INTO history_groups VALUES "
            "(?,'unresolved',NULL,'unknown',NULL)"
        ),
        (group_id,),
    )


def propagate(conn):
    # Fixed point includes cycles, multiple operations, and paused successors.
    while True:
        changed = conn.execute("""INSERT INTO forget_limits
            SELECT l.operation_id,r.consumer,l.mode FROM forget_limits l
            JOIN history_reads r ON r.source=l.group_id WHERE 1
            ON CONFLICT(operation_id,group_id) DO UPDATE SET mode='isolated'
            WHERE excluded.mode='isolated' AND forget_limits.mode='paused'""").rowcount
        if not changed:
            break


def scope_boundary(conn, members):
    dates = []
    for member in members:
        row = conn.execute(
            "SELECT occurred_at FROM history_group_times WHERE group_id=?", (member,)
        ).fetchone()
        if row:
            try:
                date = datetime.fromisoformat(row[0])
                if date.utcoffset() is not None:
                    dates.append(date.astimezone(timezone.utc))
            except ValueError:
                pass
    return json.dumps(
        {
            "member_count": len(members),
            "earliest_at": min(dates).isoformat() if dates else None,
            "latest_at": max(dates).isoformat() if dates else None,
            "time_complete": bool(members) and len(dates) == len(members),
        }
    )


def insert_scope(conn, operation_id, kind, container, members, evidence_id=None):
    scope_id = str(uuid4())
    conn.execute(
        "INSERT INTO forget_scopes VALUES (?,?,1,?,?,?,'pending',?,NULL,?,?)",
        (
            scope_id,
            operation_id,
            kind,
            container,
            json.dumps(sorted(members)),
            evidence_id,
            scope_boundary(conn, members),
            "identity_unresolved" if kind == "unresolved" else "association_unknown",
        ),
    )
    return scope_id


def update_members(conn, scope_id, members, evidence_id):
    conn.execute(
        "UPDATE forget_scopes SET members=?,revision=revision+1,state=?,"
        "evidence_id=?,boundary=? WHERE scope_id=?",
        (
            json.dumps(members),
            "pending" if members else "excluded",
            evidence_id,
            scope_boundary(conn, members),
            scope_id,
        ),
    )


def add_scopes(conn, operation_id, groups):
    containers = {}
    covered = {
        g
        for (members,) in conn.execute(
            "SELECT members FROM forget_scopes WHERE operation_id=?", (operation_id,)
        )
        for g in json.loads(members)
    }
    for group in groups:
        if group in covered:
            continue
        row = conn.execute(
            "SELECT kind,container_id FROM history_groups WHERE group_id=?", (group,)
        ).fetchone()
        containers.setdefault(tuple(row), []).append(group)
    for (kind, container), members in containers.items():
        insert_scope(conn, operation_id, kind, container, members)
    refresh_pauses(conn)


def refresh_pauses(conn):
    propagate(conn)
    # Positive evidence resolves only the identified members, never the rest of
    # an unknown range. Confirmed snapshots and unresolved identities stay fixed.
    for scope, operation, encoded, kind, boundary, evidence in conn.execute(
        "SELECT scope_id,operation_id,members,kind,boundary,evidence_id "
        "FROM forget_scopes WHERE state='pending'"
    ).fetchall():
        isolated = {
            g
            for (g,) in conn.execute(
                "SELECT group_id FROM forget_limits "
                "JOIN history_groups USING(group_id) "
                "WHERE operation_id=? AND mode='isolated' AND kind!='unresolved'",
                (operation,),
            )
        }
        members = json.loads(encoded)
        remaining = [g for g in members if g not in isolated]
        if kind == "unresolved":
            # Identity resolution changes what the user is being asked to
            # confirm. Retire that part of the old offer; display new containers.
            identified = {}
            for group in tuple(remaining):
                identity = conn.execute(
                    "SELECT kind,container_id FROM history_groups WHERE group_id=?",
                    (group,),
                ).fetchone()
                if identity[0] != "unresolved":
                    remaining.remove(group)
                    identified.setdefault(tuple(identity), []).append(group)
            for (resolved_kind, container), groups in identified.items():
                insert_scope(conn, operation, resolved_kind, container, groups)
        if remaining != members or scope_boundary(conn, remaining) != boundary:
            update_members(conn, scope, remaining, evidence)
    conn.execute("DELETE FROM forget_limits WHERE mode='paused'")
    for operation, members in conn.execute(
        "SELECT operation_id,members FROM forget_scopes WHERE state='pending'"
    ).fetchall():
        conn.executemany(
            "INSERT OR IGNORE INTO forget_limits VALUES (?,?,'paused')",
            [(operation, g) for g in json.loads(members)],
        )
    propagate(conn)


def sync_history(conn):
    """Inventory structural history; absence of telemetry never proves safety."""
    for statement in (
        (
            "INSERT OR IGNORE INTO history_groups SELECT "
            "run_id,'run',session_id,'unknown',NULL FROM runs"
        ),
        (
            "INSERT OR IGNORE INTO history_groups SELECT "
            "run_id,'run',session_id,'unknown',NULL FROM agent_log WHERE "
            "run_id IS NOT NULL"
        ),
        (
            "INSERT OR IGNORE INTO history_groups SELECT "
            "'legacy:agent_log:' || id,'legacy',session_id,'unknown',NULL "
            "FROM agent_log WHERE run_id IS NULL"
        ),
        (
            "INSERT OR IGNORE INTO history_groups SELECT "
            "batch_id,'batch',batch_id,'unknown',NULL FROM "
            "consolidation_batches"
        ),
    ):
        conn.execute(statement)
    for statement in (
        "INSERT OR IGNORE INTO history_group_times SELECT run_id,accepted_at FROM runs",
        "INSERT OR IGNORE INTO history_group_times SELECT "
        "'legacy:agent_log:' || id,created_at FROM agent_log WHERE run_id IS NULL",
        "INSERT OR IGNORE INTO history_group_times SELECT "
        "batch_id,created_at FROM consolidation_batches",
    ):
        conn.execute(statement)


def recover_legacy(conn):
    for operation, encoded in conn.execute(
        "SELECT operation_id,receipt FROM memory_operations"
    ).fetchall():
        receipt = json.loads(encoded)
        if receipt.get("action") != "delete" or receipt.get("status") != "deleted":
            continue
        # The old receipt proves deletion, not completion of source coverage.
        start_forgetting(
            conn,
            operation,
            receipt["kind"],
            receipt["memory_id"],
            1,
            receipt["committed_at"],
            force_unknown=True,
        )
        # Schema recovery has no file/participant adapters. Clear the built-in
        # projection now and durably require reconciliation before completion.
        conn.execute(
            "UPDATE tool_ledger SET summary=NULL WHERE run_id IN "
            "(SELECT group_id FROM forget_limits WHERE operation_id=? "
            "AND mode='isolated')",
            (operation,),
        )
        conn.execute(
            "INSERT INTO forget_projection_recovery VALUES (?,NULL,NULL,NULL)",
            (operation,),
        )


def start_forgetting(
    conn, operation_id, kind, memory_id, version, now, *, force_unknown=False
):
    sync_history(conn)
    rows = conn.execute(
        (
            "SELECT record_version,state FROM memory_provenance WHERE "
            "kind=? AND memory_id=?"
        ),
        (kind, memory_id),
    ).fetchall()
    complete = (
        not force_unknown
        and len(rows) >= version
        and all(row[1] != "unknown" for row in rows)
    )
    conn.execute(
        "INSERT INTO forget_operations VALUES (?,?,?,?,?)",
        (operation_id, kind, memory_id, "known" if complete else "unknown", now),
    )
    roots = conn.execute(
        """SELECT source_group_id FROM memory_sources WHERE kind=? AND memory_id=?
        UNION SELECT consumer FROM memory_uses WHERE kind=? AND memory_id=?""",
        (kind, memory_id, kind, memory_id),
    ).fetchall()
    for (group,) in roots:
        ensure_group(conn, group)
        conn.execute(
            "INSERT OR REPLACE INTO forget_limits VALUES (?,?,'isolated')",
            (operation_id, group),
        )
    propagate(conn)
    unknown = [
        r[0]
        for r in conn.execute(
            "SELECT group_id FROM history_groups WHERE evidence='unknown'"
        )
    ]
    if not complete and not unknown:
        gap = f"unresolved:memory:{kind}:{memory_id}"
        ensure_group(conn, gap)
        unknown.append(gap)
    isolated = {
        r[0]
        for r in conn.execute(
            (
                "SELECT group_id FROM forget_limits WHERE operation_id=? AND "
                "mode='isolated'"
            ),
            (operation_id,),
        )
    }
    unresolved = {
        g
        for (g,) in conn.execute(
            "SELECT group_id FROM history_groups WHERE kind='unresolved'"
        )
    }
    add_scopes(conn, operation_id, (set(unknown) - isolated) | unresolved)
    bump(conn)
