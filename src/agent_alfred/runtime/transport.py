"""Transport pool keyed by immutable config version (issue #29)."""

from __future__ import annotations

import threading
from collections.abc import Callable

from agent_alfred.model import ClientSnapshot, ModelClient


class VersionedTransportPool:
    """Caches clients per (endpoint_id, config_version).

    A new version for an endpoint evicts that endpoint's client and the
    in-memory observation / catalog caches for it. Credentials never enter
    the cache key.
    """

    def __init__(self, build: Callable[[ClientSnapshot], ModelClient]):
        self._build = build
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str], ModelClient] = {}
        self._observation: dict[str, object] = {}
        self._catalog: dict[str, object] = {}

    def create(self, snapshot: ClientSnapshot) -> ModelClient:
        return self.client_for(snapshot)

    def client_for(self, snapshot: ClientSnapshot) -> ModelClient:
        endpoint_id = snapshot.endpoint_id
        version = snapshot.config_version
        key = (endpoint_id, version)
        with self._lock:
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            for stale in [item for item in self._clients if item[0] == endpoint_id]:
                del self._clients[stale]
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
