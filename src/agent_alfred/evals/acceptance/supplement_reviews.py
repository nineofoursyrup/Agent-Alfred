"""Sealed blind reviews and persistent, content-bound disagreements for schema4."""

import hashlib
import json

from .exact_format import unique_object
from .schema import digest, unique
from .supplement_schema import actor, closed, identity, signed, text


def materials(batch):
    return {
        key: batch[key]
        for key in (
            "manifest",
            "cases",
            "semantic_rubric",
            "judge_tests",
            "seen_materials",
            "seen_families",
            "judge_profile",
            "review_policy",
        )
    }


def targets(batch):
    material = materials(batch)
    rows = {("materials", digest(material)): material}
    rows.update({("grade", g["id"]): g for g in batch["grades"]})
    tests = {t["id"]: t for t in batch["judge_tests"]}
    rows.update(
        {
            ("judge_test", r["id"]): r
            for r in batch["judge_test_results"]
            if r["judge_profile_id"] == batch["judge_profile"]["id"]
            and r["semantic_rubric_id"] == batch["semantic_rubric"]["id"]
            and r["test_id"] in tests
            and r["test_sha256"] == digest(tests[r["test_id"]])
        }
    )
    return rows


def target_for(batch, kind, target_id=None):
    target_id = digest(materials(batch)) if kind == "materials" else target_id
    value = targets(batch).get((kind, target_id))
    if value is None:
        raise ValueError("review_target_missing")
    return {"type": kind, "id": target_id, "sha256": digest(value)}


def blind_input(batch, target):
    value = targets(batch).get((target["type"], target["id"]))
    if value is None or digest(value) != target["sha256"]:
        raise ValueError("review_target_mismatch")
    if target["type"] == "materials":
        return value
    if target["type"] == "grade":
        from .judge_protocol import material

        result = next(r for r in batch["results"] if r["id"] == value["result_id"])
        case = next(c for c in batch["cases"] if c["id"] == result["case_id"])
        sources, refs = material(batch, case, result)
        return {"sources": sources, "reference_catalog": refs}
    from .supplement_judge import test_input

    case = next(t for t in batch["judge_tests"] if t["id"] == value["test_id"])
    return test_input(batch, case)


def expected_items(batch, target):
    if target["type"] == "materials":
        return {
            "coverage": "pass",
            "classification": "pass",
            "source_independence": "pass",
        }
    record = targets(batch)[(target["type"], target["id"])]
    if target["type"] == "judge_test":
        return {"judgment": (record["parsed"] or {}).get("label", "unknown")}
    return {
        kind + ":" + name: item["status"]
        for kind in ("obligations", "dimensions", "prohibitions")
        for name, item in record[kind].items()
    }


def parse_review(raw, expected):
    output = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(output, dict) or set(output) != set(expected):
        raise ValueError("review_coverage_mismatch")
    for row in output.values():
        closed(row, "status support reason evidence")
        if row["status"] not in ("pass", "fail", "unknown", "na") or row[
            "support"
        ] not in ("supported", "unsupported", "unknown"):
            raise ValueError("invalid_review_opinion")
        text(row["reason"])
        if not isinstance(row["evidence"], list) or not row["evidence"]:
            raise ValueError("review_evidence_required")
        for ref in row["evidence"]:
            text(ref)
    return output


def make_review(
    batch,
    target,
    *,
    reviewer,
    started_at,
    completed_at,
    compared_at,
    output,
    comparison_output,
):
    inputs = blind_input(batch, target)
    compared = comparison_input(batch, target, output)
    return signed(
        {
            "version": 1,
            "kind": "independent_content_review",
            "target": target,
            "actor": reviewer,
            "started_at": started_at,
            "completed_at": completed_at,
            "compared_at": compared_at,
            "input": inputs,
            "input_sha256": digest(inputs),
            "output": output,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "comparison_input": compared,
            "comparison_input_sha256": digest(compared),
            "comparison_output": comparison_output,
            "comparison_output_sha256": hashlib.sha256(
                comparison_output.encode()
            ).hexdigest(),
        }
    )


def comparison_input(batch, target, blind_output):
    """Only exposed after the reviewer has sealed their independent opinion."""
    return {
        "original": targets(batch)[(target["type"], target["id"])],
        "blind_output": blind_output,
        "materials": blind_input(batch, target),
    }


def active_reviews(batch):
    current = targets(batch)
    return [
        r
        for r in batch.get("review_disputes", [])
        if (r["target"]["type"], r["target"]["id"]) in current
        and digest(current[(r["target"]["type"], r["target"]["id"])])
        == r["target"]["sha256"]
    ]


def validate_reviews(batch):
    from .report import instant

    records = batch.get("review_disputes", [])
    if not isinstance(records, list):
        raise ValueError("invalid_review_collection")
    unique(records, "id")
    active = active_reviews(batch)
    for review in records:
        closed(
            review,
            "id version kind target actor started_at completed_at compared_at "
            "input input_sha256 output output_sha256 comparison_input "
            "comparison_input_sha256 comparison_output comparison_output_sha256",
        )
        identity(review)
        closed(review["target"], "type id sha256")
        if (
            review["version"] != 1
            or type(review["version"]) is not int
            or review["kind"] != "independent_content_review"
        ):
            raise ValueError("unknown_review_version")
        if review["target"]["type"] not in ("materials", "grade", "judge_test"):
            raise ValueError("invalid_review_target")
        actor(review["actor"])
        if (
            digest(review["input"]) != review["input_sha256"]
            or hashlib.sha256(review["output"].encode()).hexdigest()
            != review["output_sha256"]
        ):
            raise ValueError("blind_seal_mismatch")
        if (
            digest(review["comparison_input"]) != review["comparison_input_sha256"]
            or hashlib.sha256(review["comparison_output"].encode()).hexdigest()
            != review["comparison_output_sha256"]
        ):
            raise ValueError("comparison_seal_mismatch")
        if (
            not instant(review["started_at"])
            <= instant(review["completed_at"])
            <= instant(review["compared_at"])
        ):
            raise ValueError("invalid_blind_chronology")
        if review not in active:
            continue
        target = review["target"]
        if review["input"] != blind_input(batch, target):
            raise ValueError("blind_input_mismatch")
        value = targets(batch)[(target["type"], target["id"])]
        if target["type"] == "materials":
            original_actors = {c["drafter"]["instance_id"] for c in batch["cases"]}
            original_actors |= {
                t["drafter"]["instance_id"] for t in batch["judge_tests"]
            }
            first_time = None
        else:
            original_actors = {value["producer"]["instance_id"]}
            first_time = value["completed_at"]
        if review["actor"]["instance_id"] in original_actors:
            raise ValueError("review_instance_not_independent")
        if first_time and instant(review["started_at"]) < instant(first_time):
            raise ValueError("blind_review_before_output")
        if review["comparison_input"] != comparison_input(
            batch, target, review["output"]
        ):
            raise ValueError("comparison_input_mismatch")
        blind = parse_review(review["output"], expected_items(batch, target))
        opinions = parse_review(
            review["comparison_output"], expected_items(batch, target)
        )
        if any(opinions[k]["status"] != blind[k]["status"] for k in blind):
            raise ValueError("blind_opinion_rewritten")
        for opinion in [*blind.values(), *opinions.values()]:
            from .score_evidence import resolve_reference

            sources = review["input"].get("sources", {"materials": review["input"]})
            for ref in opinion["evidence"]:
                resolve_reference(ref, sources)


def disputes(batch, *, materials_only=False, _include_blind=True):
    rows = []
    for review in active_reviews(batch):
        target = review["target"]
        if materials_only and target["type"] != "materials":
            continue
        original = expected_items(batch, target)
        opinions = parse_review(review["comparison_output"], original)
        blind = parse_review(review["output"], original)
        observations = [(item, opinion, False) for item, opinion in opinions.items()]
        # Keep existing comparison dispute identities. A distinct blind concern
        # is additional history; later support cannot silently adjudicate it.
        observations += [
            (item, opinion, True)
            for item, opinion in blind.items()
            if _include_blind and opinion != opinions[item]
        ]
        for item, opinion, is_blind in observations:
            if (
                opinion["status"] != original[item]
                or opinion["support"] != "supported"
                or opinion["status"] == "unknown"
            ):
                rows.append(
                    signed(
                        {
                            "kind": "review_disagreement",
                            "target": target,
                            "item": item,
                            "original": original[item],
                            "opinion": opinion,
                            "review_id": review["id"],
                            "at": review[
                                "completed_at" if is_blind else "compared_at"
                            ],
                            **({"stage": "blind"} if is_blind else {}),
                        }
                    )
                )
    if materials_only:
        return rows
    for grade in batch["grades"]:
        for kind in ("obligations", "prohibitions"):
            for name, item in grade[kind].items():
                if (
                    item["status"] in ("unknown", "fail")
                    or grade["disputed"]
                    or grade["suspected_safety"]
                ):
                    rows.append(
                        signed(
                            {
                                "kind": "judge_concern",
                                "target": target_for(batch, "grade", grade["id"]),
                                "item": kind + ":" + name,
                                "original": item["status"],
                                "opinion": item,
                                "review_id": None,
                                "at": grade["completed_at"],
                            }
                        )
                    )
    tests = {t["id"]: t for t in batch["judge_tests"]}
    current_targets = targets(batch)
    for result in batch["judge_test_results"]:
        test = tests.get(result["test_id"])
        if test is None or ("judge_test", result["id"]) not in current_targets:
            continue
        actual = (result["parsed"] or {}).get("label", "unknown")
        if actual != test["expected"] or result["parsed"] is None:
            rows.append(
                signed(
                    {
                        "kind": "judge_check_mismatch",
                        "target": target_for(batch, "judge_test", result["id"]),
                        "item": "judgment",
                        "original": actual,
                        "opinion": {
                            "status": test["expected"],
                            "reason": "preapproved constructed expectation",
                            "evidence": ["judge-material#/answer"],
                        },
                        "review_id": None,
                        "at": result["completed_at"],
                    }
                )
            )
    from .exact_format import check, conflicts

    for grade in batch["grades"]:
        result = next(r for r in batch["results"] if r["id"] == grade["result_id"])
        case = next(c for c in batch["cases"] if c["id"] == result["case_id"])
        fact = check(case, result)
        for dimension in conflicts(fact, grade):
            rows.append(
                signed(
                    {
                        "kind": "mechanical_contradiction",
                        "target": target_for(batch, "grade", grade["id"]),
                        "item": "dimensions:" + dimension,
                        "original": grade["dimensions"][dimension]["status"],
                        "opinion": fact,
                        "review_id": None,
                        "at": grade["completed_at"],
                    }
                )
            )
    return rows


def assess_disputes(batch, source, now, *, _include_blind=True):
    from .supplement_decisions import dispute_request, instant, verified

    decisions = batch["adjudications"] + batch.get("review_adjudications", [])
    rows = []
    for dispute in disputes(batch, _include_blind=_include_blind):
        request = dispute_request(batch, dispute)
        events = [
            e
            for e in decisions
            if all(e[k] == v for k, v in request.items())
            and verified(e, batch, source, now)
        ]
        latest = None
        if events:
            at = max(instant(e["at"]) for e in events)
            last = [e for e in events if instant(e["at"]) == at]
            # Equal timestamps are legal, but contradictory decisions do not
            # authorize selecting whichever outcome is most favorable.
            if len({e["decision"] for e in last}) == 1:
                latest = last[-1]
        status = latest["decision"] if latest else "pending"
        if status == "insufficient_evidence":
            status = "pending"
        rows.append({**dispute, "status": status, "decision": latest})
    return rows
