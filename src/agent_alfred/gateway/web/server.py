"""Assemble the Dashboard: Host, broker, API, lifecycle, and the serving thread.

The ordering the decision fixes is visible here in one function:

1. the broker is built first, because the Host's authoritative state store has
   to be able to publish patches into it from the very first transition --
   including the ones that happen during start-up recovery;
2. the Host is built with that broker both as an event sink and as the
   snapshot listener, so the state a browser sees is the state the Host
   decided, published after the decision rather than around it;
3. the lifecycle takes the lock, binds, and only then writes the descriptor;
4. serving starts last, so nothing can reach a port before the descriptor
   claims it.

Closing runs the same list backwards: stop serving, stop the stream, release
the socket, forget the descriptor, drop the lock.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.gateway.web.broker import SSEBroker
from agent_alfred.gateway.web.guard import RequestGuard
from agent_alfred.gateway.web.handler import DashboardHandler, HandlerContext
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DashboardService,
    EntryDescriptor,
)
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.runtime.work import SubmitRequest, SubmitResult


class HostFacade:
    """The Dashboard's view of the Host: the read side plus one write gate.

    It adds no state and makes no decisions. Every write goes through
    ``submit``, which is the same gate the CLI uses -- that is what makes
    "at most one Run at a time" true of the process rather than of a
    particular surface.
    """

    def __init__(self, host: RuntimeHost):
        self._host = host

    def create_session(self) -> str:
        return self._host.create_session()

    def submit(self, request: SubmitRequest) -> SubmitResult:
        return self._host.submit(request)

    def session_exists(self, session_id: str) -> bool:
        return self._host.session_exists(session_id)

    def snapshot(self) -> Any:
        return self._host.snapshot()

    def list_sessions(self, *, limit: int, cursor: str | None = None) -> Any:
        return self._host.list_sessions(limit=limit, cursor=cursor)

    def open_session(
        self, session_id: str, *, page_size: int, cursor: str | None = None
    ) -> Any:
        return self._host.open_session(
            session_id, page_size=page_size, cursor=cursor
        )

    def list_runs(
        self, *, filter: str, limit: int, cursor: str | None = None
    ) -> Any:
        return self._host.list_runs(filter=filter, limit=limit, cursor=cursor)

    def locate_run(self, run_id: str, *, limit: int) -> Any | None:
        return self._host.locate_run(run_id, limit=limit)

    def mainbar_pairs(self, *, limit: int, cursor: str | None = None) -> Any:
        return self._host.mainbar_pairs(limit=limit, cursor=cursor)


class DashboardRuntime:
    """The Dashboard as one object: lock, socket, stream, and API."""

    def __init__(
        self,
        *,
        host: RuntimeHost,
        state_dir: Path,
        broker: SSEBroker,
        port: int = DEFAULT_PORT,
        bind_host: str = DEFAULT_HOST,
        csrf_token: str | None = None,
    ):
        self._host = host
        self._state_dir = state_dir
        self._broker = broker
        self._port = port
        self._bind_host = bind_host
        # One token per process, minted here so it cannot be shared with
        # anything that outlives this process. A cross-origin page cannot
        # read it (no Access-Control-Allow-Origin is ever sent), which is
        # what makes handing it out on a GET safe.
        self._csrf_token = csrf_token or secrets.token_urlsafe(32)
        self._service: DashboardService | None = None
        self._thread: Any = None

    @property
    def broker(self) -> SSEBroker:
        return self._broker

    @property
    def csrf_token(self) -> str:
        return self._csrf_token

    @property
    def service(self) -> DashboardService | None:
        return self._service

    @property
    def port(self) -> int:
        return self._port

    @property
    def descriptor(self) -> EntryDescriptor | None:
        return None if self._service is None else self._service.descriptor

    @property
    def started(self) -> bool:
        return self._service is not None and self._service.started

    def start(self) -> EntryDescriptor:
        """Lock, bind, describe, then serve. In that order or not at all."""
        if self.started:
            assert self._service is not None
            assert self._service.descriptor is not None
            return self._service.descriptor
        # The dispatcher runs before the socket exists: an event published
        # during start-up recovery is queued and delivered to the first
        # connection that arrives, rather than lost.
        self._broker.start()
        context = HandlerContext(
            guard=RequestGuard(port=self._port, csrf_token=self._csrf_token),
            api=DashboardApi(facade=HostFacade(self._host)),
            broker=self._broker,
            instance_id=self._host.process_instance_id,
        )
        self._service = DashboardService(
            state_dir=self._state_dir,
            handler=DashboardHandler,
            context=context,
            instance_id=self._host.process_instance_id,
            host=self._bind_host,
            port=self._port,
        )
        try:
            descriptor = self._service.start()
        except BaseException:
            self._service = None
            self._broker.close(timeout=0.0)
            raise
        self._thread = self._service.start_serving()
        return descriptor

    def close(self, timeout: float | None = None) -> bool:
        """Undo the whole list, from the outside in. Idempotent."""
        service, self._service = self._service, None
        stopped = True
        if service is not None:
            service.close()
            # The writer threads are what hold handler threads open; the
            # broker is closed after the socket so a stream cannot be
            # re-primed by a late patch. The broker owns the drain timeout:
            # it is the thing that has to give up on a peer that stopped
            # reading.
            stopped = (
                self._broker.close()
                if timeout is None
                else self._broker.close(timeout=timeout)
            )
        return stopped
