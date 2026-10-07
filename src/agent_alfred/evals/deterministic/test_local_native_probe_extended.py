"""Offline checks of the extended probe protocol, not macOS isolation evidence."""

import ctypes
import errno
import os
import socket
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import native_probe as baseline
from agent_alfred.evals.acceptance.controlled import native_probe_extended as probe
from agent_alfred.evals.acceptance.schema import encode
from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback


def request(tmp_path):
    target = tmp_path / "no-value-sentinel"
    target.write_bytes(b"THIS IS NOT A CREDENTIAL")
    return {
        "contract": probe.CONTRACT,
        "version": 1,
        "nonce": "b" * 64,
        "targets": {name: str(target) for name in probe.TARGETS},
        "ipv4_port": 18011,
        "ipv6_port": 18012,
        "udp4_port": 18013,
        "udp6_port": 18014,
        "browser_port": 18015,
        "keychain_path": None,
        "keychain_service": None,
        "keychain_account": None,
    }


def fake_boundaries(monkeypatch):
    def denied(*args, **kwargs):
        raise OSError(errno.EPERM, "offline permission fixture")

    monkeypatch.setattr(probe.socket, "socket", denied)
    monkeypatch.setattr(probe.socket, "getaddrinfo", denied)
    monkeypatch.setattr(probe.os, "setuid", denied)
    monkeypatch.setattr(
        probe,
        "delegate_browser",
        lambda *a: {
            "status": "unverified",
            "osstatus": -10814,
        },
    )
    monkeypatch.setattr(probe, "keychain_access", lambda *a: pytest.fail("Keychain"))
    monkeypatch.setattr(
        probe,
        "probe_fixed_exec",
        lambda: {"status": "denied", "errno": errno.EPERM},
    )


def test_fixed_exec_attempt_is_part_of_parent_and_child_observations(
    tmp_path, monkeypatch
):
    value = request(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake_boundaries(monkeypatch)
    calls = []
    monkeypatch.setattr(
        probe,
        "probe_fixed_exec",
        lambda: calls.append(True) or {"status": "denied", "errno": errno.EPERM},
        raising=False,
    )
    for contract in (probe.CONTRACT, probe.CHILD):
        result = probe.collect({**value, "contract": contract})
        row = next(
            row for row in result["observations"] if row["target"] == "fixed_exec"
        )
        assert row == {
            "target": "fixed_exec",
            "action": "execve_usr_bin_true",
            "status": "denied",
            "errno": errno.EPERM,
        }
    assert calls == [True, True]


def test_extended_probe_preserves_success_missing_and_unverified_without_contents(
    tmp_path,
    monkeypatch,
):
    value = request(tmp_path)
    original = (tmp_path / "no-value-sentinel").read_bytes()
    value["targets"]["other_case"] = str(tmp_path / "missing")
    monkeypatch.chdir(tmp_path)
    fake_boundaries(monkeypatch)
    answer = probe.collect(value)
    rows = {(row["target"], row["action"]): row for row in answer["observations"]}
    assert len(rows) == 34
    assert rows["gold", "read"]["status"] == "unexpected_allowed"
    assert rows["gold", "write_open"]["status"] == "unexpected_allowed"
    assert (tmp_path / "no-value-sentinel").read_bytes() == original
    assert rows["other_case", "read"]["status"] == "unverified"
    assert rows["udp4_port", "connect_or_send"]["status"] == "denied"
    assert rows["system_browser", "open_loopback_canary"]["status"] == "unverified"
    assert rows["test_keychain", "read"]["status"] == "not_run"
    assert original not in encode(answer)
    assert answer["actual_isolation"] == "REQUIRES_HOST_CORROBORATION"


def test_extended_file_probe_never_reads_actual_target_after_open(
    tmp_path, monkeypatch
):
    target = tmp_path / "credential-shaped-canary"
    target.write_bytes(b"DO NOT READ")
    monkeypatch.setattr(
        probe.os,
        "read",
        lambda *args: pytest.fail("extended_probe_must_not_read_target_content"),
    )
    probe.file_access(target)
    assert target.read_bytes() == b"DO NOT READ"


def test_exec_permission_requires_the_actual_fixed_exec_call(monkeypatch):
    calls = []

    def rejected(path, arguments, environment):
        calls.append((path, arguments, environment))
        raise PermissionError(errno.EACCES, "offline exec denied")

    monkeypatch.setattr(probe.os, "execve", rejected)
    monkeypatch.setattr(baseline, "_fork_probe", lambda action, **kwargs: action())
    assert probe.probe_fixed_exec() == {"status": "denied", "errno": errno.EACCES}
    assert calls == [("/usr/bin/true", ["/usr/bin/true"], {})]

    def failed_fork(*args, **kwargs):
        raise PermissionError(errno.EPERM, "offline fork denied, exec not reached")

    monkeypatch.setattr(baseline, "_fork_probe", failed_fork)
    assert probe.probe_fixed_exec() == {
        "status": "unverified",
        "reason": "exec_child_failed",
    }
    assert len(calls) == 1


def test_exec_missing_frame_or_child_contents_are_not_forwarded(monkeypatch):
    def missing_frame(*args, **kwargs):
        raise EOFError("child could have executed true")

    monkeypatch.setattr(baseline, "_fork_probe", missing_frame)
    expected = {"status": "unverified", "reason": "exec_child_failed"}
    assert probe.probe_fixed_exec() == expected
    monkeypatch.setattr(
        baseline,
        "_fork_probe",
        lambda *a, **k: {
            "pid": os.getpid(),
            "observation": {"status": "denied", "errno": errno.EPERM},
            "contents": "DO_NOT_FORWARD",
        },
    )
    assert probe.probe_fixed_exec() == expected


def test_exec_fork_cleanup_failure_remains_pending(monkeypatch):
    owner = ResumableRollback()

    class FailedClose:
        def close(self):
            raise OSError(errno.EIO, "offline cleanup failed")

    owner.own(FailedClose())

    def failed(*args, **kwargs):
        owner.raise_failure(PermissionError(errno.EPERM, "fork failed"))

    monkeypatch.setattr(baseline, "_fork_probe", failed)
    with pytest.raises(PermissionError) as failure:
        probe.probe_fixed_exec()
    assert isinstance(failure.value.__cause__, IncompleteRollback)


def test_system_framework_termination_does_not_erase_other_observations(
    tmp_path, monkeypatch
):
    value = request(tmp_path)
    value.update(
        contract=probe.CHILD,
        keychain_path=str(tmp_path / "alfred-probe.keychain-db"),
        keychain_service="alfred-probe-" + value["nonce"],
        keychain_account="alfred-probe-" + value["nonce"],
    )
    monkeypatch.chdir(tmp_path)
    fake_boundaries(monkeypatch)
    monkeypatch.setattr(probe, "keychain_access", lambda *args: os._exit(57))
    # The outer owned child keeps the RED process death away from pytest.
    result = baseline._fork_probe(lambda: probe.collect(value), timeout=4)
    rows = {row["target"]: row for row in result["observations"]}
    assert len(result["observations"]) == 34
    assert rows["test_keychain"]["status"] == "unverified"
    assert rows["test_keychain"]["reason"] == "system_probe_child_failed"
    assert rows["udp4_port"]["status"] == "denied"
    assert rows["system_browser"]["osstatus"] == -10814
    probe.validate_child(result, value)


def test_system_probe_filters_payload_and_does_not_reclassify_fork_failure(
    monkeypatch,
):
    unknown = {"status": "unverified", "reason": "system_probe_child_failed"}
    monkeypatch.setattr(baseline, "_fork_probe", lambda action, **kwargs: action())
    denied = {"status": "denied", "osstatus": -34018}
    assert probe._system_probe_in_child(lambda: denied) == denied
    assert (
        probe._system_probe_in_child(lambda: {"contents": "DO_NOT_FORWARD"}) == unknown
    )

    def failed(*args, **kwargs):
        raise PermissionError(errno.EPERM, "fork failed, system call not reached")

    monkeypatch.setattr(baseline, "_fork_probe", failed)
    assert probe._system_probe_in_child(lambda: pytest.fail("called")) == unknown


def test_system_probe_does_not_swallow_pending_cleanup(monkeypatch):
    owner = ResumableRollback()

    class FailedClose:
        def close(self):
            raise OSError(errno.EIO, "offline cleanup failed")

    owner.own(FailedClose())

    def failed(*args, **kwargs):
        owner.raise_failure(EOFError("framework terminated"))

    monkeypatch.setattr(baseline, "_fork_probe", failed)
    with pytest.raises(EOFError) as failure:
        probe._system_probe_in_child(lambda: pytest.fail("called"))
    assert isinstance(failure.value.__cause__, IncompleteRollback)


@pytest.mark.parametrize(
    "change",
    [
        {"command": "/bin/sh"},
        {"version": True},
        {"browser_port": True},
        {"targets": {"gold": "/etc/passwd"}},
        {"keychain_path": "/Users/owner/Library/Keychains/login.keychain-db"},
        {"keychain_service": "unexpected-existing-item"},
    ],
)
def test_extended_request_rejects_unbounded_command_or_existing_keychain(
    tmp_path, change
):
    with pytest.raises(ValueError, match="extended_probe"):
        probe.validate({**request(tmp_path), **change})


def test_extended_entry_is_unavailable_in_product_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: False)
    monkeypatch.setattr(probe, "collect", lambda *a: pytest.fail("IO"))
    with pytest.raises(ValueError, match="native_probe_bundle_required"):
        probe.probe_main(request(tmp_path), None)


def test_child_validation_rejects_contents_and_false_denial(tmp_path, monkeypatch):
    value = {**request(tmp_path), "contract": probe.CHILD}
    monkeypatch.chdir(tmp_path)
    fake_boundaries(monkeypatch)
    answer = probe.collect(value)
    probe.validate_child(answer, value)
    with pytest.raises(ValueError, match="child_mismatch"):
        probe.validate_child({**answer, "contents": "SHOULD_NOT_PASS"}, value)
    bad = deepcopy(answer)
    bad["observations"][0]["status"] = "denied"
    bad["observations"][0]["errno"] = errno.ENOENT
    with pytest.raises(ValueError, match="child_mismatch"):
        probe.validate_child(bad, value)


def test_parent_does_not_forward_untrusted_child_payload(tmp_path, monkeypatch):
    value = request(tmp_path)
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: True)
    monkeypatch.setattr(
        probe, "own_probe_source", lambda: value["targets"]["candidate"]
    )
    monkeypatch.setattr(probe, "collect", lambda *a: {"contract": probe.CONTRACT})
    monkeypatch.setattr(
        baseline,
        "_fork_probe",
        lambda *a, **k: {"contents": "SHOULD_NOT_PASS"},
    )
    writes = []
    probe.probe_main(value, SimpleNamespace(write=lambda row, **k: writes.append(row)))
    assert writes[0]["child"]["status"] == "unverified"
    assert b"SHOULD_NOT_PASS" not in encode(writes)


def test_own_bundle_source_is_a_read_control_only(tmp_path, monkeypatch):
    value = {**request(tmp_path), "contract": probe.CHILD}
    monkeypatch.chdir(tmp_path)
    fake_boundaries(monkeypatch)
    monkeypatch.setattr(
        probe, "own_probe_source", lambda: value["targets"]["candidate"]
    )
    answer = probe.collect(value)
    rows = {(row["target"], row["action"]): row for row in answer["observations"]}
    assert rows["candidate", "read"]["status"] == "expected_allowed"
    assert rows["candidate", "write_open"]["status"] == "unexpected_allowed"
    probe.validate_child(answer, value)
    bad = deepcopy(answer)
    gold_read = next(
        row
        for row in bad["observations"]
        if row["target"] == "gold" and row["action"] == "read"
    )
    gold_read["status"] = "expected_allowed"
    with pytest.raises(ValueError, match="child_mismatch"):
        probe.validate_child(bad, value)


def test_system_service_probe_uses_fresh_process_and_fixed_target(monkeypatch):
    value = {
        "contract": probe.SYSTEM_CONTRACT,
        "version": 1,
        "nonce": "c" * 64,
        "action": "browser",
        "browser_port": 18015,
    }
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: True)
    monkeypatch.setattr(
        baseline,
        "_fork_probe",
        lambda *a, **k: pytest.fail("system_service_probe_must_not_fork"),
    )
    called = []
    monkeypatch.setattr(
        probe,
        "delegate_browser",
        lambda port, nonce: called.append((port, nonce))
        or {"status": "denied", "osstatus": -10826},
    )
    writes = []
    frames = SimpleNamespace(write=lambda row, **k: writes.append(row))
    probe.system_probe_main(value, frames)
    assert called == [(18015, "c" * 64)]
    assert writes[0]["observation"] == {
        "target": "system_browser",
        "action": "open_loopback_canary",
        "status": "denied",
        "osstatus": -10826,
    }
    assert writes[0]["actual_isolation"] == "REQUIRES_HOST_CORROBORATION"

    monkeypatch.setattr(
        probe.socket,
        "getaddrinfo",
        lambda name, port: (_ for _ in ()).throw(
            socket.gaierror(socket.EAI_NONAME, "name not known")
        ),
    )
    probe.system_probe_main({**value, "action": "dns"}, frames)
    assert writes[1]["observation"] == {
        "target": "system_dns",
        "action": "getaddrinfo_example_com",
        "status": "unverified",
        "errno": socket.EAI_NONAME,
        "error_domain": "gai",
    }


def test_system_service_probe_rejects_arbitrary_target_and_product_bundle(
    monkeypatch,
):
    value = {
        "contract": probe.SYSTEM_CONTRACT,
        "version": 1,
        "nonce": "c" * 64,
        "action": "browser",
        "browser_port": 18015,
    }
    with pytest.raises(ValueError, match="system_direct_probe_request_invalid"):
        probe.validate_system_request({**value, "url": "https://example.com"})
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: False)
    with pytest.raises(ValueError, match="native_probe_bundle_required"):
        probe.system_probe_main(value, None)


def test_capability_probe_uses_fixed_getters_and_preserves_null_vs_denied(
    monkeypatch,
):
    calls, releases = [], []

    def queried(task, which, pointer):
        calls.append((task, which))
        result, port = {4: (0, 100), 9: (53, 0), 10: (5, 0)}[which]
        pointer._obj.value = port
        return result

    def released(task, port):
        releases.append((task, port.value))
        return 0

    system = SimpleNamespace(
        task_get_special_port=queried, mach_port_deallocate=released
    )
    monkeypatch.setattr(probe.ctypes, "CDLL", lambda path: system)
    monkeypatch.setattr(
        probe.ctypes.c_uint32, "in_dll",
        lambda library, name: ctypes.c_uint32(42 if name == "mach_task_self_" else 0),
    )
    answer = probe.system_capabilities()
    assert calls == [(42, 4), (42, 9), (42, 10)]
    assert releases == [(42, 100)]
    assert answer == {
        "status": "RAW_KERNEL_RESULTS", "cached_bootstrap_present": False,
        "queries": [
            {"target": "bootstrap", "kern_return": 0, "port_present": True,
             "status": "getter_succeeded"},
            {"target": "access", "kern_return": 53, "port_present": False,
             "status": "denied"},
            {"target": "debug_control", "kern_return": 5, "port_present": False,
             "status": "unverified"},
        ],
    }


def test_capability_request_remains_probe_only_and_does_not_call_browser(
    monkeypatch,
):
    value = {"contract": probe.SYSTEM_CONTRACT, "version": 1,
             "nonce": "c" * 64, "action": "capabilities", "browser_port": 18015}
    probe.validate_system_request(value)
    with pytest.raises(ValueError, match="system_direct_probe_request_invalid"):
        probe.validate_system_request({**value, "task": 1})
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: True)
    monkeypatch.setattr(probe, "delegate_browser", lambda *a: pytest.fail("browser"))
    monkeypatch.setattr(probe.socket, "getaddrinfo", lambda *a: pytest.fail("dns"))
    monkeypatch.setattr(
        probe, "system_capabilities", lambda: {"status": "RAW_KERNEL_RESULTS"}
    )
    writes = []
    probe.system_probe_main(
        value, SimpleNamespace(write=lambda row, **k: writes.append(row))
    )
    assert writes[0]["observation"] == {
        "target": "system_capabilities", "action": "fixed_own_task_getters",
        "status": "RAW_KERNEL_RESULTS",
    }
    assert writes[0]["actual_isolation"] == "REQUIRES_HOST_CORROBORATION"


def capability_snapshot():
    return {
        "pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
        "environment_names": [],
        "mach": {"status": "RAW_KERNEL_RESULTS", "cached_bootstrap_present": False,
                 "queries": [{"target": name, "kern_return": 53,
                              "port_present": False, "status": "denied"}
                             for name in ("bootstrap", "access", "debug_control")]},
        "io": {"target": "host_io_main", "kern_return": 53,
               "port_present": False, "status": "denied"},
        "descriptors": [{"target": f"inherited_fd_{fd}", "errno": errno.EBADF,
                         "status": "absent"} for fd in (197, 198)],
        "sockets": [{"target": name, "status": "denied", "errno": errno.EPERM,
                     "stage": "socket_creation"}
                    for name in ("ipv4_tcp", "ipv4_udp", "ipv6_tcp", "ipv6_udp",
                                 "unix_stream", "unix_dgram")],
    }


def test_capability_family_is_closed_and_forks_before_parent_getters(monkeypatch):
    value = {"contract": probe.SYSTEM_CONTRACT, "version": 1,
             "nonce": "c" * 64, "action": "capability_family", "browser_port": 18015}
    order = []

    def snapshot():
        order.append("snapshot")
        return capability_snapshot()

    def fork(action, *, timeout):
        order.append("fork")
        assert timeout == 5
        result = action()
        return {**result, "pid": os.getpid() + 1, "ppid": os.getpid()}

    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: True)
    monkeypatch.setattr(baseline, "_fork_probe", fork)
    monkeypatch.setattr(probe, "_capability_snapshot", snapshot, raising=False)
    probe.validate_system_request(value)
    with pytest.raises(ValueError, match="system_direct_probe_request_invalid"):
        probe.validate_system_request({**value, "target": "arbitrary"})
    writes = []
    probe.system_probe_main(
        value, SimpleNamespace(write=lambda v, **k: writes.append(v))
    )
    assert order == ["fork", "snapshot", "snapshot"]
    assert writes[0]["observation"]["status"] == "RAW_CAPABILITY_FAMILY"
    assert writes[0]["observation"]["child"]["pid"] != os.getpid()
    monkeypatch.setattr(baseline, "_is_probe_bundle", lambda: False)
    with pytest.raises(ValueError, match="native_probe_bundle_required"):
        probe.system_probe_main(value, None)


@pytest.mark.parametrize("change", ["contents", "false_denial", "wrong_parent"])
def test_capability_family_never_forwards_unbounded_or_misbound_child(
    monkeypatch, change
):
    child = {**capability_snapshot(), "pid": os.getpid() + 1, "ppid": os.getpid()}
    if change == "contents":
        child["contents"] = "DO_NOT_FORWARD"
    elif change == "false_denial":
        child["io"]["port_present"] = True
    else:
        child["ppid"] += 1
    monkeypatch.setattr(baseline, "_fork_probe", lambda *a, **k: child)
    monkeypatch.setattr(probe, "_capability_snapshot", capability_snapshot)
    with pytest.raises(ValueError, match="capability_(snapshot|child_binding)_invalid"):
        probe.system_capability_family()


@pytest.mark.parametrize("control", [None, KeyboardInterrupt, SystemExit])
def test_capability_family_retains_actual_fork_child_first_failure(
    monkeypatch, control
):
    parent = os.getpid()

    def snapshot():
        if os.getpid() == parent:
            raise RuntimeError("parent must not erase the known child failure")
        original = OSError(errno.EIO, "DO_NOT_EXPORT_CHILD_FAILURE_CONTENTS")

        class FailedClose:
            def close(self):
                if control is not None:
                    raise control()
                raise PermissionError(errno.EPERM, "DO_NOT_EXPORT_CLOSE_CONTENTS")

        resources = ResumableRollback()
        resources.own(FailedClose())
        resources.raise_failure(original)

    monkeypatch.setattr(probe, "_capability_snapshot", snapshot)
    result = probe.system_capability_family()
    assert result["complete"] is False
    assert result["parent"]["status"] == "not_run_after_child_failure"
    child = result["child"]
    assert child["pid"] != parent and child["ppid"] == parent
    graph = child["failure_graph"]
    assert any(row["type"] == "OSError" and row["errno"] == errno.EIO
               for row in graph["nodes"])
    expected = "PermissionError" if control is None else control.__name__
    assert any(row["type"] == expected for row in graph["nodes"])
    assert b"DO_NOT_EXPORT" not in encode(result)
    assert child["cleanup_scope"] == "owned_child_exit_only_no_recovery_claim"


@pytest.mark.parametrize("status, port", [(0, 101), (53, 0), (5, 0), (0, 0)])
def test_io_main_getter_owns_both_rights_and_retains_raw_result(
    monkeypatch, status, port
):
    calls, releases = [], []

    def host_self():
        return 100

    def query(host, result):
        calls.append(host.value)
        result._obj.value = port
        return status

    def release(task, right):
        releases.append((task, right.value))
        return 0

    system = SimpleNamespace(mach_host_self=host_self, host_get_io_main=query,
                             mach_port_deallocate=release)
    monkeypatch.setattr(probe.ctypes, "CDLL", lambda path: system)
    monkeypatch.setattr(probe.ctypes.c_uint32, "in_dll", lambda *a: ctypes.c_uint32(42))
    answer = probe.system_io_main()
    assert calls == [100]
    assert releases == ([(42, 101), (42, 100)] if port else [(42, 100)])
    assert answer["kern_return"] == status and answer["port_present"] is bool(port)
    assert answer["status"] == (
        "denied" if status == 53 and not port else "getter_succeeded"
        if status == 0 else "unverified"
    )
    assert b"101" not in encode(answer) and b"100" not in encode(answer)


def test_socket_primitive_only_denies_creation_and_retains_release_failure(monkeypatch):
    calls = []

    def denied(family, kind):
        calls.append((family, kind))
        raise PermissionError(errno.EPERM, "OFFLINE creation")

    monkeypatch.setattr(probe.socket, "SocketType", denied)
    result = probe.socket_creation(socket.AF_INET6, socket.SOCK_DGRAM)
    assert result == {"status": "denied", "errno": errno.EPERM,
                      "stage": "socket_creation"}
    assert calls == [(socket.AF_INET6, socket.SOCK_DGRAM)]

    class Created:
        def close(self):
            raise PermissionError(errno.EPERM, "OFFLINE close, creation succeeded")

    monkeypatch.setattr(probe.socket, "SocketType", lambda *a: Created())
    with pytest.raises(PermissionError) as failure:
        probe.socket_creation(socket.AF_INET6, socket.SOCK_DGRAM)
    assert isinstance(failure.value.__cause__, IncompleteRollback)


def test_descriptor_is_closed_after_open_only_probe(tmp_path, monkeypatch):
    path = tmp_path / "target"
    path.write_bytes(b"sentinel")
    captured = []
    real_open = os.open

    def tracked_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        captured.append(descriptor)
        return descriptor

    monkeypatch.setattr(probe.os, "open", tracked_open)
    probe.file_access(path)
    with pytest.raises(OSError) as failure:
        os.fstat(captured[0])
    assert failure.value.errno == errno.EBADF


def test_native_reference_never_repeats_unknown_release():
    calls = []

    def interrupted(pointer):
        calls.append(pointer.value)
        raise KeyboardInterrupt

    reference = probe._NativeReference(interrupted)
    reference.pointer = ctypes.c_void_p(100)
    with pytest.raises(KeyboardInterrupt):
        reference.close()
    with pytest.raises(ValueError, match="release_outcome_unknown"):
        reference.close()
    assert calls == [100]


@pytest.mark.parametrize("error_number", [errno.EPERM, errno.EACCES])
def test_dns_error_domain_cannot_be_permission_denial(error_number):
    def dns_failure():
        raise socket.gaierror(error_number, "offline DNS failure")

    answer = probe.observed(dns_failure)
    assert answer == {
        "status": "unverified",
        "errno": error_number,
        "error_domain": "gai",
    }


def test_successful_access_cleanup_failure_retains_owner(monkeypatch):
    retained = []

    def failed_close(owner):
        retained.append(owner)
        raise PermissionError(errno.EACCES, "offline close failure")

    monkeypatch.setattr(probe.os, "open", lambda *a: 197)
    monkeypatch.setattr(probe.os, "read", lambda *a: b"x")
    monkeypatch.setattr(probe.OwnedDescriptor, "close", failed_close)
    with pytest.raises(ValueError, match="probe_file_cleanup_failed") as failure:
        probe.observed(lambda: probe.file_access("/offline-only"))
    assert isinstance(failure.value.__cause__.__cause__, IncompleteRollback)
    assert retained[0].fd == 197


def test_native_reference_failed_status_remains_pending_without_second_release():
    calls = []
    reference = probe._NativeReference(
        lambda pointer: calls.append(pointer.value) or -50, status_result=True
    )
    reference.pointer = ctypes.c_void_p(100)
    resources = ResumableRollback()
    resources.own(reference)
    with pytest.raises(ValueError, match="probe_reference_release_failed"):
        resources.close()
    assert not resources.retry()
    assert calls == [100]


def test_keychain_query_requires_confirmed_ui_suppression(monkeypatch):
    def function(result):
        def invoke(*args):
            if isinstance(result, BaseException):
                raise result
            return result

        return invoke

    security = SimpleNamespace(
        SecKeychainSetUserInteractionAllowed=function(-50),
        SecKeychainOpen=function(AssertionError("must not open")),
        SecKeychainFindGenericPassword=function(AssertionError("must not read")),
        SecKeychainItemFreeContent=function(0),
    )
    core = SimpleNamespace(CFRelease=function(None))
    libraries = iter((security, core))
    monkeypatch.setattr(probe.ctypes, "CDLL", lambda path: next(libraries))
    assert probe.keychain_access("/offline-only", "service", "account") == {
        "status": "unverified",
        "osstatus": -50,
        "stage": "disable_ui",
    }


def test_unexpected_elevation_stops_before_system_delegation(tmp_path, monkeypatch):
    value = request(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake_boundaries(monkeypatch)
    monkeypatch.setattr(probe.os, "setuid", lambda *a: None)
    monkeypatch.setattr(probe, "delegate_browser", lambda *a: pytest.fail("delegate"))
    with pytest.raises(ValueError, match="probe_unexpected_root_authority"):
        probe.collect(value)


def test_symlink_access_does_not_swallow_unfinished_cleanup(tmp_path, monkeypatch):
    from pathlib import Path

    value = request(tmp_path)
    monkeypatch.chdir(tmp_path)
    fake_boundaries(monkeypatch)
    owner = ResumableRollback()

    class BlockedClose:
        def close(self):
            raise OSError(errno.EIO, "offline close remains pending")

    owner.own(BlockedClose())

    def access(path, **kwargs):
        if Path(path).name.startswith("probe-link-"):
            owner.raise_failure(PermissionError(errno.EACCES, "offline access denied"))

    monkeypatch.setattr(probe, "file_access", access)
    with pytest.raises(PermissionError) as failure:
        probe.collect(value)
    assert isinstance(failure.value.__cause__, IncompleteRollback)
    assert failure.value.__cause__.owner is owner
