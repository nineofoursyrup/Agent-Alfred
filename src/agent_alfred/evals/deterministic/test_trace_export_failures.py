"""Controlled real IO and recording barriers for export acceptance."""

import errno
import json
import os
import stat
import threading

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_trace_export import contents, export
from agent_alfred.runtime.work import SubmitRequest
from agent_alfred.trace_export.errors import ExportError


class RecordingGate(threading.Event):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    def wait(self, timeout=None):
        self.entered.set()
        return super().wait(timeout)


def test_ac03_ce02_real_pending_then_failed_recording(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, conn, _):
        gate = RecordingGate()
        host._recorder._before_recording_commit = gate
        conn.execute("""CREATE TRIGGER fail_export_receipt
          BEFORE UPDATE OF telemetry ON runs WHEN NEW.telemetry IS NOT NULL
          BEGIN SELECT RAISE(FAIL, 'fixture receipt unavailable'); END""")
        conn.commit()
        service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        submitted = host.submit(SubmitRequest(message="recording export"))
        try:
            assert gate.entered.wait(5)
            assert host.snapshot().coordinator_state == "recording_pending"
            with pytest.raises(ExportError, match="not_stopped"):
                service.start(submitted.run_id)
        finally:
            gate.set()
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        assert host.snapshot().coordinator_state == "recording_failed"
        task = service.wait(service.start(submitted.run_id, "diagnostic")["task_id"])
        assert task["state"] == "ready", task
        assert task["source_integrity"] == "known_incomplete"
        assert (
            json.loads(contents(service, task)["manifest.json"])["observations"][
                "recording_state"
            ]
            == "failed"
        )


@pytest.mark.parametrize(
    "failure,reason", [(errno.ENOSPC, "disk_full"), (errno.EIO, "io_failed")]
)
def test_ac21_output_write_failure_releases_real_files(
    tmp_path, monkeypatch, failure, reason
):
    from agent_alfred.trace_export.archive import OutputFile

    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        source = next((tmp_path / "traces").glob("*/*/trace.jsonl")).read_bytes()
        with monkeypatch.context() as m:

            def fail(self, raw):
                raise OSError(failure, "fixture IO failure")

            m.setattr(OutputFile, "write", fail)
            service, task = export(host, tmp_path, rid)
            assert task["reason"] == reason, task
            assert task["cleanup"] == "released"
        assert not list((tmp_path / "state" / "trace-exports").iterdir())
        assert (
            next((tmp_path / "traces").glob("*/*/trace.jsonl")).read_bytes() == source
        )
        ready = service.wait(service.start(rid)["task_id"])
        assert ready["state"] == "ready"
        service.cancel(ready["task_id"])


def test_ac26_startup_partial_cleanup_remains_owned_and_retryable(
    tmp_path, monkeypatch
):
    root = tmp_path / "state" / "trace-exports"
    directory = root / ("export-" + "a" * 32)
    directory.mkdir(parents=True, mode=0o700)
    marker = directory / "owner.json"
    marker.write_bytes(b'{"feature":"trace-export","schema":1}\n')
    marker.chmod(0o600)
    original = os.rmdir
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        with monkeypatch.context() as m:

            def fail(path, *args, **kwargs):
                if str(path) == directory.name:
                    raise OSError("fixture rmdir failure")
                return original(path, *args, **kwargs)

            m.setattr(os, "rmdir", fail)
            service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
            with pytest.raises(ExportError, match="cleanup_failed"):
                service.start(rid)
            assert not service.released()
            assert directory.exists()
            assert not marker.exists()
        task = service.wait(service.start(rid)["task_id"])
        assert task["state"] == "ready", task
        assert not directory.exists()
        owned = next(root.iterdir())
        assert stat.S_IMODE(owned.stat().st_mode) == 0o700
        assert stat.S_IMODE((owned / "archive.zip").stat().st_mode) == 0o600
        service.cancel(task["task_id"])


def test_ac21_short_read_and_close_failure_do_not_release_ownership(
    tmp_path, monkeypatch
):
    from agent_alfred.managed_state import ManagedFileLease

    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        original_read = os.pread
        with monkeypatch.context() as m:

            def short(fd, size, offset):
                if threading.current_thread().name == "trace-export":
                    return b""
                return original_read(fd, size, offset)

            m.setattr(os, "pread", short)
            service, task = export(host, tmp_path, rid)
            assert task["reason"] == "io_failed", task
            assert task["cleanup"] == "released"
        task = service.wait(service.start(rid)["task_id"])
        output = service._task.output
        original_close = ManagedFileLease.close
        with monkeypatch.context() as m:

            def fail(lease):
                if lease is output:
                    raise OSError("fixture close failure")
                return original_close(lease)

            m.setattr(ManagedFileLease, "close", fail)
            result = service.cancel(task["task_id"])
            assert result["cleanup"] == "failed"
            assert not service.released()
            with pytest.raises(ExportError, match="busy"):
                service.start(rid)
        assert service.status(task["task_id"])["cleanup"] == "released"


def test_ac29_host_close_preserves_dependencies_until_export_reader_stops(
    tmp_path, monkeypatch
):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, conn, _):
        rid, _ = run(host)
        entered, release = threading.Event(), threading.Event()
        original = os.pread
        armed = True

        def held(fd, size, offset):
            nonlocal armed
            if armed and threading.current_thread().name == "trace-export":
                armed = False
                entered.set()
                assert release.wait(5)
            return original(fd, size, offset)

        monkeypatch.setattr(os, "pread", held)
        service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        task = service.start(rid)
        try:
            assert entered.wait(5)
            assert host.close(timeout=0) is False
            host._worker.join(5)
            assert not host._worker.is_alive()
            assert host.close(timeout=0) is False
            assert not service.released()
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        finally:
            release.set()
        assert service.wait(task["task_id"])["cleanup"] == "released"
        assert host.close()


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing_bundle", "trace_missing"),
        ("missing_trace", "trace_missing"),
        ("unknown_member", "unsupported_format"),
        ("forged_meta", "unsafe_source"),
        ("duplicate_identity", "unsafe_source"),
        ("hardlink", "unsafe_source"),
    ],
)
def test_ac06_ac13_ac14_real_bundle_refusals(tmp_path, change, reason):
    import shutil

    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        bundle = next((tmp_path / "traces").glob("*/*/trace.jsonl")).parent
        if change == "missing_bundle":
            bundle.rename(tmp_path / "outside-bundle")
        elif change == "missing_trace":
            (bundle / "trace.jsonl").unlink()
        elif change == "unknown_member":
            (bundle / "secret.txt").write_text("not-an-export-input")
        elif change == "forged_meta":
            meta = json.loads((bundle / "meta.json").read_text())
            meta["run_id"] = "another-run"
            (bundle / "meta.json").write_text(json.dumps(meta))
        elif change == "duplicate_identity":
            other = tmp_path / "traces" / "2000-01-01" / bundle.name
            shutil.copytree(bundle, other)
        else:
            os.link(bundle / "trace.jsonl", tmp_path / "second-name")
        service, task = export(host, tmp_path, rid)
        assert task["reason"] == reason, task
        assert task["cleanup"] == "released"
        assert not list((tmp_path / "state" / "trace-exports").iterdir())


@pytest.mark.parametrize(
    "reference",
    [
        "/etc/passwd",
        "../other/trace.jsonl",
        "artifacts/../../private",
        "artifacts/tool-999.txt",
    ],
)
def test_ac13_tool_reference_cannot_choose_a_path(tmp_path, reference):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.tools import Tool, ToolSuccess
    from agent_alfred.tools.calendar import object_schema

    tool = Tool(
        "large",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock("text" * 100000),)),
        "local_read",
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("call", "large", {})), "done"],
        tools=(tool,),
    ) as (host, *_):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        events = [json.loads(line) for line in path.read_text().splitlines()]
        event = next(e for e in events if e["payload_name"] == "tool.finished")
        event["payload"]["audit_content"]["artifact"] = reference
        path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        )
        service, task = export(host, tmp_path, rid)
        assert task["reason"] == "unsafe_source", task
        assert task["cleanup"] == "released"


def test_ce04_dynamic_keys_source_digest_and_unreferenced_artifact_are_not_exported(
    tmp_path,
):
    import hashlib

    private = "private-dynamic-key-body"
    digest = hashlib.sha256(private.encode()).hexdigest()
    with ops_host(tmp_path, [GATE, private]) as (host, *_):
        rid, _ = run(host, private)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[-1]["payload"][private] = digest
        events[-1]["payload"]["source_digest"] = digest
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        artifacts = path.parent / "artifacts"
        artifacts.mkdir(exist_ok=True)
        unused = artifacts / "tool-999999.txt"
        unused.write_text(private)
        unused.chmod(0o600)
        service, task = export(host, tmp_path, rid)
        assert task["state"] == "ready", task
        output = contents(service, task)
        for raw in output.values():
            assert private.encode() not in raw
            assert digest.encode() not in raw
            assert str(path).encode() not in raw
        manifest = json.loads(output["manifest.json"])
        assert "unreferenced_artifacts_excluded" in manifest["sanitization"]
        assert "unknown_field_removed" in manifest["sanitization"]
        assert not any(
            name.endswith(".txt") and name.startswith("bundle/") for name in output
        )
        assert unused.read_text() == private


def test_ac06_pruned_and_unknown_run_do_not_create_empty_exports(tmp_path):
    from agent_alfred import schema

    with ops_host(tmp_path, [GATE, "done"]) as (host, _, conn, _):
        rid, _ = run(host)
        schema.record_trace_prune(
            conn,
            run_id=rid,
            prune_requested_at="fixture",
            absence_confirmed_at="fixture",
            prune_reason="manual",
        )
        conn.commit()
        service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        with pytest.raises(ExportError, match="trace_pruned"):
            service.start(rid)
        with pytest.raises(ExportError, match="unknown_run"):
            service.start("unindexed-directory")
        assert not list((tmp_path / "state" / "trace-exports").iterdir())
