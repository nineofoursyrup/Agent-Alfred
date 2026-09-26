"""Schema3 formal approvals must exist before the authorized execution window."""

from copy import deepcopy
from datetime import datetime

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.acceptance.budget import (
    AuthorizedBatch,
    validate_execution_materials,
)
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_material_chronology import (
    declared_calibration_with_time,
    set_proof_time,
)
from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
    approve_calibration,
    approve_cases,
    rehash,
)
from agent_alfred.evals.deterministic.test_acceptance_schema3 import aggregation_for
from agent_alfred.evals.deterministic.test_acceptance_trial import authorize

START = "2026-09-23T02:00:00Z"
FUTURE = "2026-09-24T00:00:00Z"


def formal_pair(
    field="aggregation", when="2026-09-23T01:30:00Z", *, candidate=None, stage="fresh"
):
    """Adversarial synthetic metadata, never actual online samples or approvals."""
    source, target = declared_calibration_with_time("case", "2026-09-23T00:00:00Z")
    if candidate is not None:
        source["candidate"] = candidate
        source["candidate_id"] = digest(candidate)
        authorize(source)
        source["authorization"]["max_requests"] = 1000
        approve_calibration(source)
        target["candidate"] = deepcopy(candidate)
        target["candidate_id"] = source["candidate_id"]
    if field == "calibration":
        set_proof_time(source["calibration_approval"], when)
    target.update(phase="formal", batch_id="approval-cutoff-target")
    originals = target["cases"]
    target["cases"] = []
    for group in (
        "conversation",
        "memory",
        "tools",
        "skills",
        "routing",
        "aggregation",
    ):
        template = next(c for c in originals if c["group"] == group)
        for index in range(20):
            case = deepcopy(template)
            case.update(
                id=f"formal-{group}-{index}",
                input=f"Unseen synthetic formal {group} {index}",
                source_family_id=f"unseen-formal-{group}",
            )
            case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
            target["cases"].append(case)
    approve_cases(target)
    target["calibration"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "material_ids": [case["material_id"] for case in source["cases"]],
    }
    target["aggregation_policy"] = aggregation_for(source)
    target["aggregation_policy"]["approval"].update(
        kind="approved",
        at=when if field == "aggregation" else "2026-09-23T01:30:00Z",
        reference="SYNTHETIC chronology fixture only",
    )
    rehash(target["aggregation_policy"])
    if stage != "fresh":
        result = deepcopy(source["results"][0])
        result.update(
            id="prior-formal-result",
            batch_id=target["batch_id"],
            case_id=target["cases"][0]["id"],
            sampled_at="2026-09-23T02:00:02Z",
            finished_at="2026-09-23T02:00:03Z",
        )
        target["results"] = [result]
        if stage == "resume":
            target["budget_started_at"] = START
    authorize(target)
    if field == "authorization":
        target["authorization"]["at"] = when
    now = START if stage == "fresh" else "2026-09-23T02:10:00Z"
    return source, target, FakeClock(wall=datetime.fromisoformat(now))


def inventory(store):
    return {
        str(p.relative_to(store.root)): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }


def test_future_aggregation_is_rejected_before_public_budget_factory(tmp_path):
    source, target, clock = formal_pair("aggregation", FUTURE)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)
    budget = AuthorizedBatch(target, clock=clock)
    built = []

    def forbidden():
        built.append(True)
        raise AssertionError("FACTORY_REACHED_NO_CLIENT_CONSTRUCTED")

    with pytest.raises(ValueError, match="aggregation_approval_after_execution"):
        validate_execution_materials(target, store, started_at=budget.started_at)
        budget.client(forbidden, role="product")
    assert built == []
    assert budget.requests == []
    assert inventory(store) == before


@pytest.mark.parametrize(
    "field,when,reason",
    [
        ("calibration", FUTURE, "calibration_approval_after_execution"),
        ("authorization", FUTURE, "authorization_after_execution"),
        (
            "aggregation",
            "2026-09-23T00:00:00Z",
            "aggregation_approval_before_calibration",
        ),
        (
            "aggregation",
            "2026-09-23T01:00:00Z",
            "aggregation_approval_before_calibration",
        ),
    ],
)
def test_related_approval_chronology_stops_before_factory(
    tmp_path, field, when, reason
):
    source, target, clock = formal_pair(field, when)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)
    built = []

    def forbidden():
        built.append(True)
        raise AssertionError("FACTORY_REACHED_NO_CLIENT_CONSTRUCTED")

    with pytest.raises(ValueError, match=reason):
        budget = AuthorizedBatch(target, clock=clock)
        validate_execution_materials(target, store, started_at=budget.started_at)
        budget.client(forbidden, role="judge")
    assert built == []
    assert target.get("requests", []) == []
    assert inventory(store) == before


@pytest.mark.parametrize("field", ["aggregation", "calibration"])
@pytest.mark.parametrize("stage", ["resume", "new_budget_with_prior_sample"])
def test_formal_approval_cannot_use_later_judge_clock(tmp_path, field, stage):
    source, target, clock = formal_pair(field, "2026-09-23T02:05:00Z", stage=stage)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)
    budget = AuthorizedBatch(target, clock=clock)
    with pytest.raises(ValueError, match=field + "_approval_after_execution"):
        validate_execution_materials(target, store, started_at=budget.started_at)
    assert budget.requests == []
    assert inventory(store) == before


@pytest.mark.parametrize(
    "when",
    [
        "2026-09-23T01:10:00Z",  # Equal to the referenced calibration approval.
        "2026-09-23T02:00:00Z",  # Equal to the shared budget start.
        "2026-09-23T10:00:00+08:00",
        "2026-09-22T21:00:00-05:00",
    ],
)
def test_aggregation_approval_accepts_closed_interval_and_timezone_equivalence(
    tmp_path, when
):
    source, target, clock = formal_pair("aggregation", when)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    budget = AuthorizedBatch(target, clock=clock)
    # Historical chronology can be valid without current source eligibility.
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(target, store, started_at=budget.started_at)
    assert budget.requests == []


def test_calibration_and_aggregation_can_equal_original_start_on_resume(tmp_path):
    source, target, clock = formal_pair("calibration", START, stage="resume")
    target["aggregation_policy"]["approval"]["at"] = "2026-09-23T10:00:00+08:00"
    rehash(target["aggregation_policy"])
    authorize(target)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    budget = AuthorizedBatch(target, clock=clock)
    # Historical chronology can be valid without current source eligibility.
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(target, store, started_at=budget.started_at)
    assert budget.started_at == START
    assert budget.requests == []


def test_new_judge_budget_approval_may_equal_first_sample(tmp_path):
    source, target, clock = formal_pair(
        "aggregation",
        "2026-09-23T02:00:02Z",
        stage="new_budget_with_prior_sample",
    )
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    budget = AuthorizedBatch(target, clock=clock)
    # Historical chronology can be valid without current source eligibility.
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(target, store, started_at=budget.started_at)
    assert budget.requests == []


@pytest.mark.parametrize(
    "stage,when",
    [
        ("fresh", START),
        ("fresh", "2026-09-23T10:00:00+08:00"),
        ("resume", "2026-09-23T02:09:00Z"),
        ("resume", "2026-09-23T02:10:00Z"),
    ],
)
def test_execution_authorization_uses_current_clock_without_resetting_budget(
    tmp_path, stage, when
):
    source, target, clock = formal_pair("authorization", when, stage=stage)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    budget = AuthorizedBatch(target, clock=clock)
    # Historical chronology can be valid without current source eligibility.
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(target, store, started_at=budget.started_at)
    if stage == "resume":
        assert budget.started_at == START
        assert budget.deadline == clock.monotonic() + 600
    assert budget.requests == []


@pytest.mark.parametrize(
    "field,reason",
    [
        ("aggregation", "approval_timezone_missing"),
        ("calibration", "approval_timezone_missing"),
        ("authorization", "timezone_required"),
    ],
)
def test_approval_times_require_timezone(tmp_path, field, reason):
    source, target, clock = formal_pair(field, "2026-09-23T01:30:00")
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match=reason):
        store.import_batch(source)
        AuthorizedBatch(target, clock=clock)


def test_test_threshold_is_not_user_approval(tmp_path):
    source, target, clock = formal_pair()
    target["aggregation_policy"]["approval"]["kind"] = "test"
    rehash(target["aggregation_policy"])
    authorize(target)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)
    budget = AuthorizedBatch(target, clock=clock)
    with pytest.raises(ValueError, match="approved_aggregation_missing"):
        validate_execution_materials(target, store, started_at=budget.started_at)
    assert budget.requests == []
    assert inventory(store) == before


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_authorization_time_interpretation_is_unchanged(version):
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.deterministic.test_acceptance_trial import trial_batch

    batch = offline_batch() if version == 1 else trial_batch()
    authorize(batch)
    batch["authorization"]["at"] = FUTURE
    budget = AuthorizedBatch(batch, clock=FakeClock(wall=datetime.fromisoformat(START)))
    assert budget.requests == []


def test_legacy_formal_calibration_approval_time_interpretation_is_unchanged(tmp_path):
    from agent_alfred.evals.deterministic.test_acceptance_c4 import seal
    from agent_alfred.evals.deterministic.test_acceptance_legacy_approval import (
        legacy_online,
    )

    source = legacy_online(1, "calibration", 5, "legacy-source")
    seal(source)
    source["calibration_approval"]["at"] = FUTURE
    target = legacy_online(1, "formal", 20, "legacy-target")
    target["calibration"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "material_ids": [c["material_id"] for c in source["cases"]],
    }
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    # Historical chronology can be valid without current source eligibility.
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(target, store, started_at=START)
    assert store.read(target["batch_id"]) == target


def test_formal_approval_reference_must_satisfy_existing_schema(tmp_path):
    source, target, clock = formal_pair()
    target["calibration"].pop("batch_id")
    target["calibration"].pop("sha256")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)
    with pytest.raises(ValueError, match="proof_fields_missing"):
        AuthorizedBatch(target, clock=clock)
    assert inventory(store) == before
