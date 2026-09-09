"""External actions record intent before crossing the non-transactional boundary."""

import hashlib
import json

from agent_alfred.clock import format_instant


class ExternalToolLedger:
    def __init__(self, store, clock):
        self._store = store
        self._clock = clock

    def start(self, tool, args, context):
        if self._store.transaction_in_progress:
            raise ValueError("caller_transaction_active")
        identity = {key: args[key] for key in tool.summary_keys if key in args}
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        now = format_instant(self._clock.wall_utc())
        with self._store.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO tool_ledger(tool_name,fingerprint,effect,status,"
                "call_id,run_id,session_id,summary,created_at) "
                "VALUES (?,?,'external','started',?,?,?,?,?)",
                (
                    tool.name,
                    fingerprint,
                    context.call_id,
                    context.run_id,
                    context.session_id,
                    tool.name,
                    now,
                ),
            )
            row_id = cursor.lastrowid
            conn.commit()
        return row_id

    def finish(self, row_id, state):
        if self._store.transaction_in_progress:
            raise ValueError("caller_transaction_active")
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE tool_ledger SET status=?,updated_at=? WHERE id=?",
                (state, format_instant(self._clock.wall_utc()), row_id),
            )
            conn.commit()

    def recover(self):
        if self._store.transaction_in_progress:
            raise ValueError("caller_transaction_active")
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE tool_ledger SET status='unknown',updated_at=? "
                "WHERE effect='external' AND status='started'",
                (format_instant(self._clock.wall_utc()),),
            )
            conn.commit()
