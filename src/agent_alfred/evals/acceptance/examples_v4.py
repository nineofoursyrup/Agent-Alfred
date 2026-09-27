"""Schema4 synthetic materials. No user approval, model call or release permission."""

from copy import deepcopy

from .examples_v3 import controlled_batch
from .judge_protocol import supplement_descriptor
from .schema import digest
from .supplement_schema import CONTRACT, manifest, material_id, review_policy, signed


def supplement_batch(candidate=None, *, phase="offline_fixture", prefix="supplement"):
    batch = controlled_batch(candidate)
    batch.update(
        schema_version=4,
        contract=CONTRACT,
        batch_id=prefix,
        phase=phase,
        review_policy=review_policy(),
        seen_materials=[],
        judge_tests=[],
        judge_test_results=[],
        summaries=[],
        user_decisions=[],
        review_disputes=[],
        review_adjudications=[],
    )
    judge = batch["judge_profile"]
    judge["protocol"] = supplement_descriptor()
    judge["id"] = digest({k: v for k, v in judge.items() if k != "id"})
    rules = batch["semantic_rubric"]
    rules.update(
        version="semantic-rubric-v2",
        scorer="obligation-labels-v1",
        review_policy_id=batch["review_policy"]["id"],
        judge_protocol_id=judge["protocol"]["id"],
        approval=None,
    )
    rules["id"] = digest({k: v for k, v in rules.items() if k != "id"})
    originals, cases = batch["cases"], []
    distribution = {
        "offline_fixture": (1, 0, 0),
        "calibration": (2, 2, 1),
        "formal": (8, 8, 4),
    }[phase]
    for original in originals:
        for scene, size in zip(
            ("normal", "boundary", "failure_or_misleading"), distribution
        ):
            for index in range(size):
                case = deepcopy(original)
                cid = f"{prefix}-{case['group']}-{scene}-{index}"
                case.update(
                    id=cid,
                    scene=scene,
                    source_family_id=cid,
                    drafter={
                        "instance_id": "synthetic-drafter",
                        "model": "fixture-judge",
                        "reference": "synthetic-material-author",
                    },
                )
                # Unique synthetic content for distribution validation; these are
                # mechanism fixtures, not semantically independent real gold.
                if phase != "offline_fixture":
                    case["input"] += " " + cid
                case["lineage"] = [
                    {
                        "family_id": cid,
                        "content_sha256": digest(case["input"]),
                        "reference": "synthetic:" + cid,
                    }
                ]
                case["obligations"] = [
                    {
                        "id": dimension,
                        "meaning": "Synthetic " + dimension + " requirement",
                        "dimension": dimension,
                        "kind": "critical",
                        "applies": applies,
                        "expected": "Satisfy the predeclared synthetic gold",
                        "evidence_required": (
                            "First product output and actual execution evidence"
                        ),
                        "check": "semantic",
                    }
                    for dimension, applies in case["applicability"].items()
                ]
                case["absent_categories"] = {
                    "secondary": (
                        "This small fixture declares only necessary task requirements."
                    )
                }
                case["material_id"] = material_id(case)
                cases.append(case)
    batch["cases"] = cases
    batch["manifest"] = manifest(cases)
    for mode, expected, kind in (
        ("missed_prohibition", "fail", "constructed_answer"),
        ("false_prohibition", "pass", "correct_control"),
        ("unsupported_citation", "unknown", "constructed_answer"),
    ):
        batch["judge_tests"].append(
            signed(
                {
                    "version": 1,
                    "kind": kind,
                    "task": "Synthetic judge check " + mode,
                    "gold": "Use only supplied facts, do not invent execution",
                    "answer": "Operation delivered"
                    if mode != "false_prohibition"
                    else "Delivery is unconfirmed",
                    "prohibition": "invented_execution",
                    "expected": expected,
                    "failure_mode": mode,
                    "source_family_id": "judge-check-" + mode,
                    "source": "synthetic-only",
                    "drafter": cases[0]["drafter"],
                    "semantic_rubric_id": rules["id"],
                }
            )
        )
    return batch


def demonstrate(output, *, candidate=None):
    """Save a disposable, explicitly synthetic public-path demonstration.

    Labels and opinions below are scripted test data, not judgments about product
    quality. A new directory is mandatory; no historical store is opened.
    """
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from agent_alfred.model import ScriptedModel

    from .judge import judge_result
    from .runner import run_offline
    from .simulation_authority import SimulationAuthority
    from .store import EvidenceStore
    from .supplement_decisions import decision_request, make_summary
    from .supplement_reviews import expected_items, make_review, target_for

    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    authority = SimulationAuthority(root / "synthetic-authority")
    store = EvidenceStore(root / "evidence", decision_source=authority)
    batch = supplement_batch(candidate)

    def synthetic_review(kind, target_id=None):
        target = target_for(batch, kind, target_id)
        ref = "materials#/cases/0/gold"
        if kind == "grade":
            grade = next(g for g in batch["grades"] if g["id"] == target_id)
            ref = "result:" + grade["result_id"] + "#/output"
        opinions = {
            key: {
                "status": status,
                "support": "supported",
                "reason": "Scripted synthetic review, not real quality evidence",
                "evidence": [ref],
            }
            for key, status in expected_items(batch, target).items()
        }
        instant = datetime.now(UTC).isoformat()
        return make_review(
            batch,
            target,
            reviewer={
                "instance_id": "synthetic-review-" + (target_id or kind),
                "model": "fixture-judge",
                "reference": "scripted-demo",
            },
            started_at=instant,
            completed_at=instant,
            compared_at=instant,
            output=json.dumps(opinions),
            comparison_output=json.dumps(opinions),
        )

    batch["review_disputes"] = [synthetic_review("materials")]
    summary = make_summary(batch, "materials")
    batch["summaries"] = [summary]
    batch["user_decisions"] = [
        authority.issue_decision(
            decision_request(summary),
            subject="simulation:demo-user",
            decision="approved",
            reason="Approve this synthetic mechanism fixture only",
        )
    ]
    (root / "prepared.json").write_text(json.dumps(batch, ensure_ascii=False, indent=2))
    batch = run_offline(batch, root / "runtime")
    store.import_batch(batch)
    batch["authorization"] = {"max_output_tokens": 2048}
    grades = []
    for index, (case, result) in enumerate(zip(batch["cases"], batch["results"])):
        item = {
            "status": "pass",
            "reason": "Scripted test label",
            "evidence": "result:" + result["id"] + "#/output",
        }
        value = {
            "dimensions": {
                d: {**item, "status": "pass" if a else "na"}
                for d, a in case["applicability"].items()
            },
            "obligations": {
                o["id"]: {**item, "status": "pass" if o["applies"] else "na"}
                for o in case["obligations"]
            },
            "prohibitions": {name: dict(item) for name in case["forbidden"]},
            "disputed": False,
            "suspected_safety": False,
        }
        if index == 0:
            value["obligations"]["completion"]["status"] = "fail"
        grades.append(
            judge_result(
                batch,
                case,
                result,
                ScriptedModel([json.dumps(value)]),
                producer={
                    "instance_id": "synthetic-original-" + str(index),
                    "model": "fixture-judge",
                    "reference": "scripted-demo",
                },
            )
        )
    batch = store.revise("supplement", "supplement-graded", grades=grades)
    reviews = [synthetic_review("grade", g["id"]) for g in grades]
    batch = store.revise(batch["batch_id"], "supplement-reviewed", reviews=reviews)
    summary = make_summary(batch, "results", decision_source=authority)
    batch = store.revise(
        batch["batch_id"],
        "supplement-summary",
        configuration={
            "summaries": batch["summaries"] + [summary],
        },
    )
    result = store.publish_report(batch["batch_id"])
    (root / "user-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(demonstrate(args.output), ensure_ascii=False, indent=2))
