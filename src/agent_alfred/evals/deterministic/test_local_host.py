"""Offline code inventory fixtures; no home directories or credentials read."""

import sys

import pytest

from agent_alfred.evals.acceptance.controlled import local_host


def host_fixture(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "python-code"
    root.mkdir(mode=0o700)
    executable = root / "python"
    executable.write_bytes(b"fixture interpreter")
    module = root / "fixture_module.py"
    module.write_text("x = 1\n")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(local_host, "_code_roots", lambda: [root])
    monkeypatch.setattr(local_host, "_python_paths", lambda: [str(root)])
    monkeypatch.setattr(local_host, "_native_images", lambda: {executable})
    monkeypatch.setattr(local_host, "_loaded_origins", lambda: {module})
    return root, module


def test_host_inventory_pins_all_bytes_then_rejects_changed_dependency(
    tmp_path, monkeypatch
):
    root, module = host_fixture(tmp_path, monkeypatch)
    manifest = local_host.capture_host_environment()
    verifier = local_host.HostEnvironmentVerifier(manifest)
    assert verifier.verify()["files_verified"] == 2
    module.write_text("x = 2\n")
    with pytest.raises(ValueError, match="local_host_code_changed"):
        verifier.verify()
    with pytest.raises(ValueError, match="local_host_code_changed"):
        local_host.HostEnvironmentVerifier(manifest)
    assert {row["path"] for row in manifest["files"]} == {
        str(root / "python"),
        str(module),
    }


def test_host_new_import_origin_and_unlisted_files_fail_closed(tmp_path, monkeypatch):
    root, module = host_fixture(tmp_path, monkeypatch)
    verifier = local_host.HostEnvironmentVerifier(local_host.capture_host_environment())
    monkeypatch.setattr(
        local_host, "_loaded_origins", lambda: {module, tmp_path / "unexpected.py"}
    )
    with pytest.raises(ValueError, match="local_host_loaded_origin_unverified"):
        verifier.verify()
    monkeypatch.setattr(local_host, "_loaded_origins", lambda: {module})
    (root / "injected.pth").write_text("import something\n")
    with pytest.raises(ValueError, match="local_host_inventory_changed"):
        verifier.verify()


def test_host_inventory_keeps_external_native_images_and_live_roots_distinct(
    tmp_path, monkeypatch
):
    root, module = host_fixture(tmp_path, monkeypatch)
    # A shared lexical prefix does not make this a child of the code root.
    external = root.with_name(root.name + "-native.dylib")
    external.write_bytes(b"fixture native image")
    images = {root / "python", external}
    monkeypatch.setattr(local_host, "_native_images", lambda: images)
    verifier = local_host.HostEnvironmentVerifier(local_host.capture_host_environment())
    assert verifier.verify()["files_verified"] == 3

    images.add(tmp_path / "new-native.dylib")
    with pytest.raises(ValueError, match="local_host_inventory_changed"):
        verifier.verify()
    images.remove(tmp_path / "new-native.dylib")
    external.write_bytes(b"changed native image")
    with pytest.raises(ValueError, match="local_host_code_changed"):
        verifier.verify()

    monkeypatch.setattr(local_host, "_code_roots", lambda: [root.parent])
    with pytest.raises(ValueError, match="local_host_environment_changed"):
        verifier.verify()


def test_host_scan_preserves_bytecode_exclusions_and_checks_nested_code(
    tmp_path, monkeypatch
):
    root, _ = host_fixture(tmp_path, monkeypatch)
    nested = root / "directory.pyc"
    nested.mkdir()
    source = nested / "nested.py"
    source.write_text("x = 1\n")
    ignored = root / "__pycache__"
    ignored.mkdir()
    (ignored / "ignored.py").symlink_to(tmp_path / "missing")
    (root / "ignored.pyc").symlink_to(tmp_path / "missing")
    verifier = local_host.HostEnvironmentVerifier(local_host.capture_host_environment())
    assert verifier.verify()["files_verified"] == 3
    source.unlink()
    with pytest.raises(ValueError, match="local_host_inventory_changed"):
        verifier.verify()
    source.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="local_host_code_symlink"):
        verifier.verify()
