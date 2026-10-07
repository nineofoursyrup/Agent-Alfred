"""Probe protocol tests use temporary sentinels and fake sockets, never macOS setup."""

import dis
import errno
import hashlib
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import native_probe as probe
from agent_alfred.evals.acceptance.schema import encode


def test_fork_probe_binds_actual_child_and_reaps_without_exec():
    result = probe._fork_probe(lambda: {"pid": os.getpid()}, timeout=2)
    assert result["pid"] != os.getpid()
    with pytest.raises(ChildProcessError):
        os.waitpid(result["pid"], os.WNOHANG)


def test_nested_fork_probe_preserves_pipe_when_stdio_fds_are_reused():
    def outer():
        inner = probe._fork_probe(lambda: {"pid": os.getpid()}, timeout=2)
        with pytest.raises(ChildProcessError):
            os.waitpid(inner["pid"], os.WNOHANG)
        return {"pid": os.getpid(), "inner_pid": inner["pid"]}

    result = probe._fork_probe(outer, timeout=4)
    assert len({os.getpid(), result["pid"], result["inner_pid"]}) == 3
    with pytest.raises(ChildProcessError):
        os.waitpid(result["pid"], os.WNOHANG)


def test_fork_probe_timeout_reaps_only_its_child(monkeypatch):
    original = os.fork
    children = []

    def remember():
        pid = original()
        if pid:
            children.append(pid)
        return pid

    monkeypatch.setattr(probe.os, "fork", remember)
    with pytest.raises(TimeoutError, match="local_frame_deadline"):
        probe._fork_probe(lambda: probe.sleep(2), timeout=0.05)
    assert len(children) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0], os.WNOHANG)


def test_fork_probe_rejects_a_forged_child_pid():
    parent = os.getpid()
    with pytest.raises(ValueError, match="native_probe_child_mismatch"):
        probe._fork_probe(lambda: {"pid": parent}, timeout=2)


def test_fork_return_interruption_keeps_the_child_and_pipes_owned(monkeypatch):
    original = probe.capture_call_result
    owners = []

    def interrupted(owner, name, operation):
        original(owner, name, operation)
        if name == "pid" and owner.pid > 0:
            owners.append(owner)
            raise KeyboardInterrupt("fork return edge")

    monkeypatch.setattr(probe, "capture_call_result", interrupted)
    with pytest.raises(KeyboardInterrupt, match="fork return edge"):
        probe._fork_probe(lambda: {"pid": os.getpid()}, timeout=2)
    owner = owners[0]
    with pytest.raises(ChildProcessError):
        os.waitpid(owner.pid, os.WNOHANG)
    for descriptor in owner.pipe:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_fork_probe_unknown_signal_is_retained_and_never_repeated(monkeypatch):
    owner = probe._ForkedProbe()
    owner.pid = 987654  # only fake wait/kill seams below
    calls = []
    monkeypatch.setattr(probe.os, "waitpid", lambda *args: (0, 0))

    def unknown(*args):
        calls.append(args)
        raise OSError("signal outcome unknown")

    monkeypatch.setattr(probe.os, "kill", unknown)
    with pytest.raises(OSError, match="outcome unknown"):
        owner.close()
    with pytest.raises(RuntimeError, match="signal_outcome_unknown"):
        owner.close()
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["close_end", "close"])
def test_cleanup_interruption_before_an_effect_remains_retryable(
    monkeypatch, operation
):
    method = getattr(probe._ForkedProbe, operation)
    interrupted_count = 0
    for _, line in dis.findlinestarts(method.__code__):
        owner = probe._ForkedProbe()
        effects = []
        if operation == "close_end":
            owner.pipe = (987652, 987653)
            monkeypatch.setattr(probe.os, "close", lambda fd: effects.append(fd))

            def action():
                owner.close_end(0)
        else:
            owner.pid = 987654
            monkeypatch.setattr(probe.os, "kill", lambda *args: effects.append(args))
            monkeypatch.setattr(
                probe.os, "waitpid", lambda *args: (owner.pid, 0) if effects else (0, 0)
            )
            action = owner.close

        def trace(frame, event, arg):
            if frame.f_code is method.__code__ and event == "line":
                if frame.f_lineno == line:
                    raise KeyboardInterrupt("before effect edge")
            return trace

        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            action()
        except KeyboardInterrupt:
            interrupted_count += 1
        finally:
            sys.settrace(previous)
        action()
        assert len(effects) == 1
    assert interrupted_count > 0


def request(tmp_path):
    root = tmp_path / ("alfred-local-probe-sentinels-" + "a" * 32)
    root.mkdir()
    expected = {}
    for name in probe.SENTINELS:
        value = f"no-value offline sentinel {name}".encode()
        (root / name).write_bytes(value)
        expected[name] = hashlib.sha256(value).hexdigest()
    return {
        "contract": probe.PROBE_CONTRACT,
        "version": 1,
        "nonce": "b" * 64,
        "sentinel_root": str(root),
        "expected_sha256": expected,
        "ipv4_port": 19001,
        "ipv6_port": 19002,
    }


def test_probe_does_not_turn_missing_file_or_refused_socket_into_isolation_pass(
    tmp_path, monkeypatch
):
    value = request(tmp_path)
    (Path(value["sentinel_root"]) / "other-case.txt").unlink()

    def denied_socket(family, kind):
        assert kind == socket.SOCK_STREAM
        code = errno.EACCES if family == socket.AF_INET else errno.ECONNREFUSED
        raise OSError(code, "safe offline socket fixture")

    monkeypatch.setattr(probe.socket, "socket", denied_socket)
    result = probe.collect_probe(value)
    assert result["actual_isolation"] == "REQUIRES_HOST_CORROBORATION"
    rows = {(row["target"], row["action"]): row for row in result["observations"]}
    assert rows["owner.txt", "read"]["status"] == "unexpected_allowed"
    assert rows["owner.txt", "write"]["status"] == "unexpected_allowed"
    assert rows["other-case.txt", "read"]["status"] == "unverified"
    assert rows["ipv4_loopback_tcp", "connect"]["status"] == "denied"
    assert rows["ipv6_loopback_tcp", "connect"]["status"] == "unverified"
    assert b"no-value offline sentinel" not in encode(result)
    assert "sentinel_root" not in result


@pytest.mark.parametrize(
    "change",
    [
        {"sentinel_root": "/Users/owner/.ssh"},
        {"ipv4_port": 443},
        {"ipv6_port": True},
        {"command": "arbitrary execution is not a probe"},
        {"expected_sha256": {}},
        {"version": True},
    ],
)
def test_probe_rejects_unscoped_files_commands_and_ports_before_any_io(
    tmp_path, change
):
    value = request(tmp_path)
    with pytest.raises(ValueError, match="native_probe"):
        probe.validate_probe_request({**value, **change})


def test_product_bundle_cannot_select_probe_entrypoint(tmp_path, monkeypatch):
    value = request(tmp_path)
    monkeypatch.setattr(probe, "_is_probe_bundle", lambda: False)
    monkeypatch.setattr(
        probe, "collect_probe", lambda *args, **kwargs: pytest.fail("IO")
    )
    with pytest.raises(ValueError, match="native_probe_bundle_required"):
        probe.probe_main(value, None)


def test_child_cannot_smuggle_raw_content_or_arbitrary_report_fields(
    tmp_path, monkeypatch
):
    value = request(tmp_path)
    monkeypatch.setattr(probe, "_is_probe_bundle", lambda: True)
    monkeypatch.setattr(
        probe, "collect_probe", lambda *args, **kwargs: {"scope": "offline"}
    )
    monkeypatch.setattr(
        probe,
        "_fork_probe",
        lambda *args, **kwargs: {"contents": "DO_NOT_RETURN"},
    )
    writes = []
    frames = SimpleNamespace(write=lambda result, **kwargs: writes.append(result))
    probe.probe_main(value, frames)
    assert writes[0]["child"] == {
        "status": "unverified",
        "reason": "child_probe_failed",
    }
    assert b"DO_NOT_RETURN" not in encode(writes)
