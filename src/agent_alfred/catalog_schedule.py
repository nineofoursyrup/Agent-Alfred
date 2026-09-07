"""Schedule catalog fetches: assigned-first, lazy expand, merged in-flight."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence

from agent_alfred.catalog import CatalogState, CatalogTransport, list_models
from agent_alfred.clock import Clock
from agent_alfred.endpoints import ModelEndpoint


class CatalogScheduler:
    def __init__(
        self,
        *,
        clock: Clock,
        pool,
        transport: CatalogTransport,
        endpoints: Sequence[ModelEndpoint],
    ):
        self._clock = clock
        self._pool = pool
        self._transport = transport
        self._endpoints = {row.endpoint_id: row for row in endpoints}
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._generation = 0

    def bump(self) -> None:
        with self._lock:
            self._generation += 1

    def cached_states(self) -> dict[str, CatalogState]:
        states: dict[str, CatalogState] = {}
        for endpoint_id in self._endpoints:
            cached = self._pool.cached_catalog(endpoint_id)
            states[endpoint_id] = (
                cached
                if isinstance(cached, CatalogState)
                else CatalogState("unfetched")
            )
        return states

    def open_assigned(
        self, *, assigned_endpoint_id: str, keys: Mapping[str, str | None]
    ) -> dict[str, CatalogState]:
        states: dict[str, CatalogState] = {}
        for endpoint_id, endpoint in self._endpoints.items():
            if endpoint_id == assigned_endpoint_id:
                states[endpoint_id] = self.ensure(
                    endpoint, api_key=keys.get(endpoint_id)
                )
            else:
                cached = self._pool.cached_catalog(endpoint_id)
                states[endpoint_id] = (
                    cached
                    if isinstance(cached, CatalogState)
                    else CatalogState("unfetched")
                )
        return states

    def ensure(
        self,
        endpoint: ModelEndpoint,
        *,
        api_key: str | None,
        refresh: bool = False,
    ) -> CatalogState:
        if not (api_key or "").strip():
            cached = self._pool.cached_catalog(endpoint.endpoint_id)
            return (
                cached
                if isinstance(cached, CatalogState)
                else CatalogState("unfetched")
            )
        if not refresh:
            cached = self._pool.cached_catalog(endpoint.endpoint_id)
            if isinstance(cached, CatalogState):
                now = self._clock.monotonic()
                if (
                    cached.expires_at is not None
                    and now < cached.expires_at
                    and cached.health in ("fresh", "stale", "unavailable")
                ):
                    return cached
        waiter: threading.Event | None = None
        owner = False
        with self._lock:
            existing = self._inflight.get(endpoint.endpoint_id)
            if existing is not None:
                waiter = existing
            else:
                waiter = threading.Event()
                self._inflight[endpoint.endpoint_id] = waiter
                owner = True
        if not owner:
            waiter.wait()
            cached = self._pool.cached_catalog(endpoint.endpoint_id)
            return (
                cached
                if isinstance(cached, CatalogState)
                else CatalogState("unfetched")
            )
        epoch = self._pool.catalog_epoch(endpoint.endpoint_id)
        try:
            return list_models(
                endpoint,
                api_key=api_key,
                transport=self._transport,
                clock=self._clock,
                pool=self._pool,
                refresh=refresh,
                epoch=epoch,
            )
        finally:
            with self._lock:
                current = self._inflight.get(endpoint.endpoint_id)
                if current is waiter:
                    self._inflight.pop(endpoint.endpoint_id, None)
            waiter.set()
