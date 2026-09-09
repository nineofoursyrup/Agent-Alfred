"""Capabilities for #18/#46 adapters; these are not Memory Store protocols."""

from collections.abc import Iterable
from sqlite3 import Connection
from typing import Protocol


class ProjectionParticipant(Protocol):
    def invalidate(
        self,
        connection: Connection,
        *,
        memories: tuple[tuple[str, str], ...],
        isolated: tuple[str, ...],
    ) -> Iterable[str]:
        """Erase affected SQLite bodies and invalidate whole candidate batches.

        Same connection, no commit/rollback, no IO. Return stable managed output
        identities needing cleanup. Registration/reconciliation is idempotent:
        return an output again only if a new invalidation needs a new generation.
        Paused sources are not confirmed pollution; their commit/read gate is
        enforced by write_projection/consume_history, not by this callback.
        """
        ...


class CleanupPort(Protocol):
    def verify(self, target_id: str, generation: int) -> bool:
        """Verify current output covers this generation, without IO effects."""
        ...

    def rebuild(self, target_id: str, generation: int) -> None:
        """Rebuild from current valid Stores and atomically replace managed output.

        No cached pre-delete bodies; no public preview before verify succeeds.
        Called under mutation admission, outside the SQLite lock. The adapter
        maps opaque target IDs to managed paths and owns file resource cleanup.
        """
        ...


class ProjectionReconciliationScope(Protocol):
    def reconciliation_targets(
        self,
        connection: Connection,
        *,
        memories: tuple[tuple[str, str], ...],
        isolated: tuple[str, ...],
    ) -> Iterable[str]:
        """Optional trusted complete set of outputs possibly affected by recovery.

        Read only, no IO or transaction ownership. Include shared outputs even
        if invalidate may fail before returning them. An empty set proves no
        managed output is affected for these inputs, not that SQLite work is done.
        Without this capability, recovery conservatively fences all known outputs.
        The same inventory must remain valid throughout the mutation admission.
        """
        ...
