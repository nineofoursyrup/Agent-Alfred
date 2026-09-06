"""Dashboard resource ownership across incomplete closes."""

from __future__ import annotations

from agent_alfred.database import open_database as file_database
from agent_alfred.evals.deterministic._web_close_test_helpers import (
    DashboardCloseRig,
)
from agent_alfred.gateway.web.lifecycle import (
    read_entry_descriptor,
)


def test_file_database_does_not_depend_on_wiring_reexport(
    monkeypatch, tmp_path
) -> None:
    from agent_alfred import wiring
    from agent_alfred.evals.deterministic._web_startup_test_helpers import (
        record_managed_state_acquires,
    )
    from agent_alfred.managed_state import ManagedStateDirectory

    def wrong_owner(_state):
        raise AssertionError("startup helper borrowed the wiring re-export")

    monkeypatch.setattr(wiring, "open_database", wrong_owner)
    acquired_paths = record_managed_state_acquires(monkeypatch)
    state = ManagedStateDirectory.acquire(tmp_path)
    try:
        conn = file_database(state)
        conn.execute("SELECT 1").fetchone()
        assert acquired_paths == [tmp_path]
        conn.close()
    finally:
        state.close()


def test_a_host_that_will_not_stop_keeps_its_database_and_its_lock(tmp_path) -> None:
    """A refused Host close is not a licence to close everything.

    ``host.close()`` answering False means a worker is still inside its Run.
    That worker's database, its descriptor and its lock are the three things
    the shutdown exists to protect until the Run is done: releasing any of
    them hands a second instance write authority over state that is still
    being written.
    """
    rig = DashboardCloseRig(tmp_path, host_results=(False, True))
    try:
        rig.runtime.start()
        # Reach the Host refusal, not merely an unfinished serving predecessor.
        assert rig.runtime.close(timeout=1.0) is False

        # The database is still open, and still usable by the worker holding the
        # Host up.
        assert rig.conn.closed is False
        assert rig.conn.close_calls == 0
        assert rig.blocked_worker_uses_the_database() > 0
        # The descriptor still names a port, and the lock is still ours.
        assert read_entry_descriptor(tmp_path) is not None
        assert rig.lock_is_held() is True
        # Not one step of the tail has run: the Broker was never asked to tear
        # down a stream whose Host is still emitting into it.
        assert rig.broker is not None and rig.broker.close_calls == 0
        assert rig.host is not None and rig.host.close_calls == 1
    finally:
        if not rig.runtime.close(timeout=2.0):
            assert rig.runtime.close(timeout=2.0) is True
    assert rig.lock.acquired is False, "the test retained its process lock"


def test_a_second_close_continues_where_the_first_one_stopped(tmp_path) -> None:
    """The unfinished steps are resumed, not restarted or skipped.

    The second call picks up at the step that refused -- it asks the Host
    again, because asking is the only way to learn that it has stopped -- and
    then runs the Broker, the database, the descriptor and the lock. What it
    must never do is run a step that already succeeded a second time.
    """
    rig = DashboardCloseRig(tmp_path, host_results=(False, True))
    rig.runtime.start()
    try:
        assert rig.runtime.close(timeout=1.0) is False
        assert rig.host is not None and rig.host.close_calls == 1
        assert rig.runtime.close(timeout=0.0) is True

        # The Host was asked until it confirmed; nothing else was asked twice.
        assert rig.host is not None and rig.host.close_calls == 2
        assert rig.broker is not None and rig.broker.close_calls == 1
        # Every resource, closed exactly once.
        assert rig.conn.close_calls == 1
        assert rig.conn.closed is True
        assert read_entry_descriptor(tmp_path) is None
        assert rig.lock.acquired is False
        assert rig.lock_is_held() is False
        # Reverse order, with nothing interleaved from the start-up.
        assert rig.trace == [
            "bind",
            "write_descriptor",
            "open_db",
            "assemble",
            "host_close",
            "host_close",
            "broker_close",
            "conn_close",
            "lock_release",
        ]
        # A third close, after everything has stopped, asks nobody.
        assert rig.runtime.close(timeout=0.0) is True
        assert rig.host.close_calls == 2
        assert rig.broker.close_calls == 1
    finally:
        if not rig.runtime.close(timeout=2.0):
            assert rig.runtime.close(timeout=2.0) is True


def test_a_broker_that_will_not_drain_holds_the_database_and_the_lock(tmp_path) -> None:
    """ "Host and Broker both stopped" is one condition, not two tries.

    A stream that has not drained still holds frames a client is owed. The
    database those frames were built from cannot go away underneath it.
    """
    rig = DashboardCloseRig(tmp_path, broker_results=(False, True))
    rig.runtime.start()
    try:
        assert rig.runtime.close(timeout=1.0) is False
        assert rig.broker is not None and rig.broker.close_calls == 1

        assert rig.conn.closed is False
        assert read_entry_descriptor(tmp_path) is not None
        assert rig.lock_is_held() is True

        assert rig.runtime.close(timeout=0.0) is True
        assert rig.broker.close_calls == 2
        assert rig.conn.close_calls == 1
        assert rig.lock.acquired is False
    finally:
        if not rig.runtime.close(timeout=2.0):
            assert rig.runtime.close(timeout=2.0) is True


def test_the_database_is_closed_exactly_once_across_every_path(tmp_path) -> None:
    """An idempotent close is not "close it every time you are asked"."""
    rig = DashboardCloseRig(tmp_path)
    rig.runtime.start()
    assert rig.runtime.close() is True
    assert rig.runtime.close() is True
    assert rig.runtime.close() is True
    assert rig.conn.close_calls == 1
    assert rig.host is not None and rig.host.close_calls == 1
    assert rig.broker is not None and rig.broker.close_calls == 1
