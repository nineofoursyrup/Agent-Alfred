"""Source readback is a live requirement, not a reusable local permission."""

import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.acceptance.supplement_decisions import (
    decision_request,
    make_summary,
)


def fixture(tmp_path, *, clock=None):
    authority = SimulationAuthority(tmp_path / "authority", clock=clock)
    batch = supplement_batch()
    summary = make_summary(batch)
    batch["summaries"].append(summary)
    event = authority.issue_decision(
        decision_request(summary),
        subject="simulation:user",
        decision="approved",
        reason="Synthetic fixture accepts these exact materials only",
        evidence=["synthetic:original-human-message"],
    )
    batch["user_decisions"].append(event)
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    store.import_batch(batch)
    return authority, store, batch, event


def test_material_readback_is_repeatable_but_never_authorizes_execution(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        material_binding,
    )

    authority, store, batch, event = fixture(tmp_path)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest(["synthetic-package"])
    )
    before = (store.root / batch["batch_id"] / "batch.json").read_bytes()
    for _ in range(2):
        admission = DecisionAdmission(
            EvidenceStore(store.root), source=authority, subject="simulation:user"
        )
        result = admission.verify_materials(binding)
        assert result["source_status"] == "VERIFIED_SYNTHETIC_ONLY"
        assert result["material_decision"]["id"] == event["id"]
        assert result["online_executable"] is False
        assert result["release_eligible"] is False
    assert (store.root / batch["batch_id"] / "batch.json").read_bytes() == before
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        DecisionAdmission(EvidenceStore(store.root)).verify_materials(binding)
    authority.revoke_decision(event["source_ref"])
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        admission.verify_materials(binding)
    assert store.read(batch["batch_id"])["user_decisions"] == [event]


def test_wrong_subject_and_future_decision_do_not_read_as_approval(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        material_binding,
    )

    authority, store, batch, _ = fixture(tmp_path)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest(["synthetic-package"])
    )
    with pytest.raises(ValueError, match="decision_subject_mismatch"):
        DecisionAdmission(
            store, source=authority, subject="simulation:someone-else"
        ).verify_materials(binding)
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        DecisionAdmission(
            store,
            source=authority,
            subject="simulation:user",
        ).verify_materials(binding, now=datetime(2000, 1, 1, tzinfo=UTC))


def test_run_and_checkpoint_scopes_require_fresh_exact_readback(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        checkpoint_request,
        material_binding,
        run_request,
    )

    authority, store, batch, material_event = fixture(tmp_path)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest(["synthetic-package"])
    )
    reader = DecisionAdmission(store, subject="simulation:user")
    request = run_request(
        binding,
        job_id="fixture-job",
        plan_sha256=digest("plan"),
        budget_sha256=digest("one-original-budget"),
    )
    with pytest.raises(ValueError, match="invalid_execution_decision"):
        reader.verify_execution(request, material_event["source_ref"], binding)
    event = authority.issue_execution_decision(
        request,
        subject="simulation:user",
        decision="approved",
        reason="Synthetic run only",
        evidence=["synthetic:run-message"],
    )
    assert reader.verify_execution(request, event["source_ref"], binding) == event
    checkpoint = checkpoint_request(
        binding,
        job_id="fixture-job",
        run_decision=event,
        judge_summary_sha256=digest("18-results-and-18-reviews"),
        disputes_sha256=digest([]),
        remaining_budget_sha256=digest("remaining"),
    )
    with pytest.raises(ValueError, match="decision_scope_mismatch"):
        reader.verify_execution(checkpoint, event["source_ref"], binding)
    continued = authority.issue_execution_decision(
        checkpoint,
        subject="simulation:user",
        decision="approved",
        reason="Synthetic product checkpoint only",
        evidence=["synthetic:checkpoint"],
    )
    assert (
        reader.verify_execution(checkpoint, continued["source_ref"], binding)
        == continued
    )
    changed = deepcopy(checkpoint)
    changed["judge_summary_sha256"] = digest("changed-results")
    changed["id"] = digest({k: v for k, v in changed.items() if k != "id"})
    with pytest.raises(ValueError, match="decision_scope_mismatch"):
        reader.verify_execution(changed, continued["source_ref"], binding)
    authority.revoke_decision(event["source_ref"])
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        reader.verify_execution(checkpoint, continued["source_ref"], binding)


def test_source_dispute_requires_individual_narrow_scope_without_rewriting_unknown(
    tmp_path,
):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        dispute_scope_request,
        material_binding,
    )
    from agent_alfred.evals.acceptance.supplement_decisions import dispute_request
    from agent_alfred.evals.acceptance.supplement_reviews import disputes
    from agent_alfred.evals.deterministic.test_acceptance_supplement import review

    authority = SimulationAuthority(tmp_path / "authority")
    batch = supplement_batch()
    batch["review_disputes"] = [
        review(
            batch,
            "materials",
            changes={
                "source_independence": {"status": "unknown", "support": "unknown"},
            },
        )
    ]
    dispute = disputes(batch)[0]
    summary = make_summary(batch)
    batch["summaries"].append(summary)
    batch["user_decisions"].append(
        authority.issue_decision(
            decision_request(summary),
            subject="simulation:user",
            decision="approved",
            reason="Approve fixture material choice",
            evidence=["synthetic:human-message"],
        )
    )
    ruling = authority.issue_decision(
        dispute_request(batch, dispute),
        subject="simulation:user",
        decision="dismissed",
        reason="Accept disclosed judge diagnostic use; independence remains unknown",
        evidence=["materials#/seen_families"],
    )
    batch["adjudications"].append(ruling)
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    store.import_batch(batch)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest(["synthetic-package"])
    )
    with pytest.raises(ValueError, match="material_dispute_scope_unverifiable"):
        DecisionAdmission(store, subject="simulation:user").verify_materials(binding)
    scope = authority.issue_execution_decision(
        dispute_scope_request(binding, dispute, ruling),
        subject="simulation:user",
        decision="approved",
        reason="Only the displayed limited diagnostic scope",
        evidence=["synthetic:original-scope-choice"],
    )
    reader = DecisionAdmission(
        store, subject="simulation:user", dispute_scope_refs=[scope["source_ref"]]
    )
    result = reader.verify_materials(binding)
    assert result["disputes"][0]["opinion"]["status"] == "unknown"
    assert result["disputes"][0]["accepted_use"] == "judge_diagnostic_only"
    assert result["source_independence"] == "unknown"
    assert store.read(batch["batch_id"])["review_disputes"] == batch["review_disputes"]
    authority.revoke_decision(ruling["source_ref"])
    with pytest.raises(ValueError, match="material_dispute_unresolved"):
        reader.verify_materials(binding)


def test_descendants_require_complete_lineage_and_preserve_old_summary_identity(
    tmp_path,
):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        material_binding,
    )

    _, store, root, event = fixture(tmp_path)
    child = store.revise(root["batch_id"], "child")
    newer = deepcopy(child)
    newer.update(
        batch_id="new-candidate",
        parent={
            "batch_id": child["batch_id"],
            "sha256": digest(child),
            "relation": "retry",
        },
    )
    newer["candidate"]["tree"] = "synthetic-new-tree"
    newer["candidate_id"] = digest(newer["candidate"])
    store.import_batch(newer)
    binding = material_binding(
        store,
        newer["batch_id"],
        approved_batch_id=root["batch_id"],
        package_manifest_sha256=digest(["synthetic-package"]),
    )
    assert [r["batch_id"] for r in binding["ancestors"]] == [root["batch_id"], "child"]
    assert list(binding["changes"]) == ["candidate_id"]
    assert binding["approved_summary"]["object_id"] == event["object_id"]
    assert binding["current_summary"]["object_id"] != event["object_id"]
    result = DecisionAdmission(store, subject="simulation:user").verify_materials(
        binding
    )
    assert result["material_decision"]["object_id"] == event["object_id"]
    assert result["run_decision_required"] is True
    store.delete(root["batch_id"])
    with pytest.raises(ValueError, match="authorization_ancestry_unverifiable"):
        DecisionAdmission(store, subject="simulation:user").verify_materials(binding)


def test_quarantined_grandparent_cannot_be_renamed_into_a_binding(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import material_binding

    store = EvidenceStore(tmp_path / "evidence")
    root = supplement_batch(prefix="calibration-isolated-c25-proposal-r1")
    store.import_batch(root)
    child = store.revise(root["batch_id"], "different-name")
    leaf = store.revise(child["batch_id"], "another-name")
    with pytest.raises(ValueError, match="execution_authorization_invalid"):
        material_binding(
            store, leaf["batch_id"], package_manifest_sha256=digest("synthetic-package")
        )
    assert (
        store.authorization_status(leaf["batch_id"])["status"]
        == "QUARANTINED_AUDIT_ONLY"
    )


def test_retry_relationship_cannot_reset_a_started_budget(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import material_binding

    _, store, batch, _ = fixture(tmp_path)
    started = deepcopy(batch)
    started.update(
        batch_id="started",
        budget_scope="one-scope",
        budget_started_at=datetime.now(UTC).isoformat(),
        parent={
            "batch_id": batch["batch_id"],
            "sha256": digest(batch),
            "relation": "retry",
        },
    )
    store.import_batch(started)
    reset = deepcopy(started)
    reset.update(
        batch_id="reset",
        budget_scope="new-scope",
        budget_started_at=datetime.now(UTC).isoformat(),
        parent={
            "batch_id": started["batch_id"],
            "sha256": digest(started),
            "relation": "retry",
        },
    )
    store.import_batch(reset)  # Historical retry packages remain readable.
    with pytest.raises(ValueError, match="execution_budget_continuity_lost"):
        material_binding(
            store, "reset", package_manifest_sha256=digest("synthetic-package")
        )


def test_public_preflight_and_cli_never_trust_local_approval_files(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import source_preflight

    authority, store, batch, _ = fixture(tmp_path)
    package_digest = digest("synthetic-package")
    files = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    report = source_preflight(
        store,
        batch["batch_id"],
        package_manifest_sha256=package_digest,
        source=authority,
        subject="simulation:user",
    )
    assert report["source_status"] == "VERIFIED_SYNTHETIC_ONLY"
    assert report["v1_release"]["verdict"] == "BLOCKED"
    command = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_alfred.evals.acceptance",
            "source-preflight",
            "--store",
            str(store.root),
            "--batch",
            batch["batch_id"],
            "--package-manifest-sha256",
            package_digest,
            "--subject",
            "simulation:user",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 2
    assert json.loads(command.stdout)["blockers"] == ["approval_source_unverifiable"]
    assert files == {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}


def test_arbitrary_source_cannot_promote_fixture_or_real_permission(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        material_binding,
    )
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.supplement_schema import signed

    _, store, batch, event = fixture(tmp_path)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest("synthetic-package")
    )

    class ForgedSource:
        def read_decision(self, source_ref):
            pytest.fail("unregistered source must not be consulted")

    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        DecisionAdmission(
            store, source=ForgedSource(), subject=event["subject"]
        ).verify_materials(binding)
    real = signed(
        {k: v for k, v in binding.items() if k != "id"} | {"simulation": False}
    )
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        DecisionAdmission(store, subject=event["subject"]).verify_materials(real)

    class Credentials:
        def values(self):
            pytest.fail("credentials must not be read")

    def forbidden_factory(*args, **kwargs):
        pytest.fail("factory must not be constructed")

    batch["simulation"] = False
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        execute(
            batch,
            tmp_path / "runtime",
            credentials=Credentials(),
            product_factory_builder=forbidden_factory,
        )
    assert not (tmp_path / "runtime").exists()


def test_material_change_and_removed_parent_cannot_reuse_old_approval(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        material_binding,
    )
    from agent_alfred.evals.acceptance.supplement_schema import manifest, material_id

    authority, store, batch, _ = fixture(tmp_path)
    cases = deepcopy(batch["cases"])
    cases[0]["gold"] = "Different approved answer would be needed"
    cases[0]["material_id"] = material_id(cases[0])
    revised = store.revise(
        batch["batch_id"],
        "new-material",
        configuration={"cases": cases, "manifest": manifest(cases)},
    )
    binding = material_binding(
        store,
        revised["batch_id"],
        approved_batch_id=batch["batch_id"],
        package_manifest_sha256=digest("synthetic-package"),
    )
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        DecisionAdmission(store, subject="simulation:user").verify_materials(binding)

    # A separate copied store cannot strip the parent from the bound candidate.
    child = store.revise(batch["batch_id"], "same-material")
    binding = material_binding(
        store,
        child["batch_id"],
        approved_batch_id=batch["batch_id"],
        package_manifest_sha256=digest("synthetic-package"),
    )
    orphan = deepcopy(child)
    orphan["parent"] = None
    copied = EvidenceStore(tmp_path / "copied", decision_source=authority)
    copied.import_batch(orphan)
    with pytest.raises(ValueError, match="approved_material_ancestor_missing"):
        DecisionAdmission(copied, subject="simulation:user").verify_materials(binding)


def test_source_corruption_is_not_recovered_from_saved_approval(tmp_path):
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        material_binding,
    )

    authority, store, batch, _ = fixture(tmp_path)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest("synthetic-package")
    )
    reader = DecisionAdmission(store, subject="simulation:user")
    reader.verify_materials(binding)
    state_path = authority.root / "state.json"
    original = state_path.read_bytes()
    state_path.write_text("{}")
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        reader.verify_materials(binding)
    state_path.write_bytes(original)
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        DecisionAdmission(store, subject="simulation:user").verify_materials(binding)


@pytest.mark.parametrize("change", [{"version": 2}, {"extra": True}])
def test_new_event_contract_never_silently_downgrades(tmp_path, change):
    from agent_alfred.evals.acceptance.execution_decisions import (
        material_binding,
        run_request,
    )
    from agent_alfred.evals.acceptance.supplement_schema import signed

    authority, store, batch, _ = fixture(tmp_path)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest("synthetic-package")
    )
    request = run_request(
        binding,
        job_id="job",
        plan_sha256=digest("plan"),
        budget_sha256=digest("budget"),
    )
    request = signed({k: v for k, v in request.items() if k != "id"} | change)
    with pytest.raises(ValueError, match="execution_decision"):
        authority.issue_execution_decision(
            request,
            subject="simulation:user",
            decision="approved",
            reason="A claimed future version is not approval",
            evidence=["synthetic:claim"],
        )


def test_run_decision_cannot_predate_its_material_decision(tmp_path):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.acceptance.execution_decisions import (
        DecisionAdmission,
        material_binding,
        run_request,
    )

    now = datetime(2026, 9, 27, tzinfo=UTC)
    clock = FakeClock(wall=now)
    authority, store, batch, _ = fixture(tmp_path, clock=clock)
    binding = material_binding(
        store, batch["batch_id"], package_manifest_sha256=digest("synthetic-package")
    )
    request = run_request(
        binding,
        job_id="job",
        plan_sha256=digest("plan"),
        budget_sha256=digest("budget"),
    )
    clock.wall = now - timedelta(seconds=1)
    event = authority.issue_execution_decision(
        request,
        subject="simulation:user",
        decision="approved",
        reason="Inconsistent source time",
        evidence=["synthetic:wrong-time"],
    )
    with pytest.raises(ValueError, match="execution_decision_before_materials"):
        DecisionAdmission(store, subject="simulation:user").verify_execution(
            request,
            event["source_ref"],
            binding,
            now=now,
        )
