"""Fixed non-billing OS probes; never a product operation or an approval source.

The host supplies disposable targets, not commands or provider credentials.
Successful access to a protected target is a failed boundary observation.
The exact probe's own bundled source is a positive read control; write is denied.
Every network payload is the public probe nonce; no target contents are emitted.
"""

import ctypes
import errno
import fcntl
import os
import re
import socket
import sys
from functools import partial
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from agent_alfred.resource_rollback import (
    IncompleteRollback,
    OwnedDescriptor,
    ResumableRollback,
    capture_call_result,
    raise_if_rollback_pending,
)

from ..schema import digest

CONTRACT = "V1-LOCAL-OS-PROBE-EXTENDED"
CHILD = "V1-LOCAL-OS-PROBE-EXTENDED-CHILD"
SYSTEM_CONTRACT = "V1-LOCAL-OS-PROBE-SYSTEM-DIRECT"
TARGETS = frozenset(
    (
        "provider_test",
        "gold",
        "other_case",
        "decision",
        "ledger",
        "witness",
        "candidate",
        "host_code",
    )
)


def validate(value):
    if type(value) is not dict or set(value) != {
        "contract",
        "version",
        "nonce",
        "targets",
        "ipv4_port",
        "ipv6_port",
        "udp4_port",
        "udp6_port",
        "browser_port",
        "keychain_path",
        "keychain_service",
        "keychain_account",
    }:
        raise ValueError("extended_probe_request_invalid")
    if (
        value["contract"] not in (CONTRACT, CHILD)
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["nonce"]) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", value["nonce"])
        or type(value["targets"]) is not dict
        or set(value["targets"]) != TARGETS
    ):
        raise ValueError("extended_probe_request_invalid")
    for raw in value["targets"].values():
        if (
            type(raw) is not str
            or len(raw) > 4096
            or not raw.startswith("/")
            or ".." in Path(raw).parts
            or "\x00" in raw
        ):
            raise ValueError("extended_probe_path_invalid")
    # Test keychains are task-owned, explicitly named and never the login keychain.
    if value["keychain_path"] is None:
        if (
            value["keychain_service"] is not None
            or value["keychain_account"] is not None
        ):
            raise ValueError("extended_probe_keychain_invalid")
    else:
        keychain = value["keychain_path"]
        if (
            type(keychain) is not str
            or not keychain.startswith("/")
            or ".." in Path(keychain).parts
            or "\x00" in keychain
            or len(keychain) > 4096
            or Path(keychain).name != "alfred-probe.keychain-db"
        ):
            raise ValueError("extended_probe_keychain_invalid")
        for key in ("keychain_service", "keychain_account"):
            if value[key] != "alfred-probe-" + value["nonce"]:
                raise ValueError("extended_probe_keychain_invalid")
    for key in ("ipv4_port", "ipv6_port", "udp4_port", "udp6_port", "browser_port"):
        if type(value[key]) is not int or not 1024 <= value[key] <= 65535:
            raise ValueError("extended_probe_port_invalid")


def own_probe_source():
    return str(
        Path(sys.executable).parent.parent
        / "Resources/packages/agent_alfred/evals/acceptance/controlled/native_probe.py"
    )


def validate_system_request(value):
    if (
        type(value) is not dict
        or set(value) != {"contract", "version", "nonce", "action", "browser_port"}
        or value["contract"] != SYSTEM_CONTRACT
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["nonce"]) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", value["nonce"])
        or value["action"] not in (
            "browser", "dns", "capabilities", "capability_family"
        )
        or type(value["browser_port"]) is not int
        or not 1024 <= value["browser_port"] <= 65535
    ):
        raise ValueError("system_direct_probe_request_invalid")


def observed(action):
    try:
        action()
        return {"status": "unexpected_allowed", "errno": None}
    except OSError as error:
        raise_if_rollback_pending(error)
        # gaierror/herror have their own numeric namespaces. On macOS EAI
        # values overlap EPERM/EACCES; neither is a POSIX permission result.
        if isinstance(error, (socket.gaierror, socket.herror)):
            return {
                "status": "unverified",
                "errno": error.errno,
                "error_domain": "gai" if isinstance(error, socket.gaierror) else "h",
            }
        return {
            "status": "denied"
            if error.errno in (errno.EACCES, errno.EPERM)
            else "unverified",
            "errno": error.errno,
        }


def _fixed_exec_child():
    return {
        "pid": os.getpid(),
        "observation": observed(
            lambda: os.execve("/usr/bin/true", ["/usr/bin/true"], {})
        ),
    }


def probe_fixed_exec():
    """A fixed no-data exec attempt in an owned fork; never replace the probe."""
    from .native_probe import _fork_probe

    unverified = {"status": "unverified", "reason": "exec_child_failed"}
    try:
        result = _fork_probe(_fixed_exec_child, timeout=2)
    except (OSError, EOFError, ValueError) as error:
        raise_if_rollback_pending(error)
        # Successful exec replaces the child and closes its frame pipe. Missing
        # output or failure of fork/cleanup is never an exec permission denial.
        return unverified
    if type(result) is not dict or set(result) != {"pid", "observation"}:
        return unverified
    row = result["observation"]
    if (
        type(row) is not dict
        or set(row) != {"status", "errno"}
        or row["status"] not in {"denied", "unverified", "unexpected_allowed"}
        or (row["errno"] is not None and type(row["errno"]) is not int)
        or (
            row["status"] == "denied"
            and row["errno"] not in (errno.EPERM, errno.EACCES)
        )
    ):
        return unverified
    return row


def _system_probe_in_child(action):
    """Contain a native framework abort without mistaking it for denial."""
    from .native_probe import _fork_probe

    unverified = {"status": "unverified", "reason": "system_probe_child_failed"}
    try:
        result = _fork_probe(
            lambda: {"pid": os.getpid(), "observation": action()}, timeout=5
        )
    except (OSError, EOFError, ValueError) as error:
        raise_if_rollback_pending(error)
        return unverified
    if type(result) is not dict or set(result) != {"pid", "observation"}:
        return unverified
    row = result["observation"]
    if (
        type(row) is not dict
        or "status" not in row
        or set(row) - {"status", "osstatus", "stage", "reason"}
        or row["status"] not in {"denied", "unverified", "unexpected_allowed"}
        or ("osstatus" in row and type(row["osstatus"]) is not int)
        or row.get("stage") not in (None, "open", "read", "disable_ui")
        or row.get("reason") not in (None, "canary_url_failed")
        or (row["status"] == "denied" and row.get("osstatus") not in (-34018, -10826))
    ):
        return unverified
    return row


def file_access(path, *, write=False):
    # Opening is sufficient to detect a boundary failure. Never read content,
    # including one byte of the actual provider credential, on unexpected allow.
    flags = os.O_WRONLY if write else os.O_RDONLY
    descriptor = OwnedDescriptor()
    resources = ResumableRollback()
    resources.own(descriptor)
    try:
        capture_call_result(
            descriptor, "fd", partial(os.open, path, flags | os.O_NONBLOCK)
        )
    except BaseException as error:
        resources.raise_failure(error)
    try:
        resources.close()
    except BaseException as error:
        # Access already succeeded. A release error cannot establish denial.
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ValueError("probe_file_cleanup_failed") from error


class _NativeReference:
    """Own an out-parameter before a C call; never release an unknown result twice."""

    def __init__(self, release, *, status_result=False):
        self.pointer = ctypes.c_void_p()
        self.release = release
        self.started = False
        self.finished = False
        self.status_result = status_result

    def close(self):
        if not self.pointer.value:
            return
        if self.finished is not False:
            if self.status_result and self.finished != 0:
                raise ValueError("probe_reference_release_failed")
            return
        if self.started:
            raise ValueError("probe_reference_release_outcome_unknown")
        self.started = True
        capture_call_result(self, "finished", partial(self.release, self.pointer))
        if self.status_result and self.finished != 0:
            raise ValueError("probe_reference_release_failed")


def network(family, kind, address, port, nonce):
    with socket.socket(family, kind) as connection:
        connection.settimeout(1)
        if kind == socket.SOCK_STREAM:
            connection.connect((address, port))
        else:
            connection.sendto(("ALFRED-PROBE-" + nonce).encode(), (address, port))


def keychain_access(path, service, account):
    """Query only the specified disposable keychain; disable authentication UI."""
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_bool]
    security.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
    security.SecKeychainOpen.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainOpen.restype = ctypes.c_int32
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    core.CFRelease.argtypes = [ctypes.c_void_p]
    core.CFRelease.restype = None
    suppression = security.SecKeychainSetUserInteractionAllowed(False)
    if suppression != 0:
        return {"status": "unverified", "osstatus": suppression, "stage": "disable_ui"}
    resources = ResumableRollback()
    reference = _NativeReference(core.CFRelease)
    data = _NativeReference(
        partial(security.SecKeychainItemFreeContent, None), status_result=True
    )
    resources.own(reference)
    resources.own(data)
    try:
        status = security.SecKeychainOpen(
            os.fsencode(path), ctypes.byref(reference.pointer)
        )
        stage = "open"
        if status == 0:
            size = ctypes.c_uint32()
            service, account = service.encode(), account.encode()
            status = security.SecKeychainFindGenericPassword(
                reference.pointer,
                len(service),
                service,
                len(account),
                account,
                ctypes.byref(size),
                ctypes.byref(data.pointer),
                None,
            )
            stage = "read"
    except BaseException as error:
        resources.raise_failure(error)
    resources.close()
    # Other Security errors (including missing items/UI unavailable) are not
    # silently interpreted as an entitlement or file-permission denial.
    return {
        "status": "unexpected_allowed"
        if status == 0
        else "denied"
        if status == -34018
        else "unverified",
        "osstatus": status,
        "stage": stage,
    }


def delegate_browser(port, nonce, *, progress=None):
    """Try only the host's loopback no-data canary URL via LaunchServices."""
    def stage(name):
        if progress is not None:
            progress(name)

    stage("before_core_load")
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    stage("after_core_load")
    stage("before_launch_load")
    launch = ctypes.CDLL(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/LaunchServices"
    )
    stage("after_launch_load")
    core.CFURLCreateWithBytes.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    core.CFURLCreateWithBytes.restype = ctypes.c_void_p
    core.CFRelease.argtypes = [ctypes.c_void_p]
    core.CFRelease.restype = None
    launch.LSOpenCFURLRef.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    launch.LSOpenCFURLRef.restype = ctypes.c_int32
    raw = f"http://127.0.0.1:{port}/alfred-probe/{nonce}".encode()
    resources = ResumableRollback()
    url = _NativeReference(core.CFRelease)
    resources.own(url)
    try:
        stage("before_url_create")
        capture_call_result(
            url.pointer,
            "value",
            partial(
                core.CFURLCreateWithBytes,
                None,
                raw,
                len(raw),
                0x08000100,
                None,
            ),
        )
        stage("after_url_create")
        if not url.pointer.value:
            resources.close()
            return {"status": "unverified", "reason": "canary_url_failed"}
        stage("before_ls_open")
        status = launch.LSOpenCFURLRef(url.pointer, None)
        stage("after_ls_open")
    except BaseException as error:
        resources.raise_failure(error)
    resources.close()
    return {
        "status": "unexpected_allowed"
        if status == 0
        else "denied"
        if status == -10826
        else "unverified",
        "osstatus": status,
    }


def system_capabilities():
    """Read fixed own-task namespace getters, without using system frameworks.

    KERN_DENIED is a permission result; missing/null rights and other kernel
    failures remain raw facts. No port name, memory address or contents leave
    this probe. These observations do not replace the historical browser crash
    or prove the complete boundary without host and inheritance corroboration.
    """
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    task = ctypes.c_uint32.in_dll(system, "mach_task_self_").value
    system.task_get_special_port.argtypes = [
        ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)
    ]
    system.task_get_special_port.restype = ctypes.c_int32
    system.mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    system.mach_port_deallocate.restype = ctypes.c_int32
    resources = ResumableRollback()
    rows = []
    try:
        for name, which in (("bootstrap", 4), ("access", 9), ("debug_control", 10)):
            reference = _NativeReference(
                partial(system.mach_port_deallocate, task), status_result=True
            )
            reference.pointer = ctypes.c_uint32()
            resources.own(reference)
            result = system.task_get_special_port(
                task, which, ctypes.byref(reference.pointer)
            )
            present = bool(reference.pointer.value)
            rows.append({
                "target": name, "kern_return": result, "port_present": present,
                "status": "denied" if result == 53 and not present
                else "getter_succeeded" if result == 0 else "unverified",
            })
        cached = bool(ctypes.c_uint32.in_dll(system, "bootstrap_port").value)
    except BaseException as error:
        resources.raise_failure(error)
    resources.close()
    return {"status": "RAW_KERNEL_RESULTS", "queries": rows,
            "cached_bootstrap_present": cached}


def system_io_main():
    """Query the ordinary HOST's fixed I/O getter; never use the returned right."""
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    task = ctypes.c_uint32.in_dll(system, "mach_task_self_").value
    system.mach_host_self.argtypes = []
    system.mach_host_self.restype = ctypes.c_uint32
    system.host_get_io_main.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
    ]
    system.host_get_io_main.restype = ctypes.c_int32
    system.mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    system.mach_port_deallocate.restype = ctypes.c_int32
    resources = ResumableRollback()
    host = _NativeReference(
        partial(system.mach_port_deallocate, task), status_result=True
    )
    io = _NativeReference(
        partial(system.mach_port_deallocate, task), status_result=True
    )
    host.pointer, io.pointer = ctypes.c_uint32(), ctypes.c_uint32()
    resources.own(host)
    resources.own(io)
    try:
        capture_call_result(host.pointer, "value", system.mach_host_self)
        result = system.host_get_io_main(host.pointer, ctypes.byref(io.pointer))
        present = bool(io.pointer.value)
    except BaseException as error:
        resources.raise_failure(error)
    resources.close()
    return {
        "target": "host_io_main", "kern_return": result, "port_present": present,
        "status": "denied" if result == 53 and not present
        else "getter_succeeded" if result == 0 else "unverified",
    }


def socket_creation(family, kind):
    """Separate actual socket creation permission from any release failure."""
    receiver = SimpleNamespace(connection=None)
    resources = ResumableRollback()
    resources.own(
        receiver,
        lambda: receiver.connection.close()
        if receiver.connection is not None else None,
    )

    def acquire():
        try:
            capture_call_result(
                receiver, "connection", partial(socket.SocketType, family, kind)
            )
        except BaseException as error:
            resources.raise_failure(error)

    result = observed(acquire)
    # Outside observed(): failure to release a created socket is not denial.
    resources.close()
    return {**result, "stage": "socket_creation"}


SOCKET_PRIMITIVES = (
    ("ipv4_tcp", socket.AF_INET, socket.SOCK_STREAM),
    ("ipv4_udp", socket.AF_INET, socket.SOCK_DGRAM),
    ("ipv6_tcp", socket.AF_INET6, socket.SOCK_STREAM),
    ("ipv6_udp", socket.AF_INET6, socket.SOCK_DGRAM),
    ("unix_stream", socket.AF_UNIX, socket.SOCK_STREAM),
    ("unix_dgram", socket.AF_UNIX, socket.SOCK_DGRAM),
)


def _capability_snapshot():
    descriptors = []
    for fd in (197, 198):
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
            row = {"status": "unexpected_inherited", "errno": None}
        except OSError as error:
            row = {"status": "absent" if error.errno == errno.EBADF else "unverified",
                   "errno": error.errno}
        descriptors.append({"target": f"inherited_fd_{fd}", **row})
    return {
        "pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
        "environment_names": sorted(os.environ), "mach": system_capabilities(),
        "io": system_io_main(), "descriptors": descriptors,
        "sockets": [{"target": name, **socket_creation(family, kind)}
                    for name, family, kind in SOCKET_PRIMITIVES],
    }


def _validate_capability_snapshot(value):
    """Permit only the fixed metadata schema, including the fork child's frame."""
    invalid = "capability_snapshot_invalid"
    if (
        type(value) is not dict
        or set(value) != {"pid", "ppid", "uid", "environment_names", "mach", "io",
                          "descriptors", "sockets"}
        or any(type(value[key]) is not int or value[key] < 0
               for key in ("pid", "ppid", "uid"))
        or value["pid"] == 0
        or type(value["environment_names"]) is not list
        or len(value["environment_names"]) > 64
        or any(type(name) is not str or len(name) > 128
               for name in value["environment_names"])
    ):
        raise ValueError(invalid)
    mach = value["mach"]
    if (type(mach) is not dict or set(mach) != {
        "status", "cached_bootstrap_present", "queries"
    } or mach["status"] != "RAW_KERNEL_RESULTS"
        or type(mach["cached_bootstrap_present"]) is not bool
        or type(mach["queries"]) is not list or len(mach["queries"]) != 3):
        raise ValueError(invalid)
    queries = [*mach["queries"], value["io"]]
    for row, target in zip(
        queries, ("bootstrap", "access", "debug_control", "host_io_main"), strict=True
    ):
        if (type(row) is not dict or set(row) != {
            "target", "kern_return", "port_present", "status"
        } or row["target"] != target or type(row["kern_return"]) is not int
            or type(row["port_present"]) is not bool):
            raise ValueError(invalid)
        expected = "denied" if row["kern_return"] == 53 and not row["port_present"] \
            else "getter_succeeded" if row["kern_return"] == 0 else "unverified"
        if row["status"] != expected:
            raise ValueError(invalid)
    for key, targets, fields in (
        ("descriptors", ("inherited_fd_197", "inherited_fd_198"),
         {"target", "status", "errno"}),
        ("sockets", tuple(item[0] for item in SOCKET_PRIMITIVES),
         {"target", "status", "errno", "stage"}),
    ):
        rows = value[key]
        if type(rows) is not list or len(rows) != len(targets):
            raise ValueError(invalid)
        for row, target in zip(rows, targets, strict=True):
            if (type(row) is not dict or set(row) != fields or row["target"] != target
                or (row["errno"] is not None and type(row["errno"]) is not int)):
                raise ValueError(invalid)
            if key == "descriptors":
                expected = "absent" if row["errno"] == errno.EBADF else \
                    "unexpected_inherited" if row["errno"] is None else "unverified"
            else:
                expected = (
                    "denied" if row["errno"] in (errno.EPERM, errno.EACCES)
                    else "unexpected_allowed" if row["errno"] is None else "unverified"
                )
                if row["stage"] != "socket_creation":
                    raise ValueError(invalid)
            if row["status"] != expected:
                raise ValueError(invalid)


def _diagnostic_error_graph(error):
    pending, indexes, nodes = [], {}, []

    def link(value):
        if value is None:
            return None
        identity = id(value)
        if identity not in indexes:
            indexes[identity] = len(pending)
            pending.append(value)
        return indexes[identity]

    root = link(error)
    for value in pending:
        rollback = isinstance(value, IncompleteRollback)
        nodes.append({
            "type": type(value).__name__, "errno": getattr(value, "errno", None),
            "cause": link(value.__cause__), "context": link(value.__context__),
            "members": [link(item) for item in getattr(value, "exceptions", ())],
            "rollback_failure": link(value.failure) if rollback else None,
            "rollback_errors": [link(item) for item in value.errors]
            if rollback else [],
        })
    return {"root": root, "nodes": nodes}


def _capability_child_snapshot():
    try:
        return _capability_snapshot()
    except BaseException as error:
        # The existing owned fork always exits after framing. Retain its known
        # first error before that exit; do not imply recovery of child resources.
        return {
            "pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
            "failure_graph": _diagnostic_error_graph(error),
            "cleanup_scope": "owned_child_exit_only_no_recovery_claim",
        }


def _validate_capability_child(value):
    if type(value) is dict and "failure_graph" not in value:
        _validate_capability_snapshot(value)
        return
    invalid = "capability_snapshot_invalid"
    if (type(value) is not dict or set(value) != {
        "pid", "ppid", "uid", "failure_graph", "cleanup_scope"
    } or any(type(value[key]) is not int or value[key] < 0
             for key in ("pid", "ppid", "uid"))
        or value["pid"] == 0
        or value["cleanup_scope"] != "owned_child_exit_only_no_recovery_claim"):
        raise ValueError(invalid)
    graph = value["failure_graph"]
    if (type(graph) is not dict or set(graph) != {"root", "nodes"}
        or type(graph["nodes"]) is not list or not 1 <= len(graph["nodes"]) <= 128):
        raise ValueError(invalid)

    def valid_link(index):
        return index is None or type(index) is int and 0 <= index < len(graph["nodes"])

    if graph["root"] is None or not valid_link(graph["root"]):
        raise ValueError(invalid)
    for row in graph["nodes"]:
        if (type(row) is not dict or set(row) != {
            "type", "errno", "cause", "context", "members",
            "rollback_failure", "rollback_errors"
        } or type(row["type"]) is not str
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", row["type"])
            or (row["errno"] is not None and type(row["errno"]) is not int)
            or any(not valid_link(row[key])
                   for key in ("cause", "context", "rollback_failure"))
            or any(type(row[key]) is not list or len(row[key]) > 128
                   or any(not valid_link(index) for index in row[key])
                   for key in ("members", "rollback_errors"))):
            raise ValueError(invalid)


def system_capability_family():
    from .native_probe import _fork_probe

    # Fork before even these fixed libSystem getters, never after a framework.
    child = _fork_probe(_capability_child_snapshot, timeout=5)
    _validate_capability_child(child)
    if "failure_graph" in child:
        # Preserve the first known failure. A second getter/release/control
        # failure in the parent must not erase the already received child graph.
        parent = {"pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
                  "status": "not_run_after_child_failure"}
    else:
        parent = _capability_snapshot()
        _validate_capability_snapshot(parent)
    if child["ppid"] != parent["pid"] or child["pid"] == parent["pid"]:
        raise ValueError("capability_child_binding_invalid")
    return {"status": "RAW_CAPABILITY_FAMILY", "parent": parent, "child": child,
            "complete": "failure_graph" not in child}


def collect(request):
    validate(request)
    # Fork before Security/CoreFoundation initialize background threads.
    rows = [
        {
            "target": "fixed_exec",
            "action": "execve_usr_bin_true",
            **probe_fixed_exec(),
        }
    ]
    for name, path in sorted(request["targets"].items()):
        for write in (False, True):
            observation = observed(lambda: file_access(path, write=write))
            if (
                name == "candidate"
                and not write
                and path == own_probe_source()
                and observation["status"] == "unexpected_allowed"
            ):
                observation = {"status": "expected_allowed", "errno": None}
            rows.append(
                {
                    "target": name,
                    "action": "write_open" if write else "read",
                    **observation,
                }
            )
    target = request["targets"]["gold"]
    suffix = "child" if request["contract"] == CHILD else "parent"
    link = Path.cwd() / ("probe-link-" + request["nonce"] + "-" + suffix)
    try:
        link.symlink_to(target)
    except OSError as error:
        rows.append(
            {
                "target": "symlink_gold",
                "status": "unverified",
                "errno": error.errno,
                "action": "create_link",
            }
        )
    else:
        rows.append(
            {
                "target": "symlink_gold",
                "action": "read",
                **observed(lambda: file_access(link)),
            }
        )
    rows.append(
        {
            "target": "traversal_gold",
            "action": "read",
            **observed(lambda: file_access(os.path.relpath(target, Path.cwd()))),
        }
    )
    for family, address, tcp, udp in (
        (socket.AF_INET, "127.0.0.1", "ipv4_port", "udp4_port"),
        (socket.AF_INET6, "::1", "ipv6_port", "udp6_port"),
    ):
        for kind, key in ((socket.SOCK_STREAM, tcp), (socket.SOCK_DGRAM, udp)):
            rows.append(
                {
                    "target": key,
                    "action": "connect_or_send",
                    **observed(
                        lambda: network(
                            family, kind, address, request[key], request["nonce"]
                        )
                    ),
                }
            )
    for family, label, address in (
        (socket.AF_INET, "ipv4", "1.1.1.1"),
        (socket.AF_INET6, "ipv6", "2606:4700:4700::1111"),
    ):
        # Public resolver and HTTPS endpoint, with no user data or API credential.
        rows.append(
            {
                "target": f"external_tcp_{label}",
                "action": "connect",
                **observed(
                    lambda: network(
                        family, socket.SOCK_STREAM, address, 443, request["nonce"]
                    )
                ),
            }
        )
        rows.append(
            {
                "target": f"external_udp_{label}",
                "action": "send",
                **observed(
                    lambda: network(
                        family, socket.SOCK_DGRAM, address, 53, request["nonce"]
                    )
                ),
            }
        )
    rows.append(
        {
            "target": "system_dns",
            "action": "getaddrinfo_example_com",
            **observed(lambda: socket.getaddrinfo("example.com", 443)),
        }
    )
    rows.append(
        {
            "target": "proxy_loopback",
            "action": "connect",
            **observed(
                lambda: network(
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    "127.0.0.1",
                    request["browser_port"],
                    request["nonce"],
                )
            ),
        }
    )
    for fd in (197, 198):
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
            observation = {"status": "unexpected_inherited"}
        except OSError as error:
            observation = {
                "status": "absent" if error.errno == errno.EBADF else "unverified",
                "errno": error.errno,
            }
        rows.append({"target": f"inherited_fd_{fd}", "action": "fstat", **observation})
    root_result = observed(lambda: os.setuid(0))
    if root_result["status"] == "unexpected_allowed":
        # Do not continue probing or spawn descendants with changed authority.
        raise ValueError("probe_unexpected_root_authority")
    rows.append({"target": "setuid_root", "action": "setuid", **root_result})
    keychain = (
        {"status": "not_run", "reason": "keychain_entry_not_selected"}
        if request["keychain_path"] is None
        else _system_probe_in_child(
            partial(
                keychain_access,
                request["keychain_path"],
                request["keychain_service"],
                request["keychain_account"],
            )
        )
    )
    rows.append({"target": "test_keychain", "action": "read", **keychain})
    rows.append(
        {
            "target": "system_browser",
            "action": "open_loopback_canary",
            **_system_probe_in_child(
                partial(delegate_browser, request["browser_port"], request["nonce"])
            ),
        }
    )
    return {
        "contract": CONTRACT,
        "version": 1,
        "nonce": request["nonce"],
        "request_sha256": digest(request),
        "pid": os.getpid(),
        "uid": os.getuid(),
        "ppid": os.getppid(),
        "inherited_child": request["contract"] == CHILD,
        "environment_names": sorted(os.environ),
        "observations": rows,
        "actual_isolation": "REQUIRES_HOST_CORROBORATION",
    }


def validate_child(value, request):
    fields = {
        "contract",
        "version",
        "nonce",
        "request_sha256",
        "pid",
        "uid",
        "ppid",
        "inherited_child",
        "environment_names",
        "observations",
        "actual_isolation",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or value["contract"] != CONTRACT
        or value["version"] != 1
        or type(value["version"]) is not int
        or value["nonce"] != request["nonce"]
        or value["request_sha256"] != digest(request)
        or value["inherited_child"] is not True
        or value["actual_isolation"] != "REQUIRES_HOST_CORROBORATION"
        or type(value["environment_names"]) is not list
        or len(value["environment_names"]) > 64
        or any(
            type(name) is not str or len(name) > 128
            for name in value["environment_names"]
        )
        or any(
            type(value[key]) is not int or value[key] < 0
            for key in ("pid", "uid", "ppid")
        )
        or type(value["observations"]) is not list
        or len(value["observations"]) != 34
    ):
        raise ValueError("extended_probe_child_mismatch")
    names = set(TARGETS) | {
        "fixed_exec",
        "symlink_gold",
        "traversal_gold",
        "ipv4_port",
        "ipv6_port",
        "udp4_port",
        "udp6_port",
        "browser_port",
        "external_tcp_ipv4",
        "external_tcp_ipv6",
        "external_udp_ipv4",
        "external_udp_ipv6",
        "system_dns",
        "proxy_loopback",
        "inherited_fd_197",
        "inherited_fd_198",
        "setuid_root",
        "test_keychain",
        "system_browser",
    }
    seen = set()
    for row in value["observations"]:
        if (
            type(row) is not dict
            or not {"target", "action", "status"} <= set(row)
            or set(row)
            - {
                "target",
                "action",
                "status",
                "errno",
                "osstatus",
                "stage",
                "reason",
                "error_domain",
            }
            or row["target"] not in names
            or row["status"]
            not in {
                "denied",
                "unverified",
                "unexpected_allowed",
                "expected_allowed",
                "unexpected_inherited",
                "absent",
                "not_run",
            }
            or type(row["action"]) is not str
            or len(row["action"]) > 64
        ):
            raise ValueError("extended_probe_child_mismatch")
        pair = (row["target"], row["action"])
        if pair in seen:
            raise ValueError("extended_probe_child_mismatch")
        seen.add(pair)
        for key in ("errno", "osstatus"):
            if row.get(key) is not None and type(row[key]) is not int:
                raise ValueError("extended_probe_child_mismatch")
        for key in ("stage", "reason"):
            if key in row and (type(row[key]) is not str or len(row[key]) > 128):
                raise ValueError("extended_probe_child_mismatch")
        if row["status"] == "denied" and (
            row.get("error_domain") is not None
            or (
                row.get("errno") not in (errno.EACCES, errno.EPERM)
                and row.get("osstatus") not in (-34018, -10826)
            )
        ):
            raise ValueError("extended_probe_child_mismatch")
        if row["status"] == "absent" and row.get("errno") != errno.EBADF:
            raise ValueError("extended_probe_child_mismatch")
        if row["status"] == "expected_allowed" and (
            pair != ("candidate", "read") or row.get("errno") is not None
        ):
            raise ValueError("extended_probe_child_mismatch")
        if row.get("stage") not in (None, "open", "read", "disable_ui"):
            raise ValueError("extended_probe_child_mismatch")
        if row.get("error_domain") not in (None, "gai", "h"):
            raise ValueError("extended_probe_child_mismatch")
        if row.get("reason") not in (
            None,
            "exec_child_failed",
            "system_probe_child_failed",
            "canary_url_failed",
            "keychain_entry_not_selected",
        ):
            raise ValueError("extended_probe_child_mismatch")


def probe_main(request, frames):
    from .native_probe import _fork_probe, _is_probe_bundle

    if not _is_probe_bundle():
        raise ValueError("native_probe_bundle_required")
    validate(request)
    if request["targets"]["candidate"] != own_probe_source():
        raise ValueError("extended_probe_candidate_source_mismatch")
    child_result = None
    if request["contract"] == CONTRACT:
        child_request = {**request, "contract": CHILD}
        try:
            child = _fork_probe(
                lambda: collect(child_request),
                timeout=20,
            )
            validate_child(child, child_request)
            child_result = child
        except (OSError, EOFError, ValueError) as error:
            raise_if_rollback_pending(error)
            child_result = {"status": "unverified", "reason": "child_probe_failed"}
    # Fork before loading Security/CoreFoundation or making service calls in
    # this process; forking after those libraries create threads is unsafe.
    result = collect(request)
    if request["contract"] == CONTRACT:
        result["child"] = child_result
    frames.write(result, deadline=monotonic() + 5)


def system_probe_main(request, frames):
    """Run a fixed service call or capability diagnostic in a fresh process.

    LaunchServices/libdispatch can abort on the child side of fork before a
    permission answer exists. Each call therefore consumes its own probe
    process. No product operation, credential, arbitrary URL or hostname is
    accepted. The host must corroborate browser canary and process outcome.
    """
    from .native_probe import _is_probe_bundle

    if not _is_probe_bundle():
        raise ValueError("native_probe_bundle_required")
    validate_system_request(request)
    if request["action"] == "browser":
        observation = delegate_browser(request["browser_port"], request["nonce"])
        target, action = "system_browser", "open_loopback_canary"
    elif request["action"] == "dns":
        observation = observed(lambda: socket.getaddrinfo("example.com", 443))
        target, action = "system_dns", "getaddrinfo_example_com"
    elif request["action"] == "capability_family":
        observation = system_capability_family()
        target, action = "system_capability_family", "fixed_parent_and_fork"
    else:
        observation = system_capabilities()
        target, action = "system_capabilities", "fixed_own_task_getters"
    frames.write(
        {
            "contract": SYSTEM_CONTRACT,
            "version": 1,
            "nonce": request["nonce"],
            "request_sha256": digest(request),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "uid": os.getuid(),
            "environment_names": sorted(os.environ),
            "observation": {"target": target, "action": action, **observation},
            "actual_isolation": "REQUIRES_HOST_CORROBORATION",
        },
        deadline=monotonic() + 5,
    )
