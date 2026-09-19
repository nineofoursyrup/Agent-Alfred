"""Public HTTP export requests use a real Dashboard and persisted Run."""

import io
import json
import zipfile
from urllib.parse import urlencode

import pytest

from agent_alfred.evals.deterministic.test_database_http import _dashboard, _post
from agent_alfred.evals.deterministic.test_web_http import _request
from agent_alfred.runtime.work import SubmitRequest


def test_http_export_of_real_run_reaches_ready(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        accepted = dashboard.host.submit(SubmitRequest(message="export fixture"))
        dashboard.host.wait(accepted.run_id)
        status, task, _ = _post(
            dashboard,
            "/api/trace-exports",
            {
                "run_id": accepted.run_id,
                "mode": "share",
                "instance_id": dashboard.instance_id,
            },
        )
        assert status == 202, task
        assert dashboard.host.trace_exports.wait(task["task_id"])["state"] == "ready"
    finally:
        assert dashboard.close()


def test_native_download_requires_origin_csrf_and_one_use(tmp_path):
    dashboard = _dashboard(tmp_path)
    try:
        accepted = dashboard.host.submit(SubmitRequest(message="download fixture"))
        dashboard.host.wait(accepted.run_id)
        service = dashboard.host.trace_exports
        task = service.wait(service.start(accepted.run_id)["task_id"])

        def request(csrf, origin=None, token=None):
            body = urlencode(
                {
                    "task_id": task["task_id"],
                    "download_token": token or task["download_token"],
                    "instance_id": dashboard.instance_id,
                    "csrf": csrf,
                }
            ).encode()
            header = (
                "POST /api/trace-exports/download HTTP/1.1\r\n"
                f"Host: localhost:{dashboard.port}\r\n"
                "Content-Type: application/x-www-form-urlencoded\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n"
            )
            if origin:
                header += f"Origin: {origin}\r\n"
            return _request(dashboard.port, (header + "\r\n").encode() + body)

        assert b" 403 " in request(dashboard.csrf_token, "https://evil.example")[0]
        assert b" 403 " in request("wrong", f"http://localhost:{dashboard.port}")[0]
        assert b" 403 " in request(dashboard.csrf_token)[0]
        header, raw = request(
            dashboard.csrf_token, f"http://localhost:{dashboard.port}"
        )
        assert b" 200 " in header, header
        assert b"application/zip" in header and b"no-store" in header
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert (
                json.loads(archive.read("manifest.json"))["source_integrity"]
                == "verified_complete"
            )
        assert (
            b" 409 "
            in request(dashboard.csrf_token, f"http://localhost:{dashboard.port}")[0]
        )
    finally:
        assert dashboard.close()


def test_ce09_real_forgetting_does_not_complete_while_zip_remains(
    tmp_path, monkeypatch
):
    import os

    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    dashboard = _dashboard(tmp_path)
    host = dashboard.host
    context = CommandContext(origin=ManualOrigin("web"), source="web")
    try:
        saved = host.memory_service.execute(
            {
                "operation_id": "export-save",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "fixture", "fact": "forgettable text"},
            },
            context,
        )
        accepted = host.submit(SubmitRequest(message="diagnostic trace"))
        host.wait(accepted.run_id)
        service = host.trace_exports
        task = service.wait(service.start(accepted.run_id, "diagnostic")["task_id"])
        assert task["state"] == "ready", task
        original = os.unlink

        def fail(path, *args, **kwargs):
            if str(path) == "archive.zip":
                raise OSError("fixture unlink failure")
            return original(path, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(os, "unlink", fail)
            deleted = host.memory_service.execute(
                {
                    "operation_id": "export-delete",
                    "kind": "semantic",
                    "action": "delete",
                    "expected_version": 1,
                    "payload": {"id": saved["memory_id"]},
                },
                context,
            )
            assert deleted["status"] == "deleted"
            assert not service.released()
            with host._store.reading() as conn:
                states = conn.execute(
                    "SELECT state FROM forget_cleanup WHERE operation_id=? "
                    "AND target_id='trace-export:managed'",
                    ("export-delete",),
                ).fetchall()
                assert states and all(row[0] != "complete" for row in states)
        assert service.status(task["task_id"])["cleanup"] == "released"
        host.memory_service.forgetting.retry_cleanup("export-delete", context)
        with host._store.reading() as conn:
            states = conn.execute(
                "SELECT state FROM forget_cleanup WHERE operation_id=? "
                "AND target_id='trace-export:managed'",
                ("export-delete",),
            ).fetchall()
            assert states and all(row[0] == "complete" for row in states)
        assert (
            service.wait(service.start(accepted.run_id)["task_id"])["state"] == "ready"
        )
        assert host.database_console.released()
    finally:
        assert dashboard.close()


def test_ce12_restart_reclaims_verified_residual_and_preserves_unknown(tmp_path):
    import subprocess
    import sys

    script = r"""
import json,sys,threading
from pathlib import Path
from agent_alfred.evals.deterministic.test_database_http import _dashboard
from agent_alfred.runtime.work import SubmitRequest
d=_dashboard(Path(sys.argv[1]))
r=d.host.submit(SubmitRequest(message='restart source'))
d.host.wait(r.run_id)
e=d.host.trace_exports
t=e.wait(e.start(r.run_id)['task_id'])
assert t['state']=='ready',t
print(json.dumps({'run_id':r.run_id,'task_id':t['task_id'],'instance':d.instance_id}),flush=True)
threading.Event().wait()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        old = json.loads(process.stdout.readline())
        leftovers = list((tmp_path / "state" / "trace-exports").glob("export-*"))
        assert len(leftovers) == 1
        user_copy = tmp_path / "user-download.zip"
        user_bytes = (leftovers[0] / "archive.zip").read_bytes()
        user_copy.write_bytes(user_bytes)
        source = {
            str(p.relative_to(tmp_path)): p.read_bytes()
            for p in (tmp_path / "state" / "traces").rglob("*")
            if p.is_file()
        }
        assert source
        process.kill()
        process.wait(timeout=5)
        unknown = tmp_path / "state" / "trace-exports" / "unrelated"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("unknown-owned-data")
        dashboard = _dashboard(tmp_path)
        try:
            assert dashboard.instance_id != old["instance"]
            assert not leftovers[0].exists()
            assert user_copy.read_bytes() == user_bytes
            assert (unknown / "keep.txt").read_text() == "unknown-owned-data"
            for name, raw in source.items():
                assert (tmp_path / name).read_bytes() == raw
            status, _, _ = _post(
                dashboard,
                "/api/trace-exports/download",
                {
                    "task_id": old["task_id"],
                    "download_token": "old-credential",
                    "instance_id": old["instance"],
                },
            )
            assert status == 409
            current = dashboard.host.trace_exports
            task = current.wait(current.start(old["run_id"])["task_id"])
            assert (
                task["state"] == "ready"
                and task["source_integrity"] == "verified_complete"
            ), task
        finally:
            assert dashboard.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_ce10_real_http_partial_body_stops_after_protection_change(
    tmp_path, monkeypatch
):
    import http.client
    import socket
    import threading

    from agent_alfred.gateway.web import trace_export_api

    dashboard = _dashboard(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original_send = socket.socket.send
    original_select = trace_export_api.select.select
    sent = 0

    def send(sock, raw, *args, **kwargs):
        nonlocal sent
        if sock.getsockname()[1] == dashboard.port:
            if sent:
                raise BlockingIOError()
            count = original_send(sock, raw[:64], *args, **kwargs)
            sent += count
            return count
        return original_send(sock, raw, *args, **kwargs)

    def writable(read, write, errors, timeout):
        if write and write[0].getsockname()[1] == dashboard.port:
            entered.set()
            assert release.wait(5)
        return original_select(read, write, errors, timeout)

    client = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=5)
    try:
        submitted = dashboard.host.submit(SubmitRequest(message="partial ZIP"))
        dashboard.host.wait(submitted.run_id)
        service = dashboard.host.trace_exports
        task = service.wait(service.start(submitted.run_id)["task_id"])
        with monkeypatch.context() as m:
            m.setattr(socket.socket, "send", send)
            m.setattr(trace_export_api.select, "select", writable)
            client.request(
                "POST",
                "/api/trace-exports/download",
                json.dumps(
                    {
                        "task_id": task["task_id"],
                        "download_token": task["download_token"],
                        "instance_id": dashboard.instance_id,
                    }
                ),
                headers={
                    "Content-Type": "application/json",
                    "x-agent-alfred-csrf": dashboard.csrf_token,
                },
            )
            response = client.getresponse()
            assert response.status == 200
            assert entered.wait(5)
            first = response.read(64)
            assert first.startswith(b"PK")
            dashboard.host._redactor.remember("newly-protected-after-64-bytes")
            release.set()
            with pytest.raises(http.client.IncompleteRead) as error:
                response.read()
            assert error.value.partial == b""
        status = service.status(task["task_id"])
        assert status["state"] == "invalidated"
        assert status["cleanup"] == "released"
        assert sent == 64
    finally:
        release.set()
        client.close()
        assert dashboard.close()
