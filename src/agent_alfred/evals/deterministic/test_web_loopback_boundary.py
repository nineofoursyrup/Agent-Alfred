"""Dashboard loopback binding boundary."""

from __future__ import annotations

import inspect

import pytest

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.gateway.web.lifecycle import (
    DEFAULT_HOST,
    DashboardService,
)
from agent_alfred.wiring import build_dashboard

# --- loopback boundary ----------------------------------------------------
#
# ADR-0014 puts "only listen on the loopback address" first in a list of four
# independent defences. It is the only one that shrinks the attack surface,
# and it is worthless if a caller can name another address.


def _service(tmp_path, *, port=17717, server_factory=None, **kwargs):
    return DashboardService(
        state_dir=tmp_path,
        handler=object(),
        instance_id="inst",
        port=port,
        server_factory=server_factory,
        **kwargs,
    )


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "localhost", "127.0.0.2", "192.168.1.10", "::1", "[::1]", ""],
)
def test_no_non_loopback_address_can_be_expressed_at_all(tmp_path, address) -> None:
    """The parameter is gone, not defaulted.

    A ``host`` knob that rejected everything but one value would still be a
    knob: it would invite the next change to widen it, and a caller reading
    the signature would reasonably assume the address is theirs to choose.
    There is no such parameter, so none of these values is reachable --
    including the empty one, which a socket would happily read as "all
    interfaces".
    """
    with pytest.raises(TypeError):
        _service(tmp_path, host=address)
    with pytest.raises(TypeError):
        _service(tmp_path, bind_host=address)
    with pytest.raises(TypeError):
        _service(tmp_path, address=address)


def test_the_decided_loopback_address_is_the_one_that_is_bound(tmp_path) -> None:
    service = _service(tmp_path, port=free_loopback_port())
    assert service.bind_address == DEFAULT_HOST == "127.0.0.1"
    try:
        service.start()
        assert service.server.server_address[0] == "127.0.0.1"
    finally:
        service.close()


def test_an_injected_factory_is_given_the_loopback_and_nothing_else(tmp_path) -> None:
    """The test seam is not a way around the boundary.

    The factory is the seam every test uses to avoid a real socket, which
    makes it the one place a non-loopback address could have been smuggled
    in. It is handed the address rather than asked for one, so it cannot.
    """
    seen: list[tuple] = []

    def factory(address, handler):
        seen.append(address)
        return _FakeServer()

    service = _service(tmp_path, port=17717, server_factory=factory)
    try:
        service.start()
    finally:
        service.close()
    assert seen == [("127.0.0.1", 17717)]


def test_the_assembly_seams_cannot_name_an_address() -> None:
    for target in (build_dashboard, DashboardService.__init__):
        names = set(inspect.signature(target).parameters)
        assert not names & {"bind_host", "host", "bind_address", "address"}


def test_no_cli_flag_can_name_an_address() -> None:
    """Only the port is overridable from the command line (ADR-0014)."""
    from agent_alfred.gateway.cli import build_parser

    options = [
        option
        for action in build_parser()._actions
        for option in action.option_strings
    ]
    assert "--port" in options
    assert not [o for o in options if "host" in o or "bind" in o or "addr" in o]


class _FakeServer:
    """The least a server has to be for the lifecycle to own it."""

    daemon_threads = True
    block_on_close = False

    def __init__(self) -> None:
        self.server_address = ("127.0.0.1", 17717)

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        return None

