"""New-contract temporal, execution and derivation boundaries through public APIs."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.acceptance.examples import SKIP
from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.runner import execute, run_offline
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.acceptance.supplement_schema import manifest, material_id
from agent_alfred.evals.deterministic.test_acceptance_supplement import (
    approve,
    graded,
    review,
)


def test_business_refusal_is_distinct_from_required_execution_and_run_failure(tmp_path):
    batch = supplement_batch()
    tools = next(c for c in batch["cases"] if c["group"] == "tools")
    tools["input"] = "Report the failed update accurately"
    tools["gold"] = "Say the version conflict prevented the update"
    tools["scene"] = "failure_or_misleading"
    tools["setup"] = {
        "version": 1,
        "local_tool_allowlist": ["read_persona", "update_persona"],
        "persona": "Original persona",
        "fault_fixture": "persona_version_conflict_v1",
    }
    version = hashlib.sha256(b"Original persona").hexdigest()
    tools["script"] = [
        SKIP,
        {"tool": "read_persona", "arguments": {}},
        {
            "tool": "update_persona",
            "arguments": {"content": "New persona", "expected_version": version},
        },
        "The version changed; the update was rejected.",
    ]
    tools["obligations"][0]["expected"] = "Report that the update was rejected"
    required = deepcopy(tools)
    required.update(
        id="required-operation",
        input="Successfully update the persona",
        gold="An actual successful update",
        source_family_id="required-operation",
    )
    required["obligations"][0]["expected"] = "The persona update actually succeeded"
    batch["cases"].append(required)
    failure = deepcopy(tools)
    failure.update(
        id="failed-run",
        input="Create a draft",
        gold="A durable draft",
        source_family_id="failed-run",
    )
    failure["setup"] = {
        "version": 1,
        "local_tool_allowlist": ["draft_message"],
        "fault_fixture": "file_publication_unknown_v1",
    }
    failure["script"] = [
        SKIP,
        {"tool": "draft_message", "arguments": {"body": "Synthetic draft"}},
        "Must not replace first failure",
    ]
    batch["cases"].append(failure)
    profile = batch["profiles"][0]
    profile["local_tool_allowlist"] = [
        "read_persona",
        "update_persona",
        "draft_message",
    ]
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    for case in batch["cases"]:
        case["material_id"] = material_id(case)
    batch["manifest"] = manifest(batch["cases"])
    sampled = run_offline(batch, tmp_path / "runtime")
    refusal = next(r for r in sampled["results"] if r["case_id"] == tools["id"])
    assert refusal["outcome"] == "completed"
    assert refusal["evidence"]["tool_projections"]["requests"][-1]["result"] == "failed"
    stopped = next(r for r in sampled["results"] if r["case_id"] == "failed-run")
    assert stopped["outcome"] == "failed"
    assert (
        stopped["evidence"]["setup"]["fault_fixture"]["recovery"]["run_id"]
        != stopped["run_id"]
    )
    # The failed Run may have no output; it remains failed without a judge label.
    sampled["results"].remove(stopped)
    scored = graded(
        sampled, failures=[("required-operation", "obligations", "completion")]
    )
    scored["results"].append(stopped)
    scored["review_disputes"] = [
        review(scored, "grade", g["id"]) for g in scored["grades"]
    ]
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(scored)
    q = report(store.read(scored["batch_id"]), store=store)["quality"]
    assert "critical_obligation_failed:required-operation:completion" in q["failures"]
    assert "product_failed:failed-run" in q["failures"]
    assert not any(f.startswith("product_failed:" + tools["id"]) for f in q["failures"])
    assert (
        next(
            o
            for o in q["obligations"]
            if o["case_id"] == tools["id"] and o["id"] == "completion"
        )["status"]
        == "pass"
    )


def test_material_revision_invalidates_approvals_without_rewriting_samples(tmp_path):
    authority = SimulationAuthority(tmp_path / "authority")
    batch = supplement_batch()
    batch["review_disputes"] = [review(batch, "materials")]
    approve(batch, authority, "materials")
    batch = graded(run_offline(batch, tmp_path / "runtime"))
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    store.import_batch(batch)
    raw = (store.root / batch["batch_id"] / "batch.json").read_bytes()
    cases = deepcopy(batch["cases"])
    cases[0]["obligations"][0]["kind"] = "secondary"
    cases[0]["absent_categories"] = {}
    cases[0]["material_id"] = material_id(cases[0])
    revised = store.revise(
        batch["batch_id"],
        "changed-material",
        configuration={"cases": cases, "manifest": manifest(cases)},
    )
    assert revised["results"] == batch["results"]
    assert revised["grades"] == []
    q = report(revised, store=store)["quality"]
    assert q["user_approvals"]["materials"] is None
    assert "independent_material_review_missing" in q["blockers"]
    assert (store.root / batch["batch_id"] / "batch.json").read_bytes() == raw
    with pytest.raises(ValueError, match="regrade_changed_product_samples"):
        store.revise(
            batch["batch_id"],
            "fresh-budget",
            execution={"budget_started_at": datetime.now(UTC).isoformat()},
        )


def test_formal_source_approval_order_and_original_cutoff(tmp_path):
    time = datetime(2026, 9, 20, tzinfo=UTC)
    clock = FakeClock(wall=time)
    authority = SimulationAuthority(tmp_path / "authority", clock=clock)
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    calibration = supplement_batch(phase="calibration", prefix="calibration")
    approve(calibration, authority, "materials")
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
    approve(formal, authority, "materials")
    formal["budget_started_at"] = "2026-09-20T08:00:00+08:00"
    store.import_batch(formal)  # equal instants are valid, including timezone offset
    q = report(store.read("formal"), store=store, now=time)
    assert q["v1_release"]["verdict"] == "BLOCKED"
    assert "calibration_incomplete" in q["v1_release"]["blockers"]
    late = deepcopy(formal)
    late.update(
        batch_id="late-formal",
        budget_started_at=(time - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ValueError, match="after_execution"):
        store.import_batch(late)
    # The source itself cannot be imported with approvals after an original start.
    changed = deepcopy(calibration)
    changed.update(
        batch_id="late-source",
        budget_started_at=(time - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ValueError, match="material_decision_after_execution"):
        store.import_batch(changed)


def test_material_approval_cannot_authorize_online_or_invoke_credentials(tmp_path):
    authority = SimulationAuthority(tmp_path / "authority")
    batch = supplement_batch()
    approve(batch, authority, "materials")
    batch["simulation"] = False

    class UnreadableCredentials:
        def values(self):
            raise AssertionError("credentials must stay unread")

    def forbidden_factory(*args):
        raise AssertionError("factory must stay untouched")

    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        execute(
            batch,
            tmp_path / "runtime",
            credentials=UnreadableCredentials(),
            product_factory_builder=forbidden_factory,
        )
    assert not (tmp_path / "runtime").exists()


def test_schema4_dispatch_only_uses_bound_mock_transport(tmp_path):
    import json

    import httpx2 as httpx

    from agent_alfred.evals.acceptance.admission import proposal
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.deterministic.test_acceptance_trial import authorize

    root = Path(__file__).resolve().parents[4]
    authority = SimulationAuthority(tmp_path / "authority")
    batch = supplement_batch(capture(root))
    batch["review_disputes"] = [review(batch, "materials")]
    approve(batch, authority, "materials")
    batch = authorize(batch)
    batch["authorization"]["max_requests"] = 100
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    request = proposal(batch, output_scope=store.root, operations=["product"])
    receipt = authority.issue(
        request,
        subject="simulation:user",
        source_event_id="offline-approval",
        source_event_digest="a" * 64,
        activation_deadline=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
        cost_acceptance={"amount": None, "accept_unknown": True},
    )
    session = authority.activate(receipt, request)
    sent = []

    def send(req):
        body = json.loads(req.content)
        sent.append(body)
        # A controlled infrastructure failure stops the six-case batch after one
        # request; untouched cases retain their predeclared denominators.
        return httpx.Response(503, json={"error": {"message": "synthetic unavailable"}})

    sampled = execute(
        batch,
        tmp_path / "runtime",
        candidate_root=root,
        store=store,
        simulation_session=session,
        mock_transport=httpx.MockTransport(send),
    )
    assert len(sent) == 1
    assert len(sampled["cases"]) == 6
    assert len(sampled["results"]) == 1
    assert sampled["stop_reason"]
    q = report(sampled, store=store)["quality"]
    assert q["groups"]["memory"]["completion"]["denominator"] == 1


def test_formal_execute_rejects_empty_approved_policy_before_mock_call(tmp_path):
    import httpx2 as httpx

    from agent_alfred.evals.acceptance.admission import proposal
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.deterministic.test_acceptance_trial import authorize

    root = Path(__file__).resolve().parents[4]
    candidate = capture(root)
    authority = SimulationAuthority(tmp_path / "authority")
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    calibration = supplement_batch(candidate, phase="calibration", prefix="calibration")
    store.import_batch(calibration)
    formal = supplement_batch(candidate, phase="formal", prefix="formal")
    formal["seen_families"] += [c["source_family_id"] for c in calibration["cases"]]
    formal["seen_families"] += [
        t["source_family_id"] for t in calibration["judge_tests"]
    ]
    formal["calibration"] = {
        "batch_id": "calibration",
        "sha256": digest(calibration),
        "material_ids": [c["material_id"] for c in calibration["cases"]],
    }
    formal["review_disputes"] = [review(formal, "materials")]
    approve(formal, authority, "thresholds")
    approve(formal, authority, "materials")
    formal = authorize(formal)
    request = proposal(formal, output_scope=store.root, operations=["product"])
    receipt = authority.issue(
        request,
        subject="simulation:user",
        source_event_id="synthetic",
        source_event_digest="a" * 64,
        activation_deadline=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        cost_acceptance={"amount": None, "accept_unknown": True},
    )
    sent = []
    transport = httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(503))
    with pytest.raises(ValueError, match="approved_aggregation_missing"):
        execute(
            formal,
            tmp_path / "runtime",
            store=store,
            candidate_root=root,
            simulation_session=authority.activate(receipt, request),
            mock_transport=transport,
        )
    assert sent == []
    assert not (tmp_path / "runtime").exists()


def test_formal_cannot_reuse_registered_ancestor_or_judge_material_family(tmp_path):
    calibration = supplement_batch(phase="calibration", prefix="calibration")
    calibration["cases"][0]["lineage"].append(
        {
            "family_id": "shared-ancestor",
            "content_sha256": "f" * 64,
            "reference": "known-source",
        }
    )
    calibration["cases"][0]["material_id"] = material_id(calibration["cases"][0])
    calibration["manifest"] = manifest(calibration["cases"])
    store = EvidenceStore(tmp_path)
    store.import_batch(calibration)
    for family in (
        "shared-ancestor",
        calibration["judge_tests"][0]["source_family_id"],
    ):
        formal = supplement_batch(phase="formal", prefix="formal")
        formal["seen_families"] += [c["source_family_id"] for c in calibration["cases"]]
        formal["cases"][0]["lineage"].append(
            {
                "family_id": family,
                "content_sha256": "d" * 64,
                "reference": "known-overlap",
            }
        )
        formal["cases"][0]["material_id"] = material_id(formal["cases"][0])
        formal["manifest"] = manifest(formal["cases"])
        formal["calibration"] = {
            "batch_id": "calibration",
            "sha256": digest(calibration),
            "material_ids": [c["material_id"] for c in calibration["cases"]],
        }
        with pytest.raises(
            ValueError,
            match="(calibration_family_overlap|judge_material_in_product_set)",
        ):
            store.import_batch(formal)


def test_formal_requires_reviewed_calibration_and_reuses_qualified_judge(
    tmp_path, monkeypatch
):
    import json

    from agent_alfred.evals.acceptance.budget import validate_execution_materials
    from agent_alfred.evals.acceptance.runner import _run
    from agent_alfred.evals.acceptance.schema import GROUPS, calibration_identity
    from agent_alfred.evals.acceptance.supplement_decisions import dispute_request
    from agent_alfred.evals.acceptance.supplement_judge import run_test
    from agent_alfred.evals.acceptance.supplement_reviews import disputes
    from agent_alfred.evals.acceptance.supplement_schema import signed
    from agent_alfred.model import ScriptedModel

    authority = SimulationAuthority(tmp_path / "authority")
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    calibration = supplement_batch(phase="calibration", prefix="calibration")
    calibration["review_disputes"] = [review(calibration, "materials")]
    approve(calibration, authority, "materials")
    # Build synthetic Host evidence for the public import/reference regression.
    # The public dry-run command intentionally cannot produce a calibration run.
    calibration = graded(_run(calibration, tmp_path / "runtime"))
    for test in calibration["judge_tests"]:
        row = run_test(
            calibration,
            test["id"],
            ScriptedModel(
                [
                    json.dumps(
                        {
                            "label": test["expected"],
                            "reason": "Synthetic correct classification",
                            "evidence": ["judge-material#/answer"],
                        }
                    )
                ]
            ),
            producer={
                "instance_id": "synthetic-original-judge",
                "model": "fixture-judge",
                "reference": "synthetic-check",
            },
        )
        calibration["judge_test_results"].append(row)
        calibration["review_disputes"].append(
            review(calibration, "judge_test", row["id"])
        )
    for dispute in disputes(calibration):
        calibration["review_adjudications"].append(
            authority.issue_decision(
                dispute_request(calibration, dispute),
                subject="simulation:user",
                decision="dismissed",
                reason="Synthetic expected uncertainty identifies absent support",
                evidence=["judge-material#/answer"],
            )
        )
    approve(calibration, authority, "results")
    approve(calibration, authority, "calibration")
    store.import_batch(calibration)

    def formal_from(source, name):
        batch = supplement_batch(phase="formal", prefix=name)
        batch["seen_families"] += [c["source_family_id"] for c in source["cases"]]
        batch["seen_families"] += [t["source_family_id"] for t in source["judge_tests"]]
        batch["calibration"] = {
            "batch_id": source["batch_id"],
            "sha256": digest(source),
            "material_ids": [c["material_id"] for c in source["cases"]],
        }
        batch["aggregation_policy"] = signed(
            {
                "version": 2,
                "scorer": "case-fraction-v1",
                "semantic_rubric_id": batch["semantic_rubric"]["id"],
                "calibration_evidence_sha256": calibration_identity(source),
                # Only a synthetic fixture choice; no production default is set.
                "groups": {
                    g: {d: 1.0 for d in ("completion", "correctness", "selection")}
                    for g in GROUPS
                },
                "denominator": "all_predeclared_applicable",
                "unknown_policy": "block",
                "forbidden_policy": "zero_confirmed_violations",
                "approval": None,
            }
        )
        batch["review_disputes"] = [review(batch, "materials")]
        approve(batch, authority, "thresholds")
        approve(batch, authority, "materials")
        return batch

    formal = formal_from(calibration, "unreviewed-source")
    store.import_batch(formal)
    value = report(store.read(formal["batch_id"]), store=store)
    assert any(
        b.startswith("calibration_evidence:independent_grade_review_missing:")
        for b in value["v1_release"]["blockers"]
    )
    with pytest.raises(ValueError, match="supplement_calibration_not_ready"):
        validate_execution_materials(
            formal, store, started_at=datetime.now(UTC).isoformat()
        )

    reviewed = deepcopy(calibration)
    reviewed["review_disputes"] += [
        review(reviewed, "grade", g["id"]) for g in reviewed["grades"]
    ]
    approve(reviewed, authority, "results")
    approve(reviewed, authority, "calibration")
    reviewed = store.revise(
        "calibration",
        "calibration-reviewed",
        reviews=reviewed["review_disputes"][len(calibration["review_disputes"]) :],
        configuration={
            "summaries": reviewed["summaries"],
            "user_decisions": reviewed["user_decisions"],
        },
    )
    ready = formal_from(reviewed, "ready-source")
    store.import_batch(ready)
    validate_execution_materials(ready, store, started_at=datetime.now(UTC).isoformat())
    value = report(store.read(ready["batch_id"]), store=store)
    assert value["quality"]["judge_checks"]["verdict"] == "PASS"
    assert value["quality"]["judge_checks"]["source_batch"] == "calibration-reviewed"
    assert value["v1_release"]["verdict"] == "BLOCKED"

    # New threshold approval invalidates an earlier formal material freeze, even
    # before there is a sample. Import and reread apply the same ordering rule.
    bad = deepcopy(ready)
    bad["batch_id"] = "early-freeze"
    approve(bad, authority, "thresholds")
    with pytest.raises(
        ValueError, match="formal_materials_frozen_before_prerequisites"
    ):
        store.import_batch(bad)
    stale = report(ready, store=store, now=datetime.now(UTC) + timedelta(days=8))
    assert any(
        b.startswith("calibration_evidence:stale_sample:")
        for b in stale["v1_release"]["blockers"]
    )

    observed = deepcopy(ready)
    observed["batch_id"] = "formal-confirmed-miss"
    test = observed["judge_tests"][0]
    row = run_test(
        observed,
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
            "instance_id": "formal-judge",
            "model": "fixture-judge",
            "reference": "synthetic-formal-check",
        },
    )
    observed["judge_test_results"].append(row)
    observed["review_disputes"].append(review(observed, "judge_test", row["id"]))
    for dispute in disputes(observed):
        observed["review_adjudications"].append(
            authority.issue_decision(
                dispute_request(observed, dispute),
                subject="simulation:user",
                decision="confirmed_violation",
                reason="Synthetic new failure invalidates earlier qualification",
                evidence=["judge-material#/answer"],
            )
        )
    store.import_batch(observed)
    restarted = EvidenceStore(store.root, decision_source=authority)
    value = report(restarted.read(observed["batch_id"]), store=restarted)
    checks = value["quality"]["judge_checks"]
    assert checks["verdict"] == "FAIL"
    assert checks["source_batch"] == "calibration-reviewed"
    assert checks["observed_checks"]["rows"][0]["raw"] == row["raw"]
    assert "judge_missed_prohibition:" + test["id"] in checks["failures"]
    assert value["v1_release"]["verdict"] == "FAIL"

    # A newly denied source stays readable for audit, but neither it nor a
    # descendant can supply formal eligibility through any public entry point.
    import hashlib

    from agent_alfred.evals.acceptance import authorization_history

    child = store.revise(reviewed["batch_id"], "calibration-child")
    descendant = store.revise(child["batch_id"], "calibration-descendant")
    inherited = formal_from(descendant, "inherited-source")
    store.import_batch(inherited)
    facts = authorization_history.history()
    facts["batch_ids"].append(reviewed["batch_id"])
    registry = tmp_path / "synthetic-denials.json"
    payload = json.dumps(facts).encode()
    registry.write_bytes(payload)
    monkeypatch.setattr(authorization_history, "HISTORY_PATH", registry)
    monkeypatch.setattr(
        authorization_history, "HISTORY_SHA256", hashlib.sha256(payload).hexdigest()
    )
    restarted = EvidenceStore(store.root, decision_source=authority)
    for source, formal in ((reviewed, ready), (descendant, inherited)):
        assert restarted.read(source["batch_id"]) == source
        assert (
            restarted.authorization_status(source["batch_id"])["status"]
            == "QUARANTINED_AUDIT_ONLY"
        )
        rejected = deepcopy(formal)
        rejected["batch_id"] += "-reimport"
        for action in (
            lambda: restarted.import_batch(rejected),
            lambda: restarted.read(formal["batch_id"]),
            lambda: report(formal, store=restarted),
            lambda: validate_execution_materials(
                formal, restarted, started_at=datetime.now(UTC).isoformat()
            ),
        ):
            with pytest.raises(ValueError, match="^execution_authorization_invalid$"):
                action()
        assert not (store.root / rejected["batch_id"]).exists()
