"""Fixed Probe-only Unix canaries, native witness and bounded service stages.

Partial frames survive framework termination. They are observations, never a
product result, authenticated owner event or permission/admission conclusion.
"""

import errno
import os
import re
import socket
from functools import partial
from time import monotonic
from types import SimpleNamespace

from agent_alfred.resource_rollback import ResumableRollback, capture_call_result

from ..schema import digest
from . import native_probe_extended as extended

CONTRACT = "V1-LOCAL-OS-PROBE-CHANNELS"
WITNESS_FIELDS = frozenset((
    "single_thread", "registered_empty", "task_exceptions_empty",
    "thread_exceptions_empty", "namespace_confined", "existing_guard_confined",
    "bootstrap_cache_empty",
))


def validate_request(value):
    invalid = "channel_probe_request_invalid"
    if (type(value) is not dict or set(value) != {
        "contract", "version", "nonce", "stream_path", "dgram_path", "browser_port",
    } or value["contract"] != CONTRACT or type(value["version"]) is not int
        or value["version"] != 1 or type(value["nonce"]) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", value["nonce"])
        or type(value["browser_port"]) is not int
        or not 1024 <= value["browser_port"] <= 65535):
        raise ValueError(invalid)
    for field, leaf in (("stream_path", "stream.sock"),
                        ("dgram_path", "datagram.sock")):
        path = value[field]
        if (type(path) is not str or len(os.fsencode(path)) > 103
            or not re.fullmatch(
                r"/private/tmp/alfred-local-channels-[a-f0-9]{32}/"
                + re.escape(leaf), path
            )):
            raise ValueError(invalid)
    if value["stream_path"].rsplit("/", 1)[0] != \
            value["dgram_path"].rsplit("/", 1)[0]:
        raise ValueError(invalid)


def native_witness():
    # Only the fixed native launcher registers this parameterless builtin.
    # Neither installed host nor ordinary fixtures claim it exists there.
    from _alfred_boundary_witness import witness

    result = witness()
    if (type(result) is not dict or set(result) != WITNESS_FIELDS
        or any(type(bit) is not bool for bit in result.values())):
        raise ValueError("channel_probe_witness_invalid")
    return result


def unix_observation(kind, path, nonce):
    owner = SimpleNamespace(connection=None)
    resources = ResumableRollback()
    resources.own(owner, lambda: owner.connection.close()
                  if owner.connection is not None else None)

    stage = "socket_creation"

    def perform():
        nonlocal stage
        try:
            capture_call_result(owner, "connection", partial(
                socket.SocketType, socket.AF_UNIX, kind,
            ))
            owner.connection.settimeout(1)
            if kind == socket.SOCK_STREAM:
                stage = "connect"
                owner.connection.connect(path)
                stage = "send"
                owner.connection.sendall(nonce.encode())
            else:
                stage = "sendto"
                owner.connection.sendto(nonce.encode(), path)
        except BaseException as error:
            resources.raise_failure(error)

    observation = extended.observed(perform)
    # Release faults cannot reclassify an actual successful operation as denial.
    resources.close()
    return {**observation, "stage": stage}


def snapshot(request):
    return {
        "pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
        "before": native_witness(),
        "unix": [{"target": name, **unix_observation(kind, request[field],
                                                    request["nonce"])}
                 for name, kind, field in (
                     ("unix_stream", socket.SOCK_STREAM, "stream_path"),
                     ("unix_dgram", socket.SOCK_DGRAM, "dgram_path"),
                 )],
        "after": native_witness(),
    }


def validate_snapshot(value):
    invalid = "channel_probe_family_invalid"
    if (type(value) is not dict or set(value) != {
        "pid", "ppid", "uid", "before", "after", "unix",
    } or any(type(value[key]) is not int or value[key] < 0
             for key in ("pid", "ppid", "uid")) or value["pid"] == 0):
        raise ValueError(invalid)
    for field in ("before", "after"):
        if (type(value[field]) is not dict or set(value[field]) != WITNESS_FIELDS
            or any(type(bit) is not bool for bit in value[field].values())):
            raise ValueError(invalid)
    if type(value["unix"]) is not list or len(value["unix"]) != 2:
        raise ValueError(invalid)
    for row, name in zip(value["unix"], ("unix_stream", "unix_dgram"), strict=True):
        if (type(row) is not dict or set(row) != {
            "target", "status", "errno", "stage",
        }
            or row["target"] != name
            or row["stage"] not in (
                "socket_creation", "connect", "send", "sendto",
            )
            or row["errno"] is not None and type(row["errno"]) is not int):
            raise ValueError(invalid)
        expected = ("denied",) if row["errno"] in (errno.EPERM, errno.EACCES) else \
            ("unexpected_allowed", "unverified") if row["errno"] is None else \
            ("unverified",)
        if row["status"] not in expected:
            raise ValueError(invalid)


def family(request):
    from .native_probe import _fork_probe

    # No CoreFoundation, LaunchServices or DNS call precedes this owned fork.
    child = _fork_probe(partial(snapshot, request), timeout=8)
    validate_snapshot(child)
    parent = snapshot(request)
    validate_snapshot(parent)
    if (child["ppid"] != parent["pid"] or child["pid"] == parent["pid"]
        or child["uid"] != parent["uid"]):
        raise ValueError("channel_probe_family_invalid")
    return {"parent": parent, "child": child, "status": "RAW_CHANNEL_FAMILY"}


def probe_main(request, frames):
    from .native_probe import _is_probe_bundle

    if not _is_probe_bundle():
        raise ValueError("native_probe_bundle_required")
    validate_request(request)
    sequence = 0

    def emit(stage, observation=None):
        nonlocal sequence
        sequence += 1
        frames.write({
            "contract": CONTRACT, "version": 1, "sequence": sequence,
            "nonce": request["nonce"], "request_sha256": digest(request),
            "pid": os.getpid(), "ppid": os.getppid(), "uid": os.getuid(),
            "environment_names": sorted(os.environ), "stage": stage,
            "observation": observation,
            "actual_isolation": "REQUIRES_HOST_CORROBORATION",
        }, deadline=monotonic() + 5)

    emit("family", family(request))
    emit("before_dns")
    dns = extended.observed(lambda: socket.getaddrinfo("example.com", 443))
    emit("after_dns", {"dns": dns, "witness": native_witness()})
    browser = extended.delegate_browser(
        request["browser_port"], request["nonce"], progress=emit,
    )
    emit("complete", {"browser": browser, "witness": native_witness()})
