"""External review is an append-only opinion, never a judge or human decision."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.schema import digest, judge_identity
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance import command, scored_fixture


def opinion(batch, index=0):
    case, result, grade = (
        batch[collection][index] for collection in ("cases", "results", "grades")
    )
    content = "The cited output alone does not establish absence of external actions."
    record = {
        "version": 1,
        "kind": "agent_review_dispute",
        "binding": {
            "batch_id": batch["batch_id"],
            "batch_sha256": digest(batch),
            "candidate_id": batch["candidate_id"],
            "contract": batch["contract"],
            "case_id": case["id"],
            "case_sha256": digest(case),
            "result_id": result["id"],
            "result_sha256": digest(result),
            "grade_id": grade["id"],
            "grade_sha256": digest(grade),
            "rubric_id": batch["rubric"]["id"],
            "judge_id": judge_identity(batch, batch["profiles"][0]),
            "judge_profile_sha256": digest(batch.get("judge_profile")),
        },
        "item": {"kind": "prohibitions", "name": "invented_execution"},
        "reason": content,
        "evidence": ["result:" + result["id"] + "#/output"],
        "at": "2026-09-23T00:00:00+00:00",
        "source": {
            "kind": "agent",
            "name": "Codex fixture reviewer",
            "reference": "synthetic-review-v1",
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
    }
    return {"id": digest(record), **record}


def revision(batch, *reviews):
    revised = deepcopy(batch)
    revised["batch_id"] = "reviewed"
    revised["parent"] = {
        "batch_id": batch["batch_id"],
        "sha256": digest(batch),
        "relation": "regrade",
    }
    revised["review_disputes"] = list(reviews)
    return revised


def test_public_import_reports_external_dispute_without_changing_judge(tmp_path):
    batch = scored_fixture()
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    reviewed = revision(batch, opinion(batch))
    source = tmp_path / "reviewed.json"
    source.write_text(json.dumps(reviewed))
    imported = command("import", "--store", store.root, "--input", source)
    assert imported.returncode == 2
    summary = json.loads(imported.stdout)
    assert "adjudication_required:conversation" in summary["quality"]["blockers"]
    assert not summary["quality"]["failures"]
    assert EvidenceStore(store.root).read("reviewed")["grades"] == batch["grades"]
    assert store.read(batch["batch_id"]) == batch


def test_public_review_import_survives_revision_and_process_restart(tmp_path):
    batch = scored_fixture()
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    source = tmp_path / "reviews.json"
    review = opinion(batch)
    source.write_text(json.dumps([review]))
    result = command(
        "import-reviews",
        "--store",
        store.root,
        "--batch",
        batch["batch_id"],
        "--new-batch",
        "reviewed",
        "--input",
        source,
    )
    assert "adjudication_required:conversation" in result.stdout
    store.revise("reviewed", "inherited")
    for action in ("report", "verify"):
        result = command(action, "--store", store.root, "--batch", "inherited")
        summary = json.loads(result.stdout)
        assert result.returncode == 2
        assert "adjudication_required:conversation" in summary["v1_release"]["blockers"]
    assert EvidenceStore(store.root).read("inherited")["review_disputes"] == [review]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["binding"].update(batch_sha256="0" * 64),
        lambda r: r["binding"].update(batch_id="missing"),
        lambda r: r["binding"].update(case_id="memory"),
        lambda r: r["binding"].update(case_sha256="0" * 64),
        lambda r: r["binding"].update(result_id="memory"),
        lambda r: r["binding"].update(result_sha256="0" * 64),
        lambda r: r["binding"].update(grade_id="grade-memory"),
        lambda r: r["binding"].update(grade_sha256="0" * 64),
        lambda r: r["binding"].update(candidate_id="0" * 64),
        lambda r: r["binding"].update(rubric_id="0" * 64),
        lambda r: r["binding"].update(judge_id="0" * 64),
        lambda r: r["binding"].update(contract="other"),
        lambda r: r["item"].update(name="missing"),
        lambda r: r.update(evidence=["result:memory#/output"]),
        lambda r: r.update(evidence=["result:conversation#/missing"]),
        lambda r: r.update(evidence=[]),
        lambda r: r["source"].update(kind="human"),
        lambda r: r["source"].update(sha256="0" * 64),
        lambda r: r.update(reason=" "),
        lambda r: r.update(at="2026-09-23"),
        lambda r: r.update(version=99),
    ],
)
def test_import_rejects_broken_cross_case_or_drifted_review(tmp_path, mutation):
    batch = scored_fixture()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    review = opinion(batch)
    mutation(review)
    review["id"] = digest({k: v for k, v in review.items() if k != "id"})
    with pytest.raises(ValueError):
        store.import_batch(revision(batch, review))


def test_review_history_cannot_be_dropped_edited_or_duplicated(tmp_path):
    batch = scored_fixture()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    review = opinion(batch)
    store.import_batch(revision(batch, review))
    for replacement in ([], [review, review], [{**review, "reason": "changed"}]):
        child = revision(store.read("reviewed"))
        child["batch_id"] = "tampered"
        child["review_disputes"] = replacement
        with pytest.raises(ValueError):
            store.import_batch(child)
    duplicate = deepcopy(review)
    duplicate["at"] = "2026-09-23T01:00:00+00:00"
    duplicate["id"] = digest({k: v for k, v in duplicate.items() if k != "id"})
    with pytest.raises(ValueError):
        store.revise("reviewed", "duplicate", reviews=[duplicate])


def test_review_pins_rules_and_original_grade_during_regrade(tmp_path):
    batch = scored_fixture()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    store.import_batch(revision(batch, opinion(batch)))
    with pytest.raises(ValueError):
        store.revise("reviewed", "no-grades", grades=[])
    changed = deepcopy(batch["rubric"])
    changed["version"] = "changed"
    changed["id"] = digest({k: v for k, v in changed.items() if k != "id"})
    with pytest.raises(ValueError):
        store.revise("reviewed", "rules-changed", configuration={"rubric": changed})


def human_fixture(review, outcome="dismissed"):
    record = {
        "version": 1,
        "kind": "human_review_adjudication",
        "review_id": review["id"],
        "human": "OFFLINE TEST HUMAN - not a real user ruling",
        "at": "2026-09-23T01:00:00+00:00",
        "reason": "Synthetic fixture for explicit human disposition only.",
        "outcome": outcome,
        "evidence": review["evidence"],
    }
    return {"id": digest(record), **record}


@pytest.mark.parametrize("outcome", ["dismissed", "confirmed_violation"])
def test_only_explicit_human_import_resolves_review_and_preserves_scores(
    tmp_path,
    outcome,
):
    batch = scored_fixture()
    # An already established failure must survive dismissal of another concern.
    batch["grades"][1]["prohibitions"]["invented_execution"]["status"] = "fail"
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    reviews = [opinion(batch), opinion(batch, 1)]
    store.import_batch(revision(batch, *reviews))
    ruling = human_fixture(reviews[0], outcome)
    source = tmp_path / "human-fixture.json"
    source.write_text(json.dumps([ruling]))
    result = command(
        "adjudicate-reviews",
        "--store",
        store.root,
        "--batch",
        "reviewed",
        "--new-batch",
        "adjudicated",
        "--input",
        source,
    )
    assert result.returncode == 2
    summary = json.loads(result.stdout)
    assert "adjudication_required:conversation" not in summary["quality"]["blockers"]
    assert "adjudication_required:memory" in summary["quality"]["blockers"]
    assert (
        "prohibition_failed:memory:invented_execution" in summary["quality"]["failures"]
    )
    if outcome == "confirmed_violation":
        assert "review_violation:" + reviews[0]["id"] in summary["quality"]["failures"]
    current = EvidenceStore(store.root).read("adjudicated")
    assert current["grades"] == batch["grades"]
    assert current["review_disputes"] == reviews
    assert current["review_adjudications"] == [ruling]
    assert current["adjudications"] == []
    assert store.read("reviewed").get("review_adjudications", []) == []
    store.revise("adjudicated", "inherited-ruling")
    assert store.read("inherited-ruling")["review_adjudications"] == [ruling]
    with pytest.raises(ValueError):
        store.revise("adjudicated", "duplicate-ruling", review_adjudications=[ruling])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(review_id="0" * 64),
        lambda r: r.update(kind="agent_review_dispute"),
        lambda r: r.update(human=""),
        lambda r: r.update(at="2026-09-22T00:00:00+00:00"),
        lambda r: r.update(outcome="auto_pass"),
        lambda r: r.update(evidence=["result:memory#/output"]),
    ],
)
def test_invalid_human_resolution_cannot_release_dispute(tmp_path, mutation):
    batch = scored_fixture()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    review = opinion(batch)
    store.import_batch(revision(batch, review))
    ruling = human_fixture(review)
    mutation(ruling)
    ruling["id"] = digest({k: v for k, v in ruling.items() if k != "id"})
    with pytest.raises(ValueError):
        store.revise("reviewed", "invalid-human", review_adjudications=[ruling])


def test_future_ruling_and_unverified_origin_remain_blocked(tmp_path):
    from agent_alfred.evals.acceptance.report import report

    batch = scored_fixture()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    review = opinion(batch)
    reviewed = revision(batch, review)
    store.import_batch(reviewed)
    store.revise("reviewed", "future", review_adjudications=[human_fixture(review)])
    summary = store.publish_report("future", now=datetime(2026, 9, 23, tzinfo=UTC))
    assert "adjudication_required:conversation" in summary["quality"]["blockers"]
    assert "review_source_not_verified" in report(reviewed)["v1_release"]["blockers"]


def test_legacy_adjudication_does_not_silently_resolve_external_review(tmp_path):
    batch = scored_fixture()
    grade = batch["grades"][0]
    batch["adjudications"] = [
        {
            "id": "old-ruling",
            "grade_id": grade["id"],
            "human": "test human",
            "at": "2026-09-21T00:00:00+00:00",
            "reason": "old fixture ruling",
            "rubric_id": batch["rubric"]["id"],
            "dimensions": deepcopy(grade["dimensions"]),
            "prohibitions": deepcopy(grade["prohibitions"]),
        }
    ]
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    store.import_batch(revision(batch, opinion(batch)))
    assert (
        "adjudication_required:conversation"
        in store.publish_report("reviewed")["quality"]["blockers"]
    )


def test_calibration_cannot_reuse_unresolved_external_review(tmp_path):
    from agent_alfred.evals.acceptance.schema import calibration_identity
    from agent_alfred.evals.deterministic.test_acceptance_review import sized_fixture

    store = EvidenceStore(tmp_path)
    calibration = sized_fixture("calibration", 5, "calibration")
    store.import_batch(calibration)
    reviewed = revision(calibration, opinion(calibration))
    reviewed["calibration_approval"] = {
        "kind": "test",
        "by": "fixture",
        "at": "2026-09-23T03:00:00+00:00",
        "reference": "fixture only",
        "evidence_sha256": calibration_identity(reviewed),
    }
    store.import_batch(reviewed)
    formal = sized_fixture("formal", 20, "formal")
    formal["calibration"] = {
        "batch_id": "reviewed",
        "sha256": digest(reviewed),
        "material_ids": [c["material_id"] for c in reviewed["cases"]],
    }
    with pytest.raises(ValueError, match="calibration_adjudication_required"):
        store.import_batch(formal)
    without_reviews = deepcopy(reviewed)
    without_reviews.pop("review_disputes")
    assert calibration_identity(without_reviews) != calibration_identity(reviewed)


def test_review_rejects_judge_profile_drift_and_pre_sample_time(tmp_path):
    batch = scored_fixture()
    model = batch["profiles"][0]["judge_model"]
    batch["judge_profile"] = {"model": model, "parameters": {"temperature": 0}}
    batch["judge_profile"]["id"] = digest(batch["judge_profile"])
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    reviewed = revision(batch, opinion(batch))
    changed = deepcopy(reviewed)
    profile = changed["judge_profile"]
    profile["parameters"]["temperature"] = 1
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    with pytest.raises(ValueError, match="review_binding_mismatch"):
        store.import_batch(changed)
    review = opinion(batch)
    review["at"] = "2026-09-19T00:00:00+00:00"
    review["id"] = digest({k: v for k, v in review.items() if k != "id"})
    with pytest.raises(ValueError):
        store.import_batch(revision(batch, review))


def test_sibling_source_and_lost_original_are_not_valid_provenance(tmp_path):
    batch = scored_fixture()
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    sibling = store.revise(batch["batch_id"], "sibling")
    with pytest.raises(ValueError):
        store.import_batch(revision(batch, opinion(sibling)))
    store.import_batch(revision(batch, opinion(batch)))
    (tmp_path / batch["batch_id"] / "batch.json").unlink()
    verified = command("verify", "--store", tmp_path, "--batch", "reviewed")
    assert verified.returncode == 2
    assert "missing_or_incomplete_batch" in verified.stdout


@pytest.mark.parametrize("location", ["review", "ruling"])
def test_public_report_rejects_sensitive_review_text(monkeypatch, location):
    from agent_alfred.evals.acceptance.report import report

    marker = "offline-review-secret-marker-only"
    monkeypatch.setenv("OFFLINE_REVIEW_TEST_SECRET", marker)
    batch = scored_fixture()
    review = opinion(batch)
    if location == "review":
        review["reason"] = marker
        review["id"] = digest({k: v for k, v in review.items() if k != "id"})
    revised = revision(batch, review)
    if location == "ruling":
        ruling = human_fixture(review)
        ruling["reason"] = marker
        ruling["id"] = digest({k: v for k, v in ruling.items() if k != "id"})
        revised["review_adjudications"] = [ruling]
    with pytest.raises(ValueError, match="sensitive_data"):
        report(revised)


def test_deep_review_revision_chain_reopens_within_bounded_time(tmp_path):
    code = """
import json, sys
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.deterministic.test_acceptance import scored_fixture
from agent_alfred.evals.deterministic.test_acceptance_disputes import opinion
store=EvidenceStore(sys.argv[1])
original=scored_fixture()
store.import_batch(original)
parent=original['batch_id']
for index in range(16):
    review=opinion(original)
    review['source']['reference']=f'independent-review-{index}'
    review['id']=digest({k:v for k,v in review.items() if k!='id'})
    current=f'review-{index}'
    store.revise(parent,current,reviews=[review]);parent=current
reopened=EvidenceStore(sys.argv[1]);report=reopened.publish_report(parent)
assert len(report['quality']['review_disputes'])==16
assert 'adjudication_required:conversation' in report['quality']['blockers']
reopened.delete(original['batch_id'])
try: reopened.read(parent)
except ValueError as error: assert str(error)=='deleted_batch'
else: raise AssertionError('A new read must detect deletion')
print('16 revisions reread; original deletion detected')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "evidence")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "original deletion detected" in completed.stdout
