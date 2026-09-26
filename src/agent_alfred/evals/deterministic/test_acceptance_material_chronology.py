"""Approval chronology uses the original execution budget and first sample."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.acceptance.budget import (
    AuthorizedBatch,
    validate_execution_materials,
)
from agent_alfred.evals.acceptance.schema import encode
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
    approve_cases,
    real_materials,
    rehash,
)
from agent_alfred.evals.deterministic.test_acceptance_trial import authorize
from agent_alfred.model import ScriptedModel

START = "2026-09-23T00:00:00Z"


def set_proof_time(proof, when):
    stamp = datetime.fromisoformat(when)
    proof["actor"].update(
        started_at=(stamp - timedelta(seconds=1)).isoformat(), completed_at=when
    )
    proof["at"] = when


def materials_with_time(field, when, *, stage="fresh"):
    """Synthetic metadata usable at real-root preflight; never online samples."""
    batch = real_materials()
    proof = (
        batch["semantic_rubric"]["approval"]
        if field == "semantic"
        else batch["case_set_approval"]
    )
    set_proof_time(proof, when)
    if field == "semantic":
        rehash(batch["semantic_rubric"])
        approve_cases(batch)
    now = datetime.fromisoformat(START)
    if stage != "fresh":
        now += timedelta(minutes=10)
        if stage == "resume":
            batch["budget_started_at"] = START
        case = batch["cases"][0]
        batch["results"] = [
            {
                "id": "synthetic-prior-result",
                "batch_id": batch["batch_id"],
                "case_id": case["id"],
                "profile_id": batch["profiles"][0]["id"],
                "source": "online",
                "sampled_at": "2026-09-23T00:00:02Z",
                "finished_at": "2026-09-23T00:00:03Z",
                "run_id": "synthetic-run",
                "outcome": "completed",
                "output": "synthetic metadata fixture",
                "recorded": True,
                "evidence": {},
            }
        ]
    authorize(batch)
    return batch, FakeClock(wall=now)


@pytest.mark.parametrize("field", ["case", "semantic"])
@pytest.mark.parametrize(
    "stage,when",
    [
        ("fresh", "2026-09-24T00:01:00Z"),
        ("resume", "2026-09-23T00:05:00Z"),
        ("new_budget_with_prior_sample", "2026-09-23T00:05:00Z"),
    ],
)
def test_material_preflight_stops_before_factory_for_approval_after_cutoff(
    tmp_path,
    field,
    stage,
    when,
):
    batch, clock = materials_with_time(field, when, stage=stage)
    budget = AuthorizedBatch(batch, clock=clock)
    constructed = []

    def builder():
        constructed.append(True)
        return ScriptedModel([])

    with pytest.raises(ValueError, match=field + "_approval_after_execution"):
        validate_execution_materials(
            batch,
            EvidenceStore(tmp_path / "evidence"),
            started_at=budget.started_at,
        )
        budget.client(builder, role="judge" if stage != "fresh" else "product")
    assert constructed == []
    assert budget.requests == []


@pytest.mark.parametrize(
    "when",
    [
        "2026-09-23T00:00:00Z",
        "2026-09-23T08:00:00+08:00",
        "2026-09-22T19:00:00-05:00",
        "2026-09-22T23:59:59.999999Z",
    ],
)
def test_material_preflight_accepts_approvals_at_or_before_budget_start(tmp_path, when):
    batch, clock = materials_with_time("semantic", when)
    set_proof_time(batch["case_set_approval"], when)
    authorize(batch)
    budget = AuthorizedBatch(batch, clock=clock)
    validate_execution_materials(
        batch,
        EvidenceStore(tmp_path / "evidence"),
        started_at=budget.started_at,
    )
    assert budget.started_at == datetime(2026, 9, 23, tzinfo=UTC).isoformat()
    assert budget.requests == []


@pytest.mark.parametrize(
    "started_at,reason",
    [
        (None, "execution_start_required"),
        ("2026-09-23T00:00:00", "timezone_required"),
    ],
)
def test_schema3_material_preflight_requires_explicit_zoned_start(
    tmp_path,
    started_at,
    reason,
):
    batch = real_materials()
    with pytest.raises(ValueError, match=reason):
        validate_execution_materials(
            batch,
            EvidenceStore(tmp_path / "evidence"),
            started_at=started_at,
        )


def declared_calibration_with_time(field, when, *, early_index=None):
    """Adversarial online metadata only: ScriptedModel scores synthetic records."""
    from copy import deepcopy

    from agent_alfred.evals.acceptance.judge import judge_result
    from agent_alfred.evals.acceptance.schema import digest, judge_model
    from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
        approve_calibration,
        material_approval,
    )
    from agent_alfred.evals.deterministic.test_acceptance_schema3 import scored_v3

    source = scored_v3("calibration", 5, "chronology-source")
    source["simulation"] = False
    source["semantic_rubric"]["approval"] = material_approval(source)
    if field == "semantic":
        set_proof_time(source["semantic_rubric"]["approval"], when)
    rehash(source["semantic_rubric"])
    source["authorization"] = {"max_output_tokens": 256}
    source["requests"] = []
    for index, (case, result, grade) in enumerate(
        zip(
            source["cases"],
            source["results"],
            source["grades"],
            strict=True,
        )
    ):
        case.pop("script")
        result["source"] = "online"
        if early_index is not None and index != early_index:
            result.update(
                sampled_at="2026-09-23T00:00:03Z", finished_at="2026-09-23T00:00:04Z"
            )
        result["evidence"]["attempts"] = [{"attempt_id": "p-" + result["id"]}]
        replacement = judge_result(source, case, result, ScriptedModel([grade["raw"]]))
        assert replacement["status"] == "scored"
        replacement["attempt_ids"] = ["j-" + result["id"]]
        source["grades"][index] = replacement
        profile = source["profiles"][0]
        for role, model, prefix, stamp in (
            ("product", profile["product_models"][0], "p-", result["sampled_at"]),
            ("judge", judge_model(source, profile), "j-", result["finished_at"]),
        ):
            source["requests"].append(
                {
                    "attempt_id": prefix + result["id"],
                    "role": role,
                    "model": {k: model[k] for k in ("endpoint_id", "model_id")},
                    "started_at": stamp,
                    "usage": None,
                    "outcome": "success",
                }
            )
    approve_cases(source)
    if field == "case":
        set_proof_time(source["case_set_approval"], when)
    authorize(source)
    source["authorization"]["max_requests"] = 1000
    source["budget_started_at"] = START
    approve_calibration(source)
    target = real_materials()
    target["batch_id"] = "chronology-target"
    target["semantic_rubric"] = deepcopy(source["semantic_rubric"])
    target["seen_families"] += sorted({c["source_family_id"] for c in source["cases"]})
    approve_cases(target)
    target["calibration"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "material_ids": [c["material_id"] for c in source["cases"]],
    }
    return source, target


@pytest.mark.parametrize(
    "field,reason",
    [
        ("case", "case_approval_after_execution"),
        ("semantic", "semantic_approval_after_execution"),
    ],
)
@pytest.mark.parametrize("early_index", [None, 29])
def test_store_rejects_calibration_approved_after_any_sample(
    tmp_path,
    field,
    reason,
    early_index,
):
    source, target = declared_calibration_with_time(
        field,
        "2026-09-23T00:00:02Z",
        early_index=early_index,
    )
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match=reason):
        store.import_batch(source)
    assert not store.root.exists()

    # A complete older package must be rejected again at the read boundary.
    package = store.root / source["batch_id"]
    package.mkdir(parents=True)
    payload = encode(source)
    (package / "batch.json").write_bytes(payload)
    (package / "complete.json").write_bytes(
        encode({"batch.json": hashlib.sha256(payload).hexdigest()})
    )
    before = {
        p.relative_to(store.root): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }
    with pytest.raises(ValueError, match=reason):
        store.import_batch(target)
    after = {
        p.relative_to(store.root): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }
    assert after == before
    assert not (store.root / target["batch_id"]).exists()
    with pytest.raises(ValueError, match=reason):
        store.read(source["batch_id"])


@pytest.mark.parametrize("field", ["case", "semantic"])
@pytest.mark.parametrize(
    "when",
    [
        "2026-09-23T00:00:01Z",
        "2026-09-23T08:00:01+08:00",
        "2026-09-22T19:00:01-05:00",
        "2026-09-23T00:00:00.999999Z",
    ],
)
def test_store_rejects_calibration_approved_after_budget_before_sample(
    tmp_path, field, when
):
    source, _ = declared_calibration_with_time(field, when)
    store = EvidenceStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match=field + "_approval_after_execution"):
        store.import_batch(source)
    assert not store.root.exists()


@pytest.mark.parametrize("field", ["case", "semantic"])
@pytest.mark.parametrize(
    "when",
    [
        "2026-09-23T00:00:00Z",
        "2026-09-23T08:00:00+08:00",
        "2026-09-22T19:00:00-05:00",
        "2026-09-22T23:59:59.999999Z",
    ],
)
def test_store_accepts_calibration_approved_by_budget_start(tmp_path, field, when):
    source, target = declared_calibration_with_time(field, when)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    assert store.read(source["batch_id"]) == source
    assert store.read(target["batch_id"]) == target


@pytest.mark.parametrize("field", ["case", "semantic"])
def test_material_preflight_rejects_unzoned_approval_before_construction(
    tmp_path, field
):
    batch, clock = materials_with_time(field, "2026-09-22T23:59:59")
    with pytest.raises(ValueError, match="invalid_agent_time"):
        validate_execution_materials(
            batch,
            EvidenceStore(tmp_path / "evidence"),
            started_at=clock.wall_utc().isoformat(),
        )


def test_material_preflight_rejects_unzoned_prior_sampling(tmp_path):
    batch, clock = materials_with_time("case", "2026-09-22T23:59:59Z", stage="resume")
    batch["results"][0]["sampled_at"] = "2026-09-23T00:00:02"
    budget = AuthorizedBatch(batch, clock=clock)
    with pytest.raises(ValueError, match="timezone_required"):
        validate_execution_materials(
            batch,
            EvidenceStore(tmp_path / "evidence"),
            started_at=budget.started_at,
        )


@pytest.mark.parametrize("stage", ["resume", "new_budget_with_prior_sample"])
def test_material_preflight_accepts_equal_original_start_on_resume(tmp_path, stage):
    batch, clock = materials_with_time("semantic", START, stage=stage)
    set_proof_time(batch["case_set_approval"], START)
    authorize(batch)
    budget = AuthorizedBatch(batch, clock=clock)
    validate_execution_materials(
        batch,
        EvidenceStore(tmp_path / "evidence"),
        started_at=budget.started_at,
    )
    assert budget.requests == []


def test_simulation_material_preflight_does_not_require_execution_start(tmp_path):
    from agent_alfred.evals.acceptance.examples_v3 import controlled_batch

    validate_execution_materials(
        controlled_batch(), EvidenceStore(tmp_path / "evidence")
    )


@pytest.mark.parametrize("when", ["2026-09-23T00:00:02Z", "2026-09-23T08:00:02+08:00"])
def test_new_judge_budget_accepts_approval_equal_to_first_sample(tmp_path, when):
    batch, clock = materials_with_time(
        "semantic",
        when,
        stage="new_budget_with_prior_sample",
    )
    set_proof_time(batch["case_set_approval"], when)
    authorize(batch)
    budget = AuthorizedBatch(batch, clock=clock)
    validate_execution_materials(
        batch,
        EvidenceStore(tmp_path / "evidence"),
        started_at=budget.started_at,
    )
    assert budget.requests == []
