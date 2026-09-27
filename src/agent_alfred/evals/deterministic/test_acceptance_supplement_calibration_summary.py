"""Formal user decisions must cover the same calibration evidence as reports."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.acceptance.supplement_decisions import (
    decision_request,
    dispute_request,
    make_summary,
)
from agent_alfred.evals.acceptance.supplement_judge import run_test
from agent_alfred.evals.acceptance.supplement_reviews import disputes
from agent_alfred.evals.acceptance.supplement_schema import signed
from agent_alfred.evals.deterministic.test_acceptance_supplement import approve, review
from agent_alfred.model import ScriptedModel


@pytest.fixture
def linked(tmp_path):
    authority = SimulationAuthority(tmp_path / "authority")
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    calibration = supplement_batch(phase="calibration", prefix="calibration")
    calibration["review_disputes"] = [review(calibration, "materials")]
    approve(calibration, authority, "materials")
    test = calibration["judge_tests"][0]
    row = run_test(
        calibration,
        test["id"],
        ScriptedModel(
            [
                json.dumps(
                    {
                        "label": "pass",
                        "reason": "Synthetic missed prohibition",
                        "evidence": ["judge-material#/answer"],
                    }
                )
            ]
        ),
        producer={
            "instance_id": "original-judge",
            "model": "fixture-judge",
            "reference": "synthetic-only",
        },
    )
    calibration["judge_test_results"] = [row]
    calibration["review_disputes"].append(review(calibration, "judge_test", row["id"]))
    for dispute in disputes(calibration):
        calibration["review_adjudications"].append(
            authority.issue_decision(
                dispute_request(calibration, dispute),
                subject="simulation:user",
                decision="confirmed_violation",
                reason="Synthetic known miss",
                evidence=["judge-material#/answer"],
            )
        )
    approve(calibration, authority, "results")
    approve(calibration, authority, "calibration")
    store.import_batch(calibration)
    formal = supplement_batch(phase="formal", prefix="formal")
    formal["seen_families"] += [c["source_family_id"] for c in calibration["cases"]]
    formal["seen_families"] += [
        t["source_family_id"] for t in calibration["judge_tests"]
    ]
    formal["calibration"] = {
        "batch_id": calibration["batch_id"],
        "sha256": digest(calibration),
        "material_ids": [c["material_id"] for c in calibration["cases"]],
    }
    store.import_batch(formal)
    return store, authority, calibration, formal


def approve_summary(store, authority, batch, summary, new_id, *, at=None):
    event = authority.issue_decision(
        decision_request(summary),
        subject="simulation:user",
        decision="approved",
        reason="Synthetic exact summary approval",
    )
    if at is not None:
        event = signed({**{k: v for k, v in event.items() if k != "id"}, "at": at})
    revised = store.revise(
        batch["batch_id"],
        new_id,
        configuration={
            "summaries": batch["summaries"] + [summary],
            "user_decisions": batch["user_decisions"] + [event],
        },
    )
    return revised, event


@pytest.mark.parametrize("stage", ["results", "calibration"])
def test_formal_summary_and_approval_include_calibration_failure(linked, stage):
    store, authority, calibration, formal = linked
    q = report(formal, store=store)["quality"]
    summary = make_summary(formal, stage, store=store)
    assert summary["version"] == 3
    assert summary["calibration_snapshot"] == calibration
    assert summary["assessment"]["verdict"] == "FAIL"
    assert summary["assessment"]["failures"] == q["failures"]
    assert summary["assessment"]["judge_checks"] == q["judge_checks"]
    assert any(f.startswith("judge_missed_prohibition:") for f in q["failures"])
    inherited = summary["assessment"]["calibration_assessment"]
    assert inherited == report(calibration, store=store)["quality"]
    assert inherited["review_disputes"][0]["status"] == "confirmed_violation"
    assert inherited["user_approvals"]["calibration"] is not None

    # Signing a display without resolving the source must never approve results.
    incomplete = make_summary(formal, stage, decision_source=authority)
    assert "calibration_package_not_verified" in incomplete["assessment"]["blockers"]
    partial, _ = approve_summary(store, authority, formal, incomplete, "partial")
    assert report(partial, store=store)["quality"]["user_approvals"][stage] is None

    complete, event = approve_summary(store, authority, formal, summary, "complete")
    fresh = EvidenceStore(store.root, decision_source=authority)
    reread = fresh.read(complete["batch_id"])
    assessed = report(reread, store=fresh)
    assert assessed["quality"]["user_approvals"][stage] == event
    assert assessed["quality"]["verdict"] == assessed["v1_release"]["verdict"] == "FAIL"
    # Saved source bytes and rulings preserve the view, never current authority.
    audit_store = EvidenceStore(store.root)
    assert audit_store.read(complete["batch_id"])["summaries"] == complete["summaries"]
    assert report(reread, store=audit_store)["quality"]["user_approvals"][stage] is None
    assert report(reread)["quality"]["user_approvals"][stage] is None

    class MissingCalibrationDecision:
        def read_decision(self, source_ref):
            if source_ref == calibration["user_decisions"][-1]["source_ref"]:
                raise OSError("synthetic source unavailable")
            return authority.read_decision(source_ref)

    unavailable = EvidenceStore(
        store.root, decision_source=MissingCalibrationDecision()
    )
    assert unavailable.read(complete["batch_id"]) == complete
    assert (
        report(complete, store=unavailable)["quality"]["user_approvals"][stage] is None
    )


def test_formal_summary_rejects_hidden_failure_or_wrong_source(linked):
    store, authority, calibration, formal = linked
    summary = make_summary(formal, "results", store=store)
    for mutation, error in (
        (lambda s: s["assessment"].update(failures=[]), "summary_coverage_mismatch"),
        (
            lambda s: s["calibration_snapshot"].update(batch_id="different-source"),
            "summary_calibration_mismatch",
        ),
        (
            lambda s: s.update(calibration_decision_snapshot=[]),
            "summary_coverage_mismatch",
        ),
    ):
        tampered = deepcopy(summary)
        mutation(tampered)
        tampered = signed({k: v for k, v in tampered.items() if k != "id"})
        with pytest.raises(ValueError, match=error):
            approve_summary(store, authority, formal, tampered, "tampered")
        assert not (store.root / "tampered").exists()
    # The latest source approval is later than the formal batch's observations.
    too_early = (
        datetime.fromisoformat(calibration["user_decisions"][-1]["at"])
        - timedelta(microseconds=1)
    ).isoformat()
    with pytest.raises(ValueError, match="decision_before_reviewed_results"):
        approve_summary(store, authority, formal, summary, "backdated", at=too_early)


def test_formal_summary_rereads_source_and_does_not_fall_back_to_snapshot(
    linked, monkeypatch
):
    from agent_alfred.evals.acceptance import authorization_history

    store, authority, calibration, formal = linked
    summary = make_summary(formal, "results", store=store)
    complete, _ = approve_summary(store, authority, formal, summary, "complete")
    newer_source = store.revise(calibration["batch_id"], "calibration-revision")
    changed = store.revise(
        complete["batch_id"],
        "different-calibration",
        configuration={
            "calibration": {
                **formal["calibration"],
                "batch_id": newer_source["batch_id"],
                "sha256": digest(newer_source),
            }
        },
    )
    assert changed["summaries"] == complete["summaries"]
    assert report(changed, store=store)["quality"]["user_approvals"]["results"] is None
    assert (
        make_summary(changed, "results", store=store)["calibration_snapshot"]
        == newer_source
    )
    source_path = store.root / calibration["batch_id"] / "batch.json"
    original = source_path.read_bytes()
    source_path.unlink()
    for action in (
        lambda: make_summary(complete, "results", store=store),
        lambda: report(complete, store=store),
        lambda: EvidenceStore(store.root).read(complete["batch_id"]),
    ):
        with pytest.raises((ValueError, FileNotFoundError)):
            action()
    source_path.write_bytes(original)
    # A newly quarantined source invalidates fresh summaries and approval checks.
    facts = authorization_history.history()
    facts["batch_ids"].append(calibration["batch_id"])
    registry = store.root.parent / "synthetic-denials.json"
    payload = json.dumps(facts).encode()
    registry.write_bytes(payload)
    monkeypatch.setattr(authorization_history, "HISTORY_PATH", registry)
    monkeypatch.setattr(
        authorization_history, "HISTORY_SHA256", hashlib.sha256(payload).hexdigest()
    )
    for action in (
        lambda: make_summary(complete, "results", store=store),
        lambda: report(complete, store=store),
    ):
        with pytest.raises(ValueError, match="execution_authorization_invalid"):
            action()


def test_cli_formal_summary_resolves_calibration_without_granting_authority(
    linked, tmp_path
):
    store, _, calibration, formal = linked
    output = tmp_path / "summary.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_alfred.evals.acceptance",
            "summary",
            "--store",
            str(store.root),
            "--batch",
            formal["batch_id"],
            "--summary-stage",
            "results",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(output.read_text())
    assert summary["calibration_snapshot"] == calibration
    assert (
        summary["assessment"]["judge_checks"]["source_batch"] == calibration["batch_id"]
    )
    assert summary["calibration_decision_snapshot"] == []
    assert (
        summary["assessment"]["calibration_assessment"]["user_approvals"]["calibration"]
        is None
    )
