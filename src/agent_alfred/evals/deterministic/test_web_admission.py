"""Dashboard mutation admission and lease contracts."""

from __future__ import annotations

import socket
import sqlite3
import threading

from agent_alfred import schema
from agent_alfred.evals.deterministic._web_runtime_test_helpers import (
    FailFinalizeWhen,
    SelectiveLatch,
    build_runtime_host,
    call_within,
    dashboard_api,
    wait_until,
)
from agent_alfred.gateway.web.api import DashboardApi, MutationGate
from agent_alfred.runtime.work import SubmitRequest

# --- the mutation gate covers the whole admission lease -------------------
#
# #23 §4: "locking chat alone and letting every other write through does not
# deliver serialisation." A lock held for the microseconds of one function
# call is not that gate: the lease a Run holds runs from ``accepted`` until
# its recording settles (ADR-0026), and every other write has to answer to
# the same authority for the same span.


def _session_rows(conn) -> list[str]:
    return [row[0] for row in conn.execute("SELECT session_id FROM sessions")]


def _start_run(host, *, message="hi"):
    session_id = host.create_session()
    result = host.submit(
        SubmitRequest(message=message, session_id=session_id, gateway="web")
    )
    assert result.kind == "accepted"
    return result, session_id


def test_a_session_write_while_a_run_is_running_is_refused_at_once() -> None:
    gate = threading.Event()
    host, conn = build_runtime_host(["pong"], gate=gate)
    host.start()
    try:
        result, _session_id = _start_run(host)
        wait_until(lambda: host.snapshot().coordinator_state == "running")
        before = _session_rows(conn)
        returned, created = call_within(dashboard_api(host).create_session, seconds=5.0)
        # Returned rather than waited: the Run is not going anywhere until
        # the gate opens, and it never does in this test.
        assert returned is True
        assert created.session_id is None
        assert created.code == "mutation_in_flight"
        assert created.status == 409
        # Nothing was written, and nothing was queued for later.
        assert _session_rows(conn) == before
        gate.set()
        host.wait(result.run_id)
    finally:
        host.close()


def test_a_session_write_while_the_recording_is_pending_is_refused() -> None:
    """``recording_pending`` still holds the lease (ADR-0026).

    The reply exists and the Run has been handed back; the one thing it is
    still holding -- the single unrecorded terminal projection -- is exactly
    what a second write would overwrite.
    """
    latch = SelectiveLatch()
    latch.arm()
    host, conn = build_runtime_host(["pong"], before_recording_commit=latch)
    host.start()
    try:
        result, _session_id = _start_run(host)
        wait_until(lambda: host.snapshot().coordinator_state == "recording_pending")
        before = _session_rows(conn)
        returned, created = call_within(dashboard_api(host).create_session, seconds=5.0)
        assert returned is True
        assert created.session_id is None
        assert created.code == "mutation_in_flight"
        assert created.status == 409
        assert _session_rows(conn) == before
        latch.release()
        host.wait(result.run_id)
    finally:
        host.close()


def test_a_session_write_is_accepted_once_the_lease_is_released() -> None:
    latch = SelectiveLatch()
    latch.arm()
    host, conn = build_runtime_host(["pong"], before_recording_commit=latch)
    host.start()
    try:
        result, _session_id = _start_run(host)
        wait_until(lambda: host.snapshot().coordinator_state == "recording_pending")
        latch.release()
        host.wait(result.run_id)
        wait_until(lambda: host.snapshot().coordinator_state == "idle")
        before = _session_rows(conn)
        created = dashboard_api(host).create_session()
        assert created.session_id is not None
        assert created.status == 201
        assert len(_session_rows(conn)) == len(before) + 1
    finally:
        host.close()


def test_a_session_write_is_refused_after_recording_failed() -> None:
    """Admission stays closed until recovery or restart (ADR-0026)."""
    flag = {"armed": False}
    database = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(database)
    wrapped = FailFinalizeWhen(database, flag, "finished_at")
    latch = SelectiveLatch()
    host, _conn = build_runtime_host(
        ["pong"], conn=wrapped, before_recording_commit=latch
    )
    host.start()
    try:
        session_id = host.create_session()
        api = dashboard_api(host)
        latch.arm()
        first = api.submit({"message": "a-q", "session_id": session_id})
        assert first.status == 202
        wait_until(lambda: host.snapshot().coordinator_state == "recording_pending")
        flag["armed"] = True
        latch.release()
        host.wait(first.run_id)
        wait_until(lambda: host.snapshot().coordinator_state == "recording_failed")
        # A Run is still refused as a Run would be, and a plain write is
        # refused too: the lease never came back.
        failed = api.submit({"message": "again", "session_id": session_id})
        assert failed.status == 503
        assert failed.code == "recording_unavailable"
        # And a plain write is refused with the *same* word, not with
        # "busy": admission is closed until the process restarts (ADR-0026),
        # so telling the client to try again shortly would be a lie.
        created = api.create_session()
        assert created.session_id is None
        assert created.code == "recording_unavailable"
        assert created.status == 409
    finally:
        host.close()


def test_two_mutations_arriving_together_admit_exactly_one() -> None:
    """The gate's judgement is the Host's, so it is one judgement.

    Two writes arriving at the same microsecond cannot both see "free":
    whatever decides is the same lock that decides admission, so the second
    one is told no instead of being let through behind the first.
    """
    host, conn = build_runtime_host(["pong"])
    host.start()
    try:
        entered = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        inner = host

        class SlowFacade:
            def __init__(self) -> None:
                self._inner = inner

            def create_session(self) -> str:
                calls.append(1)
                entered.set()
                assert release.wait(5.0), "the test never released the write"
                return self._inner.create_session()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        slow = SlowFacade()
        gate = MutationGate(slow)
        first: dict = {}

        def run_first() -> None:
            first["value"] = gate.create_session()

        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        assert entered.wait(5.0), "the first write never entered"
        # The first write is still inside the door when the second arrives.
        returned, second = call_within(gate.create_session, seconds=5.0)
        assert returned is True
        assert second == (None, "mutation_in_flight")
        release.set()
        thread.join(5.0)
        assert not thread.is_alive()
        assert len(calls) == 1
        # The first write got through and was given no refusal code.
        assert first["value"][0] is not None
        assert first["value"][1] is None
        # The lease is free again, so the refused write may now succeed.
        assert gate.create_session()[0] is not None
    finally:
        host.close()


def test_a_run_is_refused_while_a_plain_mutation_is_in_flight() -> None:
    """The same authority, the other direction: no queueing either way."""
    host, _conn = build_runtime_host(["pong"])
    host.start()
    try:
        entered = threading.Event()
        release = threading.Event()
        inner = host

        class SlowFacade:
            def __init__(self) -> None:
                self._inner = inner

            def create_session(self) -> str:
                entered.set()
                assert release.wait(5.0), "the test never released the write"
                return self._inner.create_session()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        api = DashboardApi(facade=SlowFacade())
        session_id = inner.create_session()
        thread = threading.Thread(target=api.create_session, daemon=True)
        thread.start()
        assert entered.wait(5.0), "the write never entered"
        outcome = api.submit({"message": "hi", "session_id": session_id})
        # A conflict, not a queue and not a 503: nothing is unavailable,
        # something is merely busy (ADR-0016).
        assert outcome.status == 409
        assert outcome.code == "mutation_in_flight"
        assert outcome.run_id is None
        release.set()
        thread.join(5.0)
    finally:
        host.close()


def test_the_three_run_contracts_survive_the_gate() -> None:
    """202 / 409 run_in_progress / 503 recording_unavailable, unchanged."""
    gate = threading.Event()
    host, _conn = build_runtime_host(["pong"], gate=gate)
    host.start()
    try:
        result, session_id = _start_run(host)
        assert result.kind == "accepted"
        api = dashboard_api(host)
        second = api.submit({"message": "second", "session_id": session_id})
        assert second.status == 409
        assert second.code == "run_in_progress"
        assert second.busy is not None
        # The same card the prevention path renders, from the same snapshot.
        assert second.busy.purpose == "chat"
        gate.set()
        host.wait(result.run_id)
        assert result.run_id is not None
        assert session_id is not None
    finally:
        host.close()


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    try:
        return int(probe.getsockname()[1])
    finally:
        probe.close()
