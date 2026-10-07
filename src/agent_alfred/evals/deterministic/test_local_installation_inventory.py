"""Installation inventories have their own bound, without widening IPC/evidence."""

import hashlib
import os
import sys
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.controlled import local_host, native
from agent_alfred.evals.acceptance.controlled.local_files import OwnerDirectory
from agent_alfred.evals.acceptance.controlled.local_installation import (
    verify_installation,
)
from agent_alfred.evals.acceptance.schema import encode


def installation_fixture(tmp_path, monkeypatch, inventory, *, source_id="1" * 64):
    root = tmp_path.resolve() / "owner"
    root.mkdir(mode=0o700)
    directory = OwnerDirectory(root, os.geteuid())
    bundle = {
        "source_id": source_id,
        "entrypoints": {"owner_helper": "OwnerApproval.app/helper"},
    }
    host = {"synthetic_host_inventory": True}
    # A final 30-slot native inventory exceeds the ordinary evidence limit.
    # These are byte-size fixtures; no native signature or host trust is claimed.
    large = bundle if inventory == "bundle" else host
    large["fixture_padding"] = "x" * (8 * 1024 * 1024)
    references = {}
    for name, value in (("bundle", bundle), ("host", host)):
        path = directory.file(name + ".json", create=True)
        raw = encode(value)
        path.write_bytes(raw)
        references[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    config = {
        "contract": "V1-LOCAL-INSTALLATION",
        "version": 1,
        "trust_profile": "owner_trusted_local",
        "owner_uid": os.geteuid(),
        "installation_id": "offline:inventory-size",
        "candidate_id": "1" * 64,
        "worker": "offline:worker",
        "controller": "offline:controller",
        "subject": "offline:owner",
        "protected_root": str(root),
        "bundle_root": str(root / "unused-bundle"),
        "bundle_manifest": references["bundle"],
        "owner_helper": str(root / "unused-bundle/OwnerApproval.app/helper"),
        "host_python_sha256": hashlib.sha256(
            Path(sys.executable).resolve().read_bytes()
        ).hexdigest(),
        "host_environment": references["host"],
        **{
            field: str(root / field)
            for field in (
                "readiness_path",
                "billing_path",
                "decisions_path",
                "ledger_path",
                "witness_path",
                "credential_path",
            )
        },
    }
    path = directory.file("installation.json", create=True)
    path.write_bytes(encode(config))

    class OfflineBundleVerifier:
        def __init__(self, bundle_root, manifest):
            assert bundle_root == config["bundle_root"]
            assert manifest == bundle

        def verify(self):
            return {"slot_count": 30}

    class OfflineHostVerifier:
        def __init__(self, manifest):
            assert manifest == host

        def verify(self):
            return None

    monkeypatch.setattr(native, "NativeBundleVerifier", OfflineBundleVerifier)
    monkeypatch.setattr(local_host, "HostEnvironmentVerifier", OfflineHostVerifier)
    return path, directory, references[inventory]["path"]


@pytest.mark.parametrize("inventory", ["bundle", "host"])
def test_large_installation_inventory_does_not_widen_evidence_reads(
    tmp_path, monkeypatch, inventory
):
    config, directory, inventory_path = installation_fixture(
        tmp_path, monkeypatch, inventory
    )
    assert Path(inventory_path).stat().st_size > 8 * 1024 * 1024
    observed, _, _, _, _ = verify_installation(config)
    assert observed.path == directory.path
    with pytest.raises(ValueError, match="local_protected_file_too_large"):
        directory.read(inventory_path)


@pytest.mark.parametrize("inventory", ["bundle", "host"])
def test_installation_inventory_above_64_mib_is_rejected(
    tmp_path, monkeypatch, inventory
):
    config, _, inventory_path = installation_fixture(tmp_path, monkeypatch, inventory)
    with Path(inventory_path).open("r+b") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="local_protected_file_too_large"):
        verify_installation(config)


def test_installation_cannot_bind_another_native_source_candidate(
    tmp_path, monkeypatch
):
    config, _, _ = installation_fixture(
        tmp_path, monkeypatch, "host", source_id="2" * 64
    )
    with pytest.raises(ValueError, match="local_bundle_source_candidate_mismatch"):
        verify_installation(config)


def test_warm_installation_rechecks_manifest_bytes_and_helper_without_large_return(
    tmp_path, monkeypatch
):
    path, directory, inventory_path = installation_fixture(
        tmp_path, monkeypatch, "bundle"
    )
    _, config, manifest, bundle, host = verify_installation(path)

    def warm():
        return verify_installation(
            path, config, verifier=bundle, host_verifier=host, with_manifest=False
        )

    assert warm()[2] is None
    # The caller's original return value cannot poison cached metadata.
    manifest["entrypoints"]["owner_helper"] = "wrong-helper"
    assert warm()[2] is None
    raw = Path(inventory_path).read_bytes()
    Path(inventory_path).write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="local_bundle_manifest_changed"):
        warm()
    Path(inventory_path).write_bytes(raw)
    config["owner_helper"] += "-different"
    path.write_bytes(encode(config))
    with pytest.raises(ValueError, match="local_owner_helper_mismatch"):
        warm()


def test_warm_installation_rejects_duplicate_keys_even_with_matching_new_digest(
    tmp_path, monkeypatch
):
    path, _, inventory_path = installation_fixture(tmp_path, monkeypatch, "bundle")
    _, config, _, bundle, host = verify_installation(path)
    verify_installation(
        path, config, verifier=bundle, host_verifier=host, with_manifest=False
    )
    raw = b'{"source_id":"' + b"1" * 64 + b'",' + Path(inventory_path).read_bytes()[1:]
    Path(inventory_path).write_bytes(raw)
    config["bundle_manifest"]["sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_bytes(encode(config))
    with pytest.raises(ValueError, match="duplicate_material_json_key"):
        verify_installation(
            path, config, verifier=bundle, host_verifier=host, with_manifest=False
        )
