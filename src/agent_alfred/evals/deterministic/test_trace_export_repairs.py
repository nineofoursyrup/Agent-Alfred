"""Candidate review counterexamples, using the real export lifecycle."""

import inspect
import json
import os
import sys
import threading
import zipfile

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.evals.deterministic.test_trace_export import contents, export
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.runtime.host import RuntimeHost
from agent_alfred.tools import Tool, ToolSuccess
from agent_alfred.tools.calendar import object_schema
from agent_alfred.trace_export.errors import ExportError


def descriptors():
    result = {}
    for name in os.listdir("/dev/fd"):
        try:
            fd = int(name)
            info = os.fstat(fd)
            result[fd] = (info.st_dev, info.st_ino)
        except OSError:
            pass
    return result


def test_std01_interrupted_constructor_return_remains_host_owned(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        before = descriptors()
        lines, start = inspect.getsourcelines(RuntimeHost.attach_trace_exports)
        boundary = start + next(
            i for i, line in enumerate(lines) if "self.trace_exports = exports" in line
        )

        def interrupt(frame, event, arg):
            if (
                event == "line"
                and frame.f_code.co_name == "attach_trace_exports"
                and frame.f_lineno == boundary
            ):
                raise KeyboardInterrupt("fixture constructor return")
            return interrupt

        owned = {}
        try:
            sys.settrace(interrupt)
            with pytest.raises(KeyboardInterrupt):
                host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
            sys.settrace(None)
            owned = {
                fd: identity
                for fd, identity in descriptors().items()
                if fd not in before
            }
            assert host.trace_exports is not None
            assert host.close()
            assert not {
                fd: identity
                for fd, identity in descriptors().items()
                if owned.get(fd) == identity
            }
        finally:
            sys.settrace(None)
            assert host.close()
            # Clean up a failing pre-fix reproduction without hiding the assertion.
            for fd, identity in descriptors().items():
                if owned.get(fd) == identity:
                    os.close(fd)


def test_std02_stale_cleanup_cannot_release_successor_read_lease(tmp_path, monkeypatch):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, first = export(host, tmp_path, rid)
        old = service._task
        waiting, entered, release, finished = (threading.Event() for _ in range(4))
        real_wait = old.wake.wait

        def wait(timeout=None):
            waiting.set()
            return real_wait(timeout)

        monkeypatch.setattr(old.wake, "wait", wait)
        old.wake.set()
        assert waiting.wait(5)
        original = service._cleanup

        def cleanup(task):
            if task is old and threading.current_thread() is old.thread:
                entered.set()
                assert release.wait(5)
                original(task)
                finished.set()
            else:
                original(task)

        monkeypatch.setattr(service, "_cleanup", cleanup)
        try:
            assert service.cancel(first["task_id"])["cleanup"] == "released"
            assert entered.wait(5)
            second = service.wait(service.start(rid)["task_id"])
            assert second["state"] == "ready", second
            release.set()
            assert finished.wait(5)
            with pytest.raises(ExportError, match="busy"):
                with service.leases.deleting(rid):
                    pass
            assert service.status(second["task_id"])["state"] == "ready"
            service.cancel(second["task_id"])
        finally:
            release.set()


@pytest.mark.parametrize("padding", [0, 100000, 300000])
@pytest.mark.parametrize("mode", ["share", "diagnostic"])
def test_spec01_embedded_sensitive_fields_are_protected_in_all_representations(
    tmp_path, padding, mode
):
    sensitive = "previously-unknown-sensitive-field"
    body = (
        "Result follows: "
        + json.dumps({"password": sensitive})
        + " END"
        + "中" * padding
    )
    tool = Tool(
        "result",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock(body),)),
        "local_read",
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("call", "result", {})), "done"],
        tools=(tool,),
    ) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid, mode)
        assert task["state"] == "ready", task
        output = contents(service, task)
        assert all(sensitive.encode() not in raw for raw in output.values())
        if mode == "diagnostic":
            assert any(b"Result follows:" in raw for raw in output.values())


@pytest.mark.parametrize("conflict", [999, False, "0"])
def test_spec02_conflicting_step_identity_rejects_entire_export(tmp_path, conflict):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        events = [json.loads(line) for line in path.read_text().splitlines()]
        next(e for e in events if e["payload_name"] == "step.started")["payload"][
            "step_index"
        ] = conflict
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        service, task = export(host, tmp_path, rid)
        assert task["reason"] == "unsafe_source", task
        assert task["cleanup"] == "released"
        assert task["download_token"] is None


@pytest.mark.parametrize("stage", ["after_trace_read", "before_ready"])
def test_spec03_ce03_protection_changes_after_content_read_and_before_ready(
    tmp_path, monkeypatch, stage
):
    with ops_host(tmp_path, [GATE, "newly-registered-late-secret"]) as (host, *_):
        rid, _ = run(host)
        trace = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        identity = (trace.stat().st_dev, trace.stat().st_ino)
        entered, release = threading.Event(), threading.Event()
        read, sync = os.pread, os.fsync
        armed = True

        def pause():
            nonlocal armed
            if armed:
                armed = False
                entered.set()
                assert release.wait(5)

        def after_read(fd, size, offset):
            raw = read(fd, size, offset)
            info = os.fstat(fd)
            if (
                stage == "after_trace_read"
                and threading.current_thread().name == "trace-export"
                and (info.st_dev, info.st_ino) == identity
            ):
                pause()
            return raw

        def before_ready(fd):
            sync(fd)
            if (
                stage == "before_ready"
                and threading.current_thread().name == "trace-export"
                and service._task.output is not None
                and fd == service._task.output.fd
            ):
                pause()

        monkeypatch.setattr(os, "pread", after_read)
        monkeypatch.setattr(os, "fsync", before_ready)
        service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        task = service.start(rid, "diagnostic")
        try:
            assert entered.wait(5)
            if stage == "before_ready":
                path = next((tmp_path / "state/trace-exports").glob("*/archive.zip"))
                with zipfile.ZipFile(path) as archive:
                    assert archive.testzip() is None
            host._redactor.remember("newly-registered-late-secret")
        finally:
            release.set()
        result = service.wait(task["task_id"])
        assert result["state"] == "invalidated", result
        assert result["cleanup"] == "released"
        assert result["download_token"] is None


def test_spec03_ce07_real_fanout_gap_and_fake_wall_clock_rollback(tmp_path):
    from datetime import timedelta

    observed = []

    def tool_body(arguments, context):
        before = clock.wall_utc()
        context.events.progress("transient progress creates a real seq gap")
        clock.wall -= timedelta(hours=2)
        clock.monotonic_value += 1
        observed.append((before, clock.wall_utc()))
        return ToolSuccess((TextBlock("done"),))

    tool = Tool(
        "clock_move",
        "fixture",
        object_schema({}),
        tool_body,
        "local_read",
        emits_progress=True,
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("call", "clock_move", {})), "done"],
        tools=(tool,),
    ) as (host, _, _, clock):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        original = path.read_bytes()
        source = [json.loads(line) for line in original.splitlines()]
        seq = [event["seq"] for event in source]
        assert any(b - a > 1 for a, b in zip(seq, seq[1:]))
        assert observed and observed[0][1] < observed[0][0]
        service, task = export(host, tmp_path, rid, "diagnostic")
        assert task["state"] == "ready", task
        records = [
            json.loads(line)
            for line in contents(service, task)["bundle/trace.jsonl"].splitlines()
        ]
        assert [event["seq"] for event in records] == seq
        assert [event["ts"] for event in records] == [event["ts"] for event in source]
        assert task["source_integrity"] == "verified_complete"
        assert path.read_bytes() == original
