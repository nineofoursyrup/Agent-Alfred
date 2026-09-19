"""Trace export at the real Host/managed trace boundary (issue #70)."""

import hashlib
import io
import json
import os
import threading
import zipfile

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.tools import Tool, ToolSuccess
from agent_alfred.tools.calendar import object_schema
from agent_alfred.trace_export.errors import ExportError


def test_share_export_is_readable_and_does_not_contain_source_identity(tmp_path):
    with ops_host(tmp_path, [GATE, "private answer"]) as (host, _, _, _):
        run_id, _ = run(host, "private question")
        exports = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        accepted = exports.start(run_id)
        ready = exports.wait(accepted["task_id"], timeout=5)
        assert ready["state"] == "ready", ready
        output = tmp_path / "download.zip"
        with output.open("wb") as stream:
            exports.download(ready["task_id"], ready["download_token"], stream.write)
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["source_integrity"] == "verified_complete"
            assert manifest["export_schema"] == 1
            for name, info in manifest["files"].items():
                raw = archive.read(name)
                assert len(raw) == info["bytes"]
                assert hashlib.sha256(raw).hexdigest() == info["sha256"]
            for name in archive.namelist():
                raw = archive.read(name)
                for secret in (
                    run_id,
                    "private question",
                    "private answer",
                    str(tmp_path),
                ):
                    assert secret.encode() not in raw
        assert exports.status(ready["task_id"])["state"] == "transferred"
        assert exports.released()


def export(host, path, run_id, mode="share"):
    service = host.attach_trace_exports(path / "state", path / "traces")
    task = service.start(run_id, mode)
    return service, service.wait(task["task_id"])


def contents(service, task):
    buffer = io.BytesIO()
    service.download(task["task_id"], task["download_token"], buffer.write)
    with zipfile.ZipFile(buffer) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_ce01_missing_referenced_artifact_does_not_become_complete(tmp_path):
    tool = Tool(
        "long_local",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock("中文正文" * 80000),)),
        "local_read",
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("call", "long_local", {})), "done"],
        tools=(tool,),
    ) as (host, *_):
        rid, _ = run(host)
        next((tmp_path / "traces").glob("*/*/artifacts/tool-*.txt")).unlink()
        service, task = export(host, tmp_path, rid)
        assert task["state"] == "ready", task
        assert task["source_integrity"] == "known_incomplete"
        output = contents(service, task)
        manifest = json.loads(output["manifest.json"])
        assert "artifact-1_missing" in manifest["missing"]
        assert b"known_incomplete" in output["README.txt"]
        assert not any(name.endswith("artifact-1.txt") for name in output)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("empty", "known_incomplete"),
        ("tail", "known_incomplete"),
        ("bad", "corrupt_trace"),
        ("unknown", "unsupported_format"),
        ("process", "unsafe_source"),
    ],
)
def test_source_matrix_ac05_ac07_ce07(tmp_path, change, reason):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        raw = path.read_bytes()
        if change == "empty":
            path.write_bytes(b"")
        elif change == "tail":
            path.write_bytes(raw + b"{private tail")
        elif change == "bad":
            path.write_bytes(b"{bad}\n" + raw)
        else:
            lines = [json.loads(line) for line in raw.splitlines()]
            if change == "unknown":
                lines[0]["payload_name"] = "future.event"
            else:
                lines[0]["process_instance_id"] = "other-process"
            path.write_text("".join(json.dumps(e) + "\n" for e in lines))
        before = path.read_bytes()
        service, task = export(host, tmp_path, rid)
        assert (
            task.get("source_integrity") if task["state"] == "ready" else task["reason"]
        ) == reason, task
        if task["state"] == "ready":
            output = contents(service, task)
            assert b"private tail" not in b"".join(output.values())
        assert path.read_bytes() == before


def test_ce02_finished_event_without_recording_evidence_is_unknown(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, conn, _):
        rid, _ = run(host)
        with host._store.transaction() as conn:
            conn.execute("UPDATE runs SET telemetry=NULL WHERE run_id=?", (rid,))
            conn.commit()
        service, task = export(host, tmp_path, rid)
        assert task["source_integrity"] == "unknown", task
        manifest = json.loads(contents(service, task)["manifest.json"])
        assert manifest["observations"]["recording_state"] == "unknown"


def test_ce07_gaps_and_clock_rollback_preserve_published_order(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        for n, line in enumerate(lines):
            line.update(seq=(n + 1) * 3, ts=100 - n)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        service, task = export(host, tmp_path, rid)
        output = contents(service, task)
        events = [
            json.loads(line) for line in output["bundle/trace.jsonl"].splitlines()
        ]
        assert [e["seq"] for e in events] == [e["seq"] for e in lines]
        assert events[-1]["relative_seconds"] < 0
        assert task["source_integrity"] == "verified_complete"


def test_ce05_diagnostic_inline_and_artifact_use_current_rules(tmp_path):
    private = "brand-new-credential"
    text = f"{private} {tmp_path}/state <script>inert</script> 中文🙂"
    tool = Tool(
        "long_local",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock(text * 12000),)),
        "local_read",
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("call", "long_local", {})), text],
        tools=(tool,),
    ) as (host, *_):
        rid, _ = run(host, text)
        host._redactor.remember(private)
        service, task = export(host, tmp_path, rid, "diagnostic")
        assert task["state"] == "ready", task
        output = contents(service, task)
        all_bytes = b"".join(output.values())
        assert private.encode() not in all_bytes
        assert str(tmp_path / "state").encode() not in all_bytes
        assert rid.encode() not in all_bytes
        assert "<script>inert</script> 中文🙂".encode() in all_bytes
        manifest = json.loads(output["manifest.json"])
        artifact = output["bundle/artifacts/artifact-1.txt"]
        reference = next(
            e["payload"]["audit_content"]
            for e in map(json.loads, output["bundle/trace.jsonl"].splitlines())
            if e["payload_name"] == "tool.finished"
        )
        assert reference["bytes"] == len(artifact)
        assert reference["sha256"] == hashlib.sha256(artifact).hexdigest()
        assert manifest["mode"] == "diagnostic"


def test_ce03_ready_export_invalidates_on_new_protection(tmp_path):
    with ops_host(tmp_path, [GATE, "formerly unknown"]) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid)
        host._redactor.remember("formerly unknown")
        state = service.status(task["task_id"])
        assert state["state"] == "invalidated" and state["cleanup"] == "released"
        with pytest.raises(ExportError):
            service.download(task["task_id"], task["download_token"], lambda b: None)
        assert not list((tmp_path / "state" / "trace-exports").iterdir())
        assert service.wait(service.start(rid)["task_id"])["state"] == "ready"


def test_ce06_replaced_source_before_download_cleans_task(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        path.unlink()
        path.symlink_to(tmp_path / "state" / "audit.key")
        with pytest.raises(ExportError):
            service.download(task["task_id"], task["download_token"], lambda b: None)
        assert service.released()


def test_ce08_cancel_does_not_release_active_file_reader(tmp_path, monkeypatch):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        entered, release = threading.Event(), threading.Event()
        original = os.pread
        once = True

        def held(fd, size, offset):
            nonlocal once
            if once and threading.current_thread().name.startswith("trace-export"):
                once = False
                entered.set()
                assert release.wait(5)
            return original(fd, size, offset)

        monkeypatch.setattr(os, "pread", held)
        service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        task = service.start(rid)
        try:
            assert entered.wait(3)
            cancelled = service.cancel(task["task_id"])
            assert cancelled["state"] == "cleaning"
            with pytest.raises(ExportError, match="busy"):
                service.start(rid)
        finally:
            release.set()
        state = service.wait(task["task_id"])
        assert state["state"] == "cancelled" and service.released()


def test_ac20_ready_expiry_and_single_use_credential(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, _, clock):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid)
        clock.monotonic_value += 300
        assert service.status(task["task_id"])["state"] == "expired"
        task = service.wait(service.start(rid)["task_id"])
        contents(service, task)
        with pytest.raises(ExportError, match="credential_invalid"):
            service.download(task["task_id"], task["download_token"], lambda b: None)


def test_ce09_cleanup_failure_keeps_slot_and_file_owned(tmp_path, monkeypatch):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid)
        original = os.unlink

        def fail(path, *args, **kwargs):
            if str(path) == "archive.zip":
                raise OSError("fixture cleanup withheld")
            return original(path, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(os, "unlink", fail)
            state = service.cancel(task["task_id"])
            assert state["state"] == "cleaning" and state["cleanup"] == "failed"
            assert not service.released()
            with pytest.raises(ExportError, match="busy"):
                service.start(rid)
        assert service.status(task["task_id"])["cleanup"] == "released"


def test_ce10_invalidation_during_download_stops_next_chunk(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid)
        sent = []

        def write(raw):
            sent.append(raw)
            host._redactor.remember("new send credential")
            return len(raw)

        with pytest.raises(ExportError, match="invalidated"):
            service.download(task["task_id"], task["download_token"], write)
        assert sent and service.released()


def test_ac20_generation_budget_uses_acceptance_and_waits_for_io(tmp_path, monkeypatch):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, _, clock):
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
            assert entered.wait(3)
            clock.monotonic_value += 60
            state = service.status(task["task_id"])
            assert (
                state["state"] == "cleaning" and state["reason"] == "generation_timeout"
            )
            assert not service.released()
        finally:
            release.set()
        assert service.wait(task["task_id"])["cleanup"] == "released"


def test_ce03_generation_rechecks_authoritative_version_without_notification(
    tmp_path, monkeypatch
):
    with ops_host(tmp_path, [GATE, "newly protected text"]) as (host, *_):
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
            assert entered.wait(3)
            host._redactor._on_change = None
            host._redactor.remember("newly protected text")
        finally:
            release.set()
        state = service.wait(task["task_id"])
        assert state["state"] == "invalidated" and state["cleanup"] == "released"


def test_ac17_read_lease_blocks_deleting_through_ready_and_send(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid)
        with pytest.raises(ExportError, match="busy"):
            with service.leases.deleting(rid):
                pass

        def write(raw):
            with pytest.raises(ExportError, match="busy"):
                with service.leases.deleting(rid):
                    pass
            return len(raw)

        service.download(task["task_id"], task["download_token"], write)
        with service.leases.deleting(rid):
            with pytest.raises(ExportError, match="trace_pruned"):
                service.start(rid)


def test_ac20_download_budget_stops_before_body(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, _, clock):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid)

        def headers(size):
            clock.monotonic_value += 120

        sent = []
        with pytest.raises(ExportError, match="download_timeout"):
            service.download(
                task["task_id"], task["download_token"], sent.append, headers
            )
        assert not sent and service.released()


def test_large_diagnostic_event_stream_preserves_utf8_and_sensitive_fields(tmp_path):
    text = '中文🙂\\quoted "text" ' + "x" * 100000
    with ops_host(tmp_path, [GATE, text]) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid, "diagnostic")
        assert task["state"] == "ready", task
        output = contents(service, task)
        finished = next(
            e
            for e in map(json.loads, output["bundle/trace.jsonl"].splitlines())
            if e["payload_name"] == "run.finished"
        )
        assert finished["payload"]["reply"]["blocks"][0]["text"] == text


def test_diagnostic_sensitive_json_key_across_large_whitespace(tmp_path):
    secret = "never-export-this-password"
    body = '{"pass\\u0077ord"' + " " * 100000 + ": " + json.dumps(secret) + "}"
    tool = Tool(
        "json_text",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock(body),)),
        "local_read",
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("call", "json_text", {})), "done"],
        tools=(tool,),
    ) as (host, *_):
        rid, _ = run(host)
        service, task = export(host, tmp_path, rid, "diagnostic")
        assert task["state"] == "ready", task
        output = contents(service, task)
        assert all(secret.encode() not in raw for raw in output.values())
