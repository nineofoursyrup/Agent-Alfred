"""Native local boundaries; an inspected bundle is not isolation evidence.

Importing this module does not inspect the machine or start a process. Only the
explicit owner-authentication and runner-launch functions can start native code.
Their callers must first establish the installed host and the relevant consent.
"""

import fcntl
import hashlib
import os
import plistlib
import pwd
import re
import secrets
import signal
import stat
import struct
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path, PurePosixPath

from agent_alfred.resource_rollback import (
    ResumableRollback,
    capture_call_result,
    reraise_failure,
)

from ..materials import strict_json
from ..schema import digest, encode
from . import local_sandbox

AUTH_CONTRACT = "V1-LOCAL-OWNER-AUTH"
BUNDLE_CONTRACT = "V1-LOCAL-MACOS-BUNDLE"
MAX_FRAME = 4 * 1024 * 1024
AUTH_SECONDS = 300
RUNNER_ENTRYPOINTS = {
    "launcher": "Contents/MacOS/AlfredRunner",
    "python": "Contents/Helpers/AlfredPython",
    "runner": "Contents/Resources/runner.py",
}
ENTRYPOINTS = {
    "owner_helper": "OwnerApproval.app/Contents/MacOS/AlfredOwnerApproval",
}
LAUNCHER_ENTITLEMENTS = {}
HELPER_ENTITLEMENTS = {}


def _closed(value, fields, error):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise ValueError(error)


def _instant(value):
    if type(value) is not str:
        raise ValueError("native_time_invalid")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("native_time_invalid")
    return result


def _frame(value):
    payload = encode(value)
    if not payload or len(payload) > MAX_FRAME:
        raise ValueError("native_frame_size_exceeded")
    return struct.pack(">I", len(payload)) + payload


def _unframe(data):
    if len(data) < 4:
        raise ValueError("native_frame_truncated")
    size = struct.unpack(">I", data[:4])[0]
    if size < 1 or size > MAX_FRAME or len(data) != size + 4:
        raise ValueError("native_frame_invalid")
    return strict_json(data[4:], limit=MAX_FRAME)


def authorize_owner(helper_path, request, *, reason):
    """Ask the real owner once; cancellation/failure never yields an approval.

    The installation verifies the helper's path/signature before this call. The
    receipt establishes local owner presence, not billing truth or a run grant.
    It must be persisted by the installed source, with the complete request.
    """
    if sys.platform != "darwin":
        raise ValueError("native_macos_required")
    helper = Path(helper_path)
    if not helper.is_absolute() or helper.is_symlink() or not helper.is_file():
        raise ValueError("native_owner_helper_invalid")
    if type(request) is not dict or not request:
        raise ValueError("native_owner_request_invalid")
    if type(reason) is not str or not reason.strip() or len(reason) > 2048:
        raise ValueError("native_owner_reason_invalid")
    started = datetime.now(UTC)
    challenge = {
        "contract": AUTH_CONTRACT,
        "version": 1,
        "nonce": secrets.token_hex(32),
        "request_sha256": digest(request),
        "request": request,
        "reason": reason,
        "expires_at": (started + timedelta(seconds=AUTH_SECONDS)).isoformat(),
    }
    try:
        result = subprocess.run(
            [str(helper)],
            input=_frame(challenge),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            close_fds=True,
            timeout=AUTH_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("native_owner_authentication_failed") from error
    receipt = _unframe(result.stdout)
    _closed(
        receipt,
        "contract version nonce request_sha256 expires_at owner_uid "
        "authenticated_at authentication approved",
        "native_owner_receipt_invalid",
    )
    if (
        any(
            receipt[key] != challenge[key]
            for key in ("contract", "version", "nonce", "request_sha256", "expires_at")
        )
        or type(receipt["version"]) is not int
        or type(receipt["owner_uid"]) is not int
        or receipt["owner_uid"] != os.getuid()
        or receipt["authentication"] != "deviceOwnerAuthentication"
        or receipt["approved"] is not True
    ):
        raise ValueError("native_owner_receipt_mismatch")
    authenticated = _instant(receipt["authenticated_at"])
    finished = datetime.now(UTC)
    if not started <= authenticated <= finished <= _instant(challenge["expires_at"]):
        raise ValueError("native_owner_receipt_expired")
    return receipt


def _relative(value):
    if type(value) is not str or "\\" in value:
        raise ValueError("native_bundle_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or str(path) != value:
        raise ValueError("native_bundle_path_invalid")
    if any(part in (".", "..") for part in path.parts):
        raise ValueError("native_bundle_path_invalid")
    return path


def _file_row(root, path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("native_bundle_file_unprotected")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("native_bundle_changed_during_read")
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        if (
            actual.st_size,
            actual.st_mtime_ns,
            actual.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("native_bundle_changed_during_read")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": fingerprint,
        "size": info.st_size,
        "mode": stat.S_IMODE(info.st_mode),
    }


def bundle_inventory(bundle_root):
    """Hash every regular bundle file; reject links and unprotected entries."""
    root = Path(bundle_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("native_bundle_root_invalid")
    rows = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        folder = Path(directory)
        for name in dirs:
            info = (folder / name).lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                raise ValueError("native_bundle_directory_unprotected")
        for name in files:
            rows.append(_file_row(root, folder / name))
    return sorted(rows, key=lambda row: row["path"])


def _codesign(path, *, entitlements=None):
    try:
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={},
            close_fds=True,
            timeout=30,
            check=True,
        )
        if entitlements is not None:
            result = subprocess.run(
                [
                    "/usr/bin/codesign",
                    "--display",
                    "--entitlements",
                    "-",
                    "--xml",
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={},
                close_fds=True,
                timeout=30,
                check=True,
            )
            # codesign uses stdout for the plist and stderr for diagnostics.
            actual = plistlib.loads(result.stdout) if result.stdout.strip() else {}
            if actual != entitlements:
                raise ValueError("native_entitlements_mismatch")
    except (
        OSError,
        subprocess.SubprocessError,
        plistlib.InvalidFileException,
    ) as error:
        raise ValueError("native_signature_unverifiable") from error


def verify_bundle(bundle_root, manifest):
    """Verify exact candidate bytes/signatures without launching native code.

    This result explicitly cannot satisfy actual process, filesystem, Keychain,
    egress, caller-authentication, or provider amount-cap requirements.
    """
    if sys.platform != "darwin":
        raise ValueError("native_macos_required")
    _closed(
        manifest,
        "contract version package_id source_id entrypoints slots probe files boundary",
        "native_bundle_manifest_invalid",
    )
    if (
        manifest["contract"] != BUNDLE_CONTRACT
        or type(manifest["version"]) is not int
        or manifest["version"] != 3
        or manifest["entrypoints"] != ENTRYPOINTS
        or type(manifest["source_id"]) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", manifest["source_id"])
        or type(manifest["package_id"]) is not str
        or not re.fullmatch(r"[a-f0-9]{64}", manifest["package_id"])
        or type(manifest["slots"]) is not list
        or not 1 <= len(manifest["slots"]) <= 30
        or type(manifest["files"]) is not list
        or not manifest["files"]
    ):
        raise ValueError("native_bundle_manifest_invalid")
    local_sandbox.verify_boundary(manifest["boundary"])
    for row in manifest["files"]:
        _closed(row, "path sha256 size mode", "native_bundle_file_invalid")
        _relative(row["path"])
    root = Path(bundle_root)
    if bundle_inventory(root) != manifest["files"]:
        raise ValueError("native_bundle_inventory_mismatch")
    identities = set()
    for index, slot in enumerate(manifest["slots"]):
        _closed(slot, "index bundle_id path entrypoints", "native_slot_invalid")
        if (
            type(slot["index"]) is not int
            or slot["index"] != index
            or slot["path"] != f"slots/{index:02d}.app"
            or slot["entrypoints"] != RUNNER_ENTRYPOINTS
            or slot["bundle_id"]
            != (
                f"local.agent-alfred.runner.{manifest['package_id'][:24]}.slot{index:02d}"
            )
            or type(slot["bundle_id"]) is not str
            or not re.fullmatch(
                r"local\.agent-alfred\.runner\.[a-f0-9]{24}\.slot[0-9]{2}",
                slot["bundle_id"],
            )
            or slot["bundle_id"] in identities
        ):
            raise ValueError("native_slot_invalid")
        identities.add(slot["bundle_id"])
        app = root / slot["path"]
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != slot["bundle_id"]:
            raise ValueError("native_bundle_identifier_mismatch")
        _verify_profile(app, slot["bundle_id"])
        for name, entitlement in (
            ("launcher", LAUNCHER_ENTITLEMENTS),
            ("python", HELPER_ENTITLEMENTS),
        ):
            _codesign(app / RUNNER_ENTRYPOINTS[name], entitlements=entitlement)
        _codesign(app)
    _codesign(root / ENTRYPOINTS["owner_helper"], entitlements={})
    _codesign(root / "OwnerApproval.app")
    probe = manifest["probe"]
    _closed(probe, "bundle_id path entrypoints", "native_probe_bundle_invalid")
    if (
        probe["bundle_id"]
        != f"local.agent-alfred.runner.{manifest['package_id'][:24]}.probe"
        or probe["path"] != "Probe.app"
        or probe["entrypoints"] != RUNNER_ENTRYPOINTS
    ):
        raise ValueError("native_probe_bundle_invalid")
    probe_app = root / probe["path"]
    info = plistlib.loads((probe_app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != probe["bundle_id"]:
        raise ValueError("native_bundle_identifier_mismatch")
    _verify_profile(probe_app, probe["bundle_id"])
    _codesign(probe_app)
    for name, entitlements in (
        ("launcher", LAUNCHER_ENTITLEMENTS),
        ("python", HELPER_ENTITLEMENTS),
    ):
        _codesign(probe_app / RUNNER_ENTRYPOINTS[name], entitlements=entitlements)
    return {
        "candidate_id": digest(manifest),
        "slot_count": len(manifest["slots"]),
        "probe_bundle": probe["bundle_id"],
        "entrypoints": {
            name: str(root / relative) for name, relative in ENTRYPOINTS.items()
        },
        "file_count": len(manifest["files"]),
        "inspection": "exact_inventory_and_codesign_only",
        "actual_isolation": "NOT_VERIFIED",
        "boundary": deepcopy(manifest["boundary"]),
    }


def _tree_identity(root):
    rows = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in files)]:
            info = path.lstat()
            if (
                not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                or info.st_mode & 0o022
                or info.st_uid not in (0, os.getuid())
            ):
                raise ValueError("native_bundle_identity_unprotected")
            rows.append(
                (
                    str(path.relative_to(root)),
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_uid,
                    info.st_gid,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("native_bundle_identity_unprotected")
    return tuple(sorted(rows))


class NativeBundleVerifier:
    """Pin exact bytes initially and active code's identity on every use.

    Selected slot bytes/signatures are checked again before launch. Changes fail
    instead of replacing the snapshot. This host-lifetime cache assumes the
    approved owner/kernel trust and #105 proof that runner cannot modify code.
    """

    def __init__(self, bundle_root, manifest):
        self.root = Path(bundle_root)
        self._manifest = deepcopy(manifest)
        before = _tree_identity(self.root)
        self._report = verify_bundle(self.root, self._manifest)
        self._identity = _tree_identity(self.root)
        if before != self._identity:
            raise ValueError("native_bundle_changed_during_read")
        self._active = None
        self._roots = {".", "slots", "OwnerApproval.app", "Probe.app"} | {
            slot["path"] for slot in self._manifest["slots"]
        }
        self._root_identity = tuple(
            row for row in self._identity if row[0] in self._roots
        )

    @property
    def manifest(self):
        return deepcopy(self._manifest)

    def _subtree_identity(self, prefix):
        return tuple(
            (("." if row[0] == prefix else row[0][len(prefix) + 1 :]), *row[1:])
            for row in self._identity
            if row[0] == prefix or row[0].startswith(prefix + "/")
        )

    def _current_roots(self):
        result = []
        for name in self._roots:
            info = (self.root / name).lstat()
            result.append(
                (
                    name,
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_uid,
                    info.st_gid,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
        return tuple(sorted(result))

    def verify(self):
        local_sandbox.verify_boundary(self._manifest["boundary"])
        if self._current_roots() != self._root_identity:
            raise ValueError("native_bundle_identity_changed")
        prefixes = ["OwnerApproval.app"]
        if self._active is not None:
            prefixes.append(self._active)
        for prefix in prefixes:
            if _tree_identity(self.root / prefix) != self._subtree_identity(prefix):
                raise ValueError("native_bundle_identity_changed")
        return deepcopy(self._report)

    def verify_slot(self, index):
        self.verify()
        if type(index) is not int or not 0 <= index < len(self._manifest["slots"]):
            raise ValueError("native_slot_index_invalid")
        slot = self._manifest["slots"][index]
        self._verify_app(slot)
        return deepcopy(slot)

    def verify_probe(self):
        self.verify()
        self._verify_app(self._manifest["probe"])
        return deepcopy(self._manifest["probe"])

    def _verify_app(self, slot):
        app = self.root / slot["path"]
        prefix = slot["path"] + "/"
        expected = [
            {**row, "path": row["path"][len(prefix) :]}
            for row in self._manifest["files"]
            if row["path"].startswith(prefix)
        ]
        if bundle_inventory(app) != expected:
            raise ValueError("native_bundle_inventory_mismatch")
        _verify_profile(app, slot["bundle_id"])
        _codesign(app)
        for name, entitlements in (
            ("launcher", LAUNCHER_ENTITLEMENTS),
            ("python", HELPER_ENTITLEMENTS),
        ):
            _codesign(app / RUNNER_ENTRYPOINTS[name], entitlements=entitlements)
        self._active = slot["path"]
        self.verify()


def _protected_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("native_protected_root_invalid")
    for parent in (path, *path.parents):
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_mode & 0o022
            or info.st_uid not in (0, os.getuid())
        ):
            raise ValueError("native_protected_root_invalid")
    return path


def _owner_container(bundle_id):
    # Ignore HOME, which may be attacker-controlled at a subprocess boundary.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library/Containers" / bundle_id


def _case_root(bundle_id):
    return _owner_container(bundle_id) / (
        "Data/Library/Application Support/Alfred/current-case"
    )


def _verify_profile(app, bundle_id):
    expected = local_sandbox.runner_profile(app, _case_root(bundle_id))
    if (app / local_sandbox.PROFILE_PATH).read_text() != expected:
        raise ValueError("native_sandbox_profile_mismatch")


class NativeRunnerSlot:
    """Single-use container ownership; closing a group is only best effort.

    The protected spent-slot marker is durable before creating any container.
    A failed start consumes its slot too. Each fixed policy grants only its own
    case directory, excluding all later cases even if a descendant survives;
    no process-group cleanup claim is used to justify reuse.
    """

    def __init__(
        self,
        bundle_root,
        manifest,
        *,
        slot_index,
        protected_root,
        job_id,
        operation_id,
        verifier=None,
        _probe=False,
    ):
        if verifier is None:
            verifier = NativeBundleVerifier(bundle_root, manifest)
        if (
            type(verifier) is not NativeBundleVerifier
            or verifier.root != Path(bundle_root)
            or verifier.manifest != manifest
        ):
            raise ValueError("native_verifier_mismatch")
        self._verifier = verifier
        report = verifier.verify()
        if report["slot_count"] != 30:
            raise ValueError("native_complete_slot_set_required")
        if not _probe and (type(slot_index) is not int or not 0 <= slot_index < 30):
            raise ValueError("native_slot_index_invalid")
        if not all(
            type(value) is str and 0 < len(value) <= 256
            for value in (job_id, operation_id)
        ):
            raise ValueError("native_slot_scope_invalid")
        self._bundle_root = Path(bundle_root)
        self._manifest = manifest
        self._slot = deepcopy(
            manifest["probe"] if _probe else manifest["slots"][slot_index]
        )
        self._probe = _probe
        self._protected = _protected_directory(protected_root)
        self._scope = {
            "candidate_id": report["candidate_id"],
            "slot_index": slot_index,
            "bundle_id": self._slot["bundle_id"],
            "job_id": job_id,
            "operation_id": operation_id,
        }
        self._container = _owner_container(self._slot["bundle_id"])
        self.root = _case_root(self._slot["bundle_id"])
        self._lease_fd = -1
        self._lease_closed = False
        self.process = None
        self._entered = False
        self._launched = False
        self._group_started = False
        self._group_closed = False
        self._group_signal_failure = None
        self._cleanup = ResumableRollback()
        self._cleanup.own(self, close=self._close_resources)

    def __enter__(self):
        if self._entered:
            raise ValueError("native_slot_already_entered")
        self._entered = True
        lease_path = self._protected / "native-runner.lock"
        try:
            capture_call_result(
                self,
                "_lease_fd",
                lambda: os.open(
                    lease_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
                ),
            )
            fd = self._lease_fd
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("native_lease_unprotected")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            slots = self._protected / "native-spent-slots"
            slots.mkdir(mode=0o700, exist_ok=True)
            _protected_directory(slots)
            # Bundle identifier, not job/candidate digest: re-sealing a manifest
            # must not make a previously used OS container eligible again.
            marker = slots / f"{self._slot['bundle_id']}.json"
            markfd = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(markfd, "wb") as output:
                output.write(encode(self._scope) + b"\n")
                output.flush()
                os.fsync(output.fileno())
            dirfd = os.open(slots, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            # No cleanup/reset of an existing container. It could have escaped
            # descendants or hidden prior data; a spent slot remains evidence.
            if self._container.exists() or self._container.is_symlink():
                raise ValueError("native_container_not_fresh")
            _protected_directory(self._container.parent)
            self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
            return self
        except BaseException as error:
            self._cleanup.raise_failure(error)

    def launch(self):
        if self._lease_fd < 0 or self._lease_closed is not False or self._launched:
            raise ValueError("native_slot_launch_not_available")
        self._launched = True
        if self._probe:
            self._verifier.verify_probe()
        else:
            self._verifier.verify_slot(self._slot["index"])
        _protected_directory(self.root)
        app = self._bundle_root / self._slot["path"]
        self.process = object.__new__(subprocess.Popen)
        subprocess.Popen.__init__(
            self.process,
            local_sandbox.launch_arguments(app),
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            close_fds=True,
            start_new_session=True,
        )
        return self.process

    def close(self):
        self._cleanup.close()

    def cleanup_observation(self):
        """Inert host audit facts, retaining recovered errors without secrets.

        A signal completion or absent group never proves all descendants exited.
        Only error type/errno and causal links are exported, not arbitrary error
        messages, exception owners or credential-bearing transport objects.
        """
        pending, indexes, errors = [], {}, []

        def link(value):
            if value is None:
                return None
            identity = id(value)
            if identity not in indexes:
                indexes[identity] = len(pending)
                pending.append(value)
            return indexes[identity]

        first = self._group_signal_failure
        roots = [link(error) for error in (first, *self._cleanup.errors)
                 if error is not None]
        for error in pending:
            errors.append({
                "type": type(error).__name__, "errno": getattr(error, "errno", None),
                "cause": link(error.__cause__), "context": link(error.__context__),
                "members": [link(e) for e in getattr(error, "exceptions", ())],
            })
        return {
            "contract": "V1-LOCAL-NATIVE-CLEANUP-OBSERVATION", "version": 1,
            "scope": deepcopy(self._scope),
            "pid": getattr(self.process, "pid", None),
            "leader_returncode": getattr(self.process, "returncode", None),
            "signal_attempt_started": self._group_started,
            "signal_error": None if first is None else {
                "type": type(first).__name__, "errno": first.errno,
            },
            "group_release": "signal_completed" if self._group_closed is None
            else "group_absent" if isinstance(self._group_closed, ProcessLookupError)
            else "unknown",
            "lease_closed": self._lease_fd < 0 or self._lease_closed is None,
            "descendants_scope": "same_group_best_effort_no_container_reuse",
            "error_roots": roots, "errors": errors,
        }

    def _close_resources(self):
        if self.process is not None:
            for name in ("stdin", "stdout"):
                stream = getattr(self.process, name, None)
                if stream is not None:
                    stream.close()
            if getattr(self.process, "_child_created", False):
                # Does not claim to find descendants that changed their session.
                # A completed kill effect is not repeated on a future PID reuse.
                if self._group_closed is False:
                    if getattr(self, "_group_started", False):
                        if getattr(self, "_group_signal_failure", None) is None:
                            raise ValueError("native_group_cleanup_outcome_unknown")
                    else:
                        self._group_started = True
                        try:
                            capture_call_result(
                                self,
                                "_group_closed",
                                partial(os.killpg, self.process.pid, signal.SIGKILL),
                            )
                        except ProcessLookupError:
                            # Unknown capture still cannot repeat a signal.
                            capture_call_result(self, "_group_closed", sys.exception)
                        except PermissionError:
                            capture_call_result(
                                self, "_group_signal_failure", sys.exception
                            )
                self.process.wait(timeout=10)
                if self._group_closed is False:
                    # macOS may refuse a zombie-only group. Reap only our child,
                    # then query without a signal. Child exit alone is not group
                    # absence; a live/unverifiable group keeps the first error.
                    try:
                        os.killpg(self.process.pid, 0)
                    except ProcessLookupError:
                        capture_call_result(self, "_group_closed", sys.exception)
                    except OSError as error:
                        reraise_failure(self._group_signal_failure, earlier=error)
                    else:
                        reraise_failure(self._group_signal_failure)
        # Keep the lease reachable if closing pipes/process fails. Retrying
        # cleanup cannot make the durable spent-slot marker reusable.
        if self._lease_fd >= 0 and self._lease_closed is False:
            capture_call_result(self, "_lease_closed", lambda: os.close(self._lease_fd))

    def __exit__(self, kind, error, traceback):
        if error is not None:
            self._cleanup.raise_failure(error)
        self.close()


def prepare_runner_slot(
    bundle_root,
    manifest,
    *,
    slot_index,
    protected_root,
    job_id,
    operation_id,
    verifier=None,
):
    """Return an explicit actual-setup context; construction itself only reads."""
    return NativeRunnerSlot(
        bundle_root,
        manifest,
        slot_index=slot_index,
        protected_root=protected_root,
        job_id=job_id,
        operation_id=operation_id,
        verifier=verifier,
    )


def prepare_native_probe(
    bundle_root, manifest, *, protected_root, probe_id, verifier=None
):
    """Explicit actual #105 setup only; never selected by a product case/worker."""
    return NativeRunnerSlot(
        bundle_root,
        manifest,
        slot_index=None,
        protected_root=protected_root,
        job_id=f"probe:{probe_id}",
        operation_id="native-probe",
        verifier=verifier,
        _probe=True,
    )
