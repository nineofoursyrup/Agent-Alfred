"""Acceptance owns real Hosts through construction, use and completed cleanup."""

import os
import sys
import threading
from contextlib import contextmanager

import pytest

from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.runner import run_offline
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
)
from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
from agent_alfred.resource_rollback import IncompleteRollback
from agent_alfred.runtime.host import RuntimeHost


def one_case(*, fault=False):
    batch = controlled_batch()
    batch["cases"] = [
        next(case for case in batch["cases"] if case["group"] == "conversation")
    ]
    if fault:
        batch["cases"][0]["setup"].update(
            routing=True, fault_fixture="prepared_context_projection_error_v1"
        )
    return batch


@contextmanager
def interrupt_recovery(hosts, occurrence, failure):
    code = RuntimeHost.recover.__code__
    with claimed_monitoring_tool("acceptance-recovery", local_codes=(code,)) as tool:

        def entered(actual_code, offset):
            del offset
            if actual_code is code:
                hosts.append(sys._getframe(1).f_locals["self"])
                if len(hosts) == occurrence and failure is not None:
                    raise failure

        sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, entered)
        sys.monitoring.set_local_events(tool, code, sys.monitoring.events.PY_START)
        yield


@pytest.mark.parametrize("occurrence", [1, 2, 3], ids=["preparing", "main", "reopened"])
@pytest.mark.parametrize("fault", [False, True], ids=["ordinary", "projection_fixture"])
@pytest.mark.parametrize(
    "error_type", [OSError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_start_failure_closes_real_host_before_propagating(
    tmp_path,
    occurrence,
    fault,
    error_type,
):
    before_fd = _open_fd_count()
    before_threads = set(threading.enumerate())
    hosts = []
    failure = error_type("synthetic recovery failure")
    try:
        with interrupt_recovery(hosts, occurrence, failure):
            with pytest.raises(error_type) as caught:
                run_offline(one_case(fault=fault), tmp_path)
        assert caught.value is failure
        assert len(hosts) == occurrence
        print(
            {
                "host_closed": hosts[-1].closed,
                "fd_delta": _open_fd_count() - before_fd,
                "extra_threads": len(set(threading.enumerate()) - before_threads),
            }
        )
        assert all(host.closed for host in hosts)
        assert _open_fd_count() == before_fd
        assert set(threading.enumerate()) == before_threads
    finally:
        for host in hosts:
            assert host.close() is True


@contextmanager
def observe_returns(function, returned, *, occurrence=0, failure=None, when=None):
    code = function.__code__
    with claimed_monitoring_tool("acceptance-host-return", local_codes=(code,)) as tool:

        def completed(actual_code, offset, result):
            del offset
            if actual_code is code:
                if when is not None and not when():
                    return
                returned.append(result)
                if len(returned) == occurrence and failure is not None:
                    raise failure

        sys.monitoring.register_callback(
            tool, sys.monitoring.events.PY_RETURN, completed
        )
        sys.monitoring.set_local_events(tool, code, sys.monitoring.events.PY_RETURN)
        yield


def rollback_handle(error):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, IncompleteRollback):
            return current
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item
        )
    pytest.fail("unfinished Host cleanup has no reachable retry owner")


@pytest.mark.parametrize("occurrence", [1, 2, 3], ids=["preparing", "main", "reopened"])
@pytest.mark.parametrize("fault", [False, True], ids=["ordinary", "projection_fixture"])
@pytest.mark.parametrize("boundary", ["product_assembly", "case_builder"])
def test_construction_return_interruption_keeps_the_real_host_owned(
    tmp_path,
    occurrence,
    fault,
    boundary,
):
    from agent_alfred.evals.acceptance.case_setup import build_case_host
    from agent_alfred.wiring import build_host

    before_fd = _open_fd_count()
    before_threads = set(threading.enumerate())
    returned = []
    failure = KeyboardInterrupt("synthetic construction return interruption")
    function = build_host if boundary == "product_assembly" else build_case_host
    try:
        with observe_returns(
            function, returned, occurrence=occurrence, failure=failure
        ):
            with pytest.raises(KeyboardInterrupt) as caught:
                run_offline(one_case(fault=fault), tmp_path)
        assert caught.value is failure
        assert len(returned) == occurrence
        assert all(host.closed for host in returned)
        assert _open_fd_count() == before_fd
        assert set(threading.enumerate()) == before_threads
    finally:
        for host in returned:
            assert host.close() is True


@pytest.mark.parametrize("occurrence", [1, 2, 3], ids=["preparing", "main", "reopened"])
@pytest.mark.parametrize("fault", [False, True], ids=["ordinary", "projection_fixture"])
@pytest.mark.parametrize("error_type", [OSError, SystemExit])
def test_body_interruption_preserves_original_error_and_closes_all_hosts(
    tmp_path,
    occurrence,
    fault,
    error_type,
):
    function = [
        RuntimeHost.create_session,
        RuntimeHost.read_run_evidence,
        RuntimeHost.open_session,
    ][occurrence - 1]
    before_fd = _open_fd_count()
    before_threads = set(threading.enumerate())
    hosts = []
    failure = error_type("synthetic public operation return interruption")
    try:
        with (
            interrupt_recovery(hosts, 0, None),
            observe_returns(
                function,
                [],
                occurrence=1,
                failure=failure,
                when=lambda: len(hosts) == occurrence,
            ),
        ):
            with pytest.raises(error_type) as caught:
                run_offline(one_case(fault=fault), tmp_path)
        assert caught.value is failure
        assert len(hosts) == occurrence
        assert all(host.closed for host in hosts)
        assert _open_fd_count() == before_fd
        assert set(threading.enumerate()) == before_threads
    finally:
        for host in hosts:
            assert host.close() is True


@pytest.mark.parametrize("occurrence", [1, 2, 3], ids=["preparing", "main", "reopened"])
@pytest.mark.parametrize("fault", [False, True], ids=["ordinary", "projection_fixture"])
@pytest.mark.parametrize(
    "boundary",
    [
        "normal_exit",
        "start_error",
        "start_control",
        "construction_control",
        "body_error",
        "close_return_control",
    ],
)
def test_live_trace_thread_keeps_false_close_owned_until_real_thread_exit(
    tmp_path,
    monkeypatch,
    occurrence,
    fault,
    boundary,
):
    import agent_alfred.runtime.host as host_module
    from agent_alfred.evals.acceptance.case_setup import build_case_host

    before_fd = _open_fd_count()
    before_threads = set(threading.enumerate())
    hosts = []
    drains = []
    close_results = []
    reached = threading.Event()
    release = threading.Event()
    original_run = threading.Thread.run

    def held_thread_exit(thread):
        selected = False
        if thread.name == "trace-drain":
            drains.append(thread)
            selected = len(drains) == occurrence
        original_run(thread)
        if selected:
            reached.set()
            assert release.wait(5), "test did not release actual trace thread"

    monkeypatch.setattr(threading.Thread, "run", held_thread_exit)
    monkeypatch.setattr(host_module, "_WORKER_JOIN_TIMEOUT_S", 0.05)
    error_type = {
        "normal_exit": None,
        "start_error": OSError,
        "start_control": SystemExit,
        "construction_control": SystemExit,
        "body_error": OSError,
        "close_return_control": KeyboardInterrupt,
    }[boundary]
    failure = error_type("synthetic lifecycle interruption") if error_type else None
    body = [
        RuntimeHost.create_session,
        RuntimeHost.read_run_evidence,
        RuntimeHost.open_session,
    ][occurrence - 1]
    try:
        with (
            observe_returns(
                build_case_host,
                hosts,
                occurrence=occurrence,
                failure=failure if boundary == "construction_control" else None,
            ),
            interrupt_recovery(
                [],
                occurrence,
                failure if boundary.startswith("start_") else None,
            ),
            observe_returns(
                body,
                [],
                occurrence=1,
                failure=failure if boundary == "body_error" else None,
                when=lambda: len(hosts) == occurrence,
            ),
            observe_returns(
                RuntimeHost.close,
                close_results,
                occurrence=occurrence,
                failure=failure if boundary == "close_return_control" else None,
            ),
        ):
            with pytest.raises(error_type or RuntimeError) as caught:
                run_offline(one_case(fault=fault), tmp_path)
            if failure is not None:
                assert caught.value is failure
            assert reached.is_set()
            assert len(hosts) == occurrence
            assert hosts[-1].closed is False
            assert close_results[-1] is False
            assert drains[-1].is_alive()
            assert _open_fd_count() > before_fd
            handle = rollback_handle(caught.value)
            print(
                {
                    "close_returned": close_results[-1],
                    "retained_fd_delta": _open_fd_count() - before_fd,
                    "retained_threads": len(
                        set(threading.enumerate()) - before_threads
                    ),
                }
            )
            release.set()
            drains[-1].join(2)
            assert not drains[-1].is_alive()
            assert handle.retry() is True
            assert all(host.closed for host in hosts)
            assert _open_fd_count() == before_fd
            assert set(threading.enumerate()) == before_threads
            calls = len(close_results)
            assert handle.retry() is True
            assert len(close_results) == calls
            print(
                {
                    "recovered_fd_delta": 0,
                    "recovered_extra_threads": 0,
                    "repeat_retry_host_closes": len(close_results) - calls,
                }
            )
    finally:
        release.set()
        for thread in drains:
            thread.join(2)
        for host in hosts:
            assert host.close() is True


@pytest.mark.parametrize("occurrence", [1, 2, 3], ids=["preparing", "main", "reopened"])
@pytest.mark.parametrize("fault", [False, True], ids=["ordinary", "projection_fixture"])
def test_completed_close_return_interruption_never_repeats_native_fd_close(
    tmp_path,
    monkeypatch,
    occurrence,
    fault,
):
    before_fd = _open_fd_count()
    before_threads = set(threading.enumerate())
    native_closes = []
    close_results = []
    hosts = []
    original_close = os.close
    failure = KeyboardInterrupt("synthetic completed close return interruption")

    def counted_close(fd):
        native_closes.append(fd)
        return original_close(fd)

    monkeypatch.setattr(os, "close", counted_close)
    try:
        with (
            interrupt_recovery(hosts, 0, None),
            observe_returns(
                RuntimeHost.close,
                close_results,
                occurrence=occurrence,
                failure=failure,
            ),
        ):
            with pytest.raises(KeyboardInterrupt) as caught:
                run_offline(one_case(fault=fault), tmp_path)
            assert caught.value is failure
            assert len(hosts) == occurrence
            assert close_results[-1] is True
            assert all(host.closed for host in hosts)
            assert _open_fd_count() == before_fd
            assert set(threading.enumerate()) == before_threads
            handle = rollback_handle(caught.value)
            count = len(native_closes)
            assert handle.retry() is True
            assert len(native_closes) == count
            assert handle.retry() is True
            assert len(native_closes) == count
            print(
                {
                    "completed_close_return_control": True,
                    "repeated_native_fd_closes": len(native_closes) - count,
                }
            )
    finally:
        for host in hosts:
            assert host.close() is True
