"""Full, protected material intake is an integrity conclusion, never an IO grant."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from agent_alfred.evals.acceptance.admission import proposal
from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.materials import (
    ProtectedMaterialStore,
    export_materials,
    preflight_materials,
    read_reference,
)
from agent_alfred.evals.acceptance.schema import digest, encode
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.resource_rollback import IncompleteRollback, ResumableRollback


def fixture(root):
    source = root / "source"
    store = EvidenceStore(source / "store")
    batch = supplement_batch(prefix="original")
    store.import_batch(batch)
    child = store.revise("original", "reviewed")
    (source / "proposal.json").write_bytes(
        encode(
            proposal(child, output_scope=str(root / "runtime"), operations=["judge"])
        )
    )
    # The declared full object exceeds the old P2 wire limit. This payload is
    # synthetic text, distinct from the separately measured frozen r7 bundle.
    (source / "history.txt").write_text("Synthetic original disagreement\n" * 10000)
    files = [
        {
            "path": str(p.relative_to(source)),
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(source.rglob("*"))
        if p.is_file()
    ]
    manifest = encode({"version": 1, "file_count": len(files), "files": files})
    (source / "BUNDLE-MANIFEST.json").write_bytes(manifest)
    vault = ProtectedMaterialStore(root / "vault", scope="fixture")
    reference = export_materials(
        source,
        vault=vault,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        batch_path="store/reviewed/batch.json",
        proposal_path="proposal.json",
        evidence_store_path="store",
    )
    return source, vault, reference, child


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_full_material_intake_preserves_history_and_cannot_authorize_send(tmp_path):
    source, vault, reference, child = fixture(tmp_path)
    before = snapshot(source)
    result = preflight_materials(
        reference,
        vault=vault,
        allowed_objects={reference["object_sha256"]},
        expected_manifest_sha256=reference["manifest_sha256"],
        output=tmp_path / "received",
    )
    assert result["material_integrity"] == "PASS"
    assert result["online_executable"] is False
    assert result["real_readiness"]["verdict"] == "BLOCKED"
    assert result["batch_sha256"] == digest(child)
    assert result["lineage"] == ["original", "reviewed"]
    assert EvidenceStore(tmp_path / "received/evidence").read("reviewed") == child
    assert snapshot(source) == before
    assert snapshot(tmp_path / "received/materials") == before
    assert (
        json.loads((tmp_path / "received/material-admission.json").read_bytes())
        == result
    )


def admit(root, vault, reference, *, allowed=None, expected=None):
    return preflight_materials(
        reference,
        vault=vault,
        allowed_objects={reference["object_sha256"]} if allowed is None else allowed,
        expected_manifest_sha256=expected or reference["manifest_sha256"],
        output=root / "received",
    )


def test_reference_does_not_grant_read_and_restarts_revalidate_bytes(tmp_path):
    source, vault, reference, _ = fixture(tmp_path)
    before = snapshot(source)
    with pytest.raises(ValueError, match="material_read_unauthorized"):
        admit(tmp_path, vault, reference, allowed=set())
    assert not (tmp_path / "received").exists()
    with pytest.raises(ValueError, match="material_manifest_mismatch"):
        admit(tmp_path, vault, reference, expected="0" * 64)
    restarted = ProtectedMaterialStore(vault.root, scope="other-scope")
    with pytest.raises(ValueError, match="material_scope_mismatch"):
        admit(tmp_path, restarted, reference)
    blob = next((vault.root / "blobs").iterdir())
    blob.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="material_digest_mismatch"):
        admit(tmp_path, ProtectedMaterialStore(vault.root, scope="fixture"), reference)
    assert not (tmp_path / "received/material-admission.json").exists()
    assert snapshot(source) == before


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda r: r.update(version=2), "unknown_material_contract_version"),
        (lambda r: r.update(version=True), "unknown_material_contract_version"),
        (
            lambda r: r.update(url="https://untrusted.invalid"),
            "invalid_material_contract_fields",
        ),
        (lambda r: r.update(object_sha256="../outside"), "invalid_hash"),
        (lambda r: r.update(bytes=128 * 1024 * 1024 + 1), "material_size_exceeded"),
        (lambda r: r.update(bytes=r["bytes"] - 1), "material_length_mismatch"),
    ],
)
def test_execution_metadata_rejects_ambiguous_or_unbounded_references(
    tmp_path, mutation, error
):
    _, vault, reference, _ = fixture(tmp_path)
    mutation(reference)
    with pytest.raises(ValueError, match=error):
        admit(tmp_path, vault, reference)
    assert not (tmp_path / "received/material-admission.json").exists()


def test_receiver_rejects_reordered_missing_and_symlinked_members(tmp_path):
    _, vault, reference, _ = fixture(tmp_path)
    object_path = vault.root / "objects" / reference["object_sha256"]
    original = json.loads(object_path.read_bytes())
    altered = {**original, "files": list(reversed(original["files"]))}
    changed = encode(altered)
    changed_id = hashlib.sha256(changed).hexdigest()
    (vault.root / "objects" / changed_id).write_bytes(changed)
    with pytest.raises(ValueError, match="material_manifest_mismatch"):
        admit(tmp_path, vault, {**reference, "object_sha256": changed_id})
    typed = json.loads(encode(original))
    typed["files"][0]["bytes"] = float(typed["files"][0]["bytes"])
    changed = encode(typed)
    changed_id = hashlib.sha256(changed).hexdigest()
    (vault.root / "objects" / changed_id).write_bytes(changed)
    with pytest.raises(ValueError, match="material_manifest_mismatch"):
        admit(tmp_path, vault, {**reference, "object_sha256": changed_id})
    member = vault.root / "blobs" / original["files"][0]["sha256"]
    data = member.read_bytes()
    member.unlink()
    with pytest.raises(ValueError, match="material_object_unavailable"):
        admit(tmp_path, vault, reference)
    external = tmp_path / "external"
    external.write_bytes(data)
    member.symlink_to(external)
    with pytest.raises(ValueError, match="material_object_unavailable"):
        preflight_materials(
            reference,
            vault=vault,
            allowed_objects={reference["object_sha256"]},
            expected_manifest_sha256=reference["manifest_sha256"],
            output=tmp_path / "symlink-output",
        )
    assert not (tmp_path / "symlink-output/material-admission.json").exists()


def test_cli_strict_preflight_reports_integrity_separately_from_readiness(tmp_path):
    _, vault, reference, _ = fixture(tmp_path)
    path = tmp_path / "reference.json"
    path.write_bytes(encode(reference))
    command = [
        sys.executable,
        "-m",
        "agent_alfred.evals.acceptance",
        "materials-preflight",
        "--input",
        str(path),
        "--store",
        str(vault.root),
        "--material-scope",
        "fixture",
        "--manifest-sha256",
        reference["manifest_sha256"],
        "--allowed-material-object",
        reference["object_sha256"],
        "--workspace",
        str(tmp_path / "received"),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    result = json.loads(completed.stdout)
    assert completed.returncode == 2  # successful intake does not assert release
    assert result["material_integrity"] == "PASS"
    assert result["online_executable"] is False
    path.write_bytes(encode(reference)[:-1] + b',"version":1}')
    rejected = subprocess.run(command, capture_output=True, text=True, check=False)
    assert (
        "duplicate_material_json_key"
        in json.loads(rejected.stdout)["offline_engineering"]["blockers"]
    )


def test_intake_never_unlocks_credentials_or_factory(tmp_path):
    from agent_alfred.evals.acceptance.runner import execute

    _, vault, reference, _ = fixture(tmp_path)
    admit(tmp_path, vault, reference)
    received = EvidenceStore(tmp_path / "received/evidence").read("reviewed")
    received["simulation"] = False

    class UnreadableCredentials:
        def values(self):
            raise AssertionError("credentials must remain unread")

    def forbidden_factory(*args):
        raise AssertionError("material integrity must not construct a model")

    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        execute(
            received,
            tmp_path / "runtime",
            credentials=UnreadableCredentials(),
            product_factory_builder=forbidden_factory,
        )
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize(
    "violation,error",
    [
        ("escape", "unsafe_material_path"),
        ("file_limit", "material_size_exceeded"),
        ("partial_parent", "missing_or_incomplete_batch"),
    ],
)
def test_manifest_bounds_and_complete_ancestry_are_required(tmp_path, violation, error):
    source, _, _, _ = fixture(tmp_path)
    manifest = json.loads((source / "BUNDLE-MANIFEST.json").read_bytes())
    if violation == "escape":
        manifest["files"][0]["path"] = "../outside"
    elif violation == "file_limit":
        manifest["files"][0]["bytes"] = 32 * 1024 * 1024 + 1
    else:
        manifest["files"] = [
            f for f in manifest["files"] if not f["path"].startswith("store/original/")
        ]
        manifest["file_count"] = len(manifest["files"])
    raw = encode(manifest)
    (source / "BUNDLE-MANIFEST.json").write_bytes(raw)
    vault = ProtectedMaterialStore(tmp_path / "new-vault", scope="fixture")
    with pytest.raises(ValueError, match=error):
        reference = export_materials(
            source,
            vault=vault,
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            batch_path="store/reviewed/batch.json",
            proposal_path="proposal.json",
            evidence_store_path="store",
        )
        admit(tmp_path, vault, reference)
    assert not (tmp_path / "received/material-admission.json").exists()


def test_intake_rejects_shared_vault_and_preserves_existing_outputs(tmp_path):
    _, vault, reference, _ = fixture(tmp_path)
    vault.root.chmod(0o755)
    with pytest.raises(ValueError, match="unprotected_material_store"):
        admit(tmp_path, vault, reference)
    vault.root.chmod(0o700)
    output = tmp_path / "received"
    output.mkdir()
    (output / "original").write_text("protected original")
    with pytest.raises(ValueError, match="material_output_exists"):
        admit(tmp_path, vault, reference)
    assert snapshot(output) == {"original": b"protected original"}


def reference_file(tmp_path):
    reference = {
        "contract": "V1-CALIBRATION-MATERIAL-REFERENCE",
        "version": 1,
        "scope": "fixture",
        "object_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "bytes": 1,
        "file_count": 1,
    }
    path = tmp_path / "reference.json"
    path.write_bytes(encode(reference))
    return path, reference


@pytest.mark.parametrize(
    "error_type", [OSError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_reference_close_failure_retains_owner_without_reclosing_recycled_fd(
    tmp_path, monkeypatch, error_type
):
    path, _ = reference_file(tmp_path)
    native_open, native_close = os.open, os.close
    opened, attempts, recycled = [], [], []
    failure = error_type("descriptor close completed before failure")

    def tracked_open(*args, **kwargs):
        descriptor = native_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def close_then_fail(descriptor):
        attempts.append(descriptor)
        native_close(descriptor)
        if len(attempts) == 1:
            recycled.append(native_open(os.devnull, os.O_RDONLY))
            assert recycled[0] == descriptor
            raise failure

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", tracked_open)
            patch.setattr(os, "close", close_then_fail)
            with pytest.raises(error_type) as caught:
                read_reference(path)
            assert caught.value is failure
            pending = caught.value.__cause__
            assert isinstance(pending, IncompleteRollback)
            assert pending.failure is failure
            assert pending.errors == (failure,)
            assert len(opened) == 2
            assert attempts == [opened[-1]]
            os.fstat(opened[0])  # Still reachable through the pending owner.
            assert pending.retry()
            assert attempts == list(reversed(opened))
            os.fstat(recycled[0])
            with pytest.raises(OSError):
                os.fstat(opened[0])
            assert pending.retry()
            assert attempts == list(reversed(opened))
            os.fstat(recycled[0])
    finally:
        for descriptor in set(opened + recycled):
            try:
                native_close(descriptor)
            except OSError:
                pass


@pytest.mark.parametrize("error_type", [OSError, SystemExit])
def test_export_wrapper_construction_failure_releases_descriptor(
    tmp_path, monkeypatch, error_type
):
    source, _, reference, _ = fixture(tmp_path)
    vault = ProtectedMaterialStore(tmp_path / "new-vault", scope="fixture")
    native_fdopen = os.fdopen
    descriptors = []
    failure = error_type("write wrapper construction failed")

    def fail_write_wrapper(descriptor, mode, *args, **kwargs):
        if mode == "wb":
            descriptors.append(descriptor)
            raise failure
        return native_fdopen(descriptor, mode, *args, **kwargs)

    try:
        monkeypatch.setattr(os, "fdopen", fail_write_wrapper)
        with pytest.raises(error_type) as caught:
            export_materials(
                source,
                vault=vault,
                manifest_sha256=reference["manifest_sha256"],
                batch_path="store/reviewed/batch.json",
                proposal_path="proposal.json",
                evidence_store_path="store",
            )
        assert caught.value is failure
        assert len(descriptors) == 1
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


@pytest.mark.parametrize(
    "read_type,close_type",
    [(OSError, OSError), (SystemExit, OSError), (OSError, KeyboardInterrupt)],
)
def test_reference_read_failure_survives_incomplete_cleanup(
    tmp_path, monkeypatch, read_type, close_type
):
    path, _ = reference_file(tmp_path)
    native_open, native_close, native_fdopen = os.open, os.close, os.fdopen
    opened, attempts = [], []
    read_failure = read_type("first read failure")
    close_failure = close_type("descriptor consumed during cleanup")

    def tracked_open(*args, **kwargs):
        descriptor = native_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    class FailedRead:
        def __init__(self, stream):
            self.stream = stream

        def read(self, maximum):
            raise read_failure

        def close(self):
            self.stream.close()

    def wrap(*args, **kwargs):
        return FailedRead(native_fdopen(*args, **kwargs))

    def close_then_fail(descriptor):
        attempts.append(descriptor)
        native_close(descriptor)
        if len(attempts) == 1:
            raise close_failure

    try:
        monkeypatch.setattr(os, "open", tracked_open)
        monkeypatch.setattr(os, "fdopen", wrap)
        monkeypatch.setattr(os, "close", close_then_fail)
        expected_type = ValueError if read_type is OSError else read_type
        if close_type is KeyboardInterrupt:
            expected_type = close_type
        with pytest.raises(expected_type) as caught:
            read_reference(path)
        pending = caught.value.__cause__
        assert isinstance(pending, IncompleteRollback)
        assert pending.errors == (close_failure,)
        if close_type is KeyboardInterrupt:
            assert caught.value is close_failure
        if read_type is OSError:
            assert type(pending.failure) is ValueError
            assert str(pending.failure) == "material_object_unavailable"
            assert pending.failure.__context__ is read_failure
        else:
            assert caught.value is pending.failure is read_failure
        assert attempts == [opened[-1]]
        assert pending.retry()
        assert attempts == list(reversed(opened))
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in set(opened):
            try:
                native_close(descriptor)
            except OSError:
                pass


def test_reference_stream_close_failure_keeps_descriptors_owned_until_retry(
    tmp_path, monkeypatch
):
    path, _ = reference_file(tmp_path)
    native_open, native_close, native_fdopen = os.open, os.close, os.fdopen
    opened, wrappers = [], []
    failure = OSError("wrapper close not completed")

    def tracked_open(*args, **kwargs):
        descriptor = native_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    class RetryClose:
        def __init__(self, stream):
            self.stream = stream
            self.close_calls = 0

        def read(self, maximum):
            return self.stream.read(maximum)

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise failure
            self.stream.close()

    def wrap(*args, **kwargs):
        wrapper = RetryClose(native_fdopen(*args, **kwargs))
        wrappers.append(wrapper)
        return wrapper

    try:
        monkeypatch.setattr(os, "open", tracked_open)
        monkeypatch.setattr(os, "fdopen", wrap)
        with pytest.raises(OSError) as caught:
            read_reference(path)
        assert caught.value is failure
        pending = caught.value.__cause__
        assert isinstance(pending, IncompleteRollback)
        assert wrappers[0].close_calls == 1
        for descriptor in opened:
            os.fstat(descriptor)
        assert pending.retry()
        assert wrappers[0].stream.closed
        assert wrappers[0].close_calls == 2
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        assert pending.retry()
        assert wrappers[0].close_calls == 2
    finally:
        for wrapper in wrappers:
            wrapper.stream.close()
        for descriptor in set(opened):
            try:
                native_close(descriptor)
            except OSError:
                pass


def test_reference_success_and_fdopen_return_interruption_release_all_descriptors(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_py_return_once,
    )

    path, expected = reference_file(tmp_path)
    native_open, native_close = os.open, os.close
    opened = []
    control = SystemExit("fdopen returned before wrapper publication")

    def tracked_open(*args, **kwargs):
        descriptor = native_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    try:
        monkeypatch.setattr(os, "open", tracked_open)
        assert read_reference(path) == expected
        assert len(opened) == 2
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        opened.clear()
        with interrupt_py_return_once(
            "materials-fdopen-return", os.fdopen.__code__, control
        ) as armed:
            with pytest.raises(SystemExit) as caught:
                read_reference(path)
        assert armed == [False]
        assert caught.value is control
        assert len(opened) == 2
        for descriptor in opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in set(opened):
            try:
                native_close(descriptor)
            except OSError:
                pass


def test_export_write_failure_keeps_first_error_and_consumed_descriptor_owner(
    tmp_path, monkeypatch
):
    source, _, reference, _ = fixture(tmp_path)
    vault = ProtectedMaterialStore(tmp_path / "new-vault", scope="fixture")
    native_open, native_close = os.open, os.close
    descriptors, attempts = [], []
    failure = OSError("first write sync failure")
    close_failure = OSError("write descriptor consumed during cleanup")

    def tracked_open(path, flags, *args, **kwargs):
        descriptor = native_open(path, flags, *args, **kwargs)
        if flags & os.O_WRONLY:
            descriptors.append(descriptor)
        return descriptor

    def fail_sync(descriptor):
        raise failure

    def close_then_fail(descriptor):
        native_close(descriptor)
        if descriptor in descriptors:
            attempts.append(descriptor)
            raise close_failure

    try:
        monkeypatch.setattr(os, "open", tracked_open)
        monkeypatch.setattr(os, "fsync", fail_sync)
        monkeypatch.setattr(os, "close", close_then_fail)
        with pytest.raises(OSError) as caught:
            export_materials(
                source,
                vault=vault,
                manifest_sha256=reference["manifest_sha256"],
                batch_path="store/reviewed/batch.json",
                proposal_path="proposal.json",
                evidence_store_path="store",
            )
        assert caught.value is failure
        pending = caught.value.__cause__
        assert isinstance(pending, IncompleteRollback)
        assert pending.failure is failure
        assert pending.errors == (close_failure,)
        assert len(descriptors) == 1
        assert attempts == descriptors
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
        assert pending.retry()
        assert attempts == descriptors
    finally:
        for descriptor in descriptors:
            try:
                native_close(descriptor)
            except OSError:
                pass


@pytest.mark.parametrize("entry", ["read", "export"])
def test_material_cleanup_call_interruption_cannot_orphan_descriptors(
    tmp_path, monkeypatch, entry
):
    path, _ = reference_file(tmp_path)
    source, _, reference, _ = fixture(tmp_path)
    vault = ProtectedMaterialStore(tmp_path / "new-vault", scope="fixture")
    native_open, native_close = os.open, os.close
    original_cleanup = ResumableRollback.close
    opened, write_descriptors = [], []
    control = SystemExit("interrupted before cleanup could enter")

    def tracked_open(path, flags, *args, **kwargs):
        descriptor = native_open(path, flags, *args, **kwargs)
        opened.append(descriptor)
        if flags & os.O_WRONLY:
            write_descriptors.append(descriptor)
        return descriptor

    def interrupt_cleanup(owner):
        if entry == "read" or write_descriptors:
            raise control
        return original_cleanup(owner)

    try:
        monkeypatch.setattr(os, "open", tracked_open)
        monkeypatch.setattr(ResumableRollback, "close", interrupt_cleanup)
        with pytest.raises(SystemExit) as caught:
            if entry == "read":
                read_reference(path)
            else:
                export_materials(
                    source,
                    vault=vault,
                    manifest_sha256=reference["manifest_sha256"],
                    batch_path="store/reviewed/batch.json",
                    proposal_path="proposal.json",
                    evidence_store_path="store",
                )
        assert caught.value is control
        assert opened
        if entry == "export":
            assert len(write_descriptors) == 1
        for descriptor in set(opened):
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in set(opened):
            try:
                native_close(descriptor)
            except OSError:
                pass
