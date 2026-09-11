"""Durable saved-chat opportunities; scheduling never owns model execution."""

from agent_alfred.memory.consolidation import (
    ConsolidationSourceEvidenceError,
    ConsolidationSourceTooLarge,
    SelectedConsolidationSources,
    select_consolidation_sources,
)
from agent_alfred.model import ModelRef

TABLES = ("memory_consolidation_ready", "memory_consolidation_triggers")


def migrate_scheduling(conn):
    conn.execute(
        "CREATE TABLE memory_consolidation_ready ("
        "session_id TEXT PRIMARY KEY, ready_order INTEGER NOT NULL UNIQUE, "
        "ready_at TEXT NOT NULL, error_code TEXT)"
    )
    conn.execute(
        "CREATE TABLE memory_consolidation_triggers ("
        "chat_run_id TEXT PRIMARY KEY, session_id TEXT, generation_run_id TEXT UNIQUE, "
        "status TEXT NOT NULL CHECK(status IN "
        "('pending','attempted','accepted','refused','empty','error','expired')), "
        "error_code TEXT)"
    )


class ConsolidationScheduling:
    def __init__(self, service):
        self._service = service
        self._db = service._db

    def record_chat(self, conn, run_id):
        row = conn.execute(
            "SELECT purpose,outcome FROM runs WHERE run_id=? AND phase='finished'",
            (run_id,),
        ).fetchone()
        if row != ("chat", "completed"):
            return
        from agent_alfred.memory.consolidation_service import _log_text

        roles = conn.execute(
            "SELECT role,content FROM agent_log WHERE run_id=?", (run_id,)
        ).fetchall()
        if {role for role, content in roles if _log_text(content).strip()} >= {
            "user",
            "assistant",
        }:
            conn.execute(
                "INSERT OR IGNORE INTO memory_consolidation_triggers "
                "(chat_run_id,status) VALUES (?,'pending')",
                (run_id,),
            )

    def recover(self):
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE memory_consolidation_triggers SET status='expired', "
                "error_code='scheduling_interrupted' "
                "WHERE status IN ('pending','attempted')"
            )
            self._refresh(conn)
            conn.commit()

    def claim(self, chat_run_id):
        """Spend once before admission; accepted Run binding is atomic elsewhere."""
        with self._db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM memory_consolidation_triggers WHERE chat_run_id=?",
                (chat_run_id,),
            ).fetchone()
            if row != ("pending",):
                return None
            inspection_error = self._refresh(conn)
            ready = conn.execute(
                "SELECT r.session_id FROM memory_consolidation_ready r "
                "WHERE r.error_code IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM memory_consolidation_batches b "
                "WHERE b.session_id=r.session_id AND b.status IN "
                "('queued','running','awaiting_approval','failed')) "
                "AND NOT EXISTS (SELECT 1 FROM memory_consolidation_blocks x "
                "WHERE x.session_id=r.session_id) ORDER BY r.ready_order LIMIT 1"
            ).fetchone()
            session_id = ready[0] if ready else None
            conn.execute(
                "UPDATE memory_consolidation_triggers SET status=?,session_id=?, "
                "error_code=? WHERE chat_run_id=?",
                (
                    "attempted" if ready else "error" if inspection_error else "empty",
                    session_id,
                    "scheduling_unconfirmed" if ready else inspection_error,
                    chat_run_id,
                ),
            )
            conn.commit()
        return session_id

    def bind_run(self, conn, trigger_id, session_id, run_id):
        changed = conn.execute(
            "UPDATE memory_consolidation_triggers SET status='accepted', "
            "generation_run_id=?,error_code=NULL WHERE chat_run_id=? "
            "AND session_id=? AND status='attempted' AND generation_run_id IS NULL",
            (run_id, trigger_id, session_id),
        ).rowcount
        if changed != 1:
            raise ValueError("scheduling_opportunity_already_consumed")

    def finish_admission(self, trigger_id, error_code):
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE memory_consolidation_triggers SET status=?, "
                "error_code=? WHERE chat_run_id=? AND status IN "
                "('pending','attempted')",
                (
                    "error" if error_code == "storage_read_failed" else "refused",
                    error_code,
                    trigger_id,
                ),
            )
            conn.commit()

    def refresh_session(self, conn, session_id):
        self._refresh(conn, sessions=[(session_id,)])

    def _refresh(self, conn, sessions=None):
        service = self._service
        if sessions is None:
            sessions = conn.execute(
                "SELECT session_id FROM runs WHERE purpose='chat' "
                "AND session_id IS NOT NULL GROUP BY session_id"
            ).fetchall()
        inspection_error = None
        for (session_id,) in sessions:
            unresolved = conn.execute(
                "SELECT 1 FROM memory_consolidation_batches WHERE session_id=? "
                "AND status IN ('queued','running','awaiting_approval','failed')",
                (session_id,),
            ).fetchone()
            if unresolved:
                continue
            loaded = service._load_sources(conn, session_id)
            if "error" in loaded:
                inspection_error = loaded["error"]["code"]
                service._record_prepare_failure(
                    conn, session_id, None, loaded["error"]["code"]
                )
                continue
            sources = loaded["sources"]
            selected = select_consolidation_sources(
                session_id,
                sources,
                model=ModelRef("consolidation", "primary"),
                limits=service._limits,
            )
            if isinstance(selected, ConsolidationSourceEvidenceError):
                inspection_error = selected.code
                service._record_prepare_failure(conn, session_id, None, selected.code)
                continue
            if isinstance(selected, ConsolidationSourceTooLarge):
                service._record_source_block(conn, selected)
            else:
                conn.execute(
                    "DELETE FROM memory_consolidation_blocks WHERE session_id=?",
                    (session_id,),
                )
            if not isinstance(
                selected, (SelectedConsolidationSources, ConsolidationSourceTooLarge)
            ):
                conn.execute(
                    "DELETE FROM memory_consolidation_ready WHERE session_id=?",
                    (session_id,),
                )
                continue
            threshold_source = sources[service._limits.source_threshold - 1]
            order = conn.execute(
                "SELECT min(id) FROM agent_log WHERE run_id=? AND role='user'",
                (threshold_source.run_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO memory_consolidation_ready "
                "(session_id,ready_order,ready_at,error_code) VALUES (?,?,?,NULL) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "ready_order=excluded.ready_order,ready_at=excluded.ready_at "
                "WHERE memory_consolidation_ready.ready_order<>excluded.ready_order",
                (session_id, order, threshold_source.finished_at.isoformat()),
            )
        return inspection_error
