"""Resolver process ownership and one deadline across all candidate addresses."""

import socket
import subprocess

import httpcore2
import pytest

from agent_alfred.attempt_io import AttemptIOBudget
from agent_alfred.clock import FakeClock, SystemClock


def test_multiple_addresses_share_one_deadline_and_every_failed_socket_closes():
    from agent_alfred.bounded_connect import BoundedConnector

    clock = FakeClock()
    timeouts, closed = [], []

    class Resolver:
        def resolve(self, host, port, budget):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    ("192.0.2.1", port),
                ),
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    ("192.0.2.2", port),
                ),
            ]

    class Socket:
        def __init__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            timeouts.append(self.timeout)
            clock.monotonic_value += min(3, self.timeout)
            raise socket.timeout("injected connect timeout")

        def close(self):
            closed.append(True)

    connector = BoundedConnector(resolver=Resolver(), socket_factory=Socket)
    with pytest.raises(httpcore2.ConnectTimeout):
        connector.connect_tcp("fixture.test", 443, budget=AttemptIOBudget(clock, 5))
    assert clock.monotonic_value == 5
    assert timeouts == [5, 2]
    assert len(closed) == 2


def test_resolver_timeout_kills_reaps_and_closes_before_returning():
    from agent_alfred.bounded_connect import ProcessResolver

    clock = FakeClock()
    actions = []

    class Child:
        def start(self, host, port):
            actions.append("start")

        def communicate(self, timeout):
            actions.append(("communicate", timeout))
            clock.monotonic_value += timeout
            raise subprocess.TimeoutExpired("resolver", timeout)

        def close(self):
            actions.extend(["kill", "reap", "pipes"])

    with pytest.raises(httpcore2.ConnectTimeout):
        ProcessResolver(child_factory=Child).resolve(
            "fixture.test", 443, AttemptIOBudget(clock, 5)
        )
    assert actions == ["start", ("communicate", 5), "kill", "reap", "pipes"]


def test_real_managed_resolver_localhost_returns_typed_addresses_and_reaps():
    from agent_alfred.bounded_connect import ProcessResolver

    clock = SystemClock()
    addresses = ProcessResolver().resolve(
        "localhost", 443, AttemptIOBudget(clock, clock.monotonic() + 5)
    )
    assert addresses
    assert all(row[0] in (socket.AF_INET, socket.AF_INET6) for row in addresses)
    assert all(row[3][1] == 443 for row in addresses)


def test_literal_ip_needs_no_resolver_and_named_ipv6_scope_is_explicitly_rejected():
    from agent_alfred.bounded_connect import ProcessResolver

    def forbidden():
        raise AssertionError("literal IP must not start a process")

    resolver = ProcessResolver(child_factory=forbidden)
    assert resolver.resolve("127.0.0.1", 443, AttemptIOBudget(FakeClock(), 5)) == [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("127.0.0.1", 443))
    ]
    with pytest.raises(ValueError, match="scope"):
        resolver.resolve("fe80::1%en0", 443, AttemptIOBudget(FakeClock(), 5))


def test_connect_close_failure_preserves_original_and_retry_owner():
    from agent_alfred.bounded_connect import BoundedConnector
    from agent_alfred.resource_rollback import IncompleteRollback

    original = OSError("connect failed")
    closed = []

    class Socket:
        def __init__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            raise original

        def close(self):
            closed.append(True)
            if len(closed) == 1:
                raise OSError("close failed")

    with pytest.raises(OSError) as caught:
        BoundedConnector(socket_factory=Socket).connect_tcp(
            "127.0.0.1", 443, budget=AttemptIOBudget(FakeClock(), 5)
        )
    assert caught.value is original
    assert isinstance(original.__cause__, IncompleteRollback)
    assert original.__cause__.retry()
    assert len(closed) == 2


@pytest.mark.parametrize("failure", [OSError("start"), KeyboardInterrupt("interrupt")])
def test_resolver_construction_error_retains_child_and_cleanup_progress(failure):
    from agent_alfred.bounded_connect import ProcessResolver
    from agent_alfred.resource_rollback import IncompleteRollback

    closed = []

    class Child:
        def start(self, host, port):
            raise failure

        def close(self):
            closed.append(True)
            if len(closed) == 1:
                raise OSError("reap failed")

    with pytest.raises(type(failure)) as caught:
        ProcessResolver(child_factory=Child).resolve(
            "fixture.test", 443, AttemptIOBudget(FakeClock(), 5)
        )
    assert caught.value is failure
    assert isinstance(failure.__cause__, IncompleteRollback)
    assert failure.__cause__.retry()
    assert len(closed) == 2


@pytest.mark.parametrize(
    "output",
    [
        b"{}",
        b"[]",
        b"x" * 32769,
        b'[[2,1,6,"",["not-an-ip",443]]]',
        b'[[2,1,6,"",["127.0.0.1",true]]]',
        b'[[2,1,6,"",["127.0.0.1",444]]]',
        b'[[30,1,6,"",["::1",443,0,-1]]]',
    ],
)
def test_invalid_resolver_payload_is_rejected_after_child_is_closed(output):
    from agent_alfred.bounded_connect import ProcessResolver

    actions = []

    class Child:
        def start(self, host, port):
            pass

        def communicate(self, timeout):
            return output

        def close(self):
            actions.append("closed")

    with pytest.raises(httpcore2.ConnectError, match="invalid_resolver_output"):
        ProcessResolver(child_factory=Child).resolve(
            "fixture.test", 443, AttemptIOBudget(FakeClock(), 5)
        )
    assert actions == ["closed"]


def test_real_connector_uses_localhost_process_and_returns_owned_stream():
    from agent_alfred.bounded_connect import BoundedConnector

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        clock = SystemClock()
        stream = BoundedConnector().connect_tcp(
            "localhost",
            listener.getsockname()[1],
            local_address="127.0.0.1",
            budget=AttemptIOBudget(clock, clock.monotonic() + 5),
        )
        try:
            sock = stream.get_extra_info("socket")
            assert sock.getpeername() == listener.getsockname()
        finally:
            stream.close()
        assert sock.fileno() == -1
