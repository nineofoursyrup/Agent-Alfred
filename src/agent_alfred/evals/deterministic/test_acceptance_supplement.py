"""#95 public paths with synthetic authority, real Host and immutable evidence."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.examples_v4 import supplement_batch
from agent_alfred.evals.acceptance.judge import judge_result
from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.runner import run_offline
from agent_alfred.evals.acceptance.schema import digest, encode, validate
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.acceptance.supplement_decisions import (
    decision_request,
    dispute_request,
    make_summary,
)
from agent_alfred.evals.acceptance.supplement_judge import run_test
from agent_alfred.evals.acceptance.supplement_reviews import (
    blind_input,
    disputes,
    expected_items,
    make_review,
    target_for,
)
from agent_alfred.evals.acceptance.supplement_schema import (
    manifest,
    material_id,
    signed,
)
from agent_alfred.model import ScriptedModel


def now():
    return datetime.now(UTC).isoformat()


def review(batch, kind, target_id=None, *, changes=None):
    target = target_for(batch, kind, target_id)
    inputs = blind_input(batch, target)
    if kind == "materials":
        ref = "materials#/cases/0/gold"
    else:
        refs = inputs["sources"]
        ref = (
            next(k for k in refs if k.startswith("result:")) + "#/output"
            if kind == "grade"
            else "judge-material#/answer"
        )
    opinions = {
        k: {
            "status": v,
            "support": "supported",
            "reason": "Synthetic independent inspection",
            "evidence": [ref],
        }
        for k, v in expected_items(batch, target).items()
    }
    for key, change in (changes or {}).items():
        opinions[key].update(change)
    return make_review(
        batch,
        target,
        reviewer={
            "instance_id": "separate-" + kind + "-" + (target_id or "materials"),
            "model": "fixture-judge",
            "reference": "synthetic-review-source",
        },
        started_at=now(),
        completed_at=now(),
        compared_at=now(),
        output=json.dumps(opinions),
        comparison_output=json.dumps(opinions),
    )


def approve(batch, authority, stage):
    summary = make_summary(batch, stage, decision_source=authority)
    if summary not in batch["summaries"]:
        batch["summaries"].append(summary)
    event = authority.issue_decision(
        decision_request(summary),
        subject="simulation:user",
        decision="approved",
        reason="Synthetic scope approval",
    )
    batch["user_decisions"].append(event)
    return event


def graded(batch, *, failures=()):
    batch = deepcopy(batch)
    batch["authorization"] = {"max_output_tokens": 2048}
    for case, result in zip(batch["cases"], batch["results"]):
        item = {
            "status": "pass",
            "reason": "Synthetic judgment",
            "evidence": "result:" + result["id"] + "#/output",
        }
        raw = {
            "dimensions": {
                k: {**item, "status": "pass" if applies else "na"}
                for k, applies in case["applicability"].items()
            },
            "prohibitions": {k: dict(item) for k in case["forbidden"]},
            "obligations": {
                o["id"]: {**item, "status": "pass" if o["applies"] else "na"}
                for o in case["obligations"]
            },
            "disputed": False,
            "suspected_safety": False,
        }
        for cid, kind, name in failures:
            if cid == case["id"]:
                raw[kind][name]["status"] = "fail"
        grade = judge_result(
            batch,
            case,
            result,
            ScriptedModel([json.dumps(raw)]),
            producer={
                "instance_id": "original-" + case["id"],
                "model": "fixture-judge",
                "reference": "synthetic-judge-source",
            },
        )
        assert grade["status"] == "scored", grade["error"]
        batch["grades"].append(grade)
    batch["authorization"] = None
    return batch


@pytest.fixture(scope="module")
def sampled(tmp_path_factory):
    path = tmp_path_factory.mktemp("supplement-host")
    authority = SimulationAuthority(path / "authority")
    batch = supplement_batch()
    batch["review_disputes"].append(review(batch, "materials"))
    approve(batch, authority, "materials")
    result = run_offline(batch, path / "runtime")
    assert all(r["outcome"] == "completed" for r in result["results"])
    return result, authority


def test_schema4_roundtrip_distribution_and_legacy_bytes(tmp_path):
    store = EvidenceStore(tmp_path)
    old = controlled_batch()
    store.import_batch(old)
    before = (tmp_path / old["batch_id"] / "batch.json").read_bytes()
    for phase, size in (("offline_fixture", 6), ("calibration", 30), ("formal", 120)):
        batch = supplement_batch(phase=phase, prefix=phase)
        store.import_batch(batch)
        assert len(EvidenceStore(tmp_path).read(phase)["manifest"]) == size
        assert (
            "independent_material_review_missing"
            in report(batch, store=store)["quality"]["blockers"]
        )
    assert (tmp_path / old["batch_id"] / "batch.json").read_bytes() == before
    impostor = deepcopy(old)
    impostor.update(schema_version=4, contract=batch["contract"])
    with pytest.raises(ValueError):
        store.import_batch(impostor)
    for mutation in (
        lambda b: b["cases"][0].update(scene="boundary"),
        lambda b: b["cases"][1].update(id=b["cases"][0]["id"]),
        lambda b: b.update(executor_python="bad"),
    ):
        batch = supplement_batch(phase="calibration")
        mutation(batch)
        with pytest.raises(ValueError):
            store.import_batch(batch)


def test_critical_failure_and_unknown_cannot_be_diluted(sampled, tmp_path):
    original, authority = sampled
    first = original["cases"][0]["id"]
    batch = graded(
        original,
        failures=[
            (first, "obligations", "completion"),
            (first, "prohibitions", "invented_execution"),
        ],
    )
    store = EvidenceStore(tmp_path, decision_source=authority)
    store.import_batch(batch)
    reviews = [review(batch, "grade", g["id"]) for g in batch["grades"]]
    reviews[1] = review(
        batch,
        "grade",
        batch["grades"][1]["id"],
        changes={"obligations:correctness": {"support": "unsupported"}},
    )
    revised = store.revise(batch["batch_id"], "reviewed", reviews=reviews)
    value = report(EvidenceStore(tmp_path).read("reviewed"), store=store)
    assert value["quality"]["critical_gate"]["verdict"] == "FAIL"
    assert (
        "critical_obligation_failed:" + first + ":completion"
        in value["quality"]["failures"]
    )
    assert (
        "prohibition_failed:" + first + ":invented_execution"
        in value["quality"]["failures"]
    )
    assert any(
        b.startswith("semantic_support_missing:") for b in value["quality"]["blockers"]
    )
    assert value["quality"]["groups"]["conversation"]["completion"]["denominator"] == 1
    assert value["quality"]["groups"]["conversation"]["completion"]["numerator"] == 0
    assert value["quality"]["user_approvals"]["materials"] is not None
    assert any(d["status"] == "pending" for d in value["quality"]["review_disputes"])
    assert revised["grades"] == batch["grades"]


def test_denominator_includes_unrun_and_extra_obligations_do_not_add_votes(
    sampled, tmp_path
):
    original, _ = sampled
    batch = deepcopy(original)
    batch.update(batch_id="small", summaries=[], user_decisions=[], review_disputes=[])
    case = batch["cases"][0]
    case["obligations"].extend(
        [
            {**case["obligations"][0], "id": "easy-" + str(i), "kind": "secondary"}
            for i in range(5)
        ]
    )
    case["absent_categories"] = {}
    case["material_id"] = material_id(case)
    batch["manifest"] = manifest(batch["cases"])
    batch["results"] = deepcopy(batch["results"][:1])
    batch["results"][0]["batch_id"] = "small"
    batch = graded(batch)
    batch["review_disputes"] = [review(batch, "grade", batch["grades"][0]["id"])]
    EvidenceStore(tmp_path).import_batch(batch)
    q = report(batch)["quality"]
    assert q["groups"]["conversation"]["completion"]["numerator"] == 1
    assert q["groups"]["conversation"]["completion"]["denominator"] == 1
    assert q["groups"]["memory"]["completion"]["denominator"] == 1
    assert q["groups"]["memory"]["completion"]["numerator"] == 0
    assert any(s.startswith("not_run:") for s in q["blockers"])


@pytest.mark.parametrize(
    "defect", ["same_instance", "leaked_label", "output_tampered", "backdated"]
)
def test_blind_review_cannot_fake_independence(sampled, tmp_path, defect):
    original, _ = sampled
    batch = graded(original)
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    grade = batch["grades"][0]
    record = review(batch, "grade", grade["id"])
    if defect == "same_instance":
        record["actor"]["instance_id"] = grade["producer"]["instance_id"]
    elif defect == "leaked_label":
        record["input"]["original_grade"] = grade
        record["input_sha256"] = digest(record["input"])
    elif defect == "output_tampered":
        record["output"] += " "
    else:
        record["started_at"] = "2020-01-01T00:00:00Z"
    record = signed({k: v for k, v in record.items() if k != "id"})
    with pytest.raises(ValueError):
        store.revise(batch["batch_id"], "bad", reviews=[record])
    assert not (tmp_path / "bad").exists()


def test_summary_scope_user_source_and_rulings_survive_restart(sampled, tmp_path):
    original, authority = sampled
    batch = graded(original)
    store = EvidenceStore(tmp_path, decision_source=authority)
    store.import_batch(batch)
    grade = batch["grades"][0]
    reviewed = store.revise(
        batch["batch_id"],
        "disputed",
        reviews=[
            review(
                batch,
                "grade",
                grade["id"],
                changes={"obligations:completion": {"status": "fail"}},
            )
        ],
    )
    summary = make_summary(reviewed, "results", decision_source=authority)
    event = authority.issue_decision(
        decision_request(summary),
        subject="simulation:user",
        decision="approved",
        reason="Approve exact summary, not unresolved disputes",
    )
    approved = store.revise(
        "disputed",
        "approved",
        configuration={
            "summaries": reviewed["summaries"] + [summary],
            "user_decisions": reviewed["user_decisions"] + [event],
        },
    )
    q = report(approved, store=store)["quality"]
    assert q["user_approvals"]["results"] is not None
    assert q["review_disputes"][0]["status"] == "pending"
    assert report(approved)["quality"]["user_approvals"]["results"] is None
    forged = deepcopy(approved)
    forged["user_decisions"][-1]["subject"] = "human-self-declared"
    forged["user_decisions"][-1] = signed(
        {k: v for k, v in forged["user_decisions"][-1].items() if k != "id"}
    )
    assert (
        report(forged, decision_source=authority)["quality"]["user_approvals"][
            "results"
        ]
        is None
    )
    dispute = disputes(approved)[0]
    pending = authority.issue_decision(
        dispute_request(approved, dispute),
        subject="simulation:user",
        decision="insufficient_evidence",
        reason="Need stronger evidence",
        evidence=["result:" + approved["results"][0]["id"] + "#/output"],
    )
    store.revise("approved", "pending", review_adjudications=[pending])
    restarted = EvidenceStore(tmp_path, decision_source=authority)
    assert (
        report(restarted.read("pending"), store=restarted)["quality"][
            "review_disputes"
        ][0]["status"]
        == "pending"
    )
    ruling = authority.issue_decision(
        dispute_request(approved, dispute),
        subject="simulation:user",
        decision="confirmed_violation",
        reason="Synthetic final finding",
        evidence=pending["evidence"],
    )
    final = restarted.revise("pending", "final", adjudications=[ruling])
    q = report(final, store=restarted)["quality"]
    assert q["review_disputes"][0]["status"] == "confirmed_violation"
    assert q["critical_gate"]["verdict"] == "FAIL"
    assert final["grades"] == batch["grades"]
    assert final["results"] == batch["results"]
    # Even approval from the authority cannot bless a summary that omitted the
    # authority's existing ruling. Its current complete view has another ID.
    incomplete = make_summary(final, "results")
    incomplete_approval = authority.issue_decision(
        decision_request(incomplete),
        subject="simulation:user",
        decision="approved",
        reason="Synthetic approval of an unverified view",
    )
    refused = restarted.revise(
        "final",
        "unverified-view",
        configuration={
            "summaries": final["summaries"] + [incomplete],
            "user_decisions": final["user_decisions"] + [incomplete_approval],
        },
    )
    assert (
        report(refused, store=restarted)["quality"]["user_approvals"]["results"] is None
    )

    class UnavailableSource:
        def read_decision(self, source_ref):
            raise OSError("synthetic source unavailable")

    for stage in ("results", "calibration"):
        result_summary = make_summary(final, stage, decision_source=authority)
        assert result_summary["assessment"]["critical_gate"] == q["critical_gate"]
        assert result_summary["assessment"]["failures"] == q["failures"]
        assert result_summary["disputes"][0]["status"] == "confirmed_violation"
        assert result_summary["disputes"][0]["decision"] == ruling
        before_ruling = make_summary(
            final,
            stage,
            decision_source=authority,
            now=datetime.fromisoformat(pending["at"]),
        )
        assert before_ruling["disputes"][0]["status"] == "pending"
        event = authority.issue_decision(
            decision_request(result_summary),
            subject="simulation:user",
            decision="approved",
            reason="Synthetic complete failure summary",
        )
        final = restarted.revise(
            final["batch_id"],
            "summary-" + stage,
            configuration={
                "summaries": final["summaries"] + [result_summary],
                "user_decisions": final["user_decisions"] + [event],
            },
        )
        assert report(final, store=restarted)["quality"]["user_approvals"][stage]
        untrusted = EvidenceStore(tmp_path)
        audit = untrusted.read(final["batch_id"])
        assert audit == final
        assert (
            report(audit, store=untrusted)["quality"]["user_approvals"][stage] is None
        )
        unverified = make_summary(audit, stage)
        assert unverified["disputes"][0]["status"] == "pending"
        assert unverified["id"] != result_summary["id"]
        unavailable = EvidenceStore(tmp_path, decision_source=UnavailableSource())
        audit = unavailable.read(final["batch_id"])
        assert (
            report(audit, store=unavailable)["quality"]["user_approvals"][stage] is None
        )
        assert (
            make_summary(audit, stage, decision_source=unavailable.decision_source)
            == unverified
        )

    hidden = deepcopy(result_summary)
    hidden["assessment"]["failures"] = []
    hidden = signed({k: v for k, v in hidden.items() if k != "id"})
    with pytest.raises(ValueError, match="summary_coverage_mismatch"):
        restarted.revise(
            final["batch_id"],
            "hidden-ruling",
            configuration={"summaries": final["summaries"] + [hidden]},
        )

    # A summary cannot be approved before a ruling it claims to contain.
    early = deepcopy(event)
    early["at"] = pending["at"]
    early = signed({k: v for k, v in early.items() if k != "id"})
    with pytest.raises(ValueError, match="decision_before_reviewed_results"):
        restarted.revise(
            final["batch_id"],
            "early-ruling-summary",
            configuration={"user_decisions": final["user_decisions"] + [early]},
        )
    tampered = deepcopy(summary)
    tampered["groups"]["conversation"]["cases"] = []
    tampered = signed({k: v for k, v in tampered.items() if k != "id"})
    with pytest.raises(ValueError, match="summary_coverage_mismatch"):
        store.revise(
            "disputed",
            "hidden-failure",
            configuration={"summaries": reviewed["summaries"] + [tampered]},
        )
    with pytest.raises(ValueError):
        store.revise(
            "disputed",
            "deleted-case",
            configuration={
                "cases": reviewed["cases"][1:],
                "manifest": manifest(reviewed["cases"][1:]),
            },
        )


@pytest.mark.parametrize("kind", ["materials", "grade", "judge_test"])
def test_blind_support_gap_survives_supported_comparison(sampled, tmp_path, kind):
    from agent_alfred.evals.acceptance.budget import validate_execution_materials

    original, authority = sampled
    batch = graded(original)
    if kind == "judge_test":
        test = batch["judge_tests"][1]
        result = run_test(
            batch,
            test["id"],
            ScriptedModel(
                [
                    json.dumps(
                        {
                            "label": test["expected"],
                            "reason": "Synthetic correct control",
                            "evidence": ["judge-material#/answer"],
                        }
                    )
                ]
            ),
            producer={
                "instance_id": "synthetic-check",
                "model": "fixture-judge",
                "reference": "synthetic-check",
            },
        )
        batch["judge_test_results"].append(result)
        target_id, item = result["id"], "judgment"
    elif kind == "grade":
        target_id, item = batch["grades"][0]["id"], "obligations:completion"
    else:
        # Material approval must precede any execution observation.
        batch = supplement_batch()
        target_id, item = None, "coverage"
    record = review(batch, kind, target_id)
    blind = json.loads(record["output"])
    blind[item]["support"] = "unknown" if kind == "judge_test" else "unsupported"
    blind[item]["reason"] = "Blind review found an unresolved evidence gap"
    record = make_review(
        batch,
        record["target"],
        reviewer=record["actor"],
        started_at=record["started_at"],
        completed_at=record["completed_at"],
        compared_at=record["compared_at"],
        output=json.dumps(blind),
        comparison_output=record["comparison_output"],
    )
    if kind == "materials":
        batch["review_disputes"] = [record]
    else:
        batch["review_disputes"] += [
            review(batch, "grade", g["id"])
            for g in batch["grades"]
            if kind != "grade" or g["id"] != target_id
        ]
        batch["review_disputes"].append(record)
    if kind == "materials":
        approve(batch, authority, "materials")
    store = EvidenceStore(tmp_path / "evidence", decision_source=authority)
    store.import_batch(batch)
    restarted = EvidenceStore(store.root, decision_source=authority)
    reread = restarted.read(batch["batch_id"])
    assessed = report(reread, store=restarted)["quality"]
    gaps = [d for d in assessed["review_disputes"] if d["review_id"] == record["id"]]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["status"] == "pending"
    assert gap["opinion"] == blind[item]
    assert "adjudication_required:" + gap["id"] in assessed["blockers"]
    if kind == "materials":
        with pytest.raises(ValueError, match="supplement_materials_not_ready"):
            validate_execution_materials(reread, restarted, started_at=now())
    elif kind == "grade":
        assert assessed["critical_gate"]["verdict"] == "BLOCKED"
    else:
        assert assessed["judge_checks"]["verdict"] == "BLOCKED"
        assert (
            "adjudication_required:" + gap["id"] in assessed["judge_checks"]["blockers"]
        )
    ruling = authority.issue_decision(
        dispute_request(batch, disputes(batch)[0]),
        subject="simulation:user",
        decision="dismissed",
        reason="Synthetic user resolves the blind gap",
        evidence=blind[item]["evidence"],
    )
    resolved = store.revise(batch["batch_id"], "resolved", adjudications=[ruling])
    resolved_gap = report(resolved, store=store)["quality"]["review_disputes"][0]
    assert resolved_gap["id"] == gap["id"]
    assert resolved_gap["status"] == "dismissed"
    assert resolved["review_disputes"] == batch["review_disputes"]
    assert reread["grades"] == resolved["grades"]
    if kind == "materials":
        validate_execution_materials(resolved, store, started_at=now())
    elif kind == "grade":
        assert (
            report(resolved, store=store)["quality"]["critical_gate"]["verdict"]
            == "PASS"
        )


def test_judge_checks_are_blind_separate_and_original_errors_retained(
    sampled, tmp_path
):
    original, authority = sampled
    batch = deepcopy(original)
    for test in batch["judge_tests"]:
        raw = json.dumps(
            {
                "label": "pass",
                "reason": "Synthetic judge label",
                "evidence": ["judge-material#/answer"],
            }
        )
        if test["failure_mode"] == "unsupported_citation":
            raw = '{"label":"pass","label":"fail"}'
        result = run_test(
            batch,
            test["id"],
            ScriptedModel([raw]),
            producer={
                "instance_id": "test-judge",
                "model": "fixture-judge",
                "reference": "synthetic-check",
            },
        )
        batch["judge_test_results"].append(result)
        assert "expected" not in result["input"]["sources"]["judge-material"]
        assert "failure_mode" not in json.dumps(result["input"])
    store = EvidenceStore(tmp_path, decision_source=authority)
    store.import_batch(batch)
    reviewed = store.revise(
        batch["batch_id"],
        "checks-reviewed",
        reviews=[
            review(batch, "judge_test", row["id"])
            for row in batch["judge_test_results"]
        ],
    )
    checks = report(reviewed, store=store)["quality"]["judge_checks"]
    assert checks["verdict"] == "FAIL"
    assert any(f.startswith("judge_missed_prohibition:") for f in checks["failures"])
    for stage in ("results", "calibration"):
        summary = make_summary(reviewed, stage, decision_source=authority)
        assert summary["assessment"]["judge_checks"]["failures"] == checks["failures"]
        assert summary["assessment"]["judge_checks"]["verdict"] == "FAIL"
        with_summary = store.revise(
            reviewed["batch_id"],
            "judge-failure-summary-" + stage,
            configuration={"summaries": reviewed["summaries"] + [summary]},
        )
        assert EvidenceStore(tmp_path).read(with_summary["batch_id"]) == with_summary
    assert checks["product_sample_count"] == 0
    assert checks["admission_threshold"] is None
    assert any(
        r["citation_structure_valid"] is False
        and r["raw"] == '{"label":"pass","label":"fail"}'
        for r in checks["rows"]
    )
    mixed = deepcopy(batch)
    mixed["results"].append(batch["judge_test_results"][0])
    with pytest.raises((ValueError, KeyError)):
        validate(mixed)


def test_c3_summary_bytes_remain_auditable_with_current_disputes(tmp_path):
    # Exact synthetic version-1 output generated by c3 candidate 05bcaa2a,
    # before blind concerns and judge-check dispute blockers were retained.
    fixture = Path(__file__).with_name("fixtures") / "schema4_c3_summary.json"
    batch = json.loads(fixture.read_bytes())
    original = encode(batch)
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    restarted = EvidenceStore(tmp_path)
    reread = restarted.read(batch["batch_id"])
    assert encode(reread) == original
    assert reread["summaries"][1]["version"] == 1
    q = report(reread, store=restarted)["quality"]
    assert any(d.get("stage") == "blind" for d in q["review_disputes"])
    assert any(
        b.startswith("adjudication_required:") for b in q["judge_checks"]["blockers"]
    )
    assert q["user_approvals"]["results"] is None
    assert make_summary(reread, "results")["version"] == 2
    assert make_summary(reread, "materials")["id"] != batch["summaries"][0]["id"]
    assert (tmp_path / batch["batch_id"] / "batch.json").read_bytes() == original


def test_material_chronology_on_import_read_and_regrade(sampled, tmp_path):
    original, _ = sampled
    late = deepcopy(original)
    late["user_decisions"][0]["at"] = (
        datetime.now(UTC) + timedelta(days=1)
    ).isoformat()
    late["user_decisions"][0] = signed(
        {k: v for k, v in late["user_decisions"][0].items() if k != "id"}
    )
    store = EvidenceStore(tmp_path)
    with pytest.raises(ValueError, match="material_decision_after_execution"):
        store.import_batch(late)
    store.import_batch(original)
    path = tmp_path / original["batch_id"]
    (path / "batch.json").write_bytes(encode(late))
    (path / "complete.json").write_bytes(encode({"batch.json": digest(late)}))
    with pytest.raises(ValueError, match="material_decision_after_execution"):
        EvidenceStore(tmp_path).read(original["batch_id"])


def test_cli_prepare_validate_import_summary_and_readonly_report(tmp_path):
    root = Path(__file__).resolve().parents[4]

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "agent_alfred.evals.acceptance", *args],
            cwd=root,
            text=True,
            capture_output=True,
        )

    file = tmp_path / "draft.json"
    prepared = cli(
        "prepare",
        "--schema-version",
        "4",
        "--candidate-root",
        str(root),
        "--output",
        str(file),
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert cli("validate", "--input", str(file)).returncode == 0
    imported = cli("import", "--input", str(file), "--store", str(tmp_path / "store"))
    assert imported.returncode == 2
    summary = cli(
        "summary", "--batch", "supplement", "--store", str(tmp_path / "store")
    )
    assert summary.returncode == 0
    assert len(json.loads(summary.stdout)["manifest"]) == 6

    def snapshot():
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (tmp_path / "store").rglob("*")
            if p.is_file()
        }

    before = snapshot()
    store = EvidenceStore(tmp_path / "store")
    report(store.read("supplement"), store=store)
    assert snapshot() == before


def test_confirmed_judge_miss_remains_fail_alongside_support_gap(sampled, tmp_path):
    original, authority = sampled
    batch = deepcopy(original)
    test = batch["judge_tests"][0]
    raw = json.dumps(
        {
            "label": "pass",
            "reason": "Cites a true but irrelevant gold node",
            "evidence": ["judge-material#/gold"],
        }
    )
    row = run_test(
        batch,
        test["id"],
        ScriptedModel([raw]),
        producer={
            "instance_id": "judge-check",
            "model": "fixture-judge",
            "reference": "synthetic-original",
        },
    )
    batch["judge_test_results"] = [row]
    store = EvidenceStore(tmp_path, decision_source=authority)
    store.import_batch(batch)
    sealed = review(batch, "judge_test", row["id"])
    # Blind opinion is sealed before the original judge raw/citations appear.
    assert raw not in json.dumps(sealed["input"])
    assert sealed["comparison_input"]["original"]["raw"] == raw
    compared = json.loads(sealed["comparison_output"])
    compared["judgment"]["support"] = "unsupported"
    compared["judgment"]["reason"] = (
        "The original gold citation does not establish delivery"
    )
    sealed["comparison_output"] = json.dumps(compared)
    sealed["comparison_output_sha256"] = hashlib.sha256(
        sealed["comparison_output"].encode()
    ).hexdigest()
    sealed = signed({k: v for k, v in sealed.items() if k != "id"})
    checked = store.revise(batch["batch_id"], "support-checked", reviews=[sealed])
    rulings = [
        authority.issue_decision(
            dispute_request(checked, d),
            subject="simulation:user",
            decision="confirmed_violation",
            reason="Synthetic confirmed missed prohibition",
            evidence=["judge-material#/answer"],
        )
        for d in disputes(checked)
    ]
    confirmed = store.revise(
        "support-checked", "confirmed", review_adjudications=rulings
    )
    checks = report(confirmed, store=store)["quality"]["judge_checks"]
    assert checks["verdict"] == "FAIL"
    assert "judge_missed_prohibition:" + test["id"] in checks["failures"]
    assert "judge_check_semantic_support_missing:" + test["id"] in checks["blockers"]
    assert confirmed["judge_test_results"][0]["raw"] == raw


def test_new_judge_invalidates_qualification_but_keeps_check_history(sampled, tmp_path):
    original, authority = sampled
    batch = deepcopy(original)
    test = batch["judge_tests"][0]
    row = run_test(
        batch,
        test["id"],
        ScriptedModel(
            [
                json.dumps(
                    {
                        "label": "fail",
                        "reason": "Synthetic control",
                        "evidence": ["judge-material#/answer"],
                    }
                )
            ]
        ),
        producer={
            "instance_id": "judge-check",
            "model": "fixture-judge",
            "reference": "synthetic",
        },
    )
    batch["judge_test_results"] = [row]
    store = EvidenceStore(tmp_path, decision_source=authority)
    store.import_batch(batch)
    profile = deepcopy(batch["judge_profile"])
    profile["model"]["model_id"] = "fixture-judge-next"
    profile = signed({k: v for k, v in profile.items() if k != "id"})
    revised = store.revise(
        batch["batch_id"], "new-judge", configuration={"judge_profile": profile}
    )
    assert revised["judge_test_results"] == [row]
    q = report(store.read("new-judge"), store=store)["quality"]
    assert q["user_approvals"]["materials"] is None
    assert "judge_check_result_missing:" + test["id"] in q["judge_checks"]["blockers"]
