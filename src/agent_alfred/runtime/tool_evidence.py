"""Bounded action evidence from fixed, revalidated prior ledger identities."""

import json


class ToolEvidence:
    def __init__(self, store, service, session_id, run_id):
        self._store = store
        self._service = service
        self._session = session_id
        with store.reading() as conn:
            self._ids = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT id FROM tool_ledger WHERE session_id=? "
                    "AND (run_id IS NULL OR run_id != ?) "
                    "ORDER BY id DESC",
                    (session_id, run_id),
                )
            )
        self._ceiling = 20
        self.entries = ()
        self.excluded = 0
        self.omitted = 0
        self.unknown_omitted = 0
        self.text = None

    def refresh(self):
        from agent_alfred.runtime.memory import InputEvidenceError

        rows = []
        with self._store.reading() as conn:
            for identity in self._ids:
                row = conn.execute(
                    "SELECT id,tool_name,status,created_at,run_id FROM tool_ledger "
                    "WHERE id=? AND session_id=?",
                    (identity, self._session),
                ).fetchone()
                if row is not None:
                    rows.append(row)
        evaluation = self._service.forgetting.evaluate_history(
            [row[4] for row in rows], purpose="tool_summary"
        )
        if "error" in evaluation:
            raise InputEvidenceError("tool_evidence_unavailable")
        allowed = set(evaluation["allowed"])
        safe = [
            row
            for row in rows
            if row[4] in allowed and row[2] in ("succeeded", "failed", "unknown")
        ]
        self.excluded = len(self._ids) - len(safe)
        # id is the persistent insertion sequence, never an opaque Run id.
        safe.sort(key=lambda row: (row[2] != "unknown", -row[0]))
        self._safe = tuple(
            {
                "ledger_id": row[0],
                "action": row[1],
                "status": row[2],
                "at": row[3],
                "source_run": row[4],
            }
            for row in safe
        )
        self._select()

    def _render(self, count):
        entries = self._safe[:count]
        omitted = len(self._safe) - count
        unknown = sum(entry["status"] == "unknown" for entry in self._safe[count:])
        if not self._ids:
            return None
        return (
            "既往工具账摘要（有限证据；缺少记录不能证明动作未发生；"
            "unknown 表示结果未知；新命令可再次执行）。\n"
            f"因限额省略 {omitted} 条，其中结果未知 {unknown} 条；"
            f"隔离、暂停或失效排除 {self.excluded} 条。\n"
            + json.dumps(
                entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )

    def _select(self):
        from agent_alfred.runtime.input_budget import InputLimitExceeded

        count = 0
        initial = self._render(0)
        if initial is not None and len(initial) > 4000:
            raise InputLimitExceeded(len(initial), 4000)
        while count < min(self._ceiling, len(self._safe)):
            text = self._render(count + 1)
            if text is not None and len(text) > 4000:
                break
            count += 1
        self.entries = self._safe[:count]
        self.text = self._render(count)
        self.omitted = len(self._safe) - count
        self.unknown_omitted = sum(
            entry["status"] == "unknown" for entry in self._safe[count:]
        )

    def shrink(self):
        if not self.entries:
            return False
        # The last selected entry is the oldest ordinary item, then unknown.
        self._ceiling = len(self.entries) - 1
        self._select()
        return True

    def explanation(self):
        return {
            "ledger_entries": [dict(entry) for entry in self.entries],
            "ledger_omitted": self.omitted,
            "ledger_unknown_omitted": self.unknown_omitted,
            "ledger_excluded": self.excluded,
        }
