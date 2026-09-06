"""Shared loopback socket helpers for deterministic web tests."""

from __future__ import annotations

import socket

from agent_alfred.gateway.web.lifecycle import DEFAULT_HOST


def free_loopback_port() -> int:
    """Return a currently free loopback port, releasing the probe immediately."""
    probe = socket.socket()
    try:
        probe.bind((DEFAULT_HOST, 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()
