"""Issue #28 second review closure: the four findings the second pass found.

Every test here fails on ``7f843db`` and passes after the fix. They are one
file because they are one closure: the Dashboard's **ownership** of the
things it took (a Host that will not stop must keep its database, its
descriptor and its lock), its **rollback** boundary (a serving thread that
never comes up must undo the whole start), its **lifecycle** (one state
machine, one lock, start and close cannot interleave), and the **re-seed**
every stream owes a client before its first data frame.

Two rules shape the harnesses:

- No sleeps. Everything is ordered with barriers, events and latches, so a
  slow machine makes a test slower to fail, never less correct.
- Nothing is asserted about a bug's current behaviour. Each assertion says
  what has to be true; the fact that it is false today is the red.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_alfred.evals.deterministic.test_web_broker import (
    Harness,
    _cursor_for,
    _drain,
)
from agent_alfred.events import RunStarted
from agent_alfred.gateway.web import frames, replay
from agent_alfred.gateway.web.frames import PreparedFrames
from agent_alfred.gateway.web.lifecycle import (
    LOCK_NAME,
    ProcessLock,
    StateDirLocked,
    read_entry_descriptor,
    write_entry_descriptor,
)
from agent_alfred.gateway.web.replay import ReplayRing, classify_cursor
from agent_alfred.gateway.web.server import ROLLBACK_STEP_TIMEOUT_S, DashboardRuntime

# A RunStarted frame is ~467 bytes, so 512 admits one and refuses a padded
# one -- the two events a test needs in order to reach the clear path with
# a checkpoint already issued in front of it.
_TIGHT_BYTES = 512


def _entry(seq: int, size: int = 8, chunks: int = 1) -> PreparedFrames:
    head = b"data: " + b"x" * size
    return frames.measured_frames(
        seq=seq,
        frames=tuple(head for _ in range(chunks)),
        id_line=b"id: inst:%d\n" % seq,
        replayable=True,
    )


# --- one: ownership when the Host will not stop ----------------------------


class _FakeConnection:
    """A database that behaves like a closed one once it is closed."""

    def __init__(self, trace: list[str]):
        self._trace = trace
        self.close_calls = 0
        self.closed = False
        self.uses = 0

    def execute(self, sql: str = "SELECT 1") -> "_FakeConnection":
        if self.closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        self.uses += 1
        return self

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self._trace.append("conn_close")


class _FakeHost:
    """The Host as the Dashboard sees it: one ``close`` that may refuse."""

    def __init__(self, conn: _FakeConnection, results: list[bool], trace: list[str]):
        self._conn = conn
        self._results = list(results)
        self._trace = trace
        self.close_calls = 0
        self.start_calls = 0
        self.worker_uses = 0
        self.saw_timeouts: list[float | None] = []

    def start(self) -> None:
        self.start_calls += 1

    def close(self, timeout: float | None = None) -> bool:
        self.close_calls += 1
        self.saw_timeouts.append(timeout)
        self._trace.append("host_close")
        # A worker is still inside its Run. It reaches for the database
        # *while* the shutdown is being negotiated, which is exactly the
        # moment a runtime that closed the database first would have taken
        # it away.
        self._conn.execute("SELECT 1 FROM runs")
        self.worker_uses += 1
        return self._results.pop(0) if self._results else True


class _FakeBroker:
    def __init__(
        self,
        conn: _FakeConnection,
        results: list[bool],
        trace: list[str],
    ):
        self._conn = conn
        self._results = list(results)
        self._trace = trace
        self.close_calls = 0
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def close(self, timeout: float | None = None) -> bool:
        self.close_calls += 1
        self._trace.append("broker_close")
        return self._results.pop(0) if self._results else True


class _RecordingServer:
    """Stands in for the bound socket. Never listens."""

    def __init__(self, address, handler):
        self.server_address = address
        self.handler = handler
        self.shut_down = False
        self.closed = False

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        self.shut_down = True

    def server_close(self) -> None:
        self.closed = True


class _RecordingLock(ProcessLock):
    """A real flock that records when it is let go."""

    def __init__(self, path: Path, trace: list[str]):
        super().__init__(path)
        self._trace = trace

    def release(self) -> None:
        if self._fd is not None:
            self._trace.append("lock_release")
        super().release()


class _Rig:
    """A DashboardRuntime whose every seam is a counter or a latch."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        host_results: tuple[bool, ...] = (True,),
        broker_results: tuple[bool, ...] = (True,),
        spawn: Any = None,
        rollback_step_timeout: float | None = ROLLBACK_STEP_TIMEOUT_S,
    ):
        self.tmp_path = Path(tmp_path)
        self.trace: list[str] = []
        self.host_results = list(host_results)
        self.broker_results = list(broker_results)
        self.binds = 0
        self.database_opens = 0
        self.assemblies = 0
        self.states_seen: list[str] = []
        self.on_assemble: Any = None
        self.host: _FakeHost | None = None
        self.broker: _FakeBroker | None = None
        self.conn = self._make_conn()
        self.lock = self._make_lock()
        self.runtime = DashboardRuntime(
            state_dir=self.tmp_path,
            assemble=self._assemble,
            port=7717,
            instance_id="inst-closure-2",
            open_database=self._open_database,
            server_factory=self._server_factory,
            write_descriptor=self._write_descriptor,
            lock=self.lock,
            spawn=spawn,
            rollback_step_timeout=rollback_step_timeout,
        )

    # The two seams a later closure needs to make refusable: the database
    # and the lock are built here so a subclass can stand in its own.
    def _make_conn(self) -> _FakeConnection:
        return _FakeConnection(self.trace)

    def _make_lock(self) -> ProcessLock:
        return _RecordingLock(self.tmp_path / LOCK_NAME, self.trace)

    def _server_factory(self, address, handler):
        self.binds += 1
        self.trace.append("bind")
        return _RecordingServer(address, handler)

    def _open_database(self, directory):
        self.database_opens += 1
        self.trace.append("open_db")
        return self.conn

    def _write_descriptor(self, directory, descriptor):
        self.trace.append("write_descriptor")
        return write_entry_descriptor(directory, descriptor)

    def _assemble(self, conn, instance_id):
        self.assemblies += 1
        self.trace.append("assemble")
        self.states_seen.append(self.runtime.state)
        if self.on_assemble is not None:
            self.on_assemble(self)
        self.host = _FakeHost(conn, self.host_results, self.trace)
        self.broker = _FakeBroker(conn, self.broker_results, self.trace)
        return self.host, self.broker

    # -- what a stuck Host's worker still does -----------------------------

    def blocked_worker_uses_the_database(self) -> int:
        """What a worker still finishing its Run does with the connection."""
        self.conn.execute("INSERT INTO runs VALUES (1)")
        return self.conn.uses

    def lock_is_held(self) -> bool:
        """Whether some *other* holder would be refused the state directory."""
        if not self.lock.acquired:
            return False
        probe = ProcessLock(self.tmp_path / LOCK_NAME)
        try:
            probe.acquire()
        except StateDirLocked:
            return True
        probe.release()
        return False


def test_a_host_that_will_not_stop_keeps_its_database_and_its_lock(tmp_path) -> None:
    """One: a refused Host close is not a licence to close everything.

    ``host.close()`` answering False means a worker is still inside its Run.
    That worker's database, its descriptor and its lock are the three things
    the shutdown exists to protect until the Run is done: releasing any of
    them hands a second instance write authority over state that is still
    being written.
    """
    rig = _Rig(tmp_path, host_results=(False, True))
    rig.runtime.start()
    assert rig.runtime.close(timeout=0.0) is False

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


def test_a_second_close_continues_where_the_first_one_stopped(tmp_path) -> None:
    """One: the unfinished steps are resumed, not restarted or skipped.

    The second call picks up at the step that refused -- it asks the Host
    again, because asking is the only way to learn that it has stopped -- and
    then runs the Broker, the database, the descriptor and the lock. What it
    must never do is run a step that already succeeded a second time.
    """
    rig = _Rig(tmp_path, host_results=(False, True))
    rig.runtime.start()
    assert rig.runtime.close(timeout=0.0) is False
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


def test_a_broker_that_will_not_drain_holds_the_database_and_the_lock(tmp_path) -> None:
    """One: "Host and Broker both stopped" is one condition, not two tries.

    A stream that has not drained still holds frames a client is owed. The
    database those frames were built from cannot go away underneath it.
    """
    rig = _Rig(tmp_path, broker_results=(False, True))
    rig.runtime.start()
    assert rig.runtime.close(timeout=0.0) is False

    assert rig.conn.closed is False
    assert read_entry_descriptor(tmp_path) is not None
    assert rig.lock_is_held() is True

    assert rig.runtime.close(timeout=0.0) is True
    assert rig.broker is not None and rig.broker.close_calls == 2
    assert rig.conn.close_calls == 1
    assert rig.lock.acquired is False


def test_the_database_is_closed_exactly_once_across_every_path(tmp_path) -> None:
    """One: an idempotent close is not "close it every time you are asked"."""
    rig = _Rig(tmp_path)
    rig.runtime.start()
    assert rig.runtime.close() is True
    assert rig.runtime.close() is True
    assert rig.runtime.close() is True
    assert rig.conn.close_calls == 1
    assert rig.host is not None and rig.host.close_calls == 1
    assert rig.broker is not None and rig.broker.close_calls == 1


# --- two: the serving thread is inside the rollback ------------------------


class _SpawnThatRefuses:
    """A thread that cannot be created. Nothing is started."""

    def __init__(self):
        self.calls = 0

    def __call__(self, target):
        self.calls += 1
        raise RuntimeError("cannot create the serving thread")


class _ThreadThatWillNotStart:
    def start(self):
        raise RuntimeError("cannot start the serving thread")


class _SpawnThatStartsBadly:
    def __init__(self):
        self.calls = 0

    def __call__(self, target):
        self.calls += 1
        return _ThreadThatWillNotStart()


def test_a_serving_thread_that_cannot_be_created_rolls_everything_back(
    tmp_path,
) -> None:
    """Two: step 8 is a step. It fails like any other, and undoes 1-7."""
    rig = _Rig(tmp_path, spawn=_SpawnThatRefuses())
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
    """Two: creation succeeding is not the same as the thread running."""
    rig = _Rig(tmp_path, spawn=_SpawnThatStartsBadly())
    with pytest.raises(RuntimeError, match="cannot start the serving thread"):
        rig.runtime.start()
    assert rig.lock.acquired is False
    assert read_entry_descriptor(tmp_path) is None
    assert rig.conn.close_calls == 1


def test_a_rollback_is_bounded(tmp_path) -> None:
    """Two: a failed start has to come back and say so.

    The rollback holds the lifecycle lock while it waits, so an unbounded
    wait would turn "the Dashboard could not start" into a hang a caller
    cannot tell from a slow start. It is a number even when the caller says
    nothing; why that differs from ``close()``'s ``None`` is argued on
    :data:`ROLLBACK_STEP_TIMEOUT_S`.
    """
    rig = _Rig(tmp_path, spawn=_SpawnThatRefuses())
    with pytest.raises(RuntimeError):
        rig.runtime.start()
    assert rig.host is not None
    assert rig.host.saw_timeouts == [ROLLBACK_STEP_TIMEOUT_S]

    explicit = _Rig(tmp_path, spawn=_SpawnThatRefuses(), rollback_step_timeout=0.25)
    with pytest.raises(RuntimeError):
        explicit.runtime.start()
    assert explicit.host is not None
    assert explicit.host.saw_timeouts == [0.25]


def test_a_rollback_that_cannot_stop_the_host_keeps_the_lock(tmp_path) -> None:
    """Two: a failed start never leaves a half-owned state directory.

    If the Host refuses to stop, the rollback has not finished, so the
    runtime stays in the closing state still holding everything -- rather
    than reporting failure while a worker uses resources it just released.
    """
    rig = _Rig(tmp_path, host_results=(False, True), spawn=_SpawnThatRefuses())
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


# --- three: one state machine, one lock ------------------------------------


def test_the_lifecycle_is_observable_at_every_step(tmp_path) -> None:
    """Three: new -> starting -> running -> closing -> closed."""
    rig = _Rig(tmp_path)
    assert rig.runtime.state == "new"
    rig.runtime.start()
    # Published before the Host was built: ``assemble`` is where it was read.
    assert rig.states_seen == ["starting"]
    assert rig.runtime.state == "running"
    assert rig.runtime.close() is True
    assert rig.runtime.state == "closed"


def test_a_failed_start_is_failed_and_a_stuck_close_is_closing(tmp_path) -> None:
    """Three: the two ways out of ``starting`` are different states."""
    failed = _Rig(tmp_path, spawn=_SpawnThatRefuses())
    with pytest.raises(RuntimeError):
        failed.runtime.start()
    assert failed.runtime.state == "failed"

    stuck = _Rig(tmp_path, host_results=(False, True))
    stuck.runtime.start()
    assert stuck.runtime.close(timeout=0.0) is False
    assert stuck.runtime.state == "closing"
    assert stuck.runtime.close(timeout=0.0) is True
    assert stuck.runtime.state == "closed"


def test_two_concurrent_starts_start_exactly_one_dashboard(tmp_path) -> None:
    """Three: ``start()`` is idempotent under concurrency, not just in sequence.

    Both threads are released from the barrier at the same instant, so they
    enter ``start()`` together. Only one of them may bind, open the database
    and assemble a Host; the other waits for the descriptor the first one
    published.
    """
    rig = _Rig(tmp_path)
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
    """Three: start and close are one critical section.

    The starter parks inside ``assemble`` holding the lifecycle lock, so the
    closer is provably blocked behind it. What the closer must never do is
    close the database the start is still opening, release a lock the start
    still needs, or bind a second time afterwards.
    """
    rig = _Rig(tmp_path)
    rendezvous = threading.Barrier(2)
    met = threading.Event()
    proceed = threading.Event()
    start_outcome: list[Any] = []
    start_error: list[BaseException] = []
    close_outcome: list[Any] = []

    def park(_rig: _Rig) -> None:
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
    """Three: close is terminal. A second start is refused, not retried."""
    rig = _Rig(tmp_path)
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


# --- four: the re-seed every stream owes -----------------------------------


def _wire_ids(items) -> list[int]:
    """Every ``id:`` line a browser would see, in order.

    The re-seed carries its id *in the frame body*, not in ``id_line``, so
    reading the wire is the only way to see what the client's ``lastEventId``
    becomes.
    """
    out: list[int] = []
    for item in items:
        if not isinstance(item, PreparedFrames):
            continue
        for line in item.wire_bytes().split(b"\n"):
            if line.startswith(b"id: "):
                out.append(int(line.split(b":")[-1]))
    return out


def _browser_cursor(items) -> str | None:
    """The ``Last-Event-ID`` the browser would send on the next connection.

    An SSE client sends the header only when its id buffer is non-empty, so
    "no id was ever written" and "the header is absent" are the same fact --
    and both make the next connection look like a first one.
    """
    ids = _wire_ids(items)
    return _cursor_for(ids[-1]) if ids else None


def test_the_reserved_startup_checkpoint_is_a_real_cursor() -> None:
    """Four: ``instance:0`` names the boundary before the first event.

    It is not an event position, so it consumes no seq; it is a transport
    boundary this process owns, valid as a starting point on a ring that has
    never lost anything.
    """
    assert replay.STARTUP_CHECKPOINT_SEQ == 0
    assert replay.format_cursor("inst", 0) == "inst:0"
    assert replay.parse_cursor("inst:0", "inst") == (0, None)
    # It is process-specific like every other cursor.
    assert replay.parse_cursor("inst:0", "other")[1] == "instance_mismatch"
    # And it is the only zero: a padded or negative position is still junk.
    assert replay.parse_cursor("inst:00", "inst")[1] == "malformed"
    assert replay.parse_cursor("inst:-1", "inst")[1] == "malformed"


def test_a_clean_empty_ring_accepts_the_startup_checkpoint() -> None:
    ring = ReplayRing()
    assert ring.classify_seq(0) == "valid"
    assert ring.entries_after(0) == ()
    # The ring has issued nothing, so there is no *event* checkpoint yet --
    # the reserved boundary is not one.
    assert ring.latest_complete_seq() is None
    assert ring.reseed_boundary_seq() is None


def test_the_first_frame_of_an_empty_ring_is_the_reseed() -> None:
    """Four: before any data, always -- even when there is nothing to replay.

    The dispatch algorithm copies the id buffer even for a dataless frame,
    so a stream that opens with a snapshot and no ``id:`` leaves the browser
    holding an empty buffer -- and an empty buffer means no ``Last-Event-ID``
    on the next connection, which is indistinguishable from a client that
    never connected.
    """
    harness = Harness()
    items = _drain(harness.connect())
    assert items[0].wire_bytes() == b"retry: 1000\n\n"
    assert items[1].wire_bytes() == b"id: inst-test:0\n\n"
    # The snapshot follows the re-seed, never precedes it.
    assert any(b"event: state_patch" in item.wire_bytes() for item in items[2:])
    assert _browser_cursor(items) == _cursor_for(0)


def test_the_startup_checkpoint_gives_way_to_a_real_one() -> None:
    """Four: the reserved boundary is a start, not a permanent answer."""
    harness = Harness()
    assert _wire_ids(_drain(harness.connect())) == [0]

    harness.emit_many(3)
    ring = harness.broker._ring  # noqa: SLF001
    # Real checkpoints advanced; the reserved one is still valid underneath
    # them because nothing has been lost.
    assert ring.latest_complete_seq() == 3
    assert ring.reseed_boundary_seq() == 3
    assert ring.classify_seq(0) == "valid"

    again = _drain(harness.connect(cursor=_cursor_for(0)))
    assert _wire_ids(again) == [0, 1, 2, 3]
    # And a real checkpoint resumes exactly, as before.
    assert _wire_ids(_drain(harness.connect(cursor=_cursor_for(1)))) == [1, 2, 3]


def test_a_first_event_over_budget_still_plants_a_cursor() -> None:
    """Four: an unrecoverable loss is reported, never swallowed.

    The very first event does not fit, so the ring is cleared and the floor
    moves past it. There is no issued checkpoint to re-plant -- but planting
    nothing would erase the client's cursor, and a client with no cursor
    cannot be told it has a gap. The reserved boundary is planted instead,
    and it classifies as ``too_old`` for exactly the reason the client needs
    to hear.
    """
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=8))
    harness.emit_many(1)
    ring = harness.broker._ring  # noqa: SLF001
    assert ring.replay_floor_seq() == 1
    assert ring.latest_complete_seq() is None

    items = _drain(harness.connect())
    assert _wire_ids(items) == [0]
    assert ring.classify_seq(0) == "too_old"

    # The browser's cursor is not empty, so it comes back and is told again.
    cursor = _browser_cursor(items)
    assert cursor == _cursor_for(0)
    again = _drain(harness.connect(cursor=cursor))
    notice = next(item for item in again if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"too_old"' in notice.wire_bytes()
    # And it is re-seeded again, so the gap survives as many reconnects as
    # it takes rather than vanishing on the second one.
    assert _browser_cursor(again) == _cursor_for(0)


def test_a_cleared_ring_keeps_reporting_the_gap() -> None:
    """Four: a last issued checkpoint is a re-seed boundary, not a debt.

    After an unrecoverable clear the ring cannot reproduce the checkpoint it
    issued. Keeping it as the re-seed is deliberate: the alternative is an
    empty browser cursor, and an empty cursor is a client that looks like it
    never connected -- which is the one shape that never gets a gap notice.
    """
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=_TIGHT_BYTES))
    harness.emit_many(1)  # seq 1, small enough to be issued
    # seq 2, padded past the byte budget: the ring is cleared.
    harness.emit(RunStarted(purpose="chat"), run_id="r" + "x" * 400)
    ring = harness.broker._ring  # noqa: SLF001
    assert ring.high_water_seq() == 2
    assert ring.replay_floor_seq() == 2
    assert ring.latest_complete_seq() is None
    assert ring.reseed_boundary_seq() == 1

    items = _drain(harness.connect())
    assert _wire_ids(items) == [1]
    cursor = _browser_cursor(items)
    assert cursor == _cursor_for(1)

    again = _drain(harness.connect(cursor=cursor))
    notice = next(item for item in again if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"too_old"' in notice.wire_bytes()
    assert b'"requested_seq":1' in notice.wire_bytes()
    # Re-planted, so the next reconnect reports the gap again.
    assert _browser_cursor(again) == _cursor_for(1)


def test_a_reconnect_after_an_empty_ring_is_not_a_first_connection() -> None:
    """Four: the cursor a client keeps is what lets the next one be classified.

    A stream that opened on an empty ring has nothing to replay, which is
    exactly why it owes a re-seed: without one the browser's id buffer ends
    up empty, it sends no ``Last-Event-ID``, and the server reads a second
    connection from the same client as a first connection from a new one.
    """
    harness = Harness()
    first = _drain(harness.connect())
    cursor = _browser_cursor(first)
    assert cursor == _cursor_for(0)

    again = _drain(harness.connect(cursor=cursor))
    # Not a gap: the client is resuming, and it resumed from a boundary the
    # server recognises as its own.
    assert not any(b"replay_gap" in item.wire_bytes() for item in again)
    assert _wire_ids(again) == [0]


def test_a_gap_that_survives_a_disconnect_does_not_go_quiet() -> None:
    """Four: a client that drops out mid-notice is still owed the notice.

    The notice and the snapshot carry no id, so the browser's cursor after
    reading them is whatever the re-seed planted. What matters is that it is
    a value the server can classify -- never an empty buffer that turns the
    next connection into a first one.
    """
    harness = Harness()
    harness.emit_many(3)
    interrupted = _drain(harness.connect(cursor="garbage"))
    assert any(b"replay_gap" in item.wire_bytes() for item in interrupted)
    cursor = _browser_cursor(interrupted)
    assert cursor == _cursor_for(3)
    # Reconnecting with it resumes exactly: the gap was about a cursor that
    # could not be read, not about lost facts.
    resumed = _drain(harness.connect(cursor=cursor))
    assert not any(b"replay_gap" in item.wire_bytes() for item in resumed)
    assert _wire_ids(resumed) == [3]

    # A cursor the ring cannot honour, by contrast, reports a gap every time
    # it comes back -- and still leaves the client holding a real boundary.
    lost = _drain(harness.connect(cursor=_cursor_for(9)))
    notice = next(item for item in lost if b"replay_gap" in item.wire_bytes())
    assert b'"gap_reason":"ahead"' in notice.wire_bytes()
    assert _browser_cursor(lost) == _cursor_for(3)


def test_the_replay_order_is_retry_reseed_gap_patch_then_events() -> None:
    """Four: the decided order, on the wire, in one place."""
    harness = Harness(ring=ReplayRing(max_frames=100, max_bytes=_TIGHT_BYTES))
    harness.emit_many(1)
    harness.emit(RunStarted(purpose="chat"), run_id="r" + "x" * 400)
    items = _drain(harness.connect(cursor=_cursor_for(1)))
    kinds = []
    for item in items:
        wire = item.wire_bytes()
        if wire.startswith(b"retry: "):
            kinds.append("retry")
        elif wire.startswith(b"id: "):
            kinds.append("reseed")
        elif b"replay_gap" in wire:
            kinds.append("gap")
        elif b"event: state_patch" in wire:
            kinds.append("patch")
        elif b"event: domain_event" in wire:
            kinds.append("event")
    assert kinds == ["retry", "reseed", "gap", "patch"]


def test_a_forged_positive_seq_is_still_refused() -> None:
    """Four: the reserved boundary is 0, and only 0.

    A plain positive integer the server never issued is not a checkpoint and
    cannot be faked into one -- not even one sitting inside the ring's range.
    """
    ring = ReplayRing(max_frames=100, max_bytes=1 << 10)
    ring.append(_entry(1))
    ring.append(_entry(2, size=(1 << 10) + 1))  # unrecoverable
    ring.append(_entry(3))
    assert ring.classify_seq(2) == "malformed"
    forged = classify_cursor(
        replay.format_cursor("inst", 2), ring, process_instance_id="inst"
    )
    assert forged.reason == "malformed"
    # And the reserved boundary is not a checkpoint for events.
    with pytest.raises(ValueError):
        frames.reseed_frame("inst", -1)
    with pytest.raises(ValueError):
        _entry(1).with_checkpoint(0, "inst")
