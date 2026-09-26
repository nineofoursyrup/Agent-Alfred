"""A referenced calibration must satisfy its own approval chronology."""

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.budget import validate_execution_materials
from agent_alfred.evals.acceptance.judge import judge_result
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.schema import calibration_identity, digest, encode
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_approval_cutoff import formal_pair
from agent_alfred.evals.deterministic.test_acceptance_material_chronology import (
    declared_calibration_with_time,
    set_proof_time,
)
from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
    approve_calibration,
    approve_cases,
    real_materials,
    rehash,
)
from agent_alfred.evals.deterministic.test_acceptance_trial import authorize
from agent_alfred.model import ScriptedModel


def seal_old_package(store, batch):
    path = store.root / batch["batch_id"]
    path.mkdir(parents=True)
    payload = encode(batch)
    (path / "batch.json").write_bytes(payload)
    (path / "complete.json").write_bytes(
        encode({"batch.json": hashlib.sha256(payload).hexdigest()})
    )


def file_hashes(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("field", ["case", "semantic"])
def test_calibration_source_import_rejects_approval_after_own_budget(
    tmp_path, field
):
    source, _ = declared_calibration_with_time(
        field, "2026-09-23T00:00:00.500000Z"
    )
    store = EvidenceStore(tmp_path / "evidence")
    reason = field + "_approval_after_execution"

    with pytest.raises(ValueError, match=reason):
        store.import_batch(source)
    assert not store.root.exists()
    with pytest.raises(ValueError, match=reason):
        validate_execution_materials(
            source, store, started_at=source["budget_started_at"]
        )


@pytest.mark.parametrize("new_judge_start", [None, "2026-09-23T00:10:00Z"])
def test_regrade_cannot_erase_parent_execution_start(tmp_path, new_judge_start):
    source, _ = declared_calibration_with_time("case", "2026-09-23T00:00:00Z")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    child = deepcopy(source)
    child["batch_id"] = "calibration-regrade-child"
    child["parent"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "relation": "regrade",
    }
    child["budget_scope"] = child["batch_id"]
    child["request_history"] = source.get("request_history", []) + source["requests"]
    child["requests"] = []
    child["authorization"] = None
    if new_judge_start is None:
        child.pop("budget_started_at")
    else:
        child["budget_started_at"] = new_judge_start
    set_proof_time(child["case_set_approval"], "2026-09-23T00:00:00.500000Z")

    with pytest.raises(ValueError, match="case_approval_after_execution"):
        store.import_batch(child)
    assert not (store.root / child["batch_id"]).exists()


@pytest.mark.parametrize("field", ["case", "semantic"])
def test_budget_alone_constrains_calibration_draft(tmp_path, field):
    source = real_materials()
    source["budget_started_at"] = "2026-09-23T00:00:00Z"
    if field == "case":
        set_proof_time(source["case_set_approval"], "2026-09-23T00:00:00.500000Z")
    else:
        set_proof_time(
            source["semantic_rubric"]["approval"],
            "2026-09-23T00:00:00.500000Z",
        )
        rehash(source["semantic_rubric"])
        approve_cases(source)
    assert not source["results"]
    with pytest.raises(ValueError, match=field + "_approval_after_execution"):
        EvidenceStore(tmp_path / "evidence").import_batch(source)


@pytest.mark.parametrize("field", ["case", "semantic"])
@pytest.mark.parametrize("early_index", [None, 29])
def test_first_sample_constrains_source_without_budget(tmp_path, field, early_index):
    source, _ = declared_calibration_with_time(
        field, "2026-09-23T00:00:02Z", early_index=early_index
    )
    source.pop("budget_started_at")
    if early_index == 29:
        assert source["results"][0]["sampled_at"] == "2026-09-23T00:00:03Z"
        assert source["results"][-1]["sampled_at"] == "2026-09-23T00:00:01Z"
    with pytest.raises(ValueError, match=field + "_approval_after_execution"):
        EvidenceStore(tmp_path / "evidence").import_batch(source)


@pytest.mark.parametrize("field", ["case", "semantic"])
def test_unexecuted_calibration_draft_waits_for_actual_start(tmp_path, field):
    source = real_materials()
    when = "2026-09-24T00:00:00Z"
    if field == "case":
        set_proof_time(source["case_set_approval"], when)
    else:
        set_proof_time(source["semantic_rubric"]["approval"], when)
        rehash(source["semantic_rubric"])
        approve_cases(source)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    assert store.read(source["batch_id"]) == source
    with pytest.raises(ValueError, match=field + "_approval_after_execution"):
        validate_execution_materials(
            source, store, started_at="2026-09-23T00:00:00Z"
        )


@pytest.mark.parametrize("field", ["case", "semantic"])
def test_later_judge_start_keeps_legal_original_source(tmp_path, field):
    source, _ = declared_calibration_with_time(field, "2026-09-23T00:00:00Z")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    validate_execution_materials(
        source, store, started_at="2026-09-23T00:10:00Z"
    )
    assert store.read(source["batch_id"]) == source


def test_legal_regrade_keeps_original_start_for_later_judge_budget(tmp_path):
    source, _ = declared_calibration_with_time("case", "2026-09-23T00:00:00Z")
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    child = deepcopy(source)
    child["batch_id"] = "legal-calibration-regrade"
    child["parent"] = {
        "batch_id": source["batch_id"],
        "sha256": digest(source),
        "relation": "regrade",
    }
    child["budget_scope"] = child["batch_id"]
    child["request_history"] = source.get("request_history", []) + source["requests"]
    child["requests"] = []
    child["authorization"] = None
    child["budget_started_at"] = "2026-09-23T00:10:00Z"
    store.import_batch(child)
    assert store.read(child["batch_id"]) == child
    validate_execution_materials(child, store, started_at=child["budget_started_at"])

    grandchild = deepcopy(child)
    grandchild["batch_id"] = "late-grandchild"
    grandchild["parent"] = {
        "batch_id": child["batch_id"],
        "sha256": digest(child),
        "relation": "regrade",
    }
    grandchild["budget_scope"] = grandchild["batch_id"]
    grandchild["budget_started_at"] = "2026-09-23T00:20:00Z"
    set_proof_time(
        grandchild["case_set_approval"], "2026-09-23T00:00:00.500000Z"
    )
    with pytest.raises(ValueError, match="case_approval_after_execution"):
        store.import_batch(grandchild)


@pytest.mark.parametrize("field", ["case", "semantic"])
def test_formal_cannot_reference_old_source_with_late_approval(tmp_path, field):
    source, formal, _ = formal_pair(
        "calibration", "2026-09-23T01:10:00Z", stage="resume"
    )
    late = "2026-09-23T00:00:00.500000Z"
    if field == "case":
        set_proof_time(source["case_set_approval"], late)
    else:
        for batch in (source, formal):
            set_proof_time(batch["semantic_rubric"]["approval"], late)
            rehash(batch["semantic_rubric"])
            approve_cases(batch)
        cases = {case["id"]: case for case in source["cases"]}
        for index, grade in enumerate(source["grades"]):
            result = source["results"][index]
            updated = judge_result(
                source,
                cases[result["case_id"]],
                result,
                ScriptedModel([grade["raw"]]),
            )
            assert updated["status"] == "scored"
            updated["attempt_ids"] = grade["attempt_ids"]
            source["grades"][index] = updated
    authorize(source)
    source["authorization"]["max_requests"] = 1000
    approve_calibration(source)
    formal["calibration"]["sha256"] = digest(source)
    formal["aggregation_policy"].update(
        semantic_rubric_id=source["semantic_rubric"]["id"],
        calibration_evidence_sha256=calibration_identity(source),
    )
    rehash(formal["aggregation_policy"])
    authorize(formal)
    store = EvidenceStore(tmp_path / "evidence")
    seal_old_package(store, source)
    before = file_hashes(store.root)
    reason = field + "_approval_after_execution"
    with pytest.raises(ValueError, match=reason):
        store.import_batch(formal)
    assert file_hashes(store.root) == before
    seal_old_package(store, formal)
    before = file_hashes(store.root)
    with pytest.raises(ValueError, match=reason):
        store.read(formal["batch_id"])
    with pytest.raises(ValueError, match=reason):
        report(formal, now=datetime(2026, 9, 23, 3, tzinfo=UTC), store=store)
    with pytest.raises(ValueError, match=reason):
        validate_execution_materials(
            formal, store, started_at=formal["budget_started_at"]
        )
    assert file_hashes(store.root) == before

    command = [
        sys.executable,
        "-B",
        "-m",
        "agent_alfred.evals.acceptance",
        "verify",
        "--store",
        str(store.root),
        "--batch",
        formal["batch_id"],
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["v1_release"]["blockers"] == [reason]
    assert file_hashes(store.root) == before
