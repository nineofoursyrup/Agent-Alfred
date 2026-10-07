"""Offline model-policy evidence; no real source, installation or grant."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.controlled.contract import (
    CAP_UNITS,
    advisory_run_disclosure,
    effective_judge_profile,
)
from agent_alfred.evals.acceptance.controlled.local_installation import (
    ADVISORY_REQUIRED_EVIDENCE,
    EVIDENCE_CHECKS,
    read_readiness,
)
from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.execution_decisions import (
    material_binding,
    run_request,
    validate_request,
)
from agent_alfred.evals.acceptance.schema import digest, encode
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.acceptance.supplement_quality import quality

from .test_controlled_execution import fixture
from .test_local_advisory_contract import _plan, _resign
from .test_local_source import source_fixture


def flash_material():
    batch = supplement_batch(phase="calibration")
    batch["judge_profile"] = deepcopy(batch["judge_profile"])
    for profile in batch["profiles"]:
        for model in profile["product_models"]:
            model.update(endpoint_id="deepseek", model_id="deepseek-flash")
        _resign(profile)
    batch["judge_profile"]["model"].update(
        endpoint_id="deepseek", model_id="deepseek-v4-pro"
    )
    _resign(batch["judge_profile"])
    return batch


def test_flash_profile_child_is_explicit_and_old_material_not_reapproved(tmp_path):
    batch = flash_material()
    plan = _plan(advisory=True, judge_profile=batch["judge_profile"])
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    original_digest = digest(store.read(batch["batch_id"]))
    profile = effective_judge_profile(plan, batch["judge_profile"])
    child = store.revise(
        batch["batch_id"], "flash-child", configuration={"judge_profile": profile}
    )
    assert child["judge_profile"]["model"]["model_id"] == "deepseek-flash"
    assert digest(store.read(batch["batch_id"])) == original_digest
    observed = quality(child, datetime.now(UTC))
    assert "user_materials_approval_missing" in observed["blockers"]
    assert "independent_material_review_missing" in observed["blockers"]
    for policy in (None, "unverified_same_model"):
        invalid = deepcopy(profile)
        if policy is None:
            invalid.pop("independence_policy")
        else:
            invalid["independence_policy"] = policy
        _resign(invalid)
        with pytest.raises(
            ValueError, match="judge_(same_model|independence_policy_invalid)"
        ):
            store.revise(
                batch["batch_id"],
                "invalid-" + str(policy),
                configuration={"judge_profile": invalid},
            )
    invalid = deepcopy(profile)
    invalid["model"]["model_id"] = "other-model"
    _resign(invalid)
    with pytest.raises(ValueError, match="judge_independence_policy_invalid"):
        store.revise(
            batch["batch_id"], "invalid-model", configuration={"judge_profile": invalid}
        )


def test_flash_run_owner_object_binds_model_change_and_keeps_material_scope(
    tmp_path, monkeypatch
):
    offline = fixture(tmp_path / "material")
    material = offline["submission"]["material_ref"]
    evidence = EvidenceStore(tmp_path / "material/source-materials/evidence")
    binding = material_binding(
        evidence,
        offline["batch"]["batch_id"],
        package_manifest_sha256=material["manifest_sha256"],
    )
    plan = _plan(advisory=True, judge_profile=flash_material()["judge_profile"])
    disclosure = advisory_run_disclosure(
        CAP_UNITS, judge_profile_change=plan["judge_profile_change"]
    )
    request = run_request(
        binding,
        job_id="offline-flash",
        plan_sha256=digest(plan),
        budget_sha256="b" * 64,
        disclosure=disclosure,
    )
    runtime, calls = source_fixture(tmp_path, monkeypatch)
    try:
        record = runtime.decision_control.record(
            request,
            decision="approved",
            reason="offline fixture only",
            evidence=["offline:flash-choice"],
        )
        actual = runtime.decision_source.read_decision(record["source_ref"])
        assert actual["request"] == request
        assert calls[0]["request"]["disclosure"]["cross_model_independence"] is False
        assert actual["request"]["scope"] == "run"
        assert request["version"] == 3
        forged = deepcopy(request)
        forged["disclosure"]["schema4_material_approval_inherited"] = True
        with pytest.raises(ValueError, match="advisory_run_disclosure_invalid"):
            validate_request(_resign(forged))
    finally:
        runtime._decisions.close()


def test_flash_readiness_does_not_claim_two_models_or_accept_old_identity_scope():
    """Closed offline documents exercise actual readers, never source approval."""
    documents = {}

    def document(value):
        reference = digest(value)
        documents[reference] = deepcopy(value)
        return {"path": reference, "sha256": reference}

    directory = SimpleNamespace(
        read=lambda path: encode(documents[path]),
        json=lambda path: deepcopy(documents[path]),
    )
    now = datetime.now(UTC)
    until = (now + timedelta(hours=1)).isoformat()
    config = {
        "installation_id": "offline-flash",
        "candidate_id": "d" * 64,
        "worker": "offline-worker",
        "controller": "offline-controller",
    }
    identity = {key: config[key] for key in ("installation_id", "candidate_id")}
    methods = {
        "source": "native_owner_auth_and_readback",
        "isolation": "macos_process_probe_and_owner_readback",
        "deployment": "installed_artifact_inventory_and_signature_readback",
        "capacity": "actual_local_storage_synthetic_workload",
        "phases": "synthetic_offline_mechanism_validation",
        "model_identity": "provider_account_identity_readback",
        "advisory_budget": "owner_reviewed_account_and_dispatch_evidence",
    }
    evidence = {}
    for kind in sorted(ADVISORY_REQUIRED_EVIDENCE):
        checks = EVIDENCE_CHECKS[kind]
        if kind == "model_identity":
            checks = {"flash_resolved_identity", "account_route"}
        observations = []
        for check in sorted(checks):
            result = "denied" if check.endswith("_denied") else "verified"
            raw = document(
                {
                    "contract": "V1-LOCAL-RAW-OBSERVATION",
                    "version": 1,
                    "check": check,
                    **identity,
                    "observed_at": now.isoformat(),
                    "observer": "offline-reader-test",
                    "subject": "offline-fixture",
                    "method": methods[kind],
                    "result": result,
                    "artifacts": [document({"offline-fixture": check})],
                }
            )
            observations.append(
                {
                    "check": check,
                    "expected": result,
                    "actual": result,
                    "raw_evidence": raw,
                }
            )
        evidence[kind] = document(
            {
                "contract": "V1-LOCAL-VERIFICATION-EVIDENCE",
                "version": 1,
                "kind": kind,
                **identity,
                "observed_at": now.isoformat(),
                "valid_until": until,
                "synthetic": kind == "phases",
                "evidence_class": "offline_engineering"
                if kind == "phases"
                else "actual_environment_or_account",
                "origin": "offline-fixture",
                "observations": observations,
            }
        )
    readiness = {
        "contract": "V3-LOCAL-FLASH-READINESS",
        "version": 3,
        "trust_profile": "owner_trusted_local",
        **config,
        "plan_sha256": "a" * 64,
        "binding_sha256": "b" * 64,
        "valid_from": now.isoformat(),
        "valid_until": until,
        "evidence": evidence,
        "identities": {
            "flash": "offline-Flash-identity",
            "valid_until": until,
            "evidence": evidence["model_identity"]["sha256"],
        },
        "approval_ref": "offline-no-real-approval",
    }
    config["readiness_path"] = document(readiness)["path"]
    assert (
        read_readiness(
            directory,
            config,
            now=now,
            budget_policy="advisory_dispatch",
            model_policy="flash_only",
        )
        == readiness
    )
    with pytest.raises(ValueError, match="real_execution_prerequisites_unverified"):
        read_readiness(directory, config, now=now, budget_policy="advisory_dispatch")
    changed = deepcopy(readiness)
    changed["identities"]["pro"] = "other-model"
    config["readiness_path"] = document(changed)["path"]
    with pytest.raises(ValueError, match="controlled_contract_invalid"):
        read_readiness(
            directory,
            config,
            now=now,
            budget_policy="advisory_dispatch",
            model_policy="flash_only",
        )
