"""Public-path regressions for the second independent review."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.schema import calibration_identity, digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance import scored_fixture
from agent_alfred.evals.deterministic.test_acceptance_review import (
    sized_fixture,
    specialist_fixture,
)

NOW = datetime(2026, 9, 20, 1, tzinfo=UTC)


def ruling_for(batch, grade, status="fail"):
    ruling = {
        "id": "human",
        "grade_id": grade["id"],
        "human": "fixture reviewer",
        "at": "2026-09-20T00:00:02Z",
        "reason": "explicit fixture ruling",
        "rubric_id": batch["rubric"]["id"],
        "dimensions": deepcopy(grade["dimensions"]),
        "prohibitions": deepcopy(grade["prohibitions"]),
    }
    ruling["dimensions"]["correctness"]["status"] = status
    return ruling


@pytest.mark.parametrize("status,expected", [("fail", "FAIL"), ("pass", "PASS")])
def test_error_grade_can_be_explicitly_adjudicated_without_erasing_original(
    tmp_path, status, expected
):
    batch = scored_fixture()
    grade = batch["grades"][0]
    ruling = ruling_for(batch, grade, status)
    grade.update(
        status="error",
        dimensions={},
        prohibitions={},
        raw="invalid JSON",
        error="judge_error",
    )
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    assert (
        store.publish_report(batch["batch_id"], now=NOW)["quality"]["verdict"]
        == "BLOCKED"
    )
    revised = store.revise(batch["batch_id"], "human-ruling", adjudications=[ruling])
    assert (
        store.publish_report("human-ruling", now=NOW)["quality"]["verdict"] == expected
    )
    assert (
        store.read(batch["batch_id"]) == batch and revised["grades"] == batch["grades"]
    )


def test_calibration_dispute_requires_case_ruling_before_formal_reuse(tmp_path):
    store = EvidenceStore(tmp_path)
    calibration = sized_fixture("calibration", 5, "calibration")
    grade = calibration["grades"][0]
    grade.update(disputed=True, suspected_safety=True)
    proof = {
        "kind": "test",
        "by": "fixture",
        "at": "2026-09-20T00:00:03Z",
        "reference": "fixture only",
    }
    calibration["calibration_approval"] = {
        **proof,
        "evidence_sha256": calibration_identity(calibration),
    }
    store.import_batch(calibration)
    formal = sized_fixture("formal", 20, "formal")
    formal["calibration"] = {
        "batch_id": "calibration",
        "sha256": digest(calibration),
        "material_ids": [c["material_id"] for c in calibration["cases"]],
    }
    with pytest.raises(ValueError, match="calibration_adjudication_required"):
        store.import_batch(formal)
    adjudicated = store.revise(
        "calibration",
        "calibration-adjudicated",
        adjudications=[ruling_for(calibration, grade)],
    )
    approved = deepcopy(adjudicated)
    approved["batch_id"] = "approved-calibration"
    approved["parent"] = {
        "batch_id": "calibration-adjudicated",
        "sha256": digest(adjudicated),
        "relation": "regrade",
    }
    approved["calibration_approval"] = {
        **proof,
        "evidence_sha256": calibration_identity(approved),
    }
    store.import_batch(approved)
    formal["calibration"].update(batch_id=approved["batch_id"], sha256=digest(approved))
    store.import_batch(formal)
    assert store.read("formal")["calibration"]["batch_id"] == approved["batch_id"]


def test_actual_loaded_package_must_match_declared_candidate_before_clients(tmp_path):
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.candidate import capture, git
    from agent_alfred.evals.acceptance.collect import collect_gate
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.runner import execute

    root = Path(__file__).resolve().parents[4]
    clone = tmp_path / "baseline"
    git(root, "clone", "--quiet", "--local", "--no-hardlinks", str(root), str(clone))
    package_file = clone / "src/agent_alfred/evals/acceptance/judge.py"
    package_file.write_text(
        package_file.read_text() + "\n# Candidate-only byte drift.\n"
    )
    batch = scored_fixture()
    batch["grades"] = []
    batch["candidate"] = capture(clone)
    batch["candidate_id"] = digest(batch["candidate"])
    store = EvidenceStore(tmp_path / "store")
    created = []

    def forbidden_factory():
        created.append(True)
        raise AssertionError("incorrect runtime must not construct clients")

    auth = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 2,
        "max_output_tokens": 256,
        "total_seconds": 30,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    import httpx2 as httpx

    from agent_alfred.evals.deterministic._simulation_test_helpers import session_for

    batch["authorization"] = auth
    session = session_for(batch, store)
    transport = httpx.MockTransport(lambda request: forbidden_factory())
    with pytest.raises(ValueError, match="runtime_candidate_mismatch"):
        grade_batch(
            batch,
            auth,
            simulation_session=session,
            mock_transport=transport,
            candidate_root=clone,
            store=store,
            new_batch="judged",
        )
    batch.update(results=[], grades=[], authorization=auth)
    with pytest.raises(ValueError, match="runtime_candidate_mismatch"):
        execute(
            batch,
            tmp_path / "runtime",
            simulation_session=session,
            mock_transport=transport,
            candidate_root=clone,
            store=store,
        )
    with pytest.raises(ValueError, match="runtime_candidate_mismatch"):
        collect_gate("ruff", batch["candidate"], clone, tmp_path / "checks")
    assert created == [] and not (tmp_path / "runtime").exists()


def test_specialist_failed_or_unrecorded_result_cannot_become_pass(tmp_path):
    batch = scored_fixture()
    batch["specialist"] = specialist_fixture(batch)
    row = batch["specialist"]["rows"][0]
    row.update(outcome="failed", recorded=False)
    row["evidence"].update(outcome="failed", recorded=False)
    batch["specialist"]["id"] = digest(
        {k: v for k, v in batch["specialist"].items() if k != "id"}
    )
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    summary = store.publish_report(batch["batch_id"], now=NOW)
    assert summary["consolidation"]["verdict"] == "FAIL"
    assert "consolidation_product_failed:s0" in summary["v1_release"]["failures"]
    assert "consolidation_execution_missing:s0" in summary["v1_release"]["blockers"]


def test_raw_failed_pytest_phase_overrides_successful_summary(tmp_path):
    batch = scored_fixture()
    gate = {
        "name": "pytest",
        "platform": "Ubuntu",
        "python": "3.14.7",
        "candidate_id": batch["candidate_id"],
        "environment": {"sqlite_shared_api": True},
        "command": [
            "python",
            "-m",
            "pytest",
            "-p",
            "agent_alfred.evals.acceptance.pytest_evidence",
        ],
        "started_at": "2026-09-20T00:00:00Z",
        "finished_at": "2026-09-20T00:00:01Z",
        "exit_code": 0,
        "log": "synthetic gate contradiction",
        "expected_ids": ["case"],
        "collection": ["case"],
        "outcomes": {"case": ["passed", "passed", "passed"]},
        "requires_key_exclusion": "pyproject.toml: not requires_key",
        "plan": {
            "collection": ["case"],
            "requires_key_ids": [],
            "deselected": [],
            "exit_code": 0,
        },
    }
    gate["log_sha256"] = hashlib.sha256(gate["log"].encode()).hexdigest()
    gate["raw_results"] = {
        "collection": ["case"],
        "outcomes": gate["outcomes"],
        "deselected": [],
        "exit_code": 0,
        "phases": {"case": {"setup": "passed", "call": "failed", "teardown": "passed"}},
    }
    batch["gates"] = [gate]
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    summary = store.publish_report(batch["batch_id"], now=NOW)
    assert summary["offline_engineering"]["verdict"] == "FAIL"
    assert any(
        "test_failed:case" in x for x in summary["offline_engineering"]["failures"]
    )
    assert any(
        "phase_result_mismatch" in x for x in summary["offline_engineering"]["blockers"]
    )


@pytest.mark.parametrize("mutation", ["raw", "result", "pointer"])
def test_grade_raw_and_evidence_references_must_match_recorded_result(
    tmp_path, mutation
):
    batch = scored_fixture()
    grade = batch["grades"][0]
    if mutation == "raw":
        raw = {
            k: deepcopy(grade[k])
            for k in ("dimensions", "prohibitions", "disputed", "suspected_safety")
        }
        raw["dimensions"]["correctness"]["status"] = "fail"
        grade["raw"] = json.dumps(raw)
    else:
        grade["dimensions"]["correctness"]["evidence"] = (
            "missing-result#/output"
            if mutation == "result"
            else grade["result_id"] + "#/missing-field"
        )
    store = EvidenceStore(tmp_path)
    with pytest.raises(ValueError, match="grade_raw_mismatch|broken_score_evidence"):
        store.import_batch(batch)
    assert not (tmp_path / batch["batch_id"]).exists()


@pytest.mark.parametrize("defect", ["error", "unknown", "missing_dimension"])
def test_calibration_requires_complete_effective_scores(tmp_path, defect):
    store = EvidenceStore(tmp_path)
    source = sized_fixture("calibration", 5, "calibration")
    grade = source["grades"][0]
    ruling = ruling_for(source, grade)
    if defect == "error":
        grade.update(status="error", dimensions={}, prohibitions={})
        source["adjudications"] = [ruling]
    elif defect == "unknown":
        grade["dimensions"]["correctness"]["status"] = "unknown"
    else:
        grade["dimensions"].pop("correctness")
    source["calibration_approval"] = {
        "kind": "test",
        "by": "fixture",
        "at": "2026-09-20T00:00:03Z",
        "reference": "fixture only",
        "evidence_sha256": calibration_identity(source),
    }
    store.import_batch(source)
    formal = sized_fixture("formal", 20, "formal")
    formal["calibration"] = {
        "batch_id": "calibration",
        "sha256": digest(source),
        "material_ids": [c["material_id"] for c in source["cases"]],
    }
    if defect == "error":
        store.import_batch(formal)
        assert store.read("formal")["calibration"]["batch_id"] == "calibration"
    else:
        with pytest.raises(ValueError, match="calibration_incomplete"):
            store.import_batch(formal)


def test_result_summary_cannot_overrule_failed_or_missing_recording(tmp_path):
    batch = scored_fixture()
    result = batch["results"][0]
    result["evidence"].update(
        outcome="failed", recorded=False, recording_state="pending"
    )
    batch["grades"][0]["result_hash"] = digest(result)
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    summary = store.publish_report(batch["batch_id"], now=NOW)
    assert summary["quality"]["verdict"] == "FAIL"
    assert (
        "execution_evidence_missing:" + result["case_id"]
        in summary["quality"]["blockers"]
    )


def test_formal_freshness_does_not_refresh_referenced_calibration(tmp_path):
    from datetime import timedelta

    store = EvidenceStore(tmp_path)
    source = sized_fixture("calibration", 5, "calibration")
    source["calibration_approval"] = {
        "kind": "test",
        "by": "fixture",
        "at": "2026-09-20T00:00:03Z",
        "reference": "fixture only",
        "evidence_sha256": calibration_identity(source),
    }
    store.import_batch(source)
    formal = sized_fixture("formal", 20, "formal")
    for result, grade in zip(formal["results"], formal["grades"]):
        result.update(
            sampled_at="2026-09-27T00:00:00Z", finished_at="2026-09-27T00:00:01Z"
        )
        grade["result_hash"] = digest(result)
    formal["calibration"] = {
        "batch_id": "calibration",
        "sha256": digest(source),
        "material_ids": [c["material_id"] for c in source["cases"]],
    }
    store.import_batch(formal)
    fresh = store.publish_report("formal", now=NOW.replace(hour=0) + timedelta(days=7))
    assert not any(
        "calibration_sample_invalid" in x for x in fresh["v1_release"]["blockers"]
    )
    stale = store.publish_report(
        "formal", now=NOW.replace(hour=0) + timedelta(days=7, microseconds=1)
    )
    assert any(
        "calibration_sample_invalid" in x for x in stale["v1_release"]["blockers"]
    )
