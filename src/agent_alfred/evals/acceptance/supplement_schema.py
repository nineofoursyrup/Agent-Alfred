"""Closed schema4 material contract; validation is not approval or semantic proof."""

from copy import deepcopy

from .schema import GROUPS, digest, hash_value, identifier, unique

CONTRACT = "V1-ACCEPTANCE-OFFLINE-SUPPLEMENT-SPEC-r1"
DIMENSIONS = ("completion", "correctness", "selection")
SCENES = ("normal", "boundary", "failure_or_misleading")
EXTENSIONS = {
    "manifest",
    "seen_materials",
    "judge_tests",
    "judge_test_results",
    "summaries",
    "user_decisions",
}
CASE_FIELDS = {"obligations", "absent_categories", "scene", "lineage", "drafter"}


def closed(value, fields, error="invalid_supplement_fields"):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError(error)


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("supplement_text_required")


def signed(value):
    return {**deepcopy(value), "id": digest(value)}


def identity(value):
    if value.get("id") != digest({k: v for k, v in value.items() if k != "id"}):
        raise ValueError("supplement_identity_mismatch")


def actor(value):
    closed(value, "instance_id model reference")
    for entry in value.values():
        text(entry)


def material_id(case):
    return digest(
        {k: v for k, v in case.items() if k not in ("id", "material_id", "script")}
    )


def manifest(cases):
    return [{"case_id": c["id"], "case_sha256": digest(c)} for c in cases]


def content_hash(case):
    return digest({"input": case["input"], "gold": case["gold"]})


def review_policy():
    return signed(
        {
            "version": 2,
            "authority": "user-summary-decisions-v1",
            "independent_instance": True,
            "blind_before_comparison": True,
            "same_model_allowed": True,
            "agreement_is_proof": False,
        }
    )


def validate(batch):
    from . import semantic_rules
    from .judge_protocol import supplement_descriptor

    # Reuse the closed execution profile and setup contracts without reinterpreting
    # a legacy package. Only this validator knows the additional material fields.
    shape = {k: v for k, v in batch.items() if k not in EXTENSIONS}
    shape["cases"] = [
        {k: v for k, v in c.items() if k not in CASE_FIELDS} for c in batch["cases"]
    ]
    semantic_rules.validate_shapes(shape)
    if not EXTENSIONS <= set(batch):
        raise ValueError("supplement_materials_missing")
    if batch["review_policy"] != review_policy() or batch["rubric"] is not None:
        raise ValueError("supplement_policy_mismatch")
    if len(batch["profiles"]) != 1:
        raise ValueError("single_frozen_profile_required")
    rules = batch["semantic_rubric"]
    closed(
        rules,
        "id version scorer dimensions prohibitions review_policy_id "
        "judge_protocol_id approval",
    )
    identity(rules)
    if (
        rules["version"] != "semantic-rubric-v2"
        or rules["scorer"] != "obligation-labels-v1"
        or rules["review_policy_id"] != batch["review_policy"]["id"]
        or rules["judge_protocol_id"] != supplement_descriptor()["id"]
        or batch["judge_profile"]["protocol"] != supplement_descriptor()
        or rules["approval"] is not None
    ):
        raise ValueError("supplement_semantic_mismatch")
    closed(rules["dimensions"], "completion correctness selection")
    closed(rules["prohibitions"], "evidence_required")
    for value in [*rules["dimensions"].values(), *rules["prohibitions"].values()]:
        text(value)
    for key in ("case_set_approval", "calibration_approval"):
        if batch.get(key) is not None:
            raise ValueError("legacy_approval_in_schema4")
    if not isinstance(batch["seen_families"], list):
        raise ValueError("invalid_seen_families")
    if len(set(batch["seen_families"])) != len(batch["seen_families"]):
        raise ValueError("duplicate_identity")
    for family in batch["seen_families"]:
        text(family)
    if not isinstance(batch["seen_materials"], list):
        raise ValueError("invalid_seen_materials")
    for source in batch["seen_materials"]:
        closed(source, "family_id content_sha256 reference")
        text(source["family_id"])
        text(source["reference"])
        hash_value(source["content_sha256"])
    seen_hashes = {s["content_sha256"] for s in batch["seen_materials"]}
    seen_families = set(batch["seen_families"]) | {
        s["family_id"] for s in batch["seen_materials"]
    }
    contents = set()
    registered_families = set()
    for case in batch["cases"]:
        if not CASE_FIELDS <= set(case) or case["group"] not in GROUPS:
            raise ValueError("supplement_case_incomplete")
        if case["scene"] not in SCENES:
            raise ValueError("invalid_primary_scene")
        actor(case["drafter"])
        if not isinstance(case["lineage"], list) or not case["lineage"]:
            raise ValueError("source_lineage_required")
        for source in case["lineage"]:
            closed(source, "family_id content_sha256 reference")
            text(source["family_id"])
            text(source["reference"])
            hash_value(source["content_sha256"])
        families = {s["family_id"] for s in case["lineage"]} | {
            case["source_family_id"]
        }
        if batch["phase"] == "formal" and families & registered_families:
            raise ValueError("duplicate_formal_source_family")
        registered_families.update(families)
        content = content_hash(case)
        if content in contents:
            raise ValueError("duplicate_case_content")
        contents.add(content)
        if batch["phase"] == "formal" and (
            content in seen_hashes
            or families & seen_families
            or {s["content_sha256"] for s in case["lineage"]} & seen_hashes
        ):
            raise ValueError("seen_source_in_formal")
        if case["material_id"] != material_id(case):
            raise ValueError("material_identity_mismatch")
        obligations = case["obligations"]
        if not isinstance(obligations, list) or not obligations:
            raise ValueError("obligations_required")
        unique(obligations, "id")
        for item in obligations:
            closed(
                item,
                "id meaning dimension kind applies expected evidence_required check",
            )
            identifier(item["id"])
            for key in ("meaning", "expected", "evidence_required"):
                text(item[key])
            if (
                item["dimension"] not in DIMENSIONS
                or item["kind"] not in ("critical", "secondary")
                or type(item["applies"]) is not bool
                or item["check"] not in ("semantic", "exact_format")
                or item["applies"]
                and not case["applicability"][item["dimension"]]
            ):
                raise ValueError("invalid_obligation")
            if item["check"] == "exact_format":
                from .exact_format import contract

                if contract(case) is None:
                    raise ValueError("format_obligation_without_contract")
        for dimension, applies in case["applicability"].items():
            if applies != any(
                o["applies"] and o["dimension"] == dimension for o in obligations
            ):
                raise ValueError("obligation_applicability_mismatch")
        absent = {
            kind
            for kind in ("critical", "secondary")
            if not any(o["kind"] == kind and o["applies"] for o in obligations)
        }
        if (
            not isinstance(case["absent_categories"], dict)
            or set(case["absent_categories"]) != absent
        ):
            raise ValueError("missing_category_explanation")
        for reason in case["absent_categories"].values():
            text(reason)
    if batch["manifest"] != manifest(batch["cases"]):
        raise ValueError("frozen_manifest_mismatch")
    sizes = {"calibration": (2, 2, 1), "formal": (8, 8, 4)}
    for group in GROUPS:
        rows = [c for c in batch["cases"] if c["group"] == group]
        if not rows:
            raise ValueError("six_groups_required")
        if (
            batch["phase"] in sizes
            and tuple(sum(c["scene"] == s for c in rows) for s in SCENES)
            != sizes[batch["phase"]]
        ):
            raise ValueError("scenario_distribution_mismatch")
    for result in batch["results"]:
        allowed = set(
            "id batch_id case_id profile_id source sampled_at finished_at "
            "run_id outcome output error evidence tools recorded "
            "persisted_messages".split()
        )
        if set(result) - allowed:
            raise ValueError("unknown_product_result_field")
    validate_grades(batch)
    validate_aggregation(batch)
    from .supplement_decisions import validate_records
    from .supplement_judge import validate_tests
    from .supplement_reviews import validate_reviews

    validate_tests(batch)
    validate_reviews(batch)
    validate_records(batch)


def validate_aggregation(batch):
    policy = batch["aggregation_policy"]
    if policy is None:
        return
    closed(
        policy,
        "id version scorer semantic_rubric_id calibration_evidence_sha256 "
        "groups denominator unknown_policy forbidden_policy approval",
    )
    identity(policy)
    if (
        type(policy["version"]) is not int
        or policy["version"] != 2
        or policy["scorer"] != "case-fraction-v1"
        or policy["semantic_rubric_id"] != batch["semantic_rubric"]["id"]
        or policy["denominator"] != "all_predeclared_applicable"
        or policy["unknown_policy"] != "block"
        or policy["forbidden_policy"] != "zero_confirmed_violations"
        or policy["approval"] is not None
    ):
        raise ValueError("invalid_supplement_aggregation")
    hash_value(policy["calibration_evidence_sha256"])
    if set(policy["groups"]) != set(GROUPS):
        raise ValueError("invalid_aggregation_groups")
    for thresholds in policy["groups"].values():
        if set(thresholds) != set(DIMENSIONS) or any(
            type(v) not in (float, int) or not 0 <= v <= 1 for v in thresholds.values()
        ):
            raise ValueError("invalid_aggregation_threshold")


def validate_grades(batch):
    from .report import instant

    results = {r["id"]: r for r in batch["results"]}
    cases = {c["id"]: c for c in batch["cases"]}
    for grade in batch["grades"]:
        closed(
            grade,
            "id result_id judge_id result_hash case_hash case_material_id "
            "rubric_id status disputed suspected_safety dimensions "
            "prohibitions obligations raw attempt_ids error format_check "
            "judge_conflicts protocol_id catalog_id material_id producer "
            "started_at completed_at",
        )
        actor(grade["producer"])
        if grade["producer"]["model"] != batch["judge_profile"]["model"]["model_id"]:
            raise ValueError("judge_producer_mismatch")
        result = results.get(grade["result_id"])
        if result is None:
            raise ValueError("broken_grade_reference")
        if (
            not instant(result["finished_at"])
            <= instant(grade["started_at"])
            <= instant(grade["completed_at"])
        ):
            raise ValueError("judge_before_product_output")
        case = cases[result["case_id"]]
        if not isinstance(grade["obligations"], dict):
            raise ValueError("invalid_obligation_scores")
        if grade["status"] == "scored":
            from .score_evidence import parse_judge_output

            parsed = parse_judge_output(grade["raw"], case)
            if any(parsed[k] != grade[k] for k in parsed):
                raise ValueError("grade_raw_mismatch")
        elif grade["obligations"]:
            raise ValueError("error_grade_contains_scores")
