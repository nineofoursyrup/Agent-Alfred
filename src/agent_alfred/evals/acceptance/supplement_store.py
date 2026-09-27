"""Schema4 revisions preserve original samples and append review/decision history."""

from copy import deepcopy

from .schema import digest, identifier
from .supplement_decisions import validate_chronology, validate_history

CONFIGURATION = {
    "cases",
    "manifest",
    "semantic_rubric",
    "judge_profile",
    "aggregation_policy",
    "calibration",
    "seen_materials",
    "seen_families",
    "judge_tests",
    "judge_test_results",
    "summaries",
    "user_decisions",
}


def revise(
    store,
    original,
    new_id,
    *,
    grades,
    adjudications,
    gates,
    execution,
    configuration,
    reviews,
    review_adjudications,
):
    revised = deepcopy(original)
    revised["batch_id"] = identifier(new_id)
    revised["parent"] = {
        "batch_id": original["batch_id"],
        "sha256": digest(original),
        "relation": "regrade",
    }
    if configuration is not None:
        if not isinstance(configuration, dict) or set(configuration) - CONFIGURATION:
            raise ValueError("invalid_regrade_configuration")
        revised.update(deepcopy(configuration))
        if any(
            original.get(k) != revised.get(k)
            for k in ("cases", "semantic_rubric", "judge_profile")
        ):
            revised["grades"] = []
        revised["authorization"] = None
    if grades is not None:
        if revised["grades"] and grades[: len(revised["grades"])] != revised["grades"]:
            raise ValueError("original_grades_replaced")
        revised["grades"] = deepcopy(grades)
    for key, rows in (
        ("adjudications", adjudications),
        ("gates", gates),
        ("review_disputes", reviews),
        ("review_adjudications", review_adjudications),
    ):
        revised.setdefault(key, []).extend(deepcopy(rows))
    if execution is not None:
        for key in (
            "authorization",
            "requests",
            "budget_started_at",
            "stop_reason",
            "budget_scope",
            "request_history",
        ):
            if key in execution:
                revised[key] = deepcopy(execution[key])
    store.import_batch(revised)
    return revised


def validate_links(batch, read_link, *, store):
    from .supplement_decisions import cutoff, instant
    from .supplement_reviews import active_reviews, disputes

    parent = read_link(batch["parent"]["batch_id"]) if batch["parent"] else None
    if parent:
        if digest(parent) != batch["parent"]["sha256"]:
            raise ValueError("parent_identity_mismatch")
        if parent["schema_version"] != 4:
            raise ValueError("regrade_schema_changed")
        if batch["parent"]["relation"] == "regrade":
            fields = (
                "results",
                "candidate",
                "profiles",
                "request_history",
                "budget_started_at",
                "budget_scope",
            )
            if any(batch.get(k) != parent.get(k) for k in fields):
                raise ValueError("regrade_changed_product_samples")
            previous = parent.get("requests", [])
            if batch.get("requests", [])[: len(previous)] != previous:
                raise ValueError("request_history_replaced")

            def frozen(cases):
                return [
                    {
                        k: c[k]
                        for k in (
                            "id",
                            "group",
                            "input",
                            "operation",
                            "setup",
                            "script",
                        )
                        if k in c
                    }
                    for c in cases
                ]

            if frozen(batch["cases"]) != frozen(parent["cases"]):
                raise ValueError("regrade_changed_product_samples")
            if parent["results"] and batch["phase"] != parent["phase"]:
                raise ValueError("regrade_phase_changed")
            same = all(
                batch.get(k) == parent.get(k)
                for k in ("cases", "semantic_rubric", "judge_profile")
            )
            if same and batch["grades"][: len(parent["grades"])] != parent["grades"]:
                raise ValueError("original_grades_replaced")
            if not same and any(g in parent["grades"] for g in batch["grades"]):
                raise ValueError("semantic_change_requires_regrade")
            if (
                parent["phase"] == "formal"
                and parent["results"]
                and batch["aggregation_policy"] != parent["aggregation_policy"]
            ):
                raise ValueError("aggregation_after_formal_sampling")
    validate_history(batch, parent)
    # Orphaned history is not a way to smuggle a fabricated old approval/review.
    active_ids = {r["id"] for r in active_reviews(batch)}
    prior_ids = (
        {r["id"] for r in parent.get("review_disputes", [])} if parent else set()
    )
    if any(
        r["id"] not in active_ids | prior_ids for r in batch.get("review_disputes", [])
    ):
        raise ValueError("review_target_mismatch")
    current_disputes = {d["id"] for d in disputes(batch)}
    for key in ("adjudications", "review_adjudications"):
        previous = parent.get(key, []) if parent else []
        if any(
            e["object_id"] not in current_disputes
            for e in batch.get(key, [])[len(previous) :]
        ):
            raise ValueError("dispute_target_missing")
    test_ids = {t["id"] for t in batch["judge_tests"]}
    previous_results = parent["judge_test_results"] if parent else []
    if any(
        (
            r["test_id"] not in test_ids
            or r["judge_profile_id"] != batch["judge_profile"]["id"]
            or r["semantic_rubric_id"] != batch["semantic_rubric"]["id"]
        )
        for r in batch["judge_test_results"][len(previous_results) :]
    ):
        raise ValueError("judge_check_target_missing")
    inherited = None
    ancestor = parent
    while ancestor:
        bound = cutoff(ancestor)
        if bound:
            inherited = bound if inherited is None else min(inherited, bound)
        link = ancestor["parent"]
        ancestor = (
            read_link(link["batch_id"])
            if link and link["relation"] == "regrade"
            else None
        )
    validate_chronology(batch, inherited.isoformat() if inherited else None)
    calibration = batch["calibration"]
    if calibration is None:
        return
    source = read_link(calibration["batch_id"])
    from .authorization_history import assess as authorization_status

    if authorization_status(source, store=store)["validity"] == "INVALID":
        raise ValueError("execution_authorization_invalid")
    if digest(source) != calibration["sha256"]:
        raise ValueError("calibration_identity_mismatch")
    if source["schema_version"] != 4 or source["phase"] != "calibration":
        raise ValueError("supplement_calibration_required")
    if any(
        source[k] != batch[k]
        for k in (
            "candidate",
            "profiles",
            "judge_profile",
            "semantic_rubric",
            "review_policy",
            "simulation",
        )
    ):
        raise ValueError("supplement_requires_recalibration")
    from .supplement_schema import content_hash

    source_hashes = {content_hash(c) for c in source["cases"]}
    source_hashes |= {
        s["content_sha256"] for c in source["cases"] for s in c["lineage"]
    }
    source_hashes |= {
        digest({"input": t["task"], "gold": t["gold"]}) for t in source["judge_tests"]
    }
    if source_hashes & (
        {content_hash(c) for c in batch["cases"]}
        | {s["content_sha256"] for c in batch["cases"] for s in c["lineage"]}
    ):
        raise ValueError("calibration_overlap")
    source_families = {c["source_family_id"] for c in source["cases"]}
    source_families |= {s["family_id"] for c in source["cases"] for s in c["lineage"]}
    source_families |= {t["source_family_id"] for t in source["judge_tests"]}
    target_families = {c["source_family_id"] for c in batch["cases"]}
    target_families |= {s["family_id"] for c in batch["cases"] for s in c["lineage"]}
    if source_families & target_families:
        raise ValueError("calibration_family_overlap")
    if not source_families <= set(batch["seen_families"]):
        raise ValueError("calibration_families_not_registered")
    if set(calibration["material_ids"]) != {c["material_id"] for c in source["cases"]}:
        raise ValueError("calibration_material_mismatch")
    from .schema import calibration_identity

    policy = batch["aggregation_policy"]
    if policy and policy["calibration_evidence_sha256"] != calibration_identity(source):
        raise ValueError("aggregation_calibration_mismatch")
    first = cutoff(batch, inherited.isoformat() if inherited else None)
    summaries = {s["id"]: s for s in source["summaries"]}
    calibrated = [
        instant(e["at"])
        for e in source["user_decisions"]
        if summaries[e["object_id"]]["stage"] == "calibration"
    ]
    if first and any(t > first for t in calibrated):
        raise ValueError("calibration_approval_after_execution")
    summaries = {s["id"]: s for s in batch["summaries"]}
    for event in batch["user_decisions"]:
        if summaries[event["object_id"]]["stage"] == "thresholds":
            at = instant(event["at"])
            if first and at > first:
                raise ValueError("aggregation_approval_after_execution")
            if calibrated and at < max(calibrated):
                raise ValueError("aggregation_approval_before_calibration")
    if batch["phase"] == "formal":
        from .supplement_decisions import make_summary

        def current_times(value, stage):
            current = make_summary(
                value, stage, decision_source=store.decision_source
            )["id"]
            return [
                instant(e["at"])
                for e in value["user_decisions"]
                if e["object_id"] == current and e["decision"] == "approved"
            ]

        frozen = current_times(batch, "materials")
        prerequisites = current_times(source, "calibration") + current_times(
            batch, "thresholds"
        )
        if frozen and prerequisites and max(frozen) < max(prerequisites):
            raise ValueError("formal_materials_frozen_before_prerequisites")
    return source
