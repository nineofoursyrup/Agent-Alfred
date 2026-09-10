"""Scoped model IO: identity begins after SDK encoding at HTTP transport dispatch.

The SDKs use httpx2. Its transport slots are the single version-specific assembly
seam here; codecs, retries, and runtime never inspect SDK/HTTP internals. Wrappers
preserve existing proxy mounts and ownership, and do nothing outside an Attempt.
"""

from __future__ import annotations

import socket
import threading
from contextvars import ContextVar
from functools import wraps

import httpcore2
import httpx2
from httpcore2._backends.sync import SyncStream, TLSinTLSStream
from httpcore2._exceptions import map_exceptions

from agent_alfred.adapter_common import _aborted, _error_from_exc
from agent_alfred.bounded_connect import BoundedConnector
from agent_alfred.model import ModelCallInterrupted, Usage
from agent_alfred.resource_rollback import (
    ConstructionOwner,
    ResumableRollback,
    RollbackSlot,
)
from agent_alfred.runtime.telemetry import AttemptObservationFailed

_current = ContextVar("model_attempt_io", default=None)
_install_lock = threading.Lock()


class AttemptScope:
    def __init__(self, request, *, sdk_boundary, budget=None, streamed=False):
        self.request = request
        self.sdk_boundary = sdk_boundary
        self.budget = budget
        self.started = False
        self.start_event = None
        self.local_failure = None
        self.io_failure_edges = []
        self.io_control = None
        self.streamed = streamed
        self.usage = Usage()

    def preserve_io_failure(self, failure):
        self.io_failure_edges.append(
            (failure, failure.__cause__, failure.__context__)
        )
        if self.io_control is None and isinstance(
            failure, (KeyboardInterrupt, SystemExit, GeneratorExit)
        ):
            self.io_control = failure

    def start(self):
        if self.started:
            return
        if self.budget is not None:
            self.budget.check()
        callback, attempt_id = self.start_event
        try:
            callback()
            self.request.notify_attempt_started(
                attempt_id, self.budget.deadline if self.budget is not None else None
            )
        except Exception as exc:
            self.local_failure = AttemptObservationFailed(exc)
            raise self.local_failure from exc
        self.started = True


def attempt_scope(method):
    @wraps(method)
    def scoped(self, request, *, events=None, deadline=None):
        budget = getattr(self, "_io_budget", None)
        if budget is None and deadline is not None:
            raise ValueError("absolute deadline requires an injected IO budget")
        sdk = getattr(self._client, "_sdk", self._client)
        if budget is not None and getattr(sdk, "max_retries", 0) != 0:
            raise ValueError("budgeted SDK requests require max_retries=0")
        scope = AttemptScope(
            request,
            sdk_boundary=install_http_boundary(self._client),
            budget=budget,
            streamed=self._stream,
        )
        token = _current.set(scope)
        try:
            return method(self, request, events=events, deadline=deadline)
        except ModelCallInterrupted:
            raise
        except BaseException as exc:
            if not scope.started:
                raise
            _, attempt_id = scope.start_event
            error = _error_from_exc(
                attempt_id, exc, retryable=False, code="model_call_interrupted"
            )
            result = _aborted(
                attempt_id, error, streamed=scope.streamed, usage=scope.usage
            )
            raise ModelCallInterrupted(exc, result) from exc
        finally:
            # httpcore's pool uses `raise exc from None`, mutating even a
            # process-control exception. Restore IO evidence after that hop.
            for failure, cause, context in scope.io_failure_edges:
                failure.__cause__ = cause
                failure.__context__ = context
            _current.reset(token)

    return scoped


def preserve_io_failure(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except BaseException as failure:
            scope = _current.get()
            if scope is not None:
                scope.preserve_io_failure(failure)
            raise

    return guarded


def prepare_attempt(callback, attempt_id):
    _current.get().start_event = (callback, attempt_id)


def preserve_attempt_usage(usage):
    """Publish decoded usage before callbacks, stream reads, or cleanup can fail."""
    _current.get().usage = usage


def start_transport():
    scope = _current.get()
    if not scope.sdk_boundary:
        # A directly injected Wire is the already encoded transport seam.
        scope.start()


def propagate_local_failure():
    scope = _current.get()
    if scope is not None and not scope.started:
        if scope.local_failure is not None:
            raise scope.local_failure
        raise  # preserve the SDK's local encoding failure; no Attempt exists


class ModelHTTPTransport(httpx2.BaseTransport):
    def __init__(self, inner, *, network_backend=None):
        self.inner = inner
        # The HTTP client outlives SDK exception conversion and owns every
        # connection whose failed construction still needs cleanup.
        self._pending_cleanup = RollbackSlot()
        self._close_owner = ResumableRollback()
        self._close_owner.own(self._pending_cleanup)
        # Closing the pool can itself publish failed stream cleanup. Drain
        # that slot after the pool has released its connection references.
        self._close_owner.own(inner)
        self._unbounded_connections = set()
        if isinstance(inner, httpx2.HTTPTransport):
            pool = inner._pool
            backend = network_backend or pool._network_backend
            if not isinstance(backend, DeadlineNetworkBackend):
                self._unbounded_connections = {id(c) for c in pool.connections}
                pool._network_backend = DeadlineNetworkBackend(
                    backend, self._pending_cleanup
                )

    def handle_request(self, request):
        scope = _current.get()
        if scope is not None:
            if scope.budget is not None and self._unbounded_connections:
                if any(
                    id(c) in self._unbounded_connections
                    for c in self.inner._pool.connections
                ):
                    raise RuntimeError(
                        "existing HTTP connection has no IO deadline guard"
                    )
            if scope.started:
                raise RuntimeError("a model Attempt permits only one HTTP dispatch")
            if scope.budget is not None:
                timeouts = dict(request.extensions.get("timeout", {}))
                for phase in ("connect", "read", "write"):
                    timeouts[phase] = scope.budget.remaining(timeouts.get(phase))
                # No pool queueing: internal retries must not reset a wait budget.
                timeouts["pool"] = 0.0
                request.extensions["timeout"] = timeouts
            scope.start()
        return self.inner.handle_request(request)

    def close(self):
        self._close_owner.close()


def install_http_boundary(client):
    sdk = getattr(client, "_sdk", client)
    http = getattr(sdk, "_client", None)
    if not isinstance(http, httpx2.Client):
        return False
    with _install_lock:
        if not isinstance(http._transport, ModelHTTPTransport):
            http._transport = ModelHTTPTransport(http._transport)
        for key, transport in tuple(http._mounts.items()):
            if transport is not None and not isinstance(transport, ModelHTTPTransport):
                http._mounts[key] = ModelHTTPTransport(transport)
    return True


class AttemptIOBudget:
    """Policy-owned absolute deadline shared by every blocking IO operation."""

    def __init__(self, clock, deadline):
        self.clock = clock
        self.deadline = deadline

    def remaining(self, timeout=None):
        remaining = self.deadline - self.clock.monotonic()
        if remaining <= 0:
            raise TimeoutError("attempt_deadline")
        return remaining if timeout is None else min(timeout, remaining)

    def check(self):
        self.remaining()


def _remaining(timeout):
    scope = _current.get()
    if scope is None or scope.budget is None:
        return timeout
    return scope.budget.remaining(timeout)


def deadline_chunks(response):
    iterator = iter(response)
    while True:
        _remaining(None)
        try:
            chunk = next(iterator)
        except StopIteration:
            return
        _remaining(None)
        yield chunk


class _TLSIO:
    """Socket-shaped IO for httpcore's MemoryBIO TLS, borrowing an owned stream.

    MemoryBIO never detaches or replaces the socket. Even interruption inside
    TLS construction leaves the same resource in its already-published owner.
    Each TLS read/write re-enters the stream's absolute deadline guard.
    """

    def __init__(self, stream):
        self.stream = stream
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.stream.write(data, timeout=self.timeout)

    def recv(self, count):
        return self.stream.read(count, timeout=self.timeout)

    def fileno(self):
        return self.stream.get_extra_info("socket").fileno()

    def getsockname(self):
        return self.stream.get_extra_info("client_addr")

    def getpeername(self):
        return self.stream.get_extra_info("server_addr")

    def close(self):
        self.stream.close()


class DeadlineNetworkStream(httpcore2.NetworkStream):
    def __init__(self, inner, cleanup_owner, *, close_owner=None):
        self.inner = inner
        self._cleanup_owner = cleanup_owner
        self._close_owner = close_owner or ResumableRollback()
        if close_owner is None:
            self._close_owner.own(inner)

    def _timeout(self, timeout):
        try:
            return _remaining(timeout)
        except BaseException as failure:
            self._cleanup_owner.begin(self._close_owner)
            self._close_owner.raise_failure(failure)

    @preserve_io_failure
    def read(self, max_bytes, timeout=None):
        return self.inner.read(max_bytes, timeout=self._timeout(timeout))

    @preserve_io_failure
    def write(self, buffer, timeout=None):
        timeout = self._timeout(timeout)
        if type(self.inner) is not SyncStream:
            return self.inner.write(buffer, timeout=timeout)
        # SyncStream.write loops over partial sends with one relative timeout.
        # Own that loop here so every blocking send uses the absolute remainder;
        # MemoryBIO handshake and application data both reach this same seam.
        sock = self.inner.get_extra_info("socket")
        with map_exceptions({socket.timeout: httpcore2.WriteTimeout,
                             OSError: httpcore2.WriteError}):
            view = memoryview(buffer)
            while view:
                sock.settimeout(self._timeout(timeout))
                sent = sock.send(view)
                if sent == 0:
                    raise httpcore2.WriteError("socket write made no progress")
                view = view[sent:]

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        owner = ConstructionOwner(self._close_owner)
        self._cleanup_owner.begin(owner.rollback)
        try:
            timeout = self._timeout(timeout)
            if type(self.inner) in (SyncStream, TLSinTLSStream):
                upgraded = TLSinTLSStream(
                    _TLSIO(self), ssl_context, server_hostname, timeout
                )
            else:
                # Injected backends keep their explicit upgrade semantics.
                upgraded = self.inner.start_tls(
                    ssl_context, server_hostname=server_hostname, timeout=timeout
                )
                if upgraded is not self.inner:
                    owner.publish(upgraded, parts=(self.inner,))
            return DeadlineNetworkStream(
                upgraded,
                self._cleanup_owner,
                close_owner=owner.rollback,
            )
        except BaseException as failure:
            try:
                owner.fail(failure)
            except BaseException as propagated:
                self._cleanup_owner.capture_failure(propagated)
                scope = _current.get()
                if scope is not None:
                    scope.preserve_io_failure(propagated)
                raise

    def get_extra_info(self, info):
        return self.inner.get_extra_info(info)

    def close(self):
        try:
            self._close_owner.close()
            self._cleanup_owner.complete(self._close_owner)
        except BaseException:
            # httpcore removes a connection before closing its stream. The
            # transport must retain the stream's progress before propagation.
            self._cleanup_owner.begin(self._close_owner)
            scope = _current.get()
            if scope is not None and scope.io_control is not None:
                # httpcore closes during unwinding. A failed close must not
                # replace the control signal that caused that cleanup.
                try:
                    self._close_owner.raise_incomplete(scope.io_control)
                except BaseException as propagated:
                    scope.preserve_io_failure(propagated)
                    raise
            raise


class DeadlineNetworkBackend(httpcore2.NetworkBackend):
    def __init__(self, inner, cleanup_owner):
        self.inner = inner
        self._cleanup_owner = cleanup_owner
        # Injected public backends retain their own deterministic IO semantics.
        self._connector = (
            BoundedConnector() if type(inner) is httpcore2.SyncBackend else None
        )

    def connect_tcp(self, host, port, timeout=None, **kwargs):
        try:
            return self._connect_tcp(host, port, timeout, **kwargs)
        except BaseException as exc:
            # httpcore and SDKs may replace the cause while normalizing errors.
            # Transfer ownership before those layers can discard the handle.
            self._cleanup_owner.capture_failure(exc)
            raise

    def _connect_tcp(self, host, port, timeout=None, **kwargs):
        scope = _current.get()
        if (
            self._connector is not None
            and scope is not None
            and scope.budget is not None
        ):
            close_owner = ResumableRollback()
            self._cleanup_owner.begin(close_owner)
            stream = self._connector.connect_tcp(
                host, port, timeout=timeout, budget=scope.budget,
                _rollback=close_owner, **kwargs,
            )
            return DeadlineNetworkStream(
                stream, self._cleanup_owner, close_owner=close_owner
            )
        else:
            stream = self.inner.connect_tcp(
                host, port, timeout=_remaining(timeout), **kwargs
            )
        return DeadlineNetworkStream(stream, self._cleanup_owner)

    def connect_unix_socket(self, path, timeout=None, **kwargs):
        return DeadlineNetworkStream(
            self.inner.connect_unix_socket(path, timeout=_remaining(timeout), **kwargs),
            self._cleanup_owner,
        )

    def sleep(self, seconds):
        return self.inner.sleep(_remaining(seconds))
