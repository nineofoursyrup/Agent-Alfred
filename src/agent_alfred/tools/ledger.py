"""External intent and redacted receipts share one durable operation identity."""

import hashlib
import json
from dataclasses import dataclass

from agent_alfred.clock import format_instant
from agent_alfred.messages import TextBlock, ToolResultBlock
from agent_alfred.tools import ToolExecution


@dataclass(frozen=True)
class ExternalClaim:
    row_id: int | None = None
    execution: ToolExecution | None = None
    error: str | None = None


def _receipt(execution):
    return json.dumps(
        {
            "call_id": execution.block.call_id,
            "content": [block.text for block in execution.block.content],
            "is_error": execution.block.is_error,
            "stop_reason": execution.stop_reason,
            "operation_id": execution.operation_id,
            "system_receipt": execution.system_receipt,
        },
        ensure_ascii=False,
    )


def _read_receipt(receipt):
    value = json.loads(receipt)
    return ToolExecution(
        ToolResultBlock(
            value["call_id"],
            tuple(TextBlock(s) for s in value["content"]),
            value["is_error"],
        ),
        value["stop_reason"],
        value["operation_id"],
        value["system_receipt"],
    )


class ExternalToolLedger:
    def __init__(self, store, clock):
        self._store = store
        self._clock = clock

    def start(self, tool, args, context):
        if self._store.transaction_in_progress:
            raise ValueError("caller_transaction_active")
        # Identity is host-supplied call provenance, never model arguments.
        # All parameters and the tool name participate in mismatch detection.
        key = (context.run_id, context.step_index, context.call_id)
        fingerprint = hashlib.sha256(
            json.dumps(
                [tool.name, dict(args)],
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        now = format_instant(self._clock.wall_utc())
        with self._store.transaction() as conn:
            existing = conn.execute(
                "SELECT fingerprint,receipt FROM external_tool_operations "
                "WHERE run_id=? AND step_index=? AND call_id=?",
                key,
            ).fetchone()
            if existing is not None:
                if existing[0] != fingerprint:
                    return ExternalClaim(error="invalid_input")
                if existing[1] is not None:
                    return ExternalClaim(execution=_read_receipt(existing[1]))
                # Intent with no receipt is uncertain even before Host recovery.
                return ExternalClaim(error="execution_error")
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
            conn.execute(
                "INSERT INTO external_tool_operations "
                "(run_id,step_index,call_id,fingerprint,ledger_id) VALUES (?,?,?,?,?)",
                (*key, fingerprint, row_id),
            )
            conn.commit()
        return ExternalClaim(row_id=row_id)

    def finish(self, row_id, state, execution):
        if self._store.transaction_in_progress:
            raise ValueError("caller_transaction_active")
        receipt = _receipt(execution)
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE tool_ledger SET status=?,updated_at=? WHERE id=?",
                (state, format_instant(self._clock.wall_utc()), row_id),
            )
            conn.execute(
                "UPDATE external_tool_operations SET receipt=? WHERE ledger_id=?",
                (receipt, row_id),
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
