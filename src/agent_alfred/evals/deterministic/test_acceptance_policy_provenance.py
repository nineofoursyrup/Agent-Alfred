"""Approved agent provenance at import, execution, and linked evidence boundaries."""

import hashlib
from copy import deepcopy

import pytest

from agent_alfred.evals.acceptance.budget import validate_execution_materials
from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.schema import calibration_identity, digest, validate
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_agent_policy import actor


def rehash(value):
    value["id"] = digest({k: v for k, v in value.items() if k != "id"})


def material_approval(batch, agent_id="fixture-material-approver"):
    owner = actor("material_approver")
    owner.update(
        agent_id=agent_id,
        started_at="2026-09-22T00:00:00Z",
        completed_at="2026-09-22T00:01:00Z",
    )
    return {
        "kind": "agent_approved",
        "by": agent_id,
        "at": owner["completed_at"],
        "reference": "OFFLINE SYNTHETIC PROVENANCE",
        "actor": owner,
        "policy_id": batch["review_policy"]["id"],
    }


def approve_cases(batch, agent_id="fixture-material-approver"):
    batch["case_set_approval"] = {
        **material_approval(batch, agent_id),
        "cases_sha256": digest(batch["cases"]),
        "semantic_rubric_id": batch["semantic_rubric"]["id"],
        "seen_families_sha256": digest(batch["seen_families"]),
    }


def real_materials():
    batch = controlled_batch()
    originals = batch["cases"]
    batch.update(phase="calibration", simulation=False, cases=[])
    for original in originals:
        for index in range(5):
            case = deepcopy(original)
            case.update(
                id=original["id"] + str(index), input=original["input"] + str(index)
            )
            case.pop("script")
            case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
            batch["cases"].append(case)
    batch["semantic_rubric"]["approval"] = material_approval(batch)
    rehash(batch["semantic_rubric"])
    approve_cases(batch)
    return batch


@pytest.mark.parametrize(
    "field", ["case_set_approval", "semantic", "calibration_approval"]
)
def test_non_simulation_import_requires_agent_material_provenance(tmp_path, field):
    batch = real_materials()
    legacy = {
        "kind": "approved",
        "by": "fixture-user",
        "at": "2026-09-22T00:02:00Z",
        "reference": "OFFLINE synthetic counterexample",
    }
    if field == "semantic":
        batch["semantic_rubric"]["approval"] = legacy
        rehash(batch["semantic_rubric"])
        approve_cases(batch)
    elif field == "calibration_approval":
        batch[field] = {**legacy, "evidence_sha256": calibration_identity(batch)}
    else:
        batch[field] = {**batch[field], **legacy}
        batch[field].pop("actor")
        batch[field].pop("policy_id")
    before = deepcopy(batch)
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match="agent_approval_required"):
        store.import_batch(batch)
    assert batch == before
    assert not store.root.exists()


@pytest.mark.parametrize(
    "field,reason",
    [
        ("case_set_approval", "case_set_approval_missing"),
        ("semantic", "approved_rubric_missing"),
    ],
)
def test_direct_execution_material_check_rejects_legacy_approval(
    tmp_path, field, reason
):
    batch = real_materials()
    legacy = {
        "kind": "approved",
        "by": "fixture-user",
        "at": "2026-09-22T00:02:00Z",
        "reference": "OFFLINE synthetic only",
    }
    if field == "semantic":
        batch["semantic_rubric"]["approval"] = legacy
        rehash(batch["semantic_rubric"])
        approve_cases(batch)
    else:
        proof = batch[field]
        batch[field] = {
            **legacy,
            **{
                k: v
                for k, v in proof.items()
                if k.endswith("_sha256") or k == "semantic_rubric_id"
            },
        }
    with pytest.raises(ValueError, match=reason):
        validate_execution_materials(batch, EvidenceStore(tmp_path / "evidence"))


def test_same_material_approver_can_certify_cases_and_semantics(tmp_path):
    batch = real_materials()
    store = EvidenceStore(tmp_path / "evidence")
    validate(batch)
    validate_execution_materials(batch, store, started_at="2026-09-23T00:00:00Z")
    store.import_batch(batch)
    assert store.read(batch["batch_id"]) == batch


def panel(batch, *, index=0, prefix=""):
    owner = actor("adjudicator")
    owner.update(
        agent_id=prefix + owner["agent_id"],
        started_at="2026-09-23T00:30:00Z",
        completed_at="2026-09-23T00:40:00Z",
    )
    annotations = [
        {"actor": actor(role), "output": "synthetic-only"}
        for role in ("blind_a", "blind_b")
    ]
    for row in annotations:
        row["actor"]["agent_id"] = prefix + row["actor"]["agent_id"]
    grade = batch["grades"][index]
    ruling = {
        "version": 2,
        "kind": "agent_adjudication",
        "grade_id": grade["id"],
        "actor": owner,
        "policy": batch["review_policy"],
        "blind_annotations": annotations,
        "at": owner["completed_at"],
        "reason": "OFFLINE synthetic panel",
        "rubric_id": batch["semantic_rubric"]["id"],
        "dimensions": deepcopy(grade["dimensions"]),
        "prohibitions": deepcopy(grade["prohibitions"]),
    }
    rehash(ruling)
    return ruling


@pytest.mark.parametrize("role", ["blind_a", "blind_b", "adjudicator"])
def test_material_approver_cannot_join_panel_in_a_different_role(tmp_path, role):
    from agent_alfred.evals.deterministic.test_acceptance_schema3 import scored_v3

    batch = scored_v3("offline_fixture", 1, "role-reuse")
    approve_cases(batch, "known-material-agent")
    ruling = panel(batch)
    member = (
        ruling["actor"]
        if role == "adjudicator"
        else next(
            row["actor"]
            for row in ruling["blind_annotations"]
            if row["actor"]["role"] == role
        )
    )
    member["agent_id"] = "known-material-agent"
    rehash(ruling)
    batch["adjudications"] = [ruling]
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match="agent_roles_not_independent"):
        store.import_batch(batch)
    assert not store.root.exists()


def test_panel_identity_cannot_change_role_between_grades(tmp_path):
    from agent_alfred.evals.deterministic.test_acceptance_schema3 import scored_v3

    batch = scored_v3("offline_fixture", 1, "cross-panel")
    first, second = panel(batch), panel(batch, index=1, prefix="second-")
    second["blind_annotations"][1]["actor"]["agent_id"] = first["blind_annotations"][0][
        "actor"
    ]["agent_id"]
    rehash(second)
    batch["adjudications"] = [first, second]
    with pytest.raises(ValueError, match="agent_roles_not_independent"):
        EvidenceStore(tmp_path / "evidence").import_batch(batch)


def test_material_actor_cannot_reappear_as_review_adjudicator(tmp_path):
    from agent_alfred.evals.acceptance.reviews import binding
    from agent_alfred.evals.deterministic.test_acceptance_agent_policy import (
        agent_ruling,
    )
    from agent_alfred.evals.deterministic.test_acceptance_schema3 import scored_v3

    batch = scored_v3("offline_fixture", 1, "review-role-reuse")
    approve_cases(batch, "known-material-agent")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    content = "Synthetic review of supplied execution evidence."
    review = {
        "version": 1,
        "kind": "agent_review_dispute",
        "binding": binding(batch, batch["grades"][0]["id"]),
        "item": {"kind": "prohibitions", "name": batch["cases"][0]["forbidden"][0]},
        "reason": content,
        "evidence": ["result:" + batch["results"][0]["id"] + "#/output"],
        "at": "2026-09-23T00:05:00Z",
        "source": {
            "kind": "agent",
            "name": "synthetic reviewer",
            "reference": "synthetic-only",
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
    }
    rehash(review)
    store.revise(batch["batch_id"], "reviewed", reviews=[review])
    ruling = agent_ruling(review)
    ruling["actor"]["agent_id"] = "known-material-agent"
    rehash(ruling)
    with pytest.raises(ValueError, match="agent_roles_not_independent"):
        store.revise("reviewed", "rejected-role", review_adjudications=[ruling])
    assert not (store.root / "rejected-role").exists()


def approve_calibration(batch, agent_id="fixture-calibration-approver"):
    proof = material_approval(batch, agent_id)
    proof["actor"].update(
        started_at="2026-09-23T01:00:00Z", completed_at="2026-09-23T01:10:00Z"
    )
    proof.update(
        at=proof["actor"]["completed_at"], evidence_sha256=calibration_identity(batch)
    )
    batch["calibration_approval"] = proof


@pytest.mark.parametrize(
    "origin",
    [
        "case_approval",
        "calibration_approval",
        "source_panel",
        "ancestor_panel",
    ],
)
def test_linked_calibration_roles_cannot_be_reassigned(tmp_path, origin):
    from agent_alfred.evals.deterministic.test_acceptance_schema3 import scored_v3

    source = scored_v3("calibration", 5, "source-")
    target = scored_v3("offline_fixture", 1, "target-")
    shared = "known-source-agent"
    if origin in ("source_panel", "ancestor_panel"):
        source_ruling = panel(source)
        source_ruling["blind_annotations"][0]["actor"]["agent_id"] = shared
        rehash(source_ruling)
        source["adjudications"] = [source_ruling]
        approve_cases(target, shared)
    else:
        if origin == "case_approval":
            approve_cases(source, shared)
        ruling = panel(target)
        ruling["blind_annotations"][0]["actor"]["agent_id"] = shared
        rehash(ruling)
        target["adjudications"] = [ruling]
    approve_calibration(
        source,
        shared if origin == "calibration_approval" else "fixture-calibration-approver",
    )
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    if origin == "ancestor_panel":
        child = deepcopy(source)
        child.update(
            batch_id="source-child",
            parent={
                "batch_id": source["batch_id"],
                "sha256": digest(source),
                "relation": "regrade",
            },
            adjudications=[],
        )
        approve_calibration(child)
        store.import_batch(child)
        source = child
    target["calibration"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "material_ids": [c["material_id"] for c in source["cases"]],
    }
    target["seen_families"] += sorted({c["source_family_id"] for c in source["cases"]})
    if target.get("case_set_approval"):
        approve_cases(target, shared)
    validate(target)  # Each standalone package is internally consistent.
    with pytest.raises(ValueError, match="agent_roles_not_independent"):
        store.import_batch(target)
    assert store.read(source["batch_id"]) == source
    assert not (store.root / target["batch_id"]).exists()


def test_user_execution_and_threshold_approval_are_not_agent_material_approval(
    tmp_path,
):
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.evals.deterministic.test_acceptance_schema3 import aggregation_for
    from agent_alfred.evals.deterministic.test_acceptance_trial import authorize

    batch = real_materials()
    batch["aggregation_policy"] = aggregation_for(batch)
    batch["aggregation_policy"]["approval"].update(
        kind="approved", by="synthetic-user", reference="OFFLINE user threshold fixture"
    )
    rehash(batch["aggregation_policy"])
    authorize(batch)
    budget = AuthorizedBatch(batch)
    assert budget.requests == []
    validate_execution_materials(
        batch, EvidenceStore(tmp_path / "evidence"), started_at=budget.started_at
    )
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    assert store.read(batch["batch_id"]) == batch


def test_schema2_legacy_approvals_and_agent_appendices_remain_readable(tmp_path):
    from datetime import UTC, datetime

    from agent_alfred.evals.acceptance.report import report
    from agent_alfred.evals.deterministic.test_acceptance import scored_fixture
    from agent_alfred.evals.deterministic.test_acceptance_agent_policy import (
        agent_ruling,
    )
    from agent_alfred.evals.deterministic.test_acceptance_disputes import opinion

    # Synthetic legacy document, not a newly claimed online sample or human ruling.
    batch = scored_fixture()
    batch.update(schema_version=2, phase="trial", simulation=False)
    profile = batch["profiles"][0]
    profile["local_tool_allowlist"] = ["draft_message"]
    rehash(profile)
    batch["rubric"]["approval"]["kind"] = "approved"
    rehash(batch["rubric"])
    for case, result, grade in zip(batch["cases"], batch["results"], batch["grades"]):
        case["source"]["kind"] = "approved"
        result["profile_id"] = profile["id"]
        grade.update(
            result_hash=digest(result),
            case_hash=digest(case),
            rubric_id=batch["rubric"]["id"],
        )
    batch["case_set_approval"] = {
        **batch["rubric"]["approval"],
        "cases_sha256": digest(batch["cases"]),
    }
    original = deepcopy(batch)
    store = EvidenceStore(tmp_path / "evidence")
    validate_execution_materials(batch, store)
    store.import_batch(batch)
    reviews = [opinion(batch, index) for index in (0, 1)]
    reviewed = store.revise(batch["batch_id"], "schema2-reviewed", reviews=reviews)
    revised = store.revise(
        reviewed["batch_id"],
        "schema2-agent-appendix",
        review_adjudications=[agent_ruling(review) for review in reviews],
    )
    assert store.read(batch["batch_id"]) == original
    assert store.read(revised["batch_id"]) == revised
    assert revised["grades"] == original["grades"]
    assert revised["results"] == original["results"]
    assert all("human" not in row for row in revised["review_adjudications"])
    summary = report(revised, store=store, now=datetime(2026, 9, 23, 2, tzinfo=UTC))
    assert all(
        row["status"] == "pending" for row in summary["quality"]["review_disputes"]
    )
