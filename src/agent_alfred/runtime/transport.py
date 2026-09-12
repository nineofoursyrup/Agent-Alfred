"""Transport pool keyed by immutable config version (issue #29)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from agent_alfred.model import ClientSnapshot
from agent_alfred.resource_rollback import ResumableRollback, RollbackSlot


class VersionedTransportPool:
    """Cache each model route within an endpoint's immutable config version.

    A new version for an endpoint evicts that endpoint's client and the
    in-memory observation / catalog caches for it. Credentials never enter
    the cache key.
    """

    def __init__(
        self,
        build: Callable[[ClientSnapshot], Any],
        *,
        close: Callable[[Any], None] | None = None,
    ):
        self._build = build
        self._close_client = close
        self._retired = RollbackSlot()
        self._closed = False
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str, str, str], Any] = {}
        self._observation: dict[str, object] = {}
        self._catalog: dict[str, object] = {}
        self._catalog_epoch: dict[str, int] = {}

    def create(self, snapshot: ClientSnapshot) -> Any:
        return self.client_for(snapshot)

    def client_for(self, snapshot: ClientSnapshot) -> Any:
        endpoint_id = snapshot.endpoint_id
        version = snapshot.config_version
        key = (endpoint_id, version, snapshot.model_id, snapshot.wire_style)
        with self._lock:
            if self._closed:
                raise RuntimeError("transport pool is closed")
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            same_version = any(item[:2] == key[:2] for item in self._clients)
            if not same_version:
                self._retire(endpoint_id)
                self._bump_catalog_epoch(endpoint_id)
                self._observation.pop(endpoint_id, None)
                self._catalog.pop(endpoint_id, None)
            self._retired.close()
            client = self._build(snapshot)
            self._clients[key] = client
            return client

    def cached_observation(self, endpoint_id: str) -> object | None:
        with self._lock:
            return self._observation.get(endpoint_id)

    def catalog_snapshot(self):
        import copy

        with self._lock:
            return copy.deepcopy(self._catalog)

    def cached_catalog(self, endpoint_id: str) -> object | None:
        with self._lock:
            return self._catalog.get(endpoint_id)

    def catalog_epoch(self, endpoint_id: str) -> int:
        with self._lock:
            return self._catalog_epoch.get(endpoint_id, 0)

    def store_catalog(
        self, endpoint_id: str, value: object, *, epoch: int | None = None
    ) -> None:
        with self._lock:
            current = self._catalog_epoch.get(endpoint_id, 0)
            if epoch is not None and epoch != current:
                return
            self._catalog[endpoint_id] = value

    def store_observation(self, endpoint_id: str, value: object) -> None:
        with self._lock:
            self._observation[endpoint_id] = value

    def discard(self, endpoint_id: str) -> None:
        with self._lock:
            self._retire(endpoint_id)
            self._bump_catalog_epoch(endpoint_id)
            self._observation.pop(endpoint_id, None)
            self._catalog.pop(endpoint_id, None)
            self._retired.close()

    def discard_all(self) -> None:
        with self._lock:
            self._discard_all()

    def close(self) -> None:
        """Refuse new clients and retry every unfinished eviction."""
        with self._lock:
            self._closed = True
            self._discard_all()

    def _retire(self, endpoint_id: str | None = None) -> None:
        for key, client in tuple(self._clients.items()):
            if endpoint_id is not None and key[0] != endpoint_id:
                continue
            if self._close_client is not None:
                owner = ResumableRollback()
                self._retired.begin(owner)
                owner.own(client, lambda client=client: self._close_client(client))
            # The surviving slot owns cleanup before the cache drops its ref.
            del self._clients[key]

    def _discard_all(self) -> None:
        self._retire()
        self._observation.clear()
        for endpoint_id in list(self._catalog) + list(self._catalog_epoch):
            self._bump_catalog_epoch(endpoint_id)
        self._catalog.clear()
        self._retired.close()

    def _bump_catalog_epoch(self, endpoint_id: str) -> None:
        self._catalog_epoch[endpoint_id] = self._catalog_epoch.get(endpoint_id, 0) + 1
