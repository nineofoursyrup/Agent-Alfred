"""Late stop failures cannot reclaim a worker already released by another owner."""

import errno
import os
import subprocess
import threading

from agent_alfred.evals.deterministic.test_database_http import (
    _dashboard,
    _post,
    _status,
)
from agent_alfred.evals.deterministic.test_database_lifecycle import executing_at


def test_late_stop_error_cannot_overwrite_released_owner(tmp_path, monkeypatch):
    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    real_kill = os.killpg
    entered = threading.Event()
    resume = threading.Event()
    first = None
    query_id = None
    responses = []
    try:
        with executing_at(dashboard, tmp_path, "extract", "SELECT 1") as (
            query_id,
            process,
            result,
            worker,
        ):

            def pause_first_stop(pgid, sig):
                if not entered.is_set():
                    entered.set()
                    assert resume.wait(3)
                    raise ProcessLookupError(
                        "late ESRCH after another stop reaped process"
                    )
                return real_kill(pgid, sig)

            monkeypatch.setattr(os, "killpg", pause_first_stop)
            first = threading.Thread(
                target=lambda: responses.append(
                    _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
                )
            )
            first.start()
            assert entered.wait(2)
            second = _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
            worker.join(2)
            assert not worker.is_alive()
            assert process.poll() is not None
            assert result["response"][0] == 409
            assert second[0] == 200 and second[1]["cleanup"] == "released"
            assert console.released(), second
            resume.set()
            first.join(2)
            assert not first.is_alive()
            assert len(responses) == 1 and responses[0][0] == 200
            assert responses[0][1]["cleanup"] == "released"
            status = _status(dashboard, query_id)
            assert status[1]["cleanup"] == "released"
            assert console.released()
    finally:
        resume.set()
        if first is not None:
            first.join(3)
        monkeypatch.setattr(os, "killpg", real_kill)
        # Public explicit cancellation retains cleanup reachability after this
        # negative experiment; it is not counted as the assertion under test.
        if query_id is not None:
            _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
        assert dashboard.close()


def test_late_reap_close_error_cannot_overwrite_released_owner(tmp_path, monkeypatch):
    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    real_kill = console._kill
    stop_thread = threading.local()
    entered = threading.Event()
    resume = threading.Event()
    claim = threading.Lock()
    first = None
    query_id = None
    responses = []
    errors = []

    def marked_kill(*args, **kwargs):
        stop_thread.inside = True
        try:
            return real_kill(*args, **kwargs)
        finally:
            stop_thread.inside = False

    monkeypatch.setattr(console, "_kill", marked_kill)
    real_select = subprocess._PopenSelector.select
    target_pipe = {}

    def hold_communicate_at_read_ready(selector, *args, **kwargs):
        ready = real_select(selector, *args, **kwargs)
        pipe = target_pipe.get("pipe")
        if pipe is not None and any(key.fileobj is pipe for key, _events in ready):
            assert entered.wait(3), "stop did not enter pipe cleanup"
        return ready

    # Install before communicate starts: the selector may already be blocking
    # when the real worker reaches the extraction FIFO.
    monkeypatch.setattr(
        subprocess._PopenSelector, "select", hold_communicate_at_read_ready
    )
    try:
        with executing_at(dashboard, tmp_path, "extract", "SELECT 1") as (
            query_id,
            process,
            result,
            worker,
        ):
            pipe = process.stdout
            target_pipe["pipe"] = pipe
            real_close = pipe.close

            def close_with_one_late_error():
                delayed = False
                if getattr(stop_thread, "inside", False):
                    with claim:
                        if not entered.is_set():
                            entered.set()
                            delayed = True
                if delayed:
                    assert resume.wait(5), "other owner did not complete cleanup"
                    # The competing real close must have already released this
                    # exact resource; the stale syscall outcome cannot revive it.
                    assert pipe.closed
                    raise OSError(errno.EIO, "injected late close failure")
                return real_close()

            monkeypatch.setattr(pipe, "close", close_with_one_late_error)

            def cancel():
                try:
                    responses.append(
                        _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
                    )
                except BaseException as error:
                    errors.append(error)

            first = threading.Thread(target=cancel)
            first.start()
            assert entered.wait(2), "stop did not reach actual pipe close"
            second = _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
            worker.join(2)
            assert not worker.is_alive(), result
            assert result["response"][0] == 409, result
            assert result["response"][1]["code"] == "query_cancelled", result
            assert second[0] == 200 and second[1]["cleanup"] == "released", second
            assert process.poll() is not None
            assert pipe.closed
            assert console.released(), second
            before = _status(dashboard, query_id)
            assert before[1]["cleanup"] == "released"
            resume.set()
            first.join(2)
            assert not first.is_alive()
            assert not errors, errors
            after = _status(dashboard, query_id)
            assert after[1]["cleanup"] == "released"
            assert len(responses) == 1
            assert responses[0][0] == 200
            assert responses[0][1]["cleanup"] == "released", responses
            assert console.released()
    finally:
        resume.set()
        if first is not None:
            first.join(3)
        # Restore actual IO first; the following public cleanup is teardown,
        # and does not count as the recovery assertion under test.
        monkeypatch.undo()
        if query_id is not None:
            _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
        assert dashboard.close()


def test_heal_after_stop_failure_keeps_safe_execute_http(tmp_path, monkeypatch):
    dashboard = _dashboard(tmp_path)
    console = dashboard.host.database_console
    real_kill = os.killpg
    real_select = subprocess._PopenSelector.select
    closed = threading.Event()
    publish = threading.Event()
    target_pipe = {}
    healing = threading.local()
    healer = None
    query_id = None
    healing_errors = []

    def selected(selector, *args, **kwargs):
        ready = real_select(selector, *args, **kwargs)
        pipe = target_pipe.get("pipe")
        if pipe is not None and any(key.fileobj is pipe for key, _event in ready):
            assert closed.wait(3), "healer did not close the ready pipe"
        return ready

    monkeypatch.setattr(subprocess._PopenSelector, "select", selected)
    try:
        with executing_at(dashboard, tmp_path, "extract", "SELECT 1") as (
            query_id,
            process,
            result,
            worker,
        ):
            pipe = process.stdout
            target_pipe["pipe"] = pipe
            real_close = pipe.close

            def close_before_publishing_cleanup():
                outcome = real_close()
                if getattr(healing, "active", False):
                    closed.set()
                    assert publish.wait(5), "execute owner did not return"
                return outcome

            monkeypatch.setattr(pipe, "close", close_before_publishing_cleanup)

            def refuse_stop(pgid, sig):
                if pgid == process.pid:
                    raise OSError("injected initial kill failure")
                return real_kill(pgid, sig)

            monkeypatch.setattr(os, "killpg", refuse_stop)
            cancelled = _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
            assert cancelled[0] == 200
            assert cancelled[1]["cleanup"] == "failed"
            assert process.poll() is None
            denied = _post(dashboard, "/api/database/queries", {})
            assert denied[0] == 503 and denied[1]["code"] == "cleanup_failed"
            monkeypatch.setattr(os, "killpg", real_kill)

            def heal():
                healing.active = True
                try:
                    console.invalidate_and_wait()
                except BaseException as error:
                    healing_errors.append(error)
                finally:
                    healing.active = False

            healer = threading.Thread(target=heal)
            healer.start()
            assert closed.wait(2), "healer did not reach real close"
            worker.join(2)
            assert not worker.is_alive(), result
            assert process.poll() is not None and pipe.closed
            observed = dict(result)
            # Resume the real reaper before checking the HTTP outcome, so a
            # failing assertion leaves no fixture thread or live worker behind.
            publish.set()
            healer.join(2)
            assert not healer.is_alive()
            assert not healing_errors, healing_errors
            assert console.released()
            assert observed.get("response", (None, {}))[0] == 503, observed
            assert observed["response"][1]["code"] == "cleanup_failed", observed
    finally:
        publish.set()
        if healer is not None:
            healer.join(3)
        monkeypatch.undo()
        if query_id is not None:
            _post(dashboard, f"/api/database/queries/{query_id}/cancel", {})
        assert dashboard.close()
