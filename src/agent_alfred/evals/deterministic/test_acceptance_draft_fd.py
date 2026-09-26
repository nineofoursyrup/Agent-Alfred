"""Draft readback owns native descriptors without retrying uncertain numbers."""

import os
import sys
from contextlib import contextmanager

import pytest

from agent_alfred.evals.acceptance import artifacts
from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.runner import run_offline
from agent_alfred.evals.deterministic._monitoring_test_helpers import (
    claimed_monitoring_tool,
)
from agent_alfred.evals.deterministic._trace_test_helpers import _open_fd_count
from agent_alfred.resource_rollback import (
    IncompleteRollback,
    OwnedDescriptor,
    capture_call_result,
)


def inside_artifact_open():
    frame = sys._getframe(1)
    code = artifacts.opened.__wrapped__.__code__
    while frame is not None:
        if frame.f_code is code:
            return True
        frame = frame.f_back
    return False


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
    pytest.fail("cleanup failure has no reachable rollback state")


def is_target(path, target):
    text = os.fspath(path)
    return {
        "state": text != "outbox" and not text.endswith(".md"),
        "outbox": text == "outbox",
        "body": text.endswith(".md"),
    }[target]


@pytest.mark.parametrize("target", ["state", "outbox", "body"])
def test_public_runner_propagates_uncertain_native_close_instead_of_completing(
    tmp_path,
    monkeypatch,
    target,
):
    batch = controlled_batch()
    batch["cases"] = [case for case in batch["cases"] if case["group"] == "tools"]
    original_open, original_close = os.open, os.close
    held = []
    close_calls = []
    failure = OSError("synthetic failure before native outbox descriptor close")

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if is_target(path, target) and inside_artifact_open():
            held.append(fd)
        return fd

    def blocked_close(fd):
        if fd in held:
            close_calls.append(fd)
            raise failure
        return original_close(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", blocked_close)
    result = None
    error = None
    try:
        try:
            result = run_offline(batch, tmp_path)
        except BaseException as caught:
            error = caught
        assert len(held) == 1
        assert os.fstat(held[0]).st_ino > 0
        row = result["results"][0] if result is not None else None
        print(
            {
                "returned_normally": result is not None,
                "outcome": row and row["outcome"],
                "artifacts": row and row["evidence"]["local_artifacts"]["status"],
                "real_open_descriptors": len(held),
                "propagated_original_error": error is failure,
            }
        )
        assert error is failure, "runner completed after losing native cleanup failure"
        handle = rollback_handle(error)
        attempts = len(close_calls)
        # Native close has an uncertain outcome. The shared core consumes the
        # number before attempting close and must not retry that number.
        assert handle.retry() is True
        assert len(close_calls) == attempts
        assert os.fstat(held[0]).st_ino > 0
        print(
            {
                "uncertain_number_retried": False,
                "synthetic_pre_native_failure_fd_still_open": True,
                "cleanup_requires_test_teardown": True,
            }
        )
    finally:
        for fd in held:
            original_close(fd)


def draft_state(tmp_path):
    state = tmp_path / "state"
    box = state / "outbox"
    box.mkdir(parents=True)
    (box / "draft.md").write_text("synthetic draft\n")
    return state


def track_native_descriptor(monkeypatch, target):
    original_open, original_close = os.open, os.close
    held, closed = [], []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if is_target(path, target) and inside_artifact_open():
            held.append(fd)
        return fd

    def tracked_close(fd):
        if fd in held:
            closed.append(fd)
        return original_close(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    return held, closed


@contextmanager
def reuse_number(fd):
    # The tests first prove the old descriptor is closed. Reserve exactly that
    # number for a new real resource, never an arbitrary live descriptor.
    with pytest.raises(OSError):
        os.fstat(fd)
    replacement = os.open(os.devnull, os.O_RDONLY)
    if replacement != fd:
        os.dup2(replacement, fd)
        os.close(replacement)
    try:
        yield fd
    finally:
        os.close(fd)


@pytest.mark.parametrize("target", ["state", "outbox", "body"])
@pytest.mark.parametrize(
    "body_type",
    [None, OSError, ValueError, KeyboardInterrupt, SystemExit, GeneratorExit],
)
def test_pre_close_interruption_keeps_owned_fd_recoverable_and_preserves_body_error(
    tmp_path,
    monkeypatch,
    target,
    body_type,
):
    state = draft_state(tmp_path)
    baseline = _open_fd_count()
    held, closed = track_native_descriptor(monkeypatch, target)
    close_failure = OSError("synthetic interruption before descriptor consumption")
    body_failure = body_type("synthetic body read failure") if body_type else None
    expected = body_failure or close_failure
    original_read = os.read
    body_inode = (state / "outbox" / "draft.md").stat().st_ino

    def failed_read(fd, amount):
        if body_failure is not None and os.fstat(fd).st_ino == body_inode:
            raise body_failure
        return original_read(fd, amount)

    monkeypatch.setattr(os, "read", failed_read)
    code = OwnedDescriptor.close.__code__
    handle = None
    try:
        with claimed_monitoring_tool("draft-close-entry", local_codes=(code,)) as tool:

            def interrupted(actual_code, offset):
                del offset
                if actual_code is code and sys._getframe(1).f_locals["self"].fd in held:
                    raise close_failure

            sys.monitoring.register_callback(
                tool, sys.monitoring.events.PY_START, interrupted
            )
            sys.monitoring.set_local_events(tool, code, sys.monitoring.events.PY_START)
            with pytest.raises(type(expected)) as caught:
                artifacts.drafts(state)
            assert caught.value is expected
            handle = rollback_handle(caught.value)
        assert len(held) == 1 and os.fstat(held[0]).st_ino > 0
        assert not closed
        assert handle.retry() is True
        assert closed == held
        assert _open_fd_count() == baseline
        with reuse_number(held[0]) as replacement:
            count = len(closed)
            assert handle.retry() is True
            assert handle.retry() is True
            assert len(closed) == count
            assert os.fstat(replacement).st_ino > 0
        print(
            {
                "recoverable_close_entry": target,
                "recovered_fd_delta": 0,
                "reused_descriptor_survives_retry": True,
            }
        )
    finally:
        if handle is not None:
            assert handle.retry() is True


@pytest.mark.parametrize("target", ["state", "outbox", "body"])
@pytest.mark.parametrize("boundary", ["native_capture", "owned_constructor", "yield"])
def test_open_result_and_constructor_yield_interruptions_close_the_owned_descriptor(
    tmp_path,
    monkeypatch,
    target,
    boundary,
):
    state = draft_state(tmp_path)
    baseline = _open_fd_count()
    held, closed = track_native_descriptor(monkeypatch, target)
    code, event = {
        "native_capture": (
            capture_call_result.__code__,
            sys.monitoring.events.PY_RETURN,
        ),
        "owned_constructor": (
            OwnedDescriptor.open.__func__.__code__,
            sys.monitoring.events.PY_RETURN,
        ),
        "yield": (
            artifacts.opened.__wrapped__.__code__,
            sys.monitoring.events.PY_YIELD,
        ),
    }[boundary]
    failure = SystemExit("synthetic descriptor handoff interruption")
    fired = False
    with claimed_monitoring_tool("draft-open-result", local_codes=(code,)) as tool:

        def interrupted(actual_code, offset, result):
            nonlocal fired
            del offset
            if actual_code is not code or fired:
                return
            value = (
                sys._getframe(1).f_locals["receiver"]
                if boundary == "native_capture"
                else result
            )
            descriptor = value.fd if isinstance(value, OwnedDescriptor) else value
            if descriptor in held:
                fired = True
                raise failure

        sys.monitoring.register_callback(tool, event, interrupted)
        sys.monitoring.set_local_events(tool, code, event)
        with pytest.raises(SystemExit) as caught:
            artifacts.drafts(state)
        assert caught.value is failure
    assert fired
    assert len(held) == 1 and closed == held
    with pytest.raises(OSError):
        os.fstat(held[0])
    assert _open_fd_count() == baseline


@pytest.mark.parametrize("target", ["state", "outbox", "body"])
@pytest.mark.parametrize("boundary", ["native_close", "token_return"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_post_close_interruption_never_closes_a_reused_fd_number(
    tmp_path,
    monkeypatch,
    target,
    boundary,
    error_type,
):
    state = draft_state(tmp_path)
    baseline = _open_fd_count()
    held, closed = track_native_descriptor(monkeypatch, target)
    tracked_close = os.close
    failure = error_type("synthetic interruption after actual native close")
    selected = None
    fired = False
    handle = None

    def close_then_interrupt(fd):
        nonlocal fired
        tracked_close(fd)
        if fd in held and not fired and boundary == "native_close":
            fired = True
            raise failure

    monkeypatch.setattr(os, "close", close_then_interrupt)
    code = OwnedDescriptor.close.__code__
    try:
        with claimed_monitoring_tool("draft-close-return", local_codes=(code,)) as tool:

            def entered(actual_code, offset):
                nonlocal selected
                del offset
                if actual_code is code:
                    owner = sys._getframe(1).f_locals["self"]
                    if owner.fd in held:
                        selected = owner

            def returned(actual_code, offset, result):
                nonlocal fired
                del offset, result
                if (
                    boundary == "token_return"
                    and not fired
                    and actual_code is code
                    and sys._getframe(1).f_locals["self"] is selected
                ):
                    fired = True
                    raise failure

            sys.monitoring.register_callback(
                tool, sys.monitoring.events.PY_START, entered
            )
            sys.monitoring.register_callback(
                tool, sys.monitoring.events.PY_RETURN, returned
            )
            sys.monitoring.set_local_events(
                tool,
                code,
                sys.monitoring.events.PY_START | sys.monitoring.events.PY_RETURN,
            )
            with pytest.raises(error_type) as caught:
                artifacts.drafts(state)
            assert caught.value is failure
            handle = rollback_handle(caught.value)
        assert fired and len(held) == 1 and closed == held
        assert _open_fd_count() == baseline
        with reuse_number(held[0]) as replacement:
            count = len(closed)
            assert handle.retry() is True
            assert handle.retry() is True
            assert len(closed) == count
            assert os.fstat(replacement).st_ino > 0
        print(
            {
                "post_close_interruption": boundary,
                "reused_descriptor_survives_retry": True,
            }
        )
    finally:
        if handle is not None:
            assert handle.retry() is True


def test_normal_read_closes_all_descriptor_kinds(tmp_path):
    state = draft_state(tmp_path)
    baseline = _open_fd_count()
    result = artifacts.drafts(state)
    assert result["status"] == "available"
    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "outbox/draft.md"
    assert result["files"][0]["content"] == "synthetic draft\n"
    assert _open_fd_count() == baseline


@pytest.mark.parametrize(
    "target,error_type,status",
    [
        ("state", PermissionError, "unavailable"),
        ("outbox", FileNotFoundError, "missing"),
        ("body", PermissionError, "unavailable"),
    ],
)
def test_native_open_failure_preserves_status_without_leaking_parent_descriptors(
    tmp_path,
    monkeypatch,
    target,
    error_type,
    status,
):
    state = draft_state(tmp_path)
    baseline = _open_fd_count()
    original_open = os.open

    def failed_open(path, flags, mode=0o777, *, dir_fd=None):
        if is_target(path, target) and inside_artifact_open():
            raise error_type("synthetic native open failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failed_open)
    assert artifacts.drafts(state)["status"] == status
    assert _open_fd_count() == baseline
