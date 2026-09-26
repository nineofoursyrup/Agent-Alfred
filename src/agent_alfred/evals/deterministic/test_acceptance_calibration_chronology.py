"""Schema3 formal evidence cannot cite calibration approved after execution began."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.budget import validate_execution_materials
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_approval_cutoff import (
    START,
    formal_pair,
    inventory,
)
from agent_alfred.evals.deterministic.test_acceptance_material_chronology import (
    set_proof_time,
)
from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import rehash
from agent_alfred.evals.deterministic.test_acceptance_trial import authorize


def formal_evidence(calibration_at, aggregation_at, *, stage="resume"):
    source, target, _ = formal_pair("calibration", calibration_at, stage=stage)
    target["aggregation_policy"]["approval"]["at"] = aggregation_at
    rehash(target["aggregation_policy"])
    authorize(target)
    return source, target


@pytest.mark.parametrize(
    "stage,calibration_at,aggregation_at,reason",
    [
        (
            "resume",
            "2026-09-23T02:00:01Z",  # Later than shared start, before first sample.
            "2026-09-23T01:30:00Z",
            "calibration_approval_after_execution",
        ),
        (
            "resume",
            "2026-09-23T02:05:00Z",  # Later than first sample.
            "2026-09-23T01:30:00Z",
            "calibration_approval_after_execution",
        ),
        (
            "resume",
            "2026-09-24T00:00:00Z",  # Future relative to execution.
            "2026-09-23T01:30:00Z",
            "calibration_approval_after_execution",
        ),
        (
            "new_budget_with_prior_sample",
            "2026-09-23T02:05:00Z",  # Later than earliest sample without old budget.
            "2026-09-23T01:30:00Z",
            "calibration_approval_after_execution",
        ),
        (
            "resume",
            "2026-09-23T01:10:00Z",
            "2026-09-23T01:00:00Z",  # Aggregate was approved before calibration.
            "aggregation_approval_before_calibration",
        ),
    ],
)
def test_public_import_report_and_execution_reject_contradictory_approval(
    tmp_path, stage, calibration_at, aggregation_at, reason
):
    source, target = formal_evidence(calibration_at, aggregation_at, stage=stage)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)

    with pytest.raises(ValueError, match=reason):
        store.import_batch(target)
    assert inventory(store) == before
    assert not (store.root / target["batch_id"]).exists()

    with pytest.raises(ValueError, match=reason):
        report(target, now=datetime(2026, 9, 23, 3, tzinfo=UTC), store=store)
    start = START if stage == "resume" else "2026-09-23T02:10:00Z"
    with pytest.raises(ValueError, match=reason):
        validate_execution_materials(target, store, started_at=start)
    assert inventory(store) == before


@pytest.mark.parametrize(
    "stage,calibration_at,aggregation_at",
    [
        ("resume", START, START),
        ("resume", "2026-09-23T10:00:00+08:00", START),
        (
            "new_budget_with_prior_sample",
            "2026-09-23T02:00:02Z",
            "2026-09-23T10:00:02+08:00",
        ),
    ],
)
def test_public_import_and_read_accept_equal_instant_and_legal_continuation(
    tmp_path, stage, calibration_at, aggregation_at
):
    source, target = formal_evidence(calibration_at, aggregation_at, stage=stage)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    assert store.read(target["batch_id"]) == target
    start = START if stage == "resume" else "2026-09-23T02:10:00Z"
    # Historical chronology can be valid without current source eligibility.
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(target, store, started_at=start)


def test_unexecuted_formal_draft_can_reference_future_calibration(tmp_path):
    source, target = formal_evidence(
        "2026-09-24T00:00:00Z", "2026-09-24T00:01:00Z", stage="fresh"
    )
    assert not target["results"] and "budget_started_at" not in target
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    assert store.read(target["batch_id"]) == target


def test_public_verify_rebuilds_report_for_legal_continuation(tmp_path):
    source, target = formal_evidence(START, START)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    command = [
        sys.executable,
        "-B",
        "-m",
        "agent_alfred.evals.acceptance",
        "verify",
        "--store",
        str(store.root),
        "--batch",
        target["batch_id"],
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 2, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["v1_release"]["verdict"] == "BLOCKED"
    assert not any(
        "approval_after" in item for item in summary["v1_release"]["blockers"]
    )
    assert (store.root / "latest-report.json").exists()


@pytest.mark.parametrize(
    "field,when,stage,reason",
    [
        ("case", "2026-09-23T02:00:01Z", "resume", "case_approval_after_execution"),
        (
            "aggregation",
            "2026-09-23T02:00:01Z",
            "resume",
            "aggregation_approval_after_execution",
        ),
        ("case", "2026-09-24T00:00:00Z", "resume", "case_approval_after_execution"),
        (
            "aggregation",
            "2026-09-24T00:00:00Z",
            "resume",
            "aggregation_approval_after_execution",
        ),
        (
            "case",
            "2026-09-23T02:00:01Z",
            "budget_only",
            "case_approval_after_execution",
        ),
        (
            "aggregation",
            "2026-09-23T02:00:01Z",
            "budget_only",
            "aggregation_approval_after_execution",
        ),
        (
            "case",
            "2026-09-23T02:05:00Z",
            "new_budget_with_prior_sample",
            "case_approval_after_execution",
        ),
        (
            "aggregation",
            "2026-09-23T02:05:00Z",
            "new_budget_with_prior_sample",
            "aggregation_approval_after_execution",
        ),
    ],
)
def test_formal_material_and_aggregation_public_paths_share_execution_cutoff(
    tmp_path, field, when, stage, reason
):
    source, target = formal_evidence(
        "2026-09-23T01:10:00Z", "2026-09-23T01:30:00Z",
        stage="fresh" if stage == "budget_only" else stage,
    )
    if stage == "budget_only":
        target["budget_started_at"] = START
    if field == "case":
        set_proof_time(target["case_set_approval"], when)
    else:
        target["aggregation_policy"]["approval"]["at"] = when
        rehash(target["aggregation_policy"])
    authorize(target)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)

    with pytest.raises(ValueError, match=reason):
        store.import_batch(target)
    assert inventory(store) == before
    with pytest.raises(ValueError, match=reason):
        report(target, now=datetime(2026, 9, 23, 3, tzinfo=UTC), store=store)
    start = START if stage in ("resume", "budget_only") else "2026-09-23T02:10:00Z"
    with pytest.raises(ValueError, match=reason):
        validate_execution_materials(target, store, started_at=start)
    assert inventory(store) == before


@pytest.mark.parametrize("field", ["case", "aggregation"])
@pytest.mark.parametrize(
    "stage,when",
    [
        ("resume", START),
        ("resume", "2026-09-23T10:00:00+08:00"),
        ("new_budget_with_prior_sample", "2026-09-23T02:00:02Z"),
    ],
)
def test_formal_material_and_aggregation_equal_cutoff_are_accepted(
    tmp_path, field, stage, when
):
    source, target = formal_evidence(
        "2026-09-23T01:10:00Z", "2026-09-23T01:30:00Z", stage=stage
    )
    if field == "case":
        set_proof_time(target["case_set_approval"], when)
    else:
        target["aggregation_policy"]["approval"]["at"] = when
        rehash(target["aggregation_policy"])
    authorize(target)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    assert store.read(target["batch_id"]) == target
    start = START if stage == "resume" else "2026-09-23T02:10:00Z"
    # Historical chronology can be valid without current source eligibility.
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        validate_execution_materials(target, store, started_at=start)


def test_unexecuted_draft_can_hold_later_case_and_aggregation_approvals(tmp_path):
    source, target = formal_evidence(
        "2026-09-24T00:00:00Z", "2026-09-24T00:01:00Z", stage="fresh"
    )
    set_proof_time(target["case_set_approval"], "2026-09-24T00:02:00Z")
    authorize(target)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    store.import_batch(target)
    assert store.read(target["batch_id"]) == target
