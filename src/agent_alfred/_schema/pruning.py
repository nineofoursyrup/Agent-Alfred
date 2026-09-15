"""Trace absence facts and deterministic prune reason selection."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import cast

from .contracts import PRUNE_REASON_PRIORITY, PRUNE_REASONS, PruneReason


def pick_prune_reason(reasons: Iterable[str]) -> PruneReason:
    """Pick the stored prune_reason: manual > disk_low > age > capacity."""
    best: PruneReason | None = None
    best_rank = len(PRUNE_REASON_PRIORITY)
    for reason in reasons:
        if reason not in PRUNE_REASONS:
            allowed = ", ".join(PRUNE_REASON_PRIORITY)
            raise ValueError(f"prune_reason must be one of {allowed}, got {reason!r}")
        rank = PRUNE_REASON_PRIORITY.index(reason)
        if rank < best_rank:
            best = cast(PruneReason, reason)
            best_rank = rank
    if best is None:
        raise ValueError("prune_reason must not be empty")
    return best


def record_trace_prune(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    prune_requested_at: str,
    absence_confirmed_at: str,
    prune_reason: PruneReason,
) -> None:
    """Record one pruned Run. Never commits: the caller owns the batch transaction.

    Only `prune_reason` is validated. The two timestamps are passed through as
    given -- the column is NOT NULL and nothing more, so 'banana' goes in. That
    is deliberate rather than overlooked: parse_instant states the module's
    instant contract, and applying it here would make this function the only
    write path in the schema that enforces one, which reads as a guarantee the
    other tables do not offer.
    """
    prune_reason = pick_prune_reason((prune_reason,))
    conn.execute(
        """INSERT INTO trace_prunes (
             run_id, prune_requested_at, absence_confirmed_at, prune_reason
           ) VALUES (?, ?, ?, ?)
           ON CONFLICT(run_id) DO NOTHING""",
        (run_id, prune_requested_at, absence_confirmed_at, prune_reason),
    )
