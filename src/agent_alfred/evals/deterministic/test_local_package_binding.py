"""Claimed source IDs do not substitute for the bundled package bytes."""

from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.controlled import local_runtime
from agent_alfred.evals.acceptance.schema import digest


def local_package_fixture(monkeypatch):
    candidate = {
        "files": {
            "src/agent_alfred/__init__.py": {"kind": "file", "sha256": "1" * 64},
            "src/agent_alfred/data/prices.toml": {
                "kind": "file",
                "sha256": "2" * 64,
            },
            "native/macos/runner.py": {"kind": "file", "sha256": "3" * 64},
        }
    }
    identity = digest(candidate)
    runtime = object.__new__(local_runtime.LocalInstalledRuntime)
    runtime._config = {"candidate_id": identity}
    runtime._bound_candidate_id = None
    runtime._directory = None
    apps = [f"slots/{index:02d}.app" for index in range(30)] + ["Probe.app"]
    runtime._bundle_manifest = {
        "source_id": identity,
        "slots": [{"path": path} for path in apps[:-1]],
        "probe": {"path": apps[-1]},
        "files": [
            {
                "path": f"{app}/Contents/Resources/packages/agent_alfred/{name}",
                "sha256": fingerprint,
            }
            for app in apps
            for name, fingerprint in (
                ("__init__.py", "1" * 64),
                ("data/prices.toml", "2" * 64),
            )
        ],
    }
    # Signature/host verification is an explicit offline fixture. The public
    # check must still reject a package mismatch before it reaches readiness.
    monkeypatch.setattr(runtime, "verify_installation", lambda: None)
    monkeypatch.setattr(
        local_runtime, "verify_runtime", lambda value: value == candidate
    )

    def readiness_unavailable(*args, **kwargs):
        raise ValueError("offline_readiness_stop")

    monkeypatch.setattr(local_runtime, "read_readiness", readiness_unavailable)
    plan = {
        "execution_mode": "authorized",
        "initial_phase": "diagnostic",
        "runtime_candidate": candidate,
        "candidate_id": identity,
        "operations": [
            {"kind": "product", "object_id": f"offline-case-{index}"}
            for index in range(30)
        ],
    }
    return runtime, plan


@pytest.mark.parametrize("change", ["last_slot_missing", "probe_changed", "slot_extra"])
def test_real_check_rejects_package_mismatch_despite_matching_declared_source(
    monkeypatch, change
):
    runtime, plan = local_package_fixture(monkeypatch)
    files = runtime._bundle_manifest["files"]
    if change == "last_slot_missing":
        files[:] = [row for row in files if not row["path"].startswith("slots/29.app/")]
    elif change == "probe_changed":
        files[-1]["sha256"] = "4" * 64
    else:
        files.append({**files[0], "path": files[0]["path"] + ".extra"})
    with pytest.raises(ValueError, match="local_packaged_candidate_mismatch"):
        runtime.check(
            plan, {"simulation": False}, {"simulation": False}, datetime.now(UTC)
        )
    assert runtime._bound_candidate_id is None


def test_exact_package_match_is_cached_only_after_all_apps_match(monkeypatch):
    runtime, plan = local_package_fixture(monkeypatch)
    with pytest.raises(ValueError, match="offline_readiness_stop"):
        runtime.check(
            plan, {"simulation": False}, {"simulation": False}, datetime.now(UTC)
        )
    assert runtime._bound_candidate_id == plan["candidate_id"]
