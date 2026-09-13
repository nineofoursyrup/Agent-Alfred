"""Review regressions retain real Host, native resources and public operations."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_alfred.connections import CredentialOverlay
from agent_alfred.evals.deterministic.test_mcp import FIXTURE, control, fixture_host
from agent_alfred.evals.deterministic.test_runtime_tools import calls
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.messages import ToolCallBlock
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.wiring import build_default_host


def build(state, script=(), credentials=None):
    model = ScriptedModel(list(script))
    return build_default_host(
        state_dir=state,
        factory=ScriptedModelFactory(model),
        credentials=credentials or CredentialOverlay({}, None),
    ), model


def tool(host, server="test"):
    return next(
        t
        for t in host.tools_catalog()["tools"]
        if t.get("server_key") == server and not t.get("historical")
    )


def allow(host, server="test"):
    row = tool(host, server)
    assert "error" not in host.save_tool_authorization(
        row["identity"], "allowed", host.tools_catalog()["revision"]
    )
    return row["identity"]


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("target", ["server", "validator"])
def test_std01_real_popen_return_interruption_is_owned(tmp_path, exception, target):
    from agent_alfred.mcp.validation import Validator

    state = tmp_path / "state"
    state.mkdir()
    (state / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test": {
                        "command": sys.executable,
                        "args": [str(FIXTURE)],
                        "enabled": True,
                    }
                }
            }
        )
    )
    seen = []
    validator = Validator()
    init = subprocess.Popen.__init__.__code__

    def trace(frame, event, arg):
        if frame.f_code is init and event == "return":
            process = frame.f_locals["self"]
            matched = (
                str(FIXTURE) in process.args
                if target == "server"
                else any(str(a).endswith("schema_worker.py") for a in process.args)
            )
            if matched:
                seen.append(process)
                raise exception("native process already created")
        return trace

    try:
        with pytest.raises(exception):
            try:
                sys.settrace(trace)
                if target == "server":
                    build(state)
                else:
                    validator.validate({"type": "object"}, time.monotonic() + 5)
            finally:
                sys.settrace(None)
        assert len(seen) == 1
        if target == "validator":
            assert validator.process is seen[0]
            assert validator.close() and validator.close()
        else:
            assert json.loads((state / "mcp-resources.json").read_text()) == {}
        assert seen[0].poll() is not None
        assert all(
            p is None or p.closed
            for p in (seen[0].stdin, seen[0].stdout, seen[0].stderr)
        )
    finally:
        sys.settrace(None)
        for process in seen:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe:
                    pipe.close()


def test_std02_native_control_thread_holds_gate_before_ident(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    host, _ = build(state)
    hold, entered = threading.Event(), threading.Event()
    bootstrap = threading.Thread._bootstrap_inner

    def gated(thread):
        if thread.name == "mcp-control":
            entered.set()
            assert hold.wait(10)
        return bootstrap(thread)

    def trace(frame, event, arg):
        if frame.f_code is threading.Thread.start.__code__ and event == "line":
            thread = frame.f_locals["self"]
            if (
                thread.name == "mcp-control"
                and thread._os_thread_handle.ident
                and thread.ident is None
            ):
                raise KeyboardInterrupt("native start before Python ident")
        return trace

    body = dict(
        action="apply",
        operation_id="native-start",
        server_key=None,
        token=host.connections()["mcp"]["token"],
    )
    try:
        monkeypatch.setattr(threading.Thread, "_bootstrap_inner", gated)
        with pytest.raises(KeyboardInterrupt):
            try:
                sys.settrace(trace)
                host.mcp_control(body)
            finally:
                sys.settrace(None)
        assert entered.wait(5)
        assert host.mcp_control(body)["status"] == "running"
        assert host.try_begin_mutation() == "mutation_in_flight"
        host.start()
        assert (
            host.submit(SubmitRequest(message="blocked")).kind == "mutation_in_flight"
        )
        assert not host._mcp_control.close(time.monotonic())
        hold.set()
        assert host.wait_mcp_operation("native-start", 10)
        assert host.mcp_operation("native-start")["status"] == "completed"
    finally:
        hold.set()
        sys.settrace(None)
        assert host.close()


@pytest.mark.parametrize(
    "bad",
    [
        "{bad",
        '{"mcpServers":{"test":{"command":"missing-executable-review"}}}',
        '{"mcpServers":{"test":{"command":"/bin/cat","env":{"TOKEN":"${MISSING_REVIEW}"}}}}',
    ],
)
def test_spec01_ce09_invalid_startup_has_explicit_recovery(tmp_path, bad):
    state = tmp_path / "state"
    state.mkdir()
    (state / "mcp.json").write_text(bad)
    host, _ = build(state)
    try:
        assert host.connections()["mcp"]["error"] == "configuration_invalid"
        assert not (state / "spawned").exists()
        (state / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "test": {
                            "command": sys.executable,
                            "args": [str(FIXTURE)],
                            "enabled": True,
                        }
                    }
                }
            )
        )
        token = host.connections()["mcp"]["token"]
        result = control(host, operation_id="repair", token=token)
        assert result["status"] == "completed"
        pid = (state / "spawned").read_text()
        assert tool(host)["authorization"] == "unset"
        assert control(host, operation_id="repair", token=token) == result
        assert (state / "spawned").read_text() == pid
    finally:
        assert host.close()


@pytest.mark.parametrize("mode", ["shared", "distinct", "other_missing"])
def test_spec02_ce14_reconnect_only_selected_server(tmp_path, mode):
    state = tmp_path / "state"
    state.mkdir()
    env = tmp_path / ".env"
    env.write_text("A=old\nB=old\n")
    config = {}
    for key in ("alpha", "beta"):
        cwd = tmp_path / key
        cwd.mkdir()
        config[key] = {
            "command": sys.executable,
            "args": [str(FIXTURE)],
            "cwd": str(cwd),
            "enabled": True,
            "env": {"TOKEN": "${A}" if key == "alpha" or mode == "shared" else "${B}"},
        }
    (state / "mcp.json").write_text(json.dumps({"mcpServers": config}))
    host, _ = build(state, credentials=CredentialOverlay({}, str(env)))
    try:
        allow(host, "alpha")
        allow(host, "beta")
        before = {k: (tmp_path / k / "spawned").read_text() for k in config}
        env.write_text("A=new\n" + ("" if mode == "other_missing" else "B=new\n"))
        assert DashboardApi(facade=host).reread_env()[0] == 200
        assert control(host, "reconnect", "alpha")["status"] == "completed"
        assert (tmp_path / "alpha" / "spawned").read_text() != before["alpha"]
        assert (tmp_path / "beta" / "spawned").read_text() == before["beta"]
        rows = {r["server_key"]: r for r in host.connections()["mcp"]["servers"]}
        assert rows["alpha"]["state"] == "connected"
        assert rows["beta"]["reason"] == "restart_required"
        assert tool(host, "alpha")["authorization"] == "unset"
        assert tool(host, "beta")["exposure"] == "hidden"
        assert not (tmp_path / "beta" / "called").exists()
    finally:
        assert host.close()


def test_spec03_ce03_unknown_hides_from_next_real_model_request(tmp_path):
    host, model, state = fixture_host(
        tmp_path,
        {"result": {"content": [], "isError": 1}},
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("first", "mcp_test_echo", {})),
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            calls(ToolCallBlock("second", "mcp_test_echo", {})),
            "done",
        ],
    )
    try:
        allow(host)
        host.start()
        first = host.submit(SubmitRequest(message="one"))
        assert host.wait(first.run_id, 10).error == "tool_result_unverified"
        sent = (state / "called").read_text()
        second = host.submit(SubmitRequest(message="two"))
        assert host.wait(second.run_id, 10)
        assert all(t.name != "mcp_test_echo" for t in model.requests[-1].tools)
        assert tool(host)["exposure"] == "hidden"
        assert host.tool_requests(second.run_id)[0]["result"] == "not_executed"
        assert (state / "called").read_text() == sent
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert tool(host)["exposure"] == "real"
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "uri,available",
    [
        ("https://json-schema.org/draft/2020-12/vocab/validation", True),
        ("https://json-schema.org/draft/2020-12/vocab/not-standard", False),
        ("https://example.invalid/custom", False),
    ],
)
@pytest.mark.parametrize("required", [True, False])
def test_spec04_ce08_vocabulary_exact_support(tmp_path, uri, available, required):
    host, _, _ = fixture_host(
        tmp_path,
        {
            "tools": [
                {
                    "name": "echo",
                    "inputSchema": {"type": "object", "$vocabulary": {uri: required}},
                },
                {"name": "healthy", "inputSchema": {"type": "object"}},
            ]
        },
    )
    try:
        row = host.connections()["mcp"]["servers"][0]
        assert row["available_tools"] == 1 + available
        echo = next(
            t for t in host.tools_catalog()["tools"] if t["name"] == "mcp_test_echo"
        )
        assert (echo["availability"] == "configured") is available
    finally:
        assert host.close()


@pytest.mark.parametrize(
    "change",
    ["description", "inputSchema", "outputSchema", "empty", "removed", "failed"],
)
def test_ce10_complete_observation_retires_but_failure_preserves(tmp_path, change):
    declaration = {
        "name": "echo",
        "description": "original",
        "inputSchema": {"type": "object"},
    }
    behavior = {"tools": [declaration]}
    host, _, state = fixture_host(tmp_path, behavior)
    path = tmp_path / "behavior.json"
    config = (state / "mcp.json").read_text()
    try:
        identity = allow(host)
        changed = json.loads(json.dumps(behavior))
        if change in ("inputSchema", "outputSchema"):
            changed["tools"][0][change] = {
                "type": "object",
                "properties": {"x": {"type": "string"}},
            }
        elif change == "description":
            changed["tools"][0]["description"] = "changed"
        elif change == "empty":
            changed["tools"] = []
        elif change == "failed":
            changed["pages"] = [{"tools": [declaration], "nextCursor": "same"}]
        if change == "removed":
            (state / "mcp.json").write_text('{"mcpServers":{}}')
            assert control(host)["status"] == "completed"
            (state / "mcp.json").write_text(config)
        else:
            path.write_text(json.dumps(changed))
            assert control(host, "reconnect", "test")["status"] == "completed"
        path.write_text(json.dumps(behavior))
        assert (
            control(
                host,
                "apply" if change == "removed" else "reconnect",
                None if change == "removed" else "test",
            )["status"]
            == "completed"
        )
        row = tool(host)
        assert row["identity"] == identity
        assert row["authorization"] == ("allowed" if change == "failed" else "unset")
        persisted = json.loads((state / "tool_authorizations.json").read_text())
        assert persisted["mcp_active"]
    finally:
        assert host.close()


@pytest.mark.parametrize("change", ["invalid", "rotation"])
def test_ce10_key_change_never_inherits_allowance_and_recovers(tmp_path, change):
    host, _, state = fixture_host(tmp_path, {})
    try:
        original = allow(host)
        key = json.loads((state / "mcp.key").read_text())
        if change == "invalid":
            (state / "mcp.key").write_text("invalid")
            assert control(host, "reconnect", "test")["status"] == "failed"
            assert tool(host)["exposure"] == "hidden"
        key["key"] = "22" * 32
        key["key_id"] = "new-generation"
        (state / "mcp.key").write_text(json.dumps(key))
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert tool(host)["identity"] != original
        assert tool(host)["authorization"] == "unset"
    finally:
        assert host.close()


def test_ce12_short_hash_collision_alias_shift_and_list_changed(tmp_path, monkeypatch):
    import agent_alfred.mcp as bridge_module
    from agent_alfred.tools import ToolContext

    real = bridge_module.hashlib.sha256
    prefix = "x" * 100

    class Digest:
        def hexdigest(self):
            return "0" * 64

    def collide(raw=b"", *args, **kwargs):
        # Only the explicit alias hash boundary; identity HMAC stays real.
        return (
            Digest()
            if raw.startswith(("test_" + prefix).encode())
            else real(raw, *args, **kwargs)
        )

    monkeypatch.setattr(bridge_module.hashlib, "sha256", collide)
    host, _, state = fixture_host(
        tmp_path,
        {
            "tools": [
                {"name": prefix + "B", "inputSchema": {"type": "object"}},
                {"name": prefix + "C", "inputSchema": {"type": "object"}},
            ],
            "list_changed": True,
        },
    )
    try:
        before = {
            t["original_name"]: t
            for t in host.tools_catalog()["tools"]
            if t.get("server_key")
        }
        for row in before.values():
            host.save_tool_authorization(
                row["identity"], "allowed", host.tools_catalog()["revision"]
            )
        assert len({t["alias"] for t in before.values()}) == 2
        path = tmp_path / "behavior.json"
        path.write_text(
            json.dumps(
                {
                    "tools": [
                        {"name": prefix + n, "inputSchema": {"type": "object"}}
                        for n in ("C", "A", "B")
                    ],
                    "list_changed": True,
                }
            )
        )
        # Notification must not rediscover the edited file or change permission.
        assert (
            len([t for t in host.tools_catalog()["tools"] if t.get("server_key")]) == 2
        )
        assert control(host, "reconnect", "test")["status"] == "completed"
        after = [t for t in host.tools_catalog()["tools"] if t.get("server_key")]
        assert len({t["alias"] for t in after}) == 3
        for index, row in enumerate(after):
            if row["original_name"] in before:
                old = before[row["original_name"]]
                assert row["alias"] != old["alias"]
                assert (
                    row["identity"] == old["identity"]
                    and row["authorization"] == "allowed"
                )
                context = ToolContext(
                    f"collision-{index}",
                    0,
                    str(index),
                    "test",
                    host._clock.monotonic() + 5,
                )
                assert not host._tools.execute(
                    ToolCallBlock(str(index), row["alias"], {}), context
                ).block.is_error
                assert (
                    json.loads((state / "called").read_text())["params"]["name"]
                    == row["original_name"]
                )
            else:
                assert row["authorization"] == "unset"
        snapshot = {t["original_name"]: (t["alias"], t["identity"]) for t in after}
        data = json.loads(path.read_text())
        data["tools"].reverse()
        path.write_text(json.dumps(data))
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert {
            t["original_name"]: (t["alias"], t["identity"])
            for t in host.tools_catalog()["tools"]
            if t.get("server_key")
        } == snapshot
        # Round trip to the server orders the earlier list_changed before observation.
        row = next(t for t in after if t["authorization"] == "allowed")
        context = ToolContext(
            "notification", 0, "notification", "test", host._clock.monotonic() + 5
        )
        host._tools.execute(ToolCallBlock(context.call_id, row["alias"], {}), context)
        assert host.connections()["mcp"]["servers"][0]["directory_changed"]
    finally:
        assert host.close()


def test_ce15_continue_cleanup_releases_gate_and_permits_core(tmp_path, monkeypatch):
    from agent_alfred.mcp import transport

    host, _, state = fixture_host(
        tmp_path,
        {"ignore_eof": True, "ignore_term": True},
        script=[
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            "core remains",
        ],
    )
    pid = int((state / "spawned").read_text())
    real = transport.os.killpg

    def deny(group, sig):
        if group == pid and sig in (signal.SIGTERM, signal.SIGKILL):
            raise PermissionError("controlled cleanup denial")
        return real(group, sig)

    try:
        monkeypatch.setattr(transport.os, "killpg", deny)
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert host.connections()["mcp"]["servers"][0]["reason"] == "cleanup_incomplete"
        assert not host.mutation_in_flight()
        host.start()
        run = host.submit(SubmitRequest(message="core"))
        assert host.wait(run.run_id, 10).error is None
        monkeypatch.setattr(transport.os, "killpg", real)
        assert control(host, "cleanup", "test")["status"] == "completed"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert (state / "spawned").read_text() == str(pid)
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert int((state / "spawned").read_text()) != pid
    finally:
        monkeypatch.setattr(transport.os, "killpg", real)
        assert host.close()


def test_ce09_ce15_operation_expiry_and_capacity_do_not_replay(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_alfred.runtime import mcp_control

    host, _ = build(tmp_path / "state")
    now = [time.monotonic()]
    monkeypatch.setattr(mcp_control, "time", SimpleNamespace(monotonic=lambda: now[0]))
    first_token = host.connections()["mcp"]["token"]
    try:
        for i in range(257):
            assert control(host, operation_id=f"op-{i}")["status"] == "completed"
        assert host.mcp_operation("op-0")["error"]["code"] == "operation_not_found"
        assert host.mcp_operation("op-256")["status"] == "completed"
        assert (
            control(host, operation_id="op-0", token=first_token)["error"]["code"]
            == "mcp_conflict"
        )
        token = host.connections()["mcp"]["token"]
        now[0] += 601
        assert host.mcp_operation("op-256")["error"]["code"] == "operation_not_found"
        assert (
            control(host, operation_id="op-256", token=token)["error"]["code"]
            == "mcp_conflict"
        )
    finally:
        assert host.close()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_ce14_environment_publication_failure_keeps_consistent_mcp(
    tmp_path, monkeypatch, rollback_fails
):
    from agent_alfred.mcp import MCPBridge

    state = tmp_path / "state"
    state.mkdir()
    env = tmp_path / ".env"
    env.write_text("A=old\n")
    (state / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test": {
                        "command": sys.executable,
                        "args": [str(FIXTURE)],
                        "enabled": True,
                        "env": {"TOKEN": "${A}"},
                    }
                }
            }
        )
    )
    host, _ = build(state, credentials=CredentialOverlay({}, str(env)))
    original = MCPBridge.publish_env

    def interrupted(self, values, affected):
        original(self, values, affected)
        raise OSError("after MCP environment publication")

    def denied_restore(*args):
        raise OSError("controlled rollback failure")

    try:
        allow(host)
        pid = (state / "spawned").read_text()
        env.write_text("A=new\n")
        monkeypatch.setattr(MCPBridge, "publish_env", interrupted)
        if rollback_fails:
            monkeypatch.setattr(host._integrations, "restore", denied_restore)
        status, _ = DashboardApi(facade=host).reread_env()
        assert status != 200
        assert (state / "spawned").read_text() == pid
        if rollback_fails:
            assert tool(host)["exposure"] == "hidden"
        else:
            assert tool(host)["exposure"] == "real"
            assert host.connections()["mcp"]["servers"][0]["state"] == "connected"
            assert host._mcp.env["A"] == "old"
    finally:
        assert host.close()


def test_ce13_unreaped_validator_prevents_replacement_job(tmp_path, monkeypatch):
    from agent_alfred.mcp import validation

    actual_init = validation.subprocess.Popen.__init__
    actual_kill = os.kill
    jobs = []

    def initialize(process, *args, **kwargs):
        actual_init(process, *args, **kwargs)
        if str(args[0][-1]).endswith("schema_worker.py"):
            actual_kill(process.pid, signal.SIGSTOP)
            jobs.append(process)

    def deny(pid, sig):
        if any(p.pid == pid for p in jobs) and sig == signal.SIGKILL:
            raise PermissionError("controlled validator kill denial")
        return actual_kill(pid, sig)

    monkeypatch.setattr(validation.subprocess.Popen, "__init__", initialize)
    monkeypatch.setattr(os, "kill", deny)
    host = None
    try:
        host, _, _ = fixture_host(
            tmp_path,
            {
                "tools": [
                    {"name": "one", "inputSchema": {"type": "object"}},
                    {
                        "name": "two",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"x": {"type": "string"}},
                        },
                    },
                ]
            },
        )
        assert len(jobs) == 1 and jobs[0].poll() is None
        assert host.connections()["mcp"]["servers"][0]["available_tools"] == 0
        assert control(host, "reconnect", "test")["status"] == "completed"
        assert len(jobs) == 1
        assert host.close(timeout=1) is False
    finally:
        monkeypatch.setattr(os, "kill", actual_kill)
        if host:
            assert host.close()
        for process in jobs:
            assert process.poll() is not None


@pytest.mark.parametrize(
    "dialect",
    [
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2020-12/schema",
    ],
)
def test_ce08_real_validator_file_reference_performs_zero_reads(
    tmp_path, monkeypatch, dialect
):
    from agent_alfred.mcp import validation

    source = tmp_path / "private-schema.json"
    source.write_text('{"type":"object"}')
    audit = tmp_path / "opens.log"
    wrapper = tmp_path / "observe_worker.py"
    worker = Path(validation.__file__).with_name("schema_worker.py")
    wrapper.write_text(
        "import sys,os,runpy\n"
        f"fd=os.open({str(audit)!r},os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)\n"
        "def observe(event,args):\n"
        f"    if event=='open' and args[0]=={str(source)!r}:\n"
        "        os.write(fd,b'open\\n')\n"
        "sys.addaudithook(observe)\n"
        f"with open({str(source)!r}) as positive_control: positive_control.read()\n"
        f"runpy.run_path({str(worker)!r},run_name='__main__')\n"
    )
    actual = validation.subprocess.Popen.__init__

    def initialize(process, args, **kwargs):
        if str(args[-1]).endswith("schema_worker.py"):
            args = [*args[:-1], str(wrapper)]
        actual(process, args, **kwargs)

    monkeypatch.setattr(validation.subprocess.Popen, "__init__", initialize)
    host, _, _ = fixture_host(
        tmp_path,
        {
            "tools": [
                {
                    "name": "echo",
                    "inputSchema": {
                        "type": "object",
                        "$ref": source.as_uri(),
                        "$schema": dialect,
                    },
                }
            ]
        },
    )
    try:
        assert host.connections()["mcp"]["servers"][0]["available_tools"] == 0
        assert audit.read_text() == "open\n"  # Only the positive control, no retrieval.
    finally:
        assert host.close()


def test_ce04_ce12_startup_deadlines_isolate_and_mark_unattempted(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    behavior = tmp_path / "behavior.json"
    behavior.write_text('{"block_initialize":true}')
    rows = {}
    for key in ("a", "b", "c", "d"):
        cwd = tmp_path / key
        cwd.mkdir()
        rows[key] = {
            "command": sys.executable,
            "args": [str(FIXTURE), str(behavior)],
            "cwd": str(cwd),
            "enabled": True,
        }
    (state / "mcp.json").write_text(json.dumps({"mcpServers": rows}))
    started = time.monotonic()
    host, _ = build(state)
    try:
        elapsed = time.monotonic() - started
        observed = host.connections()["mcp"]["servers"]
        assert 29 <= elapsed < 38
        assert [r["reason"] for r in observed[:3]] == ["deadline"] * 3
        assert observed[3]["reason"] == "not_attempted"
        assert not (tmp_path / "d" / "spawned").exists()
        for key in ("a", "b", "c"):
            pid = int((tmp_path / key / "spawned").read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        assert host.close()


def test_std02_start_failure_without_native_effect_releases_gate(tmp_path, monkeypatch):
    host, _ = build(tmp_path / "state")
    actual = threading.Thread.start

    def fail(thread):
        if thread.name == "mcp-control":
            raise RuntimeError("before native start")
        return actual(thread)

    body = dict(
        action="apply",
        operation_id="no-start",
        server_key=None,
        token=host.connections()["mcp"]["token"],
    )
    try:
        monkeypatch.setattr(threading.Thread, "start", fail)
        with pytest.raises(RuntimeError):
            host.mcp_control(body)
        assert host.mcp_operation("no-start")["status"] == "failed"
        assert host.mcp_control(body)["status"] == "failed"
        assert not host.mutation_in_flight()
    finally:
        assert host.close()


def test_ce04_partial_server_with_descendant_does_not_disable_healthy_server(tmp_path):
    from agent_alfred.tools import ToolContext

    state = tmp_path / "state"
    state.mkdir()
    rows = {}
    for key in ("alpha", "beta"):
        cwd = tmp_path / key
        cwd.mkdir()
        behavior = cwd / "behavior.json"
        behavior.write_text(
            json.dumps(
                {}
                if key == "alpha"
                else {
                    "grandchild": True,
                    "pages": [
                        {
                            "tools": [
                                {"name": "partial", "inputSchema": {"type": "object"}}
                            ],
                            "nextCursor": "repeat",
                        }
                    ],
                }
            )
        )
        rows[key] = {
            "command": sys.executable,
            "args": [str(FIXTURE), str(behavior)],
            "cwd": str(cwd),
            "enabled": True,
        }
    (state / "mcp.json").write_text(json.dumps({"mcpServers": rows}))
    host, _ = build(state)
    try:
        statuses = {r["server_key"]: r for r in host.connections()["mcp"]["servers"]}
        assert statuses["alpha"]["state"] == "connected"
        assert statuses["beta"]["state"] == "error"
        assert statuses["beta"]["total_tools"] == 0
        for name in ("spawned", "grandchild"):
            with pytest.raises(ProcessLookupError):
                os.kill(int((tmp_path / "beta" / name).read_text()), 0)
        allow(host, "alpha")
        context = ToolContext(
            "healthy", 0, "healthy", "test", host._clock.monotonic() + 5
        )
        assert not host._tools.execute(
            ToolCallBlock("healthy", "mcp_alpha_echo", {}), context
        ).block.is_error
        assert (tmp_path / "alpha" / "called").exists()
        assert not (tmp_path / "beta" / "called").exists()
    finally:
        assert host.close()


@pytest.mark.parametrize("target", ["server", "validator"])
@pytest.mark.parametrize("partial", [False, True])
def test_std03_real_emfile_before_or_after_pipe_creation_recovers(
    tmp_path, monkeypatch, target, partial
):
    import fcntl
    import resource

    from agent_alfred.mcp.validation import Validator

    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    initialized, restored = False, False
    active = False
    pipes, processes = [], []
    actual_pipe = os.pipe
    init_code = subprocess.Popen.__init__.__code__
    validator = Validator()
    host = None
    state = tmp_path / "state"
    state.mkdir()
    (state / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test": {
                        "command": sys.executable,
                        "args": [str(FIXTURE)],
                        "enabled": True,
                    }
                }
            }
        )
    )

    def pipe():
        result = actual_pipe()
        if active and partial:
            pipes.extend(result)
            resource.setrlimit(resource.RLIMIT_NOFILE, (0, limits[1]))
        return result

    def trace(frame, event, arg):
        nonlocal active, initialized, restored
        if frame.f_code is init_code:
            if event == "call" and not initialized:
                args = frame.f_locals["args"]
                matches = (
                    str(FIXTURE) in args
                    if target == "server"
                    else str(args[-1]).endswith("schema_worker.py")
                )
                if matches:
                    processes.append(frame.f_locals["self"])
                    active = initialized = True
                    if not partial:
                        resource.setrlimit(resource.RLIMIT_NOFILE, (0, limits[1]))
            elif event == "exception" and active:
                resource.setrlimit(resource.RLIMIT_NOFILE, limits)
                active = False
                restored = True
        return trace

    monkeypatch.setattr(os, "pipe", pipe)
    try:
        try:
            sys.settrace(trace)
            if target == "server":
                host, _ = build(
                    state,
                    [
                        '{"retrieve":false,"query":null,"reason_code":"greeting"}',
                        "core ready",
                    ],
                )
            else:
                assert (
                    validator.validate({"type": "object"}, time.monotonic() + 5)
                    == "validation_failed"
                )
        finally:
            sys.settrace(None)
            resource.setrlimit(resource.RLIMIT_NOFILE, limits)
        assert initialized and restored
        assert processes[0].pid is None and not processes[0]._child_created
        for fd in pipes:
            with pytest.raises(OSError):
                fcntl.fcntl(fd, fcntl.F_GETFD)
        if target == "server":
            assert host.connections()["mcp"]["servers"][0]["state"] == "error"
            host.start()
            run = host.submit(SubmitRequest(message="core"))
            assert host.wait(run.run_id, 10).error is None
            assert control(host, "reconnect", "test")["status"] == "completed"
            assert tool(host)["connection"] == "connected"
            assert host.close() and host.close()
        else:
            assert validator.close() and validator.close()
            assert validator.validate({"type": "object"}, time.monotonic() + 5) is None
    finally:
        sys.settrace(None)
        resource.setrlimit(resource.RLIMIT_NOFILE, limits)
        if host:
            host.close()
        validator.close()


def test_spec07_ce09_invalid_then_identical_apply_keeps_healthy_pid(tmp_path):
    host, _, state = fixture_host(tmp_path, {})
    try:
        identity = allow(host)
        original = (state / "mcp.json").read_bytes()
        pid = (state / "spawned").read_text()
        (state / "mcp.json").write_text("{bad")
        assert control(host)["error"]["code"] == "configuration_invalid"
        (state / "mcp.json").write_bytes(original)
        assert control(host)["status"] == "completed"
        assert (state / "spawned").read_text() == pid
        assert (
            tool(host)["identity"] == identity
            and tool(host)["authorization"] == "allowed"
        )
    finally:
        assert host.close()


def test_spec08_ce14_cleanup_completion_clears_residual_flag(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    env = tmp_path / ".env"
    env.write_text("TOKEN=old\n")
    behavior = tmp_path / "behavior.json"
    behavior.write_text('{"ignore_eof":true,"ignore_term":true}')
    (state / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test": {
                        "command": sys.executable,
                        "args": [str(FIXTURE), str(behavior)],
                        "enabled": True,
                        "env": {"TOKEN": "${TOKEN}"},
                    }
                }
            }
        )
    )
    host, _ = build(state, credentials=CredentialOverlay({}, str(env)))
    pid = int((state / "spawned").read_text())
    actual = os.killpg

    def deny(group, sig):
        if group == pid and sig in (signal.SIGTERM, signal.SIGKILL):
            raise PermissionError("controlled cleanup failure")
        return actual(group, sig)

    try:
        monkeypatch.setattr(os, "killpg", deny)
        env.write_text("TOKEN=new\n")
        assert DashboardApi(facade=host).reread_env()[0] == 200
        assert host.connections()["mcp"]["servers"][0]["cleanup_incomplete"]
        monkeypatch.setattr(os, "killpg", actual)
        assert control(host, "cleanup", "test")["status"] == "completed"
        row = host.connections()["mcp"]["servers"][0]
        assert not row["cleanup_incomplete"]
        assert row["reason"] == "reconnect_required"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        monkeypatch.setattr(os, "killpg", actual)
        assert host.close()
