"""Transport pool keyed by immutable config version (issue #29)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from agent_alfred.model import ClientSnapshot


class VersionedTransportPool:
    """Caches clients per (endpoint_id, config_version).

    A new version for an endpoint evicts that endpoint's client and the
    in-memory observation / catalog caches for it. Credentials never enter
    the cache key.
    """

    def __init__(self, build: Callable[[ClientSnapshot], Any]):
        self._build = build
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str], Any] = {}
        self._observation: dict[str, object] = {}
        self._catalog: dict[str, object] = {}
        self._catalog_epoch: dict[str, int] = {}

    def create(self, snapshot: ClientSnapshot) -> Any:
        return self.client_for(snapshot)

    def client_for(self, snapshot: ClientSnapshot) -> Any:
        endpoint_id = snapshot.endpoint_id
        version = snapshot.config_version
        key = (endpoint_id, version)
        with self._lock:
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            for stale in [item for item in self._clients if item[0] == endpoint_id]:
                del self._clients[stale]
            self._bump_catalog_epoch(endpoint_id)
            self._observation.pop(endpoint_id, None)
            self._catalog.pop(endpoint_id, None)
            client = self._build(snapshot)
            self._clients[key] = client
            return client

    def cached_observation(self, endpoint_id: str) -> object | None:
        with self._lock:
            return self._observation.get(endpoint_id)

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
            for stale in [item for item in self._clients if item[0] == endpoint_id]:
                del self._clients[stale]
            self._bump_catalog_epoch(endpoint_id)
            self._observation.pop(endpoint_id, None)
            self._catalog.pop(endpoint_id, None)

    def discard_all(self) -> None:
        with self._lock:
            self._clients.clear()
            self._observation.clear()
            for endpoint_id in list(self._catalog) + list(self._catalog_epoch):
                self._bump_catalog_epoch(endpoint_id)
            self._catalog.clear()

    def _bump_catalog_epoch(self, endpoint_id: str) -> None:
        self._catalog_epoch[endpoint_id] = self._catalog_epoch.get(endpoint_id, 0) + 1
