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


class _ObservableLifecycleLock:
    """Observe a real lifecycle lock immediately before a blocked acquire."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._state_lock = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self.contended = threading.Event()

    def __enter__(self):
        current = threading.get_ident()
        with self._state_lock:
            if self._owner not in (None, current):
                self.contended.set()
        self._delegate.acquire()
        with self._state_lock:
            self._owner = current
            self._depth += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        current = threading.get_ident()
        with self._state_lock:
            if self._owner != current:
                raise RuntimeError("lifecycle lock released by a non-owner")
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
            self._delegate.release()


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
    try:
        # Settle the serving predecessors and reach the Host's first refusal.
        assert stuck.runtime.close(timeout=1.0) is False
        assert stuck.host is not None
        assert stuck.host.close_calls == 1
        assert stuck.runtime.state == "closing"
        assert stuck.runtime.close(timeout=0.0) is True
        assert stuck.runtime.state == "closed"
    finally:
        stuck.runtime.close()


def test_two_concurrent_starts_start_exactly_one_dashboard(tmp_path) -> None:
    """``start()`` is idempotent under concurrency, not just in sequence.

    The first start parks in assembly while the observable lock reports the
    second start really waiting behind it. Only one may bind, open the database
    and assemble a Host; the other receives the descriptor it publishes.
    """
    rig = DashboardCloseRig(tmp_path)
    lifecycle_lock = _ObservableLifecycleLock(
        rig.runtime._lifecycle_lock  # noqa: SLF001
    )
    rig.runtime._lifecycle_lock = lifecycle_lock  # noqa: SLF001
    first_inside = threading.Event()
    release_first = threading.Event()
    first_done = threading.Event()
    second_done = threading.Event()
    outcomes: list[Any] = []
    errors: list[BaseException] = []

    def park_first(_rig: DashboardCloseRig) -> None:
        first_inside.set()
        release_first.wait()

    rig.on_assemble = park_first

    def start_one(done: threading.Event) -> None:
        try:
            outcomes.append(rig.runtime.start())
        except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
            errors.append(exc)
        finally:
            done.set()

    first = threading.Thread(target=start_one, args=(first_done,), daemon=True)
    second = threading.Thread(target=start_one, args=(second_done,), daemon=True)
    first.start()
    try:
        assert first_inside.wait(10.0), "first start never reached assembly"
        second.start()
        assert lifecycle_lock.contended.wait(10.0), (
            "second start never contended for the lifecycle lock"
        )
        assert second_done.is_set() is False
    finally:
        release_first.set()
    for thread in (first, second):
        thread.join(timeout=10.0)
        assert not thread.is_alive()
    assert first_done.is_set()
    assert second_done.is_set()

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
    lifecycle_lock = _ObservableLifecycleLock(
        rig.runtime._lifecycle_lock  # noqa: SLF001
    )
    rig.runtime._lifecycle_lock = lifecycle_lock  # noqa: SLF001
    start_inside = threading.Event()
    proceed = threading.Event()
    start_outcome: list[Any] = []
    start_error: list[BaseException] = []
    close_outcome: list[Any] = []

    def park(_rig: DashboardCloseRig) -> None:
        # Inside start(), holding the lifecycle lock. The test lock signals
        # only after close has actually attempted the contended acquisition.
        start_inside.set()
        proceed.wait()

    rig.on_assemble = park

    def starter() -> None:
        try:
            start_outcome.append(rig.runtime.start())
        except BaseException as exc:  # noqa: BLE001 - collected, not swallowed
            start_error.append(exc)

    def closer() -> None:
        close_outcome.append(rig.runtime.close())

    starter_thread = threading.Thread(target=starter, daemon=True)
    closer_thread = threading.Thread(target=closer, daemon=True)
    starter_thread.start()
    try:
        assert start_inside.wait(timeout=10.0), "start never reached assembly"
        closer_thread.start()
        assert lifecycle_lock.contended.wait(timeout=10.0), (
            "close never contended for the lifecycle lock"
        )
        assert close_outcome == []
    finally:
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
