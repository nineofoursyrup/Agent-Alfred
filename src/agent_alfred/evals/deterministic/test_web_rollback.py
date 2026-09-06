"""Dashboard startup rollback completion contracts."""

from __future__ import annotations

import pytest

from agent_alfred.evals.deterministic._web_close_test_helpers import (
    DashboardCloseRig,
    SpawnThatRefuses,
    SpawnThatStartsBadly,
)
from agent_alfred.gateway.web.lifecycle import (
    read_entry_descriptor,
)
from agent_alfred.gateway.web.server import ROLLBACK_STEP_TIMEOUT_S

# --- the serving thread is inside the rollback ----------------------------


def test_a_serving_thread_that_cannot_be_created_rolls_everything_back(
    tmp_path,
) -> None:
    """Step 8 fails like any other lifecycle step and undoes steps 1-7."""
    rig = DashboardCloseRig(tmp_path, spawn=SpawnThatRefuses())
    with pytest.raises(RuntimeError, match="cannot create the serving thread"):
        rig.runtime.start()

    # Rolled back all the way out: no lock, no descriptor, no database, and
    # no Host left running behind a start that reported failure.
    assert rig.lock.acquired is False
    assert rig.lock_is_held() is False
    assert read_entry_descriptor(tmp_path) is None
    assert rig.conn.closed is True
    assert rig.conn.close_calls == 1
    assert rig.host is not None and rig.host.close_calls == 1
    assert rig.broker is not None and rig.broker.close_calls == 1


def test_a_serving_thread_that_cannot_start_rolls_everything_back(tmp_path) -> None:
    """Creation succeeding is not the same as the thread running."""
    rig = DashboardCloseRig(tmp_path, spawn=SpawnThatStartsBadly())
    with pytest.raises(RuntimeError, match="cannot start the serving thread"):
        rig.runtime.start()
    assert rig.lock.acquired is False
    assert read_entry_descriptor(tmp_path) is None
    assert rig.conn.close_calls == 1


def test_a_rollback_is_bounded(tmp_path) -> None:
    """A failed start has to come back and say so.

    The rollback holds the lifecycle lock while it waits, so an unbounded
    wait would turn "the Dashboard could not start" into a hang a caller
    cannot tell from a slow start. It is a number even when the caller says
    nothing; why that differs from ``close()``'s ``None`` is argued on
    :data:`ROLLBACK_STEP_TIMEOUT_S`.
    """
    rig = DashboardCloseRig(tmp_path, spawn=SpawnThatRefuses())
    with pytest.raises(RuntimeError):
        rig.runtime.start()
    assert rig.host is not None
    assert rig.host.saw_timeouts == [ROLLBACK_STEP_TIMEOUT_S]

    explicit = DashboardCloseRig(
        tmp_path, spawn=SpawnThatRefuses(), rollback_step_timeout=0.25
    )
    with pytest.raises(RuntimeError):
        explicit.runtime.start()
    assert explicit.host is not None
    assert explicit.host.saw_timeouts == [0.25]


def test_a_rollback_that_cannot_stop_the_host_keeps_the_lock(tmp_path) -> None:
    """A failed start never leaves a half-owned state directory.

    If the Host refuses to stop, the rollback has not finished, so the
    runtime stays in the closing state still holding everything -- rather
    than reporting failure while a worker uses resources it just released.
    """
    rig = DashboardCloseRig(
        tmp_path, host_results=(False, True), spawn=SpawnThatRefuses()
    )
    with pytest.raises(RuntimeError):
        rig.runtime.start()

    assert rig.lock_is_held() is True
    assert read_entry_descriptor(tmp_path) is not None
    assert rig.conn.closed is False
    assert rig.runtime.state == "closing"
    # Closing again finishes what the rollback could not.
    assert rig.runtime.close(timeout=0.0) is True
    assert rig.conn.close_calls == 1
    assert rig.lock.acquired is False
    assert rig.runtime.state == "closed"
