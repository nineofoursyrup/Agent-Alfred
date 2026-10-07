"""Offline native protocol/inventory tests; no sandbox or auth UI is launched."""

import errno
import io
import os
import plistlib
import shutil
import struct
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled import native
from agent_alfred.evals.acceptance.materials import strict_json
from agent_alfred.evals.acceptance.schema import digest, encode
from agent_alfred.resource_rollback import IncompleteRollback


def auth_process(monkeypatch, mutate=lambda value: value):
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        frame = options["input"]
        assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4
        challenge = strict_json(frame[4:], limit=native.MAX_FRAME)
        receipt = {
            **{
                key: challenge[key]
                for key in (
                    "contract",
                    "version",
                    "nonce",
                    "request_sha256",
                    "expires_at",
                )
            },
            "owner_uid": os.getuid(),
            "authenticated_at": datetime.now(UTC).isoformat(),
            "authentication": "deviceOwnerAuthentication",
            "approved": True,
        }
        wire = encode(mutate(receipt))
        return SimpleNamespace(stdout=struct.pack(">I", len(wire)) + wire)

    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native.subprocess, "run", run)
    return calls


def test_owner_receipt_binds_exact_object_fresh_nonce_and_actual_uid(
    tmp_path, monkeypatch
):
    helper = tmp_path / "owner"
    helper.write_text("offline stand-in; never executed")
    calls = auth_process(monkeypatch)
    request = {"scope": "checkpoint", "summary": "原始争议", "threshold": None}
    first = native.authorize_owner(helper, request, reason="核对精确对象")
    second = native.authorize_owner(helper, request, reason="核对精确对象")
    assert first["request_sha256"] == digest(request)
    assert first["nonce"] != second["nonce"]
    assert first["owner_uid"] == os.getuid()
    assert first["approved"] is True
    assert set(first) == {
        "contract",
        "version",
        "nonce",
        "request_sha256",
        "expires_at",
        "owner_uid",
        "authenticated_at",
        "authentication",
        "approved",
    }
    for argv, options in calls:
        assert argv == [str(helper)]
        assert options["env"] == {}
        assert options["close_fds"] is True
        assert options["timeout"] == 300
        assert options["stderr"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    "mutation",
    [
        {"nonce": "0" * 64},
        {"owner_uid": -1},
        {"version": True},
        {"approved": False},
        {"request_sha256": "1" * 64},
        {"authenticated_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        {"authenticated_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        {"extra": "must not be accepted"},
    ],
)
def test_wrong_or_stale_owner_receipt_is_not_a_source_event(
    tmp_path, monkeypatch, mutation
):
    helper = tmp_path / "owner"
    helper.touch()
    auth_process(monkeypatch, lambda receipt: {**receipt, **mutation})
    with pytest.raises(ValueError, match="native_owner_receipt"):
        native.authorize_owner(helper, {"scope": "run"}, reason="fixed test")


def test_owner_cancellation_and_malformed_frame_fail_closed(tmp_path, monkeypatch):
    helper = tmp_path / "owner"
    helper.touch()
    monkeypatch.setattr(native.sys, "platform", "darwin")

    def cancelled(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(native.subprocess, "run", cancelled)
    with pytest.raises(ValueError, match="native_owner_authentication_failed"):
        native.authorize_owner(helper, {"scope": "run"}, reason="fixed test")
    for malformed in (
        b"",
        struct.pack(">I", native.MAX_FRAME + 1),
        struct.pack(">I", 2) + b"{}trailing",
        struct.pack(">I", 13) + b'{"x":1,"x":2}',
    ):
        monkeypatch.setattr(
            native.subprocess,
            "run",
            lambda *args, _data=malformed, **kwargs: SimpleNamespace(stdout=_data),
        )
        with pytest.raises(ValueError):
            native.authorize_owner(helper, {"scope": "run"}, reason="fixed test")


def bundle_fixture(tmp_path, monkeypatch, count=30):
    containers = tmp_path / "offline-container-fixture"
    containers.mkdir(exist_ok=True)
    monkeypatch.setattr(
        native, "_owner_container", lambda identity: containers / identity
    )
    monkeypatch.setattr(
        native.local_sandbox,
        "system_identity",
        lambda: {"offline": "fixture only; never OS evidence"},
    )
    root = tmp_path / "package"
    root.mkdir()
    owner = root / native.ENTRYPOINTS["owner_helper"]
    owner.parent.mkdir(parents=True)
    owner.write_text("offline native fixture; not executable")
    slots = []
    for index in range(count):
        slot = {
            "index": index,
            "bundle_id": f"local.agent-alfred.runner.{'a' * 24}.slot{index:02d}",
            "path": f"slots/{index:02d}.app",
            "entrypoints": deepcopy(native.RUNNER_ENTRYPOINTS),
        }
        app = root / slot["path"]
        for path in slot["entrypoints"].values():
            target = app / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("offline fixture")
        (app / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": slot["bundle_id"]})
        )
        (app / native.local_sandbox.PROFILE_PATH).write_text(
            native.local_sandbox.runner_profile(
                app, native._case_root(slot["bundle_id"])
            )
        )
        slots.append(slot)
    manifest = {
        "contract": native.BUNDLE_CONTRACT,
        "version": 3,
        "boundary": native.local_sandbox.boundary_contract(),
        "source_id": "a" * 64,
        "package_id": "a" * 64,
        "entrypoints": deepcopy(native.ENTRYPOINTS),
        "slots": slots,
    }
    shutil.copytree(root / "slots/00.app", root / "Probe.app")
    probe_id = f"local.agent-alfred.runner.{'a' * 24}.probe"
    (root / "Probe.app/Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": probe_id})
    )
    (root / "Probe.app" / native.local_sandbox.PROFILE_PATH).write_text(
        native.local_sandbox.runner_profile(
            root / "Probe.app", native._case_root(probe_id)
        )
    )
    manifest["probe"] = {
        "bundle_id": probe_id,
        "path": "Probe.app",
        "entrypoints": deepcopy(native.RUNNER_ENTRYPOINTS),
    }
    manifest["files"] = native.bundle_inventory(root)
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native, "_codesign", lambda *args, **kwargs: None)
    return root, manifest


def test_inventory_rejects_changed_bytes_extra_files_and_symlinks(
    tmp_path, monkeypatch
):
    root, manifest = bundle_fixture(tmp_path, monkeypatch, count=1)
    report = native.verify_bundle(root, manifest)
    assert report["actual_isolation"] == "NOT_VERIFIED"
    assert report["slot_count"] == 1
    owner = root / native.ENTRYPOINTS["owner_helper"]
    original = owner.read_bytes()
    owner.write_bytes(original + b"changed")
    with pytest.raises(ValueError, match="inventory_mismatch"):
        native.verify_bundle(root, manifest)
    owner.write_bytes(original)
    extra = root / "extra"
    extra.write_bytes(b"unreviewed dependency")
    with pytest.raises(ValueError, match="inventory_mismatch"):
        native.verify_bundle(root, manifest)
    extra.unlink()
    extra.symlink_to(owner)
    with pytest.raises(ValueError, match="unprotected"):
        native.verify_bundle(root, manifest)


def test_prior_app_sandbox_manifest_cannot_be_relabelled_as_current_boundary(
    tmp_path, monkeypatch
):
    root, manifest = bundle_fixture(tmp_path, monkeypatch, count=1)
    legacy = deepcopy(manifest)
    legacy["version"] = 2
    legacy.pop("boundary", None)
    with pytest.raises(ValueError, match="native_bundle_manifest_invalid"):
        native.verify_bundle(root, legacy)


def test_previous_selective_mach_boundary_cannot_admit_the_current_bundle(
    tmp_path, monkeypatch
):
    root, manifest = bundle_fixture(tmp_path, monkeypatch, count=1)
    legacy = deepcopy(manifest)
    legacy["boundary"]["version"] = 2
    with pytest.raises(ValueError, match="local_sandbox_boundary_changed"):
        native.verify_bundle(root, legacy)


@pytest.mark.parametrize("changed", ["os_build", "kernel_version"])
def test_os_or_loader_change_invalidates_an_already_inspected_bundle(
    tmp_path, monkeypatch, changed
):
    root, manifest = bundle_fixture(tmp_path, monkeypatch, count=1)
    verifier = native.NativeBundleVerifier(root, manifest)
    original = deepcopy(manifest["boundary"]["system"])
    monkeypatch.setattr(
        native.local_sandbox, "system_identity", lambda: {**original, changed: "new"}
    )
    with pytest.raises(ValueError, match="local_sandbox_boundary_changed"):
        verifier.verify_slot(0)


def test_resealing_a_broadened_profile_does_not_bypass_the_fixed_policy(
    tmp_path, monkeypatch
):
    root, manifest = bundle_fixture(tmp_path, monkeypatch, count=1)
    profile = root / manifest["slots"][0]["path"] / native.local_sandbox.PROFILE_PATH
    profile.write_text(profile.read_text() + "(allow network*)\n")
    manifest["files"] = native.bundle_inventory(root)
    with pytest.raises(ValueError, match="native_sandbox_profile_mismatch"):
        native.verify_bundle(root, manifest)


def test_launch_requires_the_fixed_loader_without_fallback(tmp_path, monkeypatch):
    prepare, protected, _ = slot_fixture(tmp_path, monkeypatch)
    called = []

    def loader_missing(process, args, **kwargs):
        called.append((args, kwargs))
        raise FileNotFoundError("offline loader unavailable")

    monkeypatch.setattr(native.subprocess.Popen, "__init__", loader_missing)
    with pytest.raises(FileNotFoundError, match="offline loader unavailable"):
        with prepare() as slot:
            slot.launch()
    assert len(called) == 1
    args, options = called[0]
    assert len(args) == 1
    assert args[0].endswith("/Contents/MacOS/AlfredRunner")
    assert options["env"] == {} and options["close_fds"] is True
    assert len(list((protected / "native-spent-slots").glob("*.json"))) == 1


def test_host_lifetime_pin_does_not_accept_replaced_equal_content(
    tmp_path, monkeypatch
):
    root, manifest = bundle_fixture(tmp_path, monkeypatch, count=1)
    verifier = native.NativeBundleVerifier(root, manifest)
    assert verifier.verify_slot(0)["index"] == 0
    owner = root / native.ENTRYPOINTS["owner_helper"]
    replacement = owner.with_name("replacement")
    replacement.write_bytes(owner.read_bytes())
    replacement.replace(owner)
    with pytest.raises(ValueError, match="identity_changed"):
        verifier.verify()


def slot_fixture(tmp_path, monkeypatch):
    root, manifest = bundle_fixture(tmp_path, monkeypatch)
    # Protocol/durability test roots, not actual ~/Library/Containers or OS proof.
    containers = tmp_path / "offline-container-fixture"
    containers.mkdir(exist_ok=True)
    protected = tmp_path / "host"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(
        native, "_owner_container", lambda identity: containers / identity
    )
    monkeypatch.setattr(native, "_protected_directory", lambda path: Path(path))
    verifier = native.NativeBundleVerifier(root, manifest)

    def prepare(index=0, job="job-one"):
        return native.prepare_runner_slot(
            root,
            manifest,
            slot_index=index,
            protected_root=protected,
            job_id=job,
            operation_id=f"operation-{index}",
            verifier=verifier,
        )

    return prepare, protected, containers


def test_slot_is_burned_before_preparation_and_never_reused_across_jobs(
    tmp_path, monkeypatch
):
    prepare, protected, containers = slot_fixture(tmp_path, monkeypatch)
    slot = prepare()
    assert not list(containers.iterdir())
    with slot:
        assert slot.root.is_dir()
        markers = list((protected / "native-spent-slots").glob("*.json"))
        assert len(markers) == 1
        assert strict_json(markers[0].read_bytes())["operation_id"] == "operation-0"
        with pytest.raises(BlockingIOError):
            with prepare(1):
                pytest.fail("second simultaneous owner entered")
    with pytest.raises(FileExistsError):
        with prepare(job="new-job-cannot-reset"):
            pytest.fail("spent slot was reused")
    with prepare(1) as next_slot:
        assert next_slot.root != slot.root
        assert next_slot.root.is_dir()


def test_existing_container_cannot_be_relabelled_as_fresh(tmp_path, monkeypatch):
    prepare, protected, _ = slot_fixture(tmp_path, monkeypatch)
    slot = prepare()
    slot.root.mkdir(parents=True)
    old = slot.root / "prior-hidden-material"
    old.write_text("keep original")
    with pytest.raises(ValueError, match="container_not_fresh"):
        with slot:
            pytest.fail("existing container accepted")
    assert old.read_text() == "keep original"
    assert len(list((protected / "native-spent-slots").glob("*.json"))) == 1
    with pytest.raises(FileExistsError):
        with prepare():
            pytest.fail("failed preparation reset a spent slot")


def test_offline_smoke_bundle_cannot_prepare_a_real_case(tmp_path, monkeypatch):
    root, manifest = bundle_fixture(tmp_path, monkeypatch, count=1)
    with pytest.raises(ValueError, match="complete_slot_set_required"):
        native.prepare_runner_slot(
            root,
            manifest,
            slot_index=0,
            protected_root=tmp_path,
            job_id="job",
            operation_id="operation",
        )


def test_cleanup_failure_retains_first_failure_and_lease_until_retry(
    tmp_path, monkeypatch
):
    prepare, _, _ = slot_fixture(tmp_path, monkeypatch)
    first = RuntimeError("original observation failure")
    failed = [True]
    kills = []

    class Process:
        _child_created = True
        pid = 123456789  # never sent to the OS: killpg is intercepted below
        stdin = io.BytesIO()
        stdout = io.BytesIO()

        def wait(self, timeout):
            if failed[0]:
                raise OSError("offline cleanup failure")
            return 0

    monkeypatch.setattr(native.os, "killpg", lambda *args: kills.append(args))
    slot = prepare()
    with pytest.raises(RuntimeError, match="original observation") as caught:
        with slot:
            slot.process = Process()
            raise first
    assert caught.value is first
    pending = first.__cause__
    assert isinstance(pending, IncompleteRollback)
    assert pending.failure is first
    with pytest.raises(BlockingIOError):
        with prepare(1):
            pytest.fail("cleanup failure released the session lease")
    failed[0] = False
    assert pending.retry() is True
    assert len(kills) == 1
    with prepare(1):
        pass


def test_separate_probe_slot_cannot_consume_a_product_slot(tmp_path, monkeypatch):
    root, manifest = bundle_fixture(tmp_path, monkeypatch)
    protected = tmp_path / "host"
    protected.mkdir(mode=0o700)
    containers = tmp_path / "offline-container-fixture"
    containers.mkdir(exist_ok=True)
    monkeypatch.setattr(
        native, "_owner_container", lambda identity: containers / identity
    )
    monkeypatch.setattr(native, "_protected_directory", lambda path: Path(path))
    with native.prepare_native_probe(
        root, manifest, protected_root=protected, probe_id="explicit-os-probe"
    ) as slot:
        assert ".probe/" in str(slot.root)
        assert slot.process is None  # only offline path/marker preparation
    spent = list((protected / "native-spent-slots").glob("*.json"))
    assert len(spent) == 1
    record = strict_json(spent[0].read_bytes())
    assert record["slot_index"] is None
    assert record["operation_id"] == "native-probe"


def test_cleanup_observation_keeps_recovered_errno_without_exception_messages(
    tmp_path, monkeypatch
):
    prepare, _, _ = slot_fixture(tmp_path, monkeypatch)
    slot = prepare()
    first = PermissionError(errno.EPERM, "SECRET-shaped error must not be exported")
    first.__cause__ = RuntimeError("SECRET earlier cause")
    slot._group_signal_failure = first
    slot._group_started = True
    slot._group_closed = ProcessLookupError(errno.ESRCH, "old group absent")
    slot.process = SimpleNamespace(pid=987654, returncode=0)
    report = slot.cleanup_observation()
    assert report["signal_error"] == {"type": "PermissionError", "errno": 1}
    assert report["group_release"] == "group_absent"
    assert report["leader_returncode"] == 0
    assert report["error_roots"] == [0]
    assert report["errors"][0]["cause"] == 1
    assert report["errors"][1]["type"] == "RuntimeError"
    assert "SECRET" not in str(report)
