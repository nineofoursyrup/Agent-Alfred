"""Cancellable resolver process and one connection budget across all addresses."""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import sys

import httpcore2
from httpcore2._backends.sync import SyncStream

from agent_alfred.resource_rollback import ConstructionOwner, ResumableRollback

_MAX_OUTPUT = 32768
_RESOLVE = """import json,socket,sys
try:
 rows=socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),0,socket.SOCK_STREAM)
 if not 0<len(rows)<=64: sys.exit(2)
 data=json.dumps(rows,ensure_ascii=True)
 if len(data)>32768: sys.exit(2)
 sys.stdout.write(data)
except Exception:
 sys.exit(2)
"""


class _ResolverChild:
    def __init__(self):
        self.process = None

    def start(self, host, port):
        # Publish the process object before __init__ can create its child/pipes.
        self.process = subprocess.Popen.__new__(subprocess.Popen)
        self.process.__init__(
            [sys.executable, "-I", "-c", _RESOLVE, host, str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
        )

    def communicate(self, timeout):
        output, _ = self.process.communicate(timeout=timeout)
        if self.process.returncode != 0:
            raise httpcore2.ConnectError("resolver_failed")
        return output

    def close(self):
        process = self.process
        if process is None:
            return
        if getattr(process, "_child_created", False):
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()
        for name in ("stdin", "stdout", "stderr"):
            pipe = getattr(process, name, None)
            if pipe is not None:
                pipe.close()


def _literal(host, port):
    scope = 0
    scoped = "%" in host
    if scoped:
        host, suffix = host.rsplit("%", 1)
        if not suffix.isascii() or not suffix.isdecimal():
            raise ValueError("unsupported_ipv6_scope")
        scope = int(suffix)
        if not 0 <= scope <= 0xFFFFFFFF:
            raise ValueError("unsupported_ipv6_scope")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if scoped:
            raise ValueError("unsupported_ipv6_scope") from None
        return None
    if address.version == 4:
        if scoped:
            raise ValueError("unsupported_ipv6_scope")
        return (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            (str(address), port),
        )
    return (
        socket.AF_INET6,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        (str(address), port, 0, scope),
    )


def _addresses(output, port):
    if not isinstance(output, bytes) or len(output) > _MAX_OUTPUT:
        raise httpcore2.ConnectError("invalid_resolver_output")
    try:
        rows = json.loads(output)
        if type(rows) is not list or not 0 < len(rows) <= 64:
            raise ValueError
        result = []
        for row in rows:
            if type(row) is not list or len(row) != 5:
                raise ValueError
            family, kind, protocol, canonical, address = row
            if (
                type(family) is not int
                or family not in (socket.AF_INET, socket.AF_INET6)
                or type(kind) is not int
                or kind != socket.SOCK_STREAM
                or type(protocol) is not int
                or protocol not in (0, socket.IPPROTO_TCP)
                or type(canonical) is not str
                or type(address) is not list
            ):
                raise ValueError
            size = 2 if family == socket.AF_INET else 4
            if len(address) != size or type(address[0]) is not str or "%" in address[0]:
                raise ValueError
            if type(address[1]) is not int or address[1] != port:
                raise ValueError
            parsed = ipaddress.ip_address(address[0])
            if parsed.version != (4 if family == socket.AF_INET else 6):
                raise ValueError
            if family == socket.AF_INET6 and any(
                type(value) is not int or not 0 <= value <= 0xFFFFFFFF
                for value in address[2:]
            ):
                raise ValueError
            result.append((family, kind, protocol, tuple(address)))
        return result
    except ValueError, TypeError, UnicodeError:
        raise httpcore2.ConnectError("invalid_resolver_output") from None


class ProcessResolver:
    def __init__(self, *, child_factory=_ResolverChild):
        self._child_factory = child_factory

    def resolve(self, host, port, budget):
        if (
            type(host) is not str
            or not host
            or len(host) > 253
            or type(port) is not int
            or not 0 < port <= 65535
        ):
            raise ValueError("invalid_resolver_target")
        budget.check()
        literal = _literal(host, port)
        if literal is not None:
            return [literal]
        owner = ConstructionOwner()
        child = self._child_factory()
        owner.rollback.own(child)
        try:
            child.start(host, port)
            try:
                output = child.communicate(budget.remaining())
            except subprocess.TimeoutExpired:
                raise httpcore2.ConnectTimeout("resolver_deadline") from None
            child.close()
            owner.rollback.transfer(child)
            budget.check()
            return _addresses(output, port)
        except BaseException as exc:
            owner.fail(exc)


class _SocketResource:
    def __init__(self, socket_type):
        self.socket = None
        self._socket_type = socket_type

    def open(self, family, kind, protocol):
        self.socket = self._socket_type.__new__(self._socket_type)
        self.socket.__init__(family, kind, protocol)

    def close(self):
        if self.socket is not None:
            self.socket.close()


class BoundedConnector:
    def __init__(self, *, resolver=None, socket_factory=socket.socket):
        self._resolver = resolver or ProcessResolver()
        self._socket_type = socket_factory

    def connect_tcp(
        self,
        host,
        port,
        *,
        budget,
        timeout=None,
        local_address=None,
        socket_options=None,
        _rollback: ResumableRollback | None = None,
    ):
        local = None
        if local_address is not None:
            local = _literal(local_address, 0)
            if local is None:
                raise ValueError("local_address_must_be_literal")
        addresses = self._resolver.resolve(host, port, budget)
        last = None
        for family, kind, protocol, address in addresses:
            if local is not None and local[0] != family:
                continue
            owner = ConstructionOwner(_rollback)
            resource = _SocketResource(self._socket_type)
            owner.rollback.own(resource)
            try:
                resource.open(family, kind, protocol)
                sock = resource.socket
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if local is not None:
                    sock.bind(local[3])
                sock.settimeout(budget.remaining(timeout))
                sock.connect(address)
                budget.check()
                stream = SyncStream(sock)
                # An offered owner keeps the original socket token across
                # both this return and the backend wrapper's later return.
                # SyncStream is only a view; no physical resource changes.
                if _rollback is None:
                    owner.rollback.transfer(resource)
                return stream
            except OSError as exc:
                # Cleanup is a separate failure boundary: never catch and discard
                # a failed close while moving on to another address.
                last = exc
                if not owner.rollback.retry():
                    owner.rollback.raise_incomplete(exc)
            except BaseException as exc:
                owner.fail(exc)
        if isinstance(last, (socket.timeout, TimeoutError)):
            raise httpcore2.ConnectTimeout("connection_deadline") from last
        raise httpcore2.ConnectError("connection_failed") from last
