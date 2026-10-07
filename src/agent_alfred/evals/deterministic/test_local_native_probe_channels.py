"""Probe-only channel evidence and stage retention; no OS PASS from fixtures."""

import errno
import os
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import native_probe as baseline
from agent_alfred.evals.acceptance.controlled import native_probe_channels as probe
from agent_alfred.evals.acceptance.controlled import native_probe_extended as extended
from agent_alfred.evals.acceptance.schema import digest


def request():
    root = "/private/tmp/alfred-local-channels-" + "a" * 32
    return {
        "contract": probe.CONTRACT, "version": 1, "nonce": "b" * 64,
        "stream_path": root + "/stream.sock",
        "dgram_path": root + "/datagram.sock", "browser_port": 18753,
    }


def test_product_and_bad_request_are_rejected_before_any_probe(monkeypatch):
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: False)
    monkeypatch.setattr(probe, "family", lambda *a: pytest.fail("probe"))
    with pytest.raises(ValueError, match="native_probe_bundle_required"):
        probe.probe_main(request(), SimpleNamespace(write=pytest.fail))
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: True)
    for mutate in (
        lambda v: v.update(stream_path="/Users/owner/secret"),
        lambda v: v.update(dgram_path=v["stream_path"]),
        lambda v: v.update(stream_path=v["stream_path"].replace(".sock", "/sock")),
        lambda v: v.update(version=True),
        lambda v: v.update(browser_port=True),
        lambda v: v.update(command="echo"),
    ):
        value = request()
        mutate(value)
        with pytest.raises(ValueError, match="channel_probe_request_invalid"):
            probe.probe_main(value, SimpleNamespace(write=pytest.fail))


@pytest.mark.parametrize("code, expected", [
    (errno.EPERM, "denied"), (errno.EACCES, "denied"),
    (errno.ENOENT, "unverified"), (errno.ECONNREFUSED, "unverified"),
])
def test_unix_actual_operation_classification_and_owned_socket(
    monkeypatch, code, expected,
):
    calls = []

    class Connection:
        def __init__(self, family, kind):
            calls.append(("created", family, kind))

        def settimeout(self, timeout):
            calls.append(("timeout", timeout))

        def connect(self, path):
            calls.append(("connect", path))
            raise OSError(code, "fixed offline seam")

        def sendto(self, payload, path):
            calls.append(("sendto", payload, path))
            raise OSError(code, "fixed offline seam")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(probe.socket, "SocketType", Connection)
    for kind, path in (
        (probe.socket.SOCK_STREAM, request()["stream_path"]),
        (probe.socket.SOCK_DGRAM, request()["dgram_path"]),
    ):
        row = probe.unix_observation(kind, path, request()["nonce"])
        assert row == {"status": expected, "errno": code,
                       "stage": "connect" if kind == probe.socket.SOCK_STREAM
                       else "sendto"}
        assert calls[-1] == ("close",)
    assert sum(row[0] == "close" for row in calls) == 2


def test_native_witness_is_no_arg_boolean_metadata_only(monkeypatch):
    states = {name: True for name in probe.WITNESS_FIELDS}
    calls = []
    monkeypatch.setitem(sys.modules, "_alfred_boundary_witness", SimpleNamespace(
        witness=lambda: calls.append(()) or deepcopy(states),
    ))
    assert probe.native_witness() == states
    assert calls == [()]
    states["namespace_confined"] = False
    assert probe.native_witness()["namespace_confined"] is False
    states["kernel_address"] = "DO NOT EMIT"
    with pytest.raises(ValueError, match="channel_probe_witness_invalid"):
        probe.native_witness()


def test_partial_stage_frames_survive_browser_abort_and_are_bound(monkeypatch):
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: True)
    family = {"status": "RAW_FAMILY_FIXTURE"}
    monkeypatch.setattr(probe, "family", lambda *a: family)
    monkeypatch.setattr(probe.socket, "getaddrinfo", lambda *a: [])
    monkeypatch.setattr(probe, "native_witness", lambda: {})
    frames = []
    failure = OSError(errno.EIO, "native framework seam")

    def browser(port, nonce, *, progress):
        progress("before_core_load")
        raise failure

    monkeypatch.setattr(extended, "delegate_browser", browser)
    with pytest.raises(OSError) as caught:
        probe.probe_main(request(), SimpleNamespace(
            write=lambda value, **kwargs: frames.append(deepcopy(value)),
        ))
    assert caught.value is failure
    assert [row["stage"] for row in frames] == [
        "family", "before_dns", "after_dns", "before_core_load",
    ]
    assert frames[0]["observation"] == family
    assert [row["sequence"] for row in frames] == [1, 2, 3, 4]
    assert all(row["pid"] == os.getpid() and row["ppid"] == os.getppid()
               and row["request_sha256"] == digest(request())
               and row["nonce"] == request()["nonce"] for row in frames)


def test_stage_write_failure_prevents_framework_side_effect(monkeypatch):
    failure = SystemExit(51)
    loads = []

    def progress(stage):
        raise failure

    monkeypatch.setattr(extended.ctypes, "CDLL", lambda p: loads.append(p))
    with pytest.raises(SystemExit) as caught:
        extended.delegate_browser(18753, "b" * 64, progress=progress)
    assert caught.value is failure
    assert loads == []


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "frame"), SystemExit(73)])
def test_stage_failure_after_url_acquisition_releases_once_without_open(
    monkeypatch, failure,
):
    releases, opens, stages = [], [], []

    def fixed(result, side_effect=None):
        def call(*args):
            if side_effect is not None:
                side_effect(args)
            return result
        return call

    core = SimpleNamespace(
        CFRelease=fixed(None, lambda args: releases.append(args[0].value)),
        CFURLCreateWithBytes=fixed(73),
    )
    launch = SimpleNamespace(LSOpenCFURLRef=fixed(
        0, lambda args: opens.append(True),
    ))
    monkeypatch.setattr(extended.ctypes, "CDLL", lambda path: core
                        if "CoreFoundation" in path else launch)

    def progress(stage):
        stages.append(stage)
        if stage == "after_url_create":
            raise failure

    with pytest.raises(type(failure)) as caught:
        extended.delegate_browser(18753, "b" * 64, progress=progress)
    assert caught.value is failure
    assert releases == [73]
    assert opens == []
    assert stages[-2:] == ["before_url_create", "after_url_create"]


def test_family_binds_fork_identity_and_keeps_false_witness(monkeypatch):
    states = {name: True for name in probe.WITNESS_FIELDS}
    parent = {
        "pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
        "before": deepcopy(states), "after": deepcopy(states),
        "unix": [
            {"target": "unix_stream", "status": "denied", "errno": 1,
             "stage": "connect"},
            {"target": "unix_dgram", "status": "denied", "errno": 1,
             "stage": "sendto"},
        ],
    }
    child = {**deepcopy(parent), "pid": os.getpid() + 1, "ppid": os.getpid()}
    child["after"]["namespace_confined"] = False
    monkeypatch.setattr(baseline, "_fork_probe", lambda *a, **k: deepcopy(child))
    monkeypatch.setattr(probe, "snapshot", lambda *a: deepcopy(parent))
    row = probe.family(request())
    assert row["child"]["after"]["namespace_confined"] is False
    child["ppid"] = 0
    with pytest.raises(ValueError, match="channel_probe_family_invalid"):
        probe.family(request())
    child["ppid"] = os.getpid()
    child["unix"][0]["errno"] = errno.ENOENT
    with pytest.raises(ValueError, match="channel_probe_family_invalid"):
        probe.family(request())


def test_actual_socket_timeout_rows_survive_family(monkeypatch):
    states = {name: True for name in probe.WITNESS_FIELDS}
    monkeypatch.setitem(sys.modules, "_alfred_boundary_witness", SimpleNamespace(
        witness=lambda: deepcopy(states),
    ))

    class Connection:
        def __init__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            raise TimeoutError("fixed offline timeout")

        def sendto(self, payload, path):
            raise TimeoutError("fixed offline timeout")

        def close(self):
            pass

    monkeypatch.setattr(probe.socket, "SocketType", Connection)
    child = {**probe.snapshot(request()), "pid": os.getpid() + 1,
             "ppid": os.getpid()}
    monkeypatch.setattr(baseline, "_fork_probe", lambda *a, **k: deepcopy(child))
    row = probe.family(request())
    assert all(item["status"] == "unverified" and item["errno"] is None
               for side in ("parent", "child") for item in row[side]["unix"])
    child["unix"][0]["status"] = "denied"
    with pytest.raises(ValueError, match="channel_probe_family_invalid"):
        probe.family(request())
