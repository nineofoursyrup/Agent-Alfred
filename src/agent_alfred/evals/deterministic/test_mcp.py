"""MCP-SPEC-r1 acceptance at real Host, process, registry and ledger seams."""

import json
import sys
from pathlib import Path

import pytest

from agent_alfred.connections import CredentialOverlay
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.wiring import build_default_host

FIXTURE = Path(__file__).with_name("mcp_server_fixture.py")


def configure(tmp_path, **options):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    row = {"command": sys.executable, "args": [str(FIXTURE)], **options}
    (state / "mcp.json").write_text(json.dumps({"mcpServers": {"test": row}}))
    return state


@pytest.mark.parametrize("enabled", [None, False, True])
def test_ce01_launch_permission_is_separate(tmp_path, enabled):
    state = configure(tmp_path, **({} if enabled is None else {"enabled": enabled}))
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        mcp = host.connections()["mcp"]
        row = mcp["servers"][0]
        assert row["state"] == ("connected" if enabled else "configured_untested")
        assert (state / "spawned").exists() is bool(enabled)
        assert not (state / "called").exists()
        tools = [
            t
            for t in host.tools_catalog()["tools"]
            if t["source_id"].startswith("mcp:")
        ]
        assert len(tools) == int(bool(enabled))
        if enabled:
            assert tools[0]["authorization"] == "unset"
            assert tools[0]["exposure"] == "hidden"
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "reply,expected,connected,text",
    [
        (
            {"content": [{"type": "text", "text": "answer"}]},
            "succeeded",
            True,
            "answer",
        ),
        ({"content": []}, "succeeded", True, "没有可展示的文本"),
        (
            {"content": [], "structuredContent": {"ok": True}},
            "succeeded",
            True,
            "没有可展示的文本",
        ),
        (
            {"content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}]},
            "succeeded",
            True,
            "不支持",
        ),
        (
            {"content": [{"type": "text", "text": "failed safely"}], "isError": True},
            "failed",
            True,
            "failed safely",
        ),
        ({"content": [], "isError": 1}, "unknown", False, None),
        ({"structuredContent": {}}, "unknown", False, None),
        ({"content": [{"type": "text"}]}, "unknown", False, None),
    ],
)
def test_ce11_real_host_result_matrix(tmp_path, reply, expected, connected, text):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    behavior = tmp_path / "behavior.json"
    behavior.write_text(json.dumps({"result": reply}))
    state = configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "Done",
        ]
    )
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
    )
    try:
        catalog = host.tools_catalog()
        row = next(t for t in catalog["tools"] if t["name"] == "mcp_test_echo")
        assert "error" not in host.save_tool_authorization(
            row["identity"], "allowed", catalog["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        result = host.wait(run.run_id, timeout=10)
        rows = host.tool_requests(run.run_id)
        assert rows[0]["result"] == expected
        assert rows[0]["cost"]["kind"] == "unknown"
        assert (
            host.connections()["mcp"]["servers"][0]["state"] == "connected"
        ) is connected
        assert len(model.requests) == (3 if connected else 2)
        if text:
            assert text in str(model.requests[-1].messages)
        if not connected:
            assert result.error == "tool_result_unverified"
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "schema,usable",
    [
        (
            {
                "type": "object",
                "$defs": {"name": {"type": "string"}},
                "properties": {"name": {"$ref": "#/$defs/name"}},
            },
            True,
        ),
        (
            {
                "type": "object",
                "$id": "https://schema.invalid/root",
                "$defs": {"name": {"type": "string"}},
                "properties": {
                    "name": {"$ref": "https://schema.invalid/root#/$defs/name"}
                },
            },
            True,
        ),
        ({"type": "object", "properties": {"name": {"type": "WRONG"}}}, False),
        ({"type": "object", "$ref": "https://127.0.0.1:1/private"}, False),
        ({"type": "object", "$ref": "file:///private"}, False),
        ({"type": "object", "$ref": "#"}, False),
        ({"type": "object", "$dynamicRef": "#x"}, False),
        (
            {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
            True,
        ),
        (
            {
                "type": "object",
                "$schema": "https://json-schema.org/draft/2019-09/schema",
            },
            False,
        ),
    ],
)
def test_ce08_schema_declaration_real_validator(tmp_path, schema, usable):
    behavior = tmp_path / "behavior.json"
    behavior.write_text(
        json.dumps({"tools": [{"name": "echo", "inputSchema": schema}]})
    )
    state = configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        row = host.connections()["mcp"]["servers"][0]
        assert row["state"] == "connected"
        assert row["total_tools"] == 1
        assert row["available_tools"] == int(usable)
        tool = next(
            t
            for t in host.tools_catalog()["tools"]
            if t["source_id"].startswith("mcp:")
        )
        assert (tool["availability"] == "configured") is usable
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "raw",
    [
        '{"servers":{}}',
        '{"mcpServers":{},"mcpServers":{}}',
        '{"mcpServers":{"test":{"command":"missing-alfred-test-command","enabled":true}}}',
        '{"mcpServers":{"test":{"command":"/bin/cat","enabled":1}}}',
        '{"mcpServers":{"test":{"command":"/bin/cat","env":{"TOKEN":"literal-secret"}}}}',
        '{"mcpServers":{"test":{"command":"/bin/cat","timeout_s":true}}}',
    ],
)
def test_ce09_invalid_configuration_never_partially_starts(tmp_path, raw):
    state = tmp_path / "state"
    state.mkdir()
    (state / "mcp.json").write_text(raw)
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        assert host.connections()["mcp"]["error"] == "configuration_invalid"
        assert host.connections()["mcp"]["servers"] == []
    finally:
        assert host.close()


def control(host, action="apply", server_key=None, **override):
    import uuid

    body = {
        "action": action,
        "operation_id": uuid.uuid4().hex,
        "token": host.connections()["mcp"]["token"],
        "server_key": server_key,
        **override,
    }
    result = host.mcp_control(body)
    if "error" in result:
        return result
    assert host.wait_mcp_operation(body["operation_id"], timeout=15)
    return host.mcp_operation(body["operation_id"])


@pytest.mark.parametrize(
    "authorization,restored", [("allowed", "unset"), ("denied", "denied")]
)
def test_ce02_ce10_replacement_retires_allowance_persistently(
    tmp_path, authorization, restored
):
    state = configure(tmp_path, enabled=True)

    def build():
        return build_default_host(
            state_dir=state,
            factory=ScriptedModelFactory(ScriptedModel([])),
            credentials=CredentialOverlay({}, None),
        )

    host = build()
    try:
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        identity = row["identity"]
        host.save_tool_authorization(
            identity, authorization, host.tools_catalog()["revision"]
        )
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert (
            next(
                t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
            )["authorization"]
            == authorization
        )
        # Change args, keeping reported name and tool contract identical.
        behavior = tmp_path / "behavior.json"
        behavior.write_text("{}")
        configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
        assert control(host)["status"] == "completed"
        current = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        assert current["identity"] != identity and current["authorization"] == "unset"
    finally:
        assert host.close()
    configure(tmp_path, enabled=True)
    host = build()
    try:
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        assert row["identity"] == identity
        assert row["authorization"] == restored
    finally:
        assert host.close()


def test_ce09_stale_preview_and_duplicate_operation(tmp_path):
    import uuid

    state = configure(tmp_path, enabled=True)
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        token = host.connections()["mcp"]["token"]
        pid = (state / "spawned").read_text()
        configure(tmp_path, enabled=False)
        body = {
            "token": token,
            "operation_id": uuid.uuid4().hex,
            "action": "apply",
            "server_key": None,
        }
        assert host.mcp_control(body)["error"]["code"] == "mcp_conflict"
        assert (state / "spawned").read_text() == pid
        body["token"] = host.connections()["mcp"]["token"]
        assert host.mcp_control(body)["status"] == "running"
        assert host.wait_mcp_operation(body["operation_id"], 10)
        first = host.mcp_operation(body["operation_id"])
        assert host.mcp_control(body) == first
        assert (
            host.mcp_control({**body, "action": "cleanup"})["error"]["code"]
            == "operation_conflict"
        )
        assert not host.mutation_in_flight()
    finally:
        assert host.close()


def test_ce14_env_publish_stops_old_capability_without_spawn(tmp_path):
    from agent_alfred.gateway.web.api import DashboardApi

    path = tmp_path / ".env"
    path.write_text("MCP_TOKEN=old-short\n")
    state = configure(tmp_path, enabled=True, env={"TOKEN": "${MCP_TOKEN}"})
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, str(path)),
    )
    try:
        catalog = host.tools_catalog()
        row = next(t for t in catalog["tools"] if t["name"] == "mcp_test_echo")
        host.save_tool_authorization(row["identity"], "allowed", catalog["revision"])
        pid = (state / "spawned").read_text()
        path.write_text("MCP_TOKEN=new-short\n")
        status, body = DashboardApi(facade=host).reread_env()
        assert status == 200
        server = body["mcp"]["servers"][0]
        assert server["state"] == "configured_untested"
        assert server["reason"] == "restart_required"
        assert (state / "spawned").read_text() == pid
        assert (
            next(
                t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
            )["exposure"]
            == "hidden"
        )
        assert control(host, "reconnect", "test")["status"] == "completed"
        new = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        assert new["identity"] != row["identity"] and new["authorization"] == "unset"
        assert all(
            secret not in json.dumps(body) for secret in ("old-short", "new-short")
        )
    finally:
        assert host.close()


def fixture_host(tmp_path, behavior, *, clock=None, script=None):
    behavior_path = tmp_path / "behavior.json"
    behavior_path.write_text(json.dumps(behavior))
    state = configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior_path)])
    model = ScriptedModel(script or [])
    host = build_default_host(
        state_dir=state,
        clock=clock,
        factory=ScriptedModelFactory(model),
        credentials=CredentialOverlay({}, None),
    )
    return host, model, state


@pytest.mark.parametrize(
    "frame_bytes,expected", [(1024 * 1024, "succeeded"), (1024 * 1024 + 1, "unknown")]
)
def test_ce12_frame_utf8_exact_boundary(tmp_path, frame_bytes, expected):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    host, model, state = fixture_host(
        tmp_path,
        {"call_frame_bytes": frame_bytes},
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "done",
        ],
    )
    try:
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10)
        assert host.tool_requests(run.run_id)[0]["result"] == expected
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "pages,connected",
    [
        ([{"tools": [], "nextCursor": "a"}, {"tools": []}], True),
        ([{"tools": [], "nextCursor": "a"}, {"tools": [], "nextCursor": "a"}], False),
        ([{"tools": [], "nextCursor": "a"}, {}], False),
    ],
)
def test_ce04_ce12_no_partial_directory(tmp_path, pages, connected):
    host, _, state = fixture_host(tmp_path, {"pages": pages})
    try:
        row = host.connections()["mcp"]["servers"][0]
        assert (row["state"] == "connected") is connected
        assert row["total_tools"] == 0
    finally:
        assert host.close()


def test_ce04_no_tools_capability_never_lists(tmp_path):
    host, _, state = fixture_host(tmp_path, {"capabilities": {}})
    try:
        assert host.connections()["mcp"]["servers"][0]["state"] == "connected"
        requests = [
            json.loads(line)
            for line in (state / "requests.jsonl").read_text().splitlines()
        ]
        assert "tools/list" not in [r.get("method") for r in requests]
    finally:
        assert host.close()


def test_ce03_ce05_deadline_after_real_execution_stops_batch(tmp_path):
    import os
    import select

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    event, release = tmp_path / "events", tmp_path / "release"
    os.mkfifo(event)
    os.mkfifo(release)
    event_fd = os.open(event, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    clock = FakeClock()
    host, model, state = fixture_host(
        tmp_path,
        {
            "block_call": True,
            "event_fifo": str(event),
            "release_fifo": str(release),
            "server_requests": [
                "ping",
                "roots/list",
                "sampling/createMessage",
                "elicitation/create",
                "unknown",
            ],
        },
        clock=clock,
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(
                ToolCallBlock("echo", "mcp_test_echo", {}),
                ToolCallBlock("later", "mcp_test_echo", {}),
            ),
            "must not run",
        ],
    )
    try:
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert select.select([event_fd], [], [], 5)[0]
        assert b"called" in os.read(event_fd, 1024)
        clock.monotonic_value += 61
        assert select.select([event_fd], [], [], 5)[0]
        assert b"cancelled" in os.read(event_fd, 1024)
        result = host.wait(run.run_id, 10)
        assert result.error == "tool_result_unverified"
        rows = host.tool_requests(run.run_id)
        assert rows[0]["result"] == "unknown"
        assert rows[1]["start_confirmation"] == "not_started"
        assert len(model.requests) == 2
        requests = [
            json.loads(line)
            for line in (state / "requests.jsonl").read_text().splitlines()
        ]
        assert len([r for r in requests if r.get("method") == "tools/call"]) == 1
        replies = {
            r["id"]: r for r in requests if str(r.get("id", "")).startswith("server-")
        }
        assert replies["server-0"]["result"] == {}
        assert [replies["server-" + str(i)]["error"]["code"] for i in range(1, 5)] == [
            -32601,
            -32601,
            -32602,
            -32601,
        ]
    finally:
        os.close(event_fd)
        os.close(release_fd)
        assert host.close()


def test_ce12_discovery_counts_actual_utf8_frame_bytes(tmp_path):
    # Padding is legal JSON whitespace and still consumes real transport bytes.
    pages = [{"tools": [], "nextCursor": str(i)} for i in range(4)] + [{"tools": []}]
    host, _, state = fixture_host(
        tmp_path, {"pages": pages, "list_frame_bytes": 1024 * 1024}
    )
    try:
        assert host.connections()["mcp"]["servers"][0]["state"] == "error"
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "is_error,structured,expected",
    [
        (True, None, "failed"),
        (False, None, "unknown"),
        (False, {"answer": "ok"}, "succeeded"),
        (False, {"answer": 1}, "unknown"),
    ],
)
def test_ic01_error_result_precedes_success_output_schema(
    tmp_path, is_error, structured, expected
):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    reply = {"content": [{"type": "text", "text": "safe reply"}], "isError": is_error}
    if structured is not None:
        reply["structuredContent"] = structured
    host, model, state = fixture_host(
        tmp_path,
        {
            "tools": [
                {
                    "name": "echo",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    },
                }
            ],
            "result": reply,
        },
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "done",
        ],
    )
    try:
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10)
        assert host.tool_requests(run.run_id)[0]["result"] == expected
        assert len(model.requests) == (2 if expected == "unknown" else 3)
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "code,expected",
    [(-32601, "failed"), (-32602, "failed"), (-32603, "unknown"), (-32000, "unknown")],
)
def test_ce11_json_rpc_error_codes(tmp_path, code, expected):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    host, model, state = fixture_host(
        tmp_path,
        {"error": {"code": code, "message": "server detail"}},
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "done",
        ],
    )
    try:
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10)
        assert host.tool_requests(run.run_id)[0]["result"] == expected
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "boundary,kind", [("before", "not_billable"), ("partial", "unknown")]
)
def test_ce07_actual_stdio_write_boundary(tmp_path, monkeypatch, boundary, kind):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.mcp import transport
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    clock = FakeClock()
    host, model, state = fixture_host(
        tmp_path,
        {},
        clock=clock,
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "done",
        ],
    )
    try:
        session = host._mcp.rows["test"]["session"]
        fd = session.process.stdin.fileno()
        actual_write = transport.os.write
        written = []

        def write(target, data):
            if target == fd and b"tools/call" in data:
                count = actual_write(target, data[:5])
                written.append(count)
                clock.monotonic_value += 61
                return count
            return actual_write(target, data)

        if boundary == "partial":
            monkeypatch.setattr(transport.os, "write", write)
        else:
            original = session.request

            def expire(method, params, deadline, monotonic):
                clock.monotonic_value += 61
                return original(method, params, deadline, monotonic)

            monkeypatch.setattr(session, "request", expire)
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10)
        cost = host.tool_requests(run.run_id)[0]["cost"]
        assert cost["kind"] == kind
        if boundary == "before":
            assert cost["reason"] == "transport_not_sent"
            assert not (state / "called").exists()
            assert len(model.requests) == 3
        else:
            assert written == [5]
            assert host.tool_requests(run.run_id)[0]["result"] == "unknown"
            assert len(model.requests) == 2
    finally:
        assert host.close()


def test_ce13_real_validator_process_is_killed_before_replacement(
    tmp_path, monkeypatch
):
    import os
    import signal

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.mcp import validation
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    clock = FakeClock()
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string", "pattern": "^(a+)+$"}},
    }
    host, model, state = fixture_host(
        tmp_path,
        {
            "tools": [
                {
                    "name": "echo",
                    "inputSchema": {"type": "object"},
                    "outputSchema": schema,
                }
            ],
            "result": {
                "content": [],
                "structuredContent": {"answer": "a" * 100000 + "!"},
            },
        },
        clock=clock,
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "must not run",
        ],
    )
    processes = []
    actual = validation.subprocess.Popen.__init__

    def spawn(process, *args, **kwargs):
        actual(process, *args, **kwargs)
        if args[0][-1].endswith("schema_worker.py"):
            # Explicit OS barrier: a real validator process is stopped, then its
            # actual communicate deadline and kill/reap path must run.
            os.kill(process.pid, signal.SIGSTOP)
            processes.append(process)
        return None

    try:
        monkeypatch.setattr(validation.subprocess.Popen, "__init__", spawn)
        row = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10).error == "tool_result_unverified"
        assert len(processes) == 1 and processes[0].poll() is not None
        assert host.tool_requests(run.run_id)[0]["result"] == "unknown"
        assert len(model.requests) == 2
    finally:
        assert host.close()


@pytest.mark.parametrize("mode", ["normal", "SIGINT", "SIGTERM", "raise", "SIGKILL"])
def test_ce06_real_host_process_shutdown_and_kill_limit(tmp_path, mode):
    import os
    import select
    import signal
    import subprocess

    behavior = tmp_path / "behavior.json"
    behavior.write_text(
        json.dumps({"grandchild": True, "ignore_eof": mode == "SIGKILL"})
    )
    state = configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
    child = subprocess.Popen(
        [sys.executable, str(FIXTURE.with_name("mcp_host_fixture.py")), str(state)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pid = None
    try:
        assert select.select([child.stdout], [], [], 10)[0]
        ready = json.loads(child.stdout.readline())
        assert ready["ready"] and ready["mcp"]["servers"][0]["state"] == "connected"
        pid = int((state / "spawned").read_text())
        grandchild = int((state / "grandchild").read_text())
        if mode.startswith("SIG"):
            child.send_signal(getattr(signal, mode))
        else:
            child.stdin.write("raise\n" if mode == "raise" else "close\n")
            child.stdin.flush()
        out, err = child.communicate(timeout=12)
        if mode == "SIGKILL":
            assert child.returncode == -signal.SIGKILL
            assert '"closed": true' not in out
            os.kill(pid, 0)  # Deliberately uncooperative service remains observable.
        else:
            assert '"closed": true' in out
            for owned in (pid, grandchild):
                with pytest.raises(ProcessLookupError):
                    os.kill(owned, 0)
    finally:
        # The experiment owns these process identities; it never sweeps others.
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for stream in (child.stdin, child.stdout, child.stderr):
            if not stream.closed:
                stream.close()


def test_ce15_incomplete_cleanup_keeps_ownership_and_replay_is_read_only(
    tmp_path, monkeypatch
):
    import os
    import signal
    import uuid

    from agent_alfred.mcp import transport

    host, _, state = fixture_host(tmp_path, {"ignore_eof": True, "ignore_term": True})
    pid = int((state / "spawned").read_text())
    real_kill = transport.os.killpg

    def blocked(group, sig):
        if group == pid and sig in (signal.SIGTERM, signal.SIGKILL):
            raise PermissionError("synthetic signal denial")
        return real_kill(group, sig)

    try:
        monkeypatch.setattr(transport.os, "killpg", blocked)
        body = {
            "action": "reconnect",
            "server_key": "test",
            "operation_id": uuid.uuid4().hex,
            "token": host.connections()["mcp"]["token"],
        }
        assert host.mcp_control(body)["status"] == "running"
        assert host.wait_mcp_operation(body["operation_id"], 10)
        assert host.connections()["mcp"]["servers"][0]["reason"] == "cleanup_incomplete"
        assert not host.mutation_in_flight()
        assert host.mcp_control(body) == host.mcp_operation(body["operation_id"])
        assert int((state / "spawned").read_text()) == pid
        assert host.close(timeout=0.01) is False
        monkeypatch.setattr(transport.os, "killpg", real_kill)
        assert host.close(timeout=8) is True
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        monkeypatch.setattr(transport.os, "killpg", real_kill)
        host.close(timeout=8)


@pytest.mark.parametrize("extra,expected", [(0, None), (1, "configuration_invalid")])
def test_ce09_config_utf8_exact_limit(tmp_path, extra, expected):
    state = tmp_path / "state"
    state.mkdir()
    raw = b'{"mcpServers":{}}'
    (state / "mcp.json").write_bytes(raw + b" " * (256 * 1024 + extra - len(raw)))
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        assert host.connections()["mcp"]["error"] == expected
    finally:
        assert host.close()


@pytest.mark.parametrize("count,connected", [(100, True), (101, False)])
def test_ce12_tool_count_exact_limit(tmp_path, count, connected):
    host, _, _ = fixture_host(
        tmp_path,
        {
            "tools": [
                {"name": f"tool-{i}", "inputSchema": {"type": "object"}}
                for i in range(count)
            ]
        },
    )
    try:
        row = host.connections()["mcp"]["servers"][0]
        assert (row["state"] == "connected") == connected
        assert row["total_tools"] == (count if connected else 0)
    finally:
        assert host.close()


def test_ce12_alias_collision_routes_by_identity_not_order(tmp_path):
    from agent_alfred.messages import ToolCallBlock

    names = ["a.b", "a/b", "x" * 100 + "A", "x" * 100 + "B"]
    host, model, state = fixture_host(
        tmp_path,
        {
            "tools": [
                {"name": n, "inputSchema": {"type": "object"}} for n in reversed(names)
            ]
        },
    )
    try:
        rows = [t for t in host.tools_catalog()["tools"] if t.get("server_key")]
        assert len({t["alias"] for t in rows}) == 4
        for index, row in enumerate(rows):
            host.save_tool_authorization(
                row["identity"], "allowed", host.tools_catalog()["revision"]
            )
        # Real registry invocation is explicitly an agreed CE-02/12 seam.
        from agent_alfred.tools import ToolContext

        for index, row in enumerate(rows):
            context = ToolContext(
                f"route-{index}",
                0,
                f"call-{index}",
                "test",
                host._clock.monotonic() + 5,
            )
            result = host._tools.execute(
                ToolCallBlock(context.call_id, row["alias"], {}), context
            )
            assert not result.block.is_error
            assert (
                json.loads((state / "called").read_text())["params"]["name"]
                == row["original_name"]
            )
        old = {r["original_name"]: (r["identity"], r["alias"]) for r in rows}
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert {
            t["original_name"]: (t["identity"], t["alias"])
            for t in host.tools_catalog()["tools"]
            if t.get("server_key")
        } == old
    finally:
        assert host.close()


def test_ce05_ce12_stderr_partial_line_is_not_published(tmp_path):
    host, _, state = fixture_host(tmp_path, {"stderr_prefix": "split-secret"})
    try:
        # The external process wrote no newline: even a known credential split
        # across future reads must never become independently visible fragments.
        assert host.connections()["mcp"]["servers"][0]["diagnostics"] == []
    finally:
        assert host.close()


@pytest.mark.parametrize("count,valid", [(8, True), (9, False)])
def test_ce09_server_count_exact_limit(tmp_path, count, valid):
    state = tmp_path / "state"
    state.mkdir()
    config = {
        str(i): {"command": sys.executable, "enabled": False} for i in range(count)
    }
    (state / "mcp.json").write_text(json.dumps({"mcpServers": config}))
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        value = host.connections()["mcp"]
        assert (value["error"] is None) == valid
        assert len(value["servers"]) == (count if valid else 0)
    finally:
        assert host.close()


@pytest.mark.parametrize("count,connected", [(20, True), (21, False)])
def test_ce12_page_count_exact_limit(tmp_path, count, connected):
    pages = [{"tools": [], "nextCursor": str(i)} for i in range(count - 1)] + [
        {"tools": []}
    ]
    host, _, _ = fixture_host(tmp_path, {"pages": pages})
    try:
        assert (
            host.connections()["mcp"]["servers"][0]["state"] == "connected"
        ) == connected
    finally:
        assert host.close()


@pytest.mark.parametrize("extra,available", [(0, True), (1, False)])
def test_ce08_schema_utf8_exact_limit(tmp_path, extra, available):
    schema = {"type": "object", "description": ""}
    schema["description"] = "x" * (
        65536 + extra - len(json.dumps(schema, ensure_ascii=False).encode())
    )
    host, _, _ = fixture_host(
        tmp_path, {"tools": [{"name": "echo", "inputSchema": schema}]}
    )
    try:
        assert host.connections()["mcp"]["servers"][0]["available_tools"] == int(
            available
        )
    finally:
        assert host.close()


@pytest.mark.parametrize("extra,connected", [(0, True), (1, False)])
def test_ce12_total_tool_count_exact_limit(tmp_path, extra, connected):
    state = tmp_path / "state"
    state.mkdir()
    config = {}
    for index in range(5):
        cwd = tmp_path / f"server-{index}"
        cwd.mkdir()
        behavior = tmp_path / f"behavior-{index}.json"
        count = 80 + (extra if index == 4 else 0)
        behavior.write_text(
            json.dumps(
                {
                    "tools": [
                        {"name": str(i), "inputSchema": {"type": "object"}}
                        for i in range(count)
                    ]
                }
            )
        )
        config[str(index)] = {
            "command": sys.executable,
            "args": [str(FIXTURE), str(behavior)],
            "cwd": str(cwd),
            "enabled": True,
        }
    (state / "mcp.json").write_text(json.dumps({"mcpServers": config}))
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        rows = host.connections()["mcp"]["servers"]
        assert all(r["state"] == "connected" for r in rows[:4])
        assert (rows[4]["state"] == "connected") == connected
        assert sum(r["total_tools"] for r in rows) == (400 if connected else 320)
    finally:
        assert host.close()


def test_ce01_unauthorized_real_host_invocation_sends_zero_requests(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    host, model, state = fixture_host(
        tmp_path,
        {},
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "not authorized",
        ],
    )
    try:
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10).outcome == "completed"
        assert host.tool_requests(run.run_id)[0]["result"] == "not_executed"
        assert not (state / "called").exists()
        assert host.tool_requests(run.run_id)[0]["start_confirmation"] == "not_started"
        assert "not_authorized" in str(model.requests[-1].messages)
    finally:
        assert host.close()


def test_ce09_absolute_python_environment_is_preserved(tmp_path):
    host, _, _ = fixture_host(tmp_path, {"expected_prefix": sys.prefix})
    try:
        assert host.connections()["mcp"]["servers"][0]["state"] == "connected"
    finally:
        assert host.close()


@pytest.mark.parametrize("observed_change", [False, True])
def test_ce02_ce10_restart_reconciles_only_observed_sources(tmp_path, observed_change):
    state = configure(tmp_path, enabled=True)

    def build():
        return build_default_host(
            state_dir=state,
            factory=ScriptedModelFactory(ScriptedModel([])),
            credentials=CredentialOverlay({}, None),
        )

    host = build()
    row = next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))
    host.save_tool_authorization(
        row["identity"], "allowed", host.tools_catalog()["revision"]
    )
    assert host.close()
    behavior = tmp_path / "replacement.json"
    behavior.write_text("{}")
    configure(tmp_path, enabled=True, args=[str(FIXTURE), str(behavior)])
    if observed_change:
        host = build()
        try:
            assert (
                next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))[
                    "authorization"
                ]
                == "unset"
            )
        finally:
            assert host.close()
    configure(tmp_path, enabled=True)
    host = build()
    try:
        assert next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))[
            "authorization"
        ] == ("unset" if observed_change else "allowed")
    finally:
        assert host.close()


def test_ce10_disabled_and_json_key_order_preserve_allowance(tmp_path):
    host, _, state = fixture_host(tmp_path, {})
    try:
        row = next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        path = state / "mcp.json"
        config = json.loads(path.read_text())
        config["mcpServers"]["test"]["enabled"] = False
        path.write_text(json.dumps(config))
        assert control(host)["status"] == "completed"
        config["mcpServers"]["test"]["enabled"] = True
        config["mcpServers"]["test"] = dict(
            reversed(list(config["mcpServers"]["test"].items()))
        )
        path.write_text(json.dumps(config))
        assert control(host)["status"] == "completed"
        current = next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))
        assert (
            current["identity"] == row["identity"]
            and current["authorization"] == "allowed"
        )
    finally:
        assert host.close()


def test_ce10_retirement_write_failure_does_not_open_new_generation(
    tmp_path, monkeypatch
):
    from agent_alfred.tools import authorization

    host, _, state = fixture_host(tmp_path, {})
    try:
        row = next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        old = (state / "tool_authorizations.json").read_bytes()
        config = json.loads((state / "mcp.json").read_text())
        config["mcpServers"]["test"]["args"].append("replacement")
        (state / "mcp.json").write_text(json.dumps(config))

        def fail(*args, **kwargs):
            raise OSError("synthetic retirement IO failure")

        monkeypatch.setattr(authorization, "write_atomic", fail)
        assert control(host)["status"] == "failed"
        assert (state / "tool_authorizations.json").read_bytes() == old
        assert not (state / "called").exists()
        assert host.connections()["mcp"]["servers"][0]["state"] == "error"
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "dialect",
    [
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2020-12/schema",
    ],
)
def test_ce08_forbidden_reference_never_requests_network(tmp_path, dialect):
    from agent_alfred.evals.deterministic.test_integrations import tavily_http

    with tavily_http() as (url, requests):
        host, _, _ = fixture_host(
            tmp_path,
            {
                "tools": [
                    {
                        "name": "echo",
                        "inputSchema": {
                            "type": "object",
                            "$ref": url + "/schema",
                            "$schema": dialect,
                        },
                    }
                ]
            },
        )
        try:
            assert host.connections()["mcp"]["servers"][0]["available_tools"] == 0
            assert requests == []
        finally:
            assert host.close()


def test_ce12_stderr_flood_is_drained_and_bounded(tmp_path):
    host, _, _ = fixture_host(tmp_path, {"stderr_bytes": 2 * 1024 * 1024})
    try:
        row = host.connections()["mcp"]["servers"][0]
        assert row["state"] == "connected"
        assert len(row["diagnostics"]) <= 50
        assert sum(len(s.encode()) for s in row["diagnostics"]) <= 65536
    finally:
        assert host.close()


def test_ce09_old_host_token_is_not_replayed_after_restart(tmp_path):
    import uuid

    state = configure(tmp_path, enabled=False)

    def build():
        return build_default_host(
            state_dir=state,
            factory=ScriptedModelFactory(ScriptedModel([])),
            credentials=CredentialOverlay({}, None),
        )

    host = build()
    body = {
        "action": "apply",
        "server_key": None,
        "operation_id": uuid.uuid4().hex,
        "token": host.connections()["mcp"]["token"],
    }
    assert host.close()
    host = build()
    try:
        assert host.mcp_control(body)["error"]["code"] == "mcp_conflict"
    finally:
        assert host.close()


def test_ce14_process_environment_wins_without_restart(tmp_path):
    from agent_alfred.gateway.web.api import DashboardApi

    path = tmp_path / ".env"
    path.write_text("MCP_TOKEN=disk-old\n")
    state = configure(tmp_path, enabled=True, env={"TOKEN": "${MCP_TOKEN}"})
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({"MCP_TOKEN": "process-value"}, str(path)),
    )
    try:
        pid = (state / "spawned").read_text()
        path.write_text("MCP_TOKEN=disk-new\n")
        status, body = DashboardApi(facade=host).reread_env()
        assert status == 200 and body["mcp"]["servers"][0]["state"] == "connected"
        assert (state / "spawned").read_text() == pid
        assert (
            json.loads((state / "environment.json").read_text())["TOKEN"]
            == "process-value"
        )
    finally:
        assert host.close()


def test_ce03_late_success_after_cancel_never_restores_old_run(tmp_path, monkeypatch):
    import os
    import select

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.mcp import transport
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    event, release = tmp_path / "event", tmp_path / "release"
    os.mkfifo(event)
    os.mkfifo(release)
    event_fd = os.open(event, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    clock = FakeClock()
    host, model, state = fixture_host(
        tmp_path,
        {
            "block_call": True,
            "ignore_eof": True,
            "duplicate": True,
            "event_fifo": str(event),
            "release_fifo": str(release),
        },
        clock=clock,
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "must not run",
        ],
    )
    pid = int((state / "spawned").read_text())
    actual = transport.os.killpg

    def blocked(group, sig):
        if group == pid and sig:
            raise PermissionError("synthetic cleanup denial")
        return actual(group, sig)

    try:
        monkeypatch.setattr(transport.os, "killpg", blocked)
        row = next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert select.select([event_fd], [], [], 5)[0]
        assert b"called" in os.read(event_fd, 1024)
        clock.monotonic_value += 61
        assert select.select([event_fd], [], [], 5)[0]
        assert b"cancelled" in os.read(event_fd, 1024)
        assert host.wait(run.run_id, 10).error == "tool_result_unverified"
        before = host.tool_requests(run.run_id)
        assert host.connections()["mcp"]["servers"][0]["cleanup_incomplete"]
        os.write(release_fd, b"release\n")
        assert select.select([event_fd], [], [], 5)[0]
        assert b"late_sent" in os.read(event_fd, 1024)
        session = host._mcp.rows["test"]["session"]
        with session.condition:
            assert session.condition.wait_for(
                lambda: "late_or_duplicate_response" in session.diagnostics, timeout=5
            )
        assert host.tool_requests(run.run_id) == before and len(model.requests) == 2
        monkeypatch.setattr(transport.os, "killpg", actual)
        assert control(host, "cleanup", "test")["status"] == "completed"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert int((state / "spawned").read_text()) != pid
    finally:
        monkeypatch.setattr(transport.os, "killpg", actual)
        os.close(event_fd)
        os.close(release_fd)
        assert host.close(timeout=8)


def test_ce06_ce15_inherited_resource_is_never_killed_or_claimed_clean(tmp_path):
    import os
    import signal
    import subprocess

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
    )
    state = configure(tmp_path, enabled=True)
    (state / "mcp-resources.json").write_text(json.dumps({"test": process.pid}))
    host = build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(ScriptedModel([])),
        credentials=CredentialOverlay({}, None),
    )
    try:
        assert not (state / "spawned").exists()
        assert control(host, "cleanup", "test")["status"] == "completed"
        row = host.connections()["mcp"]["servers"][0]
        assert (
            row["state"] == "error"
            and row["reason"] == "resource_ownership_unconfirmed"
        )
        assert process.poll() is None
        assert host.close(timeout=0.1) is False
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        assert host.close(timeout=8)


@pytest.mark.parametrize("nesting,expected", [(60, "succeeded"), (61, "unknown")])
def test_ce12_protocol_depth_exact_boundary(tmp_path, nesting, expected):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.runtime.host import SubmitRequest

    value = {}
    for _ in range(nesting):
        value = {"x": value}
    host, model, _ = fixture_host(
        tmp_path,
        {"result": {"content": [], "structuredContent": {"value": value}}},
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("echo", "mcp_test_echo", {})),
            "done",
        ],
    )
    try:
        row = next(t for t in host.tools_catalog()["tools"] if t.get("server_key"))
        host.save_tool_authorization(
            row["identity"], "allowed", host.tools_catalog()["revision"]
        )
        host.start()
        run = host.submit(SubmitRequest(message="echo"))
        assert host.wait(run.run_id, 10)
        assert host.tool_requests(run.run_id)[0]["result"] == expected
    finally:
        assert host.close()


def test_ce13_discovery_validator_timeout_keeps_tool_visible_unavailable(
    tmp_path, monkeypatch
):
    import os
    import signal

    from agent_alfred.mcp import validation

    actual = validation.subprocess.Popen.__init__
    processes = []

    def spawn(process, *args, **kwargs):
        actual(process, *args, **kwargs)
        if args[0][-1].endswith("schema_worker.py"):
            os.kill(process.pid, signal.SIGSTOP)
            processes.append(process)
        return None

    monkeypatch.setattr(validation.subprocess.Popen, "__init__", spawn)
    host, _, _ = fixture_host(tmp_path, {})
    try:
        row = host.connections()["mcp"]["servers"][0]
        assert (
            row["state"] == "connected"
            and row["total_tools"] == 1
            and row["available_tools"] == 0
        )
        assert len(processes) == 1 and processes[0].poll() is not None
    finally:
        assert host.close()
