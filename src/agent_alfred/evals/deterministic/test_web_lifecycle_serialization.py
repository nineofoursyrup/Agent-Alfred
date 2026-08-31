"""Dashboard lifecycle serialization contracts."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from agent_alfred.evals.deterministic._web_close_test_helpers import (
    DashboardCloseRig,
    SpawnThatRefuses,
)

# --- lifecycle state machine and lock -------------------------------------


def test_the_lifecycle_is_observable_at_every_step(tmp_path) -> None:
    """The lifecycle is new -> starting -> running -> closing -> closed."""
    rig = DashboardCloseRig(tmp_path)
    assert rig.runtime.state == "new"
    rig.runtime.start()
    # Published before the Host was built: ``assemble`` is where it was read.
    assert rig.states_seen == ["starting"]
    assert rig.runtime.state == "running"
    assert rig.runtime.close() is True
    assert rig.runtime.state == "closed"


def test_a_failed_start_is_failed_and_a_stuck_close_is_closing(tmp_path) -> None:
    """The two ways out of ``starting`` are different states."""
    failed = DashboardCloseRig(tmp_path, spawn=SpawnThatRefuses())
    with pytest.raises(RuntimeError):
        failed.runtime.start()
    assert failed.runtime.state == "failed"

    stuck = DashboardCloseRig(tmp_path, host_results=(False, True))
    stuck.runtime.start()
    assert stuck.runtime.close(timeout=0.0) is False
    assert stuck.runtime.state == "closing"
    assert stuck.runtime.close(timeout=0.0) is True
    assert stuck.runtime.state == "closed"


def test_two_concurrent_starts_start_exactly_one_dashboard(tmp_path) -> None:
    """``start()`` is idempotent under concurrency, not just in sequence.

    Both threads are released from the barrier at the same instant, so they
    enter ``start()`` together. Only one of them may bind, open the database
    and assemble a Host; the other waits for the descriptor the first one
    published.
    """
    rig = DashboardCloseRig(tmp_path)
    rendezvous = threading.Barrier(2)
    outcomes: list[Any] = []
    errors: list[BaseException] = []

    def start_one() -> None:
        rendezvous.wait()
        try:
            outcomes.append(rig.runtime.start())
        except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=start_one, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive()

    assert errors == []
    # One bind, one database, one Host -- and the same descriptor twice.
    assert rig.binds == 1
    assert rig.database_opens == 1
    assert rig.assemblies == 1
    assert len(outcomes) == 2
    assert outcomes[0] is outcomes[1]
    assert rig.runtime.state == "running"
    rig.runtime.close()


def test_a_close_cannot_run_inside_a_start(tmp_path) -> None:
    """Start and close are one critical section.

    The starter parks inside ``assemble`` holding the lifecycle lock, so the
    closer is provably blocked behind it. What the closer must never do is
    close the database the start is still opening, release a lock the start
    still needs, or bind a second time afterwards.
    """
    rig = DashboardCloseRig(tmp_path)
    rendezvous = threading.Barrier(2)
    met = threading.Event()
    proceed = threading.Event()
    start_outcome: list[Any] = []
    start_error: list[BaseException] = []
    close_outcome: list[Any] = []

    def park(_rig: DashboardCloseRig) -> None:
        # Inside start(), holding the lifecycle lock. Meet the closer here so
        # it is guaranteed to call close() while this thread still owns it.
        rendezvous.wait()
        met.set()
        proceed.wait(timeout=10.0)

    rig.on_assemble = park

    def starter() -> None:
        try:
            start_outcome.append(rig.runtime.start())
        except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
            start_error.append(exc)

    def closer() -> None:
        rendezvous.wait()
        close_outcome.append(rig.runtime.close())

    starter_thread = threading.Thread(target=starter, daemon=True)
    closer_thread = threading.Thread(target=closer, daemon=True)
    closer_thread.start()
    starter_thread.start()
    # Both parties have met: the closer is now calling close() against a lock
    # the starter holds.
    assert met.wait(timeout=10.0)
    proceed.set()
    starter_thread.join(timeout=10.0)
    closer_thread.join(timeout=10.0)
    assert not starter_thread.is_alive()
    assert not closer_thread.is_alive()

    assert start_error == []
    assert close_outcome == [True]
    # Nothing was done twice.
    assert rig.binds == 1
    assert rig.database_opens == 1
    assert rig.assemblies == 1
    # The whole start ran, then the whole close ran. No interleaving.
    assert rig.trace == [
        "bind",
        "write_descriptor",
        "open_db",
        "assemble",
        "host_close",
        "broker_close",
        "conn_close",
        "lock_release",
    ]
    assert rig.conn.close_calls == 1
    assert rig.lock.acquired is False
    assert rig.runtime.state == "closed"


def test_a_closed_runtime_is_never_resurrected(tmp_path) -> None:
    """Close is terminal. A second start is refused, not retried."""
    rig = DashboardCloseRig(tmp_path)
    rig.runtime.start()
    assert rig.runtime.close() is True
    with pytest.raises(RuntimeError):
        rig.runtime.start()
    assert rig.binds == 1
    assert rig.database_opens == 1
    assert rig.assemblies == 1
    assert rig.runtime.state == "closed"
    # And the refused start did not take the lock back.
    assert rig.lock.acquired is False
    assert rig.lock_is_held() is False
