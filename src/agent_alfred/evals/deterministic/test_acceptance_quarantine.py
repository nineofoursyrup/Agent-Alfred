"""Historical facts remain readable; copies and descendants retain audit limits."""

from copy import deepcopy

from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.store import EvidenceStore


def test_import_read_report_and_regrade_preserve_quarantine(tmp_path):
    batch = controlled_batch()
    batch["batch_id"] = "calibration-isolated-c25-proposal-r1"
    original = deepcopy(batch)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    assert store.read(batch["batch_id"]) == original
    assert (
        store.authorization_status(batch["batch_id"])["status"]
        == "QUARANTINED_AUDIT_ONLY"
    )
    summary = report(batch, store=store)
    assert summary["authorization"]["validity"] == "INVALID"
    assert "execution_authorization_invalid" in summary["v1_release"]["blockers"]
    child = store.revise(batch["batch_id"], "renamed-copy")
    assert (
        store.authorization_status(child["batch_id"])["status"]
        == "QUARANTINED_AUDIT_ONLY"
    )
    assert report(child)["authorization"]["status"] == "QUARANTINED_AUDIT_ONLY"
    assert store.read(batch["batch_id"]) == original


def test_known_invalid_calibration_cannot_be_a_formal_source(tmp_path):
    import pytest

    from agent_alfred.evals.acceptance.calibration import validate_source

    batch = controlled_batch()
    batch["batch_id"] = "calibration-isolated-c25-proposal-r1"
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    with pytest.raises(ValueError, match="execution_authorization_invalid"):
        validate_source(store.read(batch["batch_id"]), store)


def test_quarantined_seen_family_cannot_be_declared_unseen(tmp_path):
    from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
        approve_cases,
        real_materials,
    )

    batch = real_materials()
    batch["cases"][0]["source_family_id"] = "c24-memory-local-scope-override"
    approve_cases(batch)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    summary = report(store.read(batch["batch_id"]), store=store)
    assert "seen_material_reused" in summary["v1_release"]["blockers"]
    assert batch["cases"][0]["id"] in summary["authorization"]["seen_case_ids"]


def test_relabeling_known_incident_as_simulation_cannot_enter_factory(tmp_path):
    import pytest

    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.deterministic.test_acceptance_execution_policy import (
        authorized_fixture,
    )

    batch, root = authorized_fixture()
    batch["batch_id"] = "calibration-isolated-c25-proposal-r1"
    batch["authorization"]["binding"] = binding(batch)
    with pytest.raises(ValueError, match="execution_authorization_invalid"):
        execute(
            batch,
            tmp_path / "runtime",
            candidate_root=root,
            store=EvidenceStore(tmp_path / "evidence"),
            product_factory_builder=lambda: pytest.fail("quarantined factory"),
        )
