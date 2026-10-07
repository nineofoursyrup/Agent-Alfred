"""Fixed non-billing #105 probes, executed only by explicit owner preparation.

These functions are not called at import/build time. They probe only disposable
sentinel files and IPv4/IPv6 loopback TCP listeners supplied by the trusted host.
They never receive credentials, call owner authentication, send provider traffic,
or accept a command, URL, SQL statement, helper path or host RPC.
"""

import errno
import hashlib
import os
import plistlib
import re
import signal
import socket
import sys
from collections import deque
from datetime import UTC, datetime
from functools import partial
from itertools import starmap
from operator import call
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

from agent_alfred.resource_rollback import (
    OwnedDescriptor,
    ResumableRollback,
    capture_call_result,
    raise_if_rollback_pending,
)

from ..schema import digest

PROBE_CONTRACT = "V1-LOCAL-OS-PROBE"
CHILD_CONTRACT = "V1-LOCAL-OS-PROBE-CHILD"
SENTINELS = (
    "owner.txt",
    "provider-sentinel.txt",
    "approval.json",
    "ledger.sqlite",
    "witness.json",
    "other-case.txt",
)


def validate_probe_request(value):
    if type(value) is not dict or set(value) != {
        "contract",
        "version",
        "nonce",
        "sentinel_root",
        "expected_sha256",
        "ipv4_port",
        "ipv6_port",
    }:
        raise ValueError("native_probe_request_invalid")
    if (
        value["contract"] not in (PROBE_CONTRACT, CHILD_CONTRACT)
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["nonce"]) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", value["nonce"])
        or type(value["sentinel_root"]) is not str
        or type(value["expected_sha256"]) is not dict
        or set(value["expected_sha256"]) != set(SENTINELS)
    ):
        raise ValueError("native_probe_request_invalid")
    root = Path(value["sentinel_root"])
    if (
        not root.is_absolute()
        or ".." in root.parts
        or str(root) != value["sentinel_root"]
        or not re.fullmatch(r"alfred-local-probe-sentinels-[a-f0-9]{32}", root.name)
    ):
        raise ValueError("native_probe_sentinel_root_invalid")
    for fingerprint in value["expected_sha256"].values():
        if type(fingerprint) is not str or not re.fullmatch(
            r"[a-f0-9]{64}", fingerprint
        ):
            raise ValueError("native_probe_sentinel_digest_invalid")
    for key in ("ipv4_port", "ipv6_port"):
        if type(value[key]) is not int or not 1024 <= value[key] <= 65535:
            raise ValueError("native_probe_port_invalid")
    return root


def _failure(error):
    return {
        "status": "denied"
        if error.errno in (errno.EACCES, errno.EPERM)
        else "unverified",
        "errno": error.errno,
        "sha256": None,
    }


def _file_probe(path, expected, *, write=False):
    try:
        flags = (os.O_WRONLY | os.O_APPEND) if write else os.O_RDONLY
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "ab" if write else "rb") as stream:
            if write:
                # Disposable sentinel only. Unexpected success is recorded as a
                # failure, preserving the append instead of repairing evidence.
                stream.write(b"\nnative-probe-unexpected-write\n")
                stream.flush()
                return {"status": "unexpected_allowed", "errno": None, "sha256": None}
            data = stream.read(65537)
            observed = hashlib.sha256(data).hexdigest()
            return {
                "status": "unexpected_allowed"
                if observed == expected
                else "unverified",
                "errno": None,
                "sha256": observed,
            }
    except OSError as error:
        return _failure(error)


def _loopback_probe(family, port):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            connection.connect(
                ("127.0.0.1" if family == socket.AF_INET else "::1", port)
            )
        return {"status": "unexpected_allowed", "errno": None, "sha256": None}
    except OSError as error:
        return _failure(error)


def collect_probe(request, *, inherited_child=False):
    """Perform only the fixed probes; unknown/missing/refused is never DENIED."""
    root = validate_probe_request(request)
    rows = []
    for name in SENTINELS:
        for action in ("read", "write"):
            rows.append(
                {
                    "target": name,
                    "action": action,
                    **_file_probe(
                        root / name,
                        request["expected_sha256"][name],
                        write=action == "write",
                    ),
                }
            )
    for family, key, name in (
        (socket.AF_INET, "ipv4_port", "ipv4_loopback_tcp"),
        (socket.AF_INET6, "ipv6_port", "ipv6_loopback_tcp"),
    ):
        rows.append(
            {
                "target": name,
                "action": "connect",
                **_loopback_probe(family, request[key]),
            }
        )
    return {
        "contract": PROBE_CONTRACT,
        "version": 1,
        "nonce": request["nonce"],
        "request_sha256": digest(request),
        "at": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "inherited_child": inherited_child,
        "observations": rows,
        "scope": "sentinel_files_and_loopback_tcp_only",
        "actual_isolation": "REQUIRES_HOST_CORROBORATION",
    }


def _is_probe_bundle():
    executable = Path(sys.executable)
    info = executable.parent.parent / "Info.plist"
    try:
        identity = plistlib.loads(info.read_bytes())["CFBundleIdentifier"]
    except OSError, KeyError, plistlib.InvalidFileException:
        return False
    return type(identity) is str and bool(
        re.fullmatch(r"local\.agent-alfred\.runner\.[a-f0-9]{24}\.probe", identity)
    )


def _validate_child_result(value, request):
    expected_pairs = [
        (name, action) for name in SENTINELS for action in ("read", "write")
    ]
    expected_pairs += [
        (name, "connect") for name in ("ipv4_loopback_tcp", "ipv6_loopback_tcp")
    ]
    if (
        type(value) is not dict
        or set(value)
        != {
            "contract",
            "version",
            "nonce",
            "request_sha256",
            "at",
            "pid",
            "inherited_child",
            "observations",
            "scope",
            "actual_isolation",
        }
        or value["contract"] != PROBE_CONTRACT
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["nonce"] != request["nonce"]
        or value["request_sha256"] != digest(request)
        or value["inherited_child"] is not True
        or type(value["pid"]) is not int
        or value["pid"] <= 0
        or type(value["at"]) is not str
        or len(value["at"]) > 40
        or value["scope"] != "sentinel_files_and_loopback_tcp_only"
        or value["actual_isolation"] != "REQUIRES_HOST_CORROBORATION"
        or type(value["observations"]) is not list
        or len(value["observations"]) != len(expected_pairs)
    ):
        raise ValueError("native_probe_child_mismatch")
    for row, pair in zip(value["observations"], expected_pairs, strict=True):
        if (
            type(row) is not dict
            or set(row) != {"target", "action", "status", "errno", "sha256"}
            or (row["target"], row["action"]) != pair
            or row["status"] not in ("denied", "unverified", "unexpected_allowed")
            or (row["errno"] is not None and type(row["errno"]) is not int)
            or (
                row["status"] == "denied"
                and row["errno"] not in (errno.EACCES, errno.EPERM)
            )
            or (
                row["sha256"] is not None
                and (
                    type(row["sha256"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
                )
            )
        ):
            raise ValueError("native_probe_child_mismatch")


class _ForkedProbe:
    """Retain the pipe, child and one-shot effects across interruption edges."""

    def __init__(self):
        self.pipe = None
        self.pid = -1
        self.wait_result = (0, 0)
        self.read_end, self.write_end = OwnedDescriptor(), OwnedDescriptor()
        self.pipe_adopted = False
        self.signal_started = False
        self.signalled = False

    def close_end(self, index):
        if self.pipe is None:
            return
        if not self.pipe_adopted:
            # The raw tuple remains owned until both tokens and the handoff
            # fact are published in one C loop, without Python line edges.
            deque(
                starmap(call, (
                    (setattr, self.read_end, "fd", self.pipe[0]),
                    (setattr, self.write_end, "fd", self.pipe[1]),
                    (setattr, self, "pipe_adopted", True),
                )), maxlen=0,
            )
        (self.read_end if index == 0 else self.write_end).close()

    def poll(self):
        if self.pid > 0 and self.wait_result[0] != self.pid:
            capture_call_result(
                self, "wait_result", partial(os.waitpid, self.pid, os.WNOHANG)
            )
        return self.pid > 0 and self.wait_result[0] == self.pid

    def close(self):
        if self.pid > 0 and not self.poll():
            if self.signalled is False:
                if self.signal_started:
                    raise RuntimeError("native_probe_signal_outcome_unknown")
                result = map(
                    partial(setattr, self, "signalled"),
                    starmap(os.kill, ((self.pid, signal.SIGKILL),)),
                )
                # Mark, native call and capture use the same C-loop pattern as
                # OwnedDescriptor. An actual ambiguous call still isn't retried.
                deque(
                    starmap(call, (
                        (setattr, self, "signal_started", True),
                        (next, result),
                    )), maxlen=0,
                )
            until = monotonic() + 5
            while not self.poll():
                if monotonic() >= until:
                    raise TimeoutError("native_probe_child_not_reaped")
                sleep(0.01)
        self.close_end(1)
        self.close_end(0)


def _fork_probe(action, *, timeout):
    """Fixed callback, inherited kernel policy, bounded JSON, no new exec.

    All parent-side native results are published to a retained rollback owner.
    The fork child closes host control FDs and always exits, including failures.
    """
    from .local_ipc import Frames

    child = _ForkedProbe()
    resources = ResumableRollback()
    resources.own(child)
    try:
        capture_call_result(child, "pipe", os.pipe)
        capture_call_result(child, "pid", os.fork)
        deadline = monotonic() + timeout
        if child.pid == 0:
            try:
                # A nested probe may allocate its pipe at closed stdio FDs.
                # Keep its frame writer and close each other descriptor once.
                for descriptor in dict.fromkeys((child.pipe[0], 0, 1)):
                    if descriptor != child.pipe[1]:
                        os.close(descriptor)
                stream = SimpleNamespace(fileno=lambda: child.pipe[1])
                Frames(None, stream).write(action(), deadline=deadline)
            except BaseException:
                os._exit(1)
            os._exit(0)
        child.close_end(1)
        stream = SimpleNamespace(fileno=lambda: child.pipe[0])
        value = Frames(stream, None).read(deadline=deadline)
        while not child.poll():
            if monotonic() >= deadline:
                raise TimeoutError("native_probe_child_deadline")
            sleep(0.01)
        if (
            child.wait_result[1] != 0
            or type(value) is not dict
            or value.get("pid") != child.pid
        ):
            raise ValueError("native_probe_child_mismatch")
    except BaseException as error:
        if child.pid == 0:
            os._exit(1)
        resources.raise_failure(error)
    resources.close()
    return value


def probe_main(request, frames):
    if not _is_probe_bundle():
        raise ValueError("native_probe_bundle_required")
    validate_probe_request(request)
    child = request["contract"] == CHILD_CONTRACT
    child_result = None
    if not child:
        child_request = {**request, "contract": CHILD_CONTRACT}
        try:
            observed = _fork_probe(
                lambda: collect_probe(child_request, inherited_child=True),
                timeout=10,
            )
            _validate_child_result(observed, child_request)
            child_result = observed
        except (OSError, EOFError, ValueError) as error:
            raise_if_rollback_pending(error)
            child_result = {"status": "unverified", "reason": "child_probe_failed"}
    result = collect_probe(request, inherited_child=child)
    if not child:
        result["child"] = child_result
    frames.write(result, deadline=monotonic() + 5)


def dispatch():
    """Fixed native bootstrap; product bundles cannot select the probe handler."""
    from .local_ipc import Frames
    from .local_runner import worker_main

    frames = Frames(sys.stdin.buffer, sys.stdout.buffer)
    initial = frames.read(deadline=monotonic() + 30)
    from . import native_probe_channels as channels
    from . import native_probe_extended as extended

    if type(initial) is dict and initial.get("contract") == channels.CONTRACT:
        channels.probe_main(initial, frames)
        return

    if type(initial) is dict and initial.get("contract") in (
        extended.CONTRACT,
        extended.CHILD,
    ):
        extended.probe_main(initial, frames)
        return
    if type(initial) is dict and initial.get("contract") == extended.SYSTEM_CONTRACT:
        extended.system_probe_main(initial, frames)
        return
    if type(initial) is dict and initial.get("contract") in (
        PROBE_CONTRACT,
        CHILD_CONTRACT,
    ):
        probe_main(initial, frames)
    else:
        if _is_probe_bundle():
            raise ValueError("native_probe_cannot_run_product")
        worker_main(initial_frame=initial, frames=frames)
