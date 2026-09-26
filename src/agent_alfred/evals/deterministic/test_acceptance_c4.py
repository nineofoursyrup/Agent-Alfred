"""Adversarial metadata fixtures; no real evaluation or authority is claimed."""

import json
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.budget import binding
from agent_alfred.evals.acceptance.schema import calibration_identity, digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance import scored_fixture
from agent_alfred.evals.deterministic.test_acceptance_c3 import ruling_for
from agent_alfred.evals.deterministic.test_acceptance_review import (
    sized_fixture,
    specialist_fixture,
)


def declared_online_fixture(phase, size, identity):
    # Deliberately test claimed online metadata admission, never run a model.
    batch = sized_fixture(phase, size, identity)
    batch["simulation"] = False
    batch["rubric"]["approval"]["kind"] = "approved"
    batch["rubric"]["id"] = digest(
        {k: v for k, v in batch["rubric"].items() if k != "id"}
    )
    batch["requests"] = []
    for case, result, grade in zip(batch["cases"], batch["results"], batch["grades"]):
        case["source"]["kind"] = "approved"
        result["source"] = "online"
        result["evidence"]["attempts"] = [{"attempt_id": "p-" + result["id"]}]
        grade.update(
            result_hash=digest(result),
            case_hash=digest(case),
            case_material_id=case["material_id"],
            rubric_id=batch["rubric"]["id"],
            judge_id=digest(batch["profiles"][0]["judge_model"]),
            attempt_ids=["j-" + result["id"]],
        )
        grade["raw"] = json.dumps(
            {
                k: grade[k]
                for k in ("dimensions", "prohibitions", "disputed", "suspected_safety")
            }
        )
        for role, model, prefix in [
            ("product", batch["profiles"][0]["product_models"][0], "p-"),
            ("judge", batch["profiles"][0]["judge_model"], "j-"),
        ]:
            batch["requests"].append(
                {
                    "attempt_id": prefix + result["id"],
                    "role": role,
                    "model": {k: model[k] for k in ("endpoint_id", "model_id")},
                    "started_at": "2026-09-20T00:00:00Z"
                    if role == "product"
                    else "2026-09-20T00:00:02Z",
                    "usage": None,
                    "outcome": "success",
                }
            )
    batch["case_set_approval"] = {
        "kind": "approved",
        "by": "ADVERSARIAL FIXTURE ONLY",
        "at": "2026-09-19T00:00:00Z",
        "reference": "not real authority",
        "cases_sha256": digest(batch["cases"]),
    }
    batch["authorization"] = {
        "binding": binding(batch),
        "by": "ADVERSARIAL FIXTURE ONLY",
        "at": "2026-09-19T00:00:00Z",
        "max_requests": 1000,
        "max_output_tokens": 256,
        "total_seconds": 60,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    batch["budget_started_at"] = "2026-09-20T00:00:00Z"
    return batch


def seal(source):
    source["calibration_approval"] = {
        "kind": "approved",
        "by": "ADVERSARIAL FIXTURE ONLY",
        "at": "2026-09-20T00:00:03Z",
        "reference": "not real authority",
        "evidence_sha256": calibration_identity(source),
    }


def import_pair(store, source):
    seal(source)
    store.import_batch(source)
    formal = declared_online_fixture("formal", 20, "formal")
    formal["calibration"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "material_ids": [c["material_id"] for c in source["cases"]],
    }
    store.import_batch(formal)
    return formal


@pytest.mark.parametrize(
    "defect",
    [
        "sample_source",
        "case_source",
        "case_approval",
        "recording",
        "run_id",
        "judge_id",
        "raw",
        "authorization",
        "request_model",
        "request_ref",
    ],
)
def test_calibration_outer_label_cannot_override_internal_evidence(tmp_path, defect):
    source = declared_online_fixture("calibration", 5, "calibration")
    result, grade = source["results"][0], source["grades"][0]
    if defect == "sample_source":
        result["source"] = "simulation"
    elif defect == "case_source":
        source["cases"][0]["source"]["kind"] = "synthetic"
        grade["case_hash"] = digest(source["cases"][0])
    elif defect == "case_approval":
        source.pop("case_set_approval")
    elif defect == "recording":
        result["evidence"]["recording_state"] = "pending"
    elif defect == "run_id":
        result["evidence"]["run_id"] = "different-run"
    elif defect == "judge_id":
        grade["judge_id"] = "different-judge"
    elif defect == "raw":
        grade.pop("raw")
    elif defect == "authorization":
        source["authorization"] = None
    elif defect == "request_model":
        source["requests"][0]["model"]["model_id"] = "different-model"
    else:
        result["evidence"]["attempts"][0]["attempt_id"] = "missing-request"
    grade["result_hash"] = digest(result)
    with pytest.raises(ValueError, match="calibration_"):
        import_pair(EvidenceStore(tmp_path), source)


def test_calibration_keeps_product_failure_and_human_resolved_judge_error(
    tmp_path,
):
    source = declared_online_fixture("calibration", 5, "calibration")
    result, grade = source["results"][0], source["grades"][0]
    result.update(outcome="failed", output=None, recorded=False)
    grade["dimensions"]["completion"]["status"] = "fail"
    grade["result_hash"] = digest(result)
    grade["raw"] = json.dumps(
        {
            k: grade[k]
            for k in ("dimensions", "prohibitions", "disputed", "suspected_safety")
        }
    )
    error_grade = source["grades"][1]
    ruling = ruling_for(source, error_grade, "pass")
    source["adjudications"] = [ruling]
    source["requests"] = [
        r
        for r in source["requests"]
        if r["attempt_id"] not in error_grade["attempt_ids"]
    ]
    error_grade.update(
        status="error",
        raw=None,
        error="judge_error",
        attempt_ids=[],
        dimensions={},
        prohibitions={},
    )
    store = EvidenceStore(tmp_path)
    formal = import_pair(store, source)
    assert store.read(formal["batch_id"])["calibration"]["batch_id"] == "calibration"
    assert store.read("calibration")["results"][0]["outcome"] == "failed"


def test_specialist_empty_output_is_completion_failure_even_with_high_aggregate(
    tmp_path,
):
    batch = scored_fixture()
    batch["specialist"] = specialist_fixture(batch)
    row = batch["specialist"]["rows"][0]
    row.update(
        output="", correct_atoms=0, output_atoms=0, covered_items=0, expected_items=1
    )
    batch["specialist"]["id"] = digest(
        {k: v for k, v in batch["specialist"].items() if k != "id"}
    )
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    report = store.publish_report(
        batch["batch_id"], now=datetime(2026, 9, 20, 1, tzinfo=UTC)
    )
    assert report["consolidation"]["verdict"] == "FAIL"
    assert "consolidation_product_failed:s0" in report["v1_release"]["failures"]
    assert report["v1_release"]["blockers"]


def test_judge_preserves_actual_attempt_links_for_calibration(tmp_path):
    from agent_alfred.evals.acceptance.judge import judge_result

    batch = scored_fixture()
    batch["authorization"] = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-19T00:00:00Z",
        "max_requests": 1,
        "max_output_tokens": 256,
        "total_seconds": 60,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    source = batch["grades"][0]
    raw = json.dumps(
        {
            k: source[k]
            for k in ("dimensions", "prohibitions", "disputed", "suspected_safety")
        }
    )
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        budget_client,
        text_transport,
    )

    seen = []
    budget, client = budget_client(
        batch, tmp_path, text_transport([raw], seen), role="judge"
    )
    grade = judge_result(
        batch,
        batch["cases"][0],
        batch["results"][0],
        client,
    )
    assert grade["status"] == "scored" and grade["raw"] == raw
    assert grade["attempt_ids"] == [r["attempt_id"] for r in budget.requests]
    assert len(grade["attempt_ids"]) == len(seen) == 1
    budget.close()


def test_specialist_unknown_execution_is_missing_evidence_not_proven_failure(tmp_path):
    batch = scored_fixture()
    batch["specialist"] = specialist_fixture(batch)
    row = batch["specialist"]["rows"][0]
    row.pop("outcome")
    row.update(
        output=None, correct_atoms=0, output_atoms=0, covered_items=0, expected_items=1
    )
    batch["specialist"]["id"] = digest(
        {k: v for k, v in batch["specialist"].items() if k != "id"}
    )
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    result = store.publish_report(
        batch["batch_id"], now=datetime(2026, 9, 20, 1, tzinfo=UTC)
    )
    assert result["consolidation"]["verdict"] == "BLOCKED"
    assert not result["consolidation"]["failures"]
