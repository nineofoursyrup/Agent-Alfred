"""Actual 512 MiB source boundary, through the Host with a real tool bundle."""

import hashlib
import json
from contextlib import contextmanager

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.evals.deterministic.ops_support import GATE, run
from agent_alfred.evals.deterministic.test_database_http import _post
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.evals.deterministic.test_web_http import _get, _request
from agent_alfred.messages import TextBlock, ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.tools import Tool, ToolSuccess
from agent_alfred.tools.calendar import object_schema
from agent_alfred.wiring import build_dashboard

LIMIT = 512 * 1024 * 1024


@contextmanager
def limit_host(path, script, *, tools):
    dashboard = build_dashboard(
        state_dir=path / "state",
        trace_root=path / "traces",
        clock=FakeClock(),
        port=free_loopback_port(),
        factory=ScriptedModelFactory(ScriptedModel(script)),
        extra_tools=tools,
    )
    dashboard.start()
    try:
        yield dashboard.host, HTTPExports(dashboard)
    finally:
        assert dashboard.close()


class HTTPExports:
    """Exercise real guarded HTTP; use the worker Event only to await completion."""

    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.service = dashboard.host.trace_exports

    def start(self, run_id, mode="share"):
        status, body, _ = _post(
            self.dashboard,
            "/api/trace-exports",
            {
                "run_id": run_id,
                "mode": mode,
                "instance_id": self.dashboard.instance_id,
            },
        )
        assert status == 202, body
        return body

    def wait(self, task_id, timeout=40):
        self.service.wait(task_id, timeout)
        head, raw = _request(
            self.dashboard.port,
            _get(self.dashboard.port, "/api/trace-exports/" + task_id),
        )
        assert b" 200 " in head
        return json.loads(raw)

    def cancel(self, task_id):
        status, body, _ = _post(
            self.dashboard, "/api/trace-exports/" + task_id + "/cancel", {}
        )
        assert status == 200, body
        return body

    def released(self):
        return self.service.released()


def test_ac19_actual_source_512_mib_and_one_byte_over(tmp_path):
    tool = Tool(
        "large",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock("x" * 300000),)),
        "local_read",
    )
    with limit_host(
        tmp_path, [GATE, calls(ToolCallBlock("a", "large", {})), "done"], tools=(tool,)
    ) as (host, service):
        rid, _ = run(host)
        bundle = next((tmp_path / "traces").glob("*/*"))
        artifact = next((bundle / "artifacts").iterdir())
        trace = bundle / "trace.jsonl"
        lines = [json.loads(line) for line in trace.read_text().splitlines()]
        n = LIMIT - 10000
        for _ in range(8):
            for event in lines:
                if event["payload_name"] == "tool.finished":
                    event["payload"]["audit_content"]["bytes"] = n
                    event["payload"]["original_bytes"] = n
            trace.write_text("".join(json.dumps(line) + "\n" for line in lines))
            needed = (
                LIMIT - (bundle / "meta.json").stat().st_size - trace.stat().st_size
            )
            if n == needed:
                break
            n = needed
        assert n == needed
        with artifact.open("r+b") as file:
            file.truncate(n)
        assert sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()) == LIMIT
        digest = hashlib.sha256()
        with artifact.open("rb") as file:
            while chunk := file.read(65536):
                digest.update(chunk)
        for event in lines:
            if event["payload_name"] == "tool.finished":
                event["payload"]["content_digest"] = digest.hexdigest()
        trace.write_text("".join(json.dumps(line) + "\n" for line in lines))
        task = service.wait(service.start(rid)["task_id"], timeout=40)
        assert task["state"] == "ready", task
        assert service.cancel(task["task_id"])["cleanup"] == "released"
        with artifact.open("r+b") as file:
            file.truncate(n + 1)
        over = service.wait(service.start(rid)["task_id"])
        assert over["reason"] == "source_limit" and over["cleanup"] == "released", over


def test_ac19_actual_zip_and_output_limits_are_independent(tmp_path):
    import zipfile
    import zlib

    tool = Tool(
        "large",
        "fixture",
        object_schema({}),
        lambda a, c: ToolSuccess((TextBlock("x" * 300000),)),
        "local_read",
    )
    with limit_host(
        tmp_path, [GATE, calls(ToolCallBlock("a", "large", {})), "done"], tools=(tool,)
    ) as (host, service):
        rid, _ = run(host)
        bundle = next((tmp_path / "traces").glob("*/*"))
        artifact = next((bundle / "artifacts").iterdir())
        trace = bundle / "trace.jsonl"
        # A safely readable, explicitly incomplete source still uses the full limits.
        event = next(
            e
            for e in map(json.loads, trace.read_text().splitlines())
            if e["payload_name"] == "tool.finished"
        )

        def resize(n):
            event["payload"]["audit_content"]["bytes"] = n
            trace.write_text(json.dumps(event) + "\n")
            with artifact.open("r+b") as file:
                file.truncate(n)
            digest = hashlib.sha256()
            with artifact.open("rb") as file:
                while chunk := file.read(65536):
                    digest.update(chunk)
            event["payload"]["original_bytes"] = n
            event["payload"]["content_digest"] = digest.hexdigest()
            trace.write_text(json.dumps(event) + "\n")
            assert (
                sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()) <= LIMIT
            )

        def generate(n):
            resize(n)
            return service.wait(service.start(rid, "diagnostic")["task_id"], timeout=50)

        def sizes(task):
            # Read actual disk metadata, without accumulating a 512 MiB download.
            path = next((tmp_path / "state" / "trace-exports").glob("*/archive.zip"))
            with zipfile.ZipFile(path) as archive:
                uncompressed = sum(i.file_size for i in archive.infolist())
            return path.stat().st_size, uncompressed

        base = generate(300000)
        assert base["state"] == "ready", base
        zip_size, output_size = sizes(base)
        service.cancel(base["task_id"])
        near = 300000 + LIMIT - zip_size - 100
        probe = generate(near)
        assert probe["state"] == "ready", probe
        measured_zip, measured_output = sizes(probe)
        service.cancel(probe["task_id"])
        exact = near + LIMIT - measured_zip
        task = generate(exact)
        assert task["state"] == "ready", task
        assert sizes(task)[0] == LIMIT
        overhead = sizes(task)[0] - sizes(task)[1]
        service.cancel(task["task_id"])
        over = generate(exact + 1)
        assert over["reason"] == "zip_limit" and over["cleanup"] == "released", over
        # At exactly the uncompressed limit packaging may continue, but ZIP
        # overhead still correctly rejects the final archive.
        output_exact = generate(exact + overhead)
        assert output_exact["reason"] == "zip_limit", output_exact
        output_over = generate(exact + overhead + 1)
        assert output_over["reason"] == "output_limit", output_over
        assert service.released()
        # CE-11: this source is extremely compressible; compression cannot
        # legitimize its oversized sanitized representation.
        compressor = zlib.compressobj()
        compressed = 0
        with artifact.open("rb") as file:
            while chunk := file.read(65536):
                compressed += len(compressor.compress(chunk))
        compressed += len(compressor.flush())
        assert compressed < LIMIT // 10
