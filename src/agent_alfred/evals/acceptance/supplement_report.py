"""Shared report/admission rules for schema4 calibration eligibility."""

from .supplement_decisions import approval


def material_blockers(batch, now, source):
    from .supplement_reviews import active_reviews, assess_disputes

    blockers = []
    if not approval(batch, "materials", source, now):
        blockers.append("user_materials_approval_missing")
    if not any(r["target"]["type"] == "materials" for r in active_reviews(batch)):
        blockers.append("independent_material_review_missing")
    if any(
        d["target"]["type"] == "materials" and d["status"] != "dismissed"
        for d in assess_disputes(batch, source, now)
    ):
        blockers.append("material_adjudication_required")
    return blockers


def calibration_blockers(calibration, now, source):
    from .supplement_quality import quality

    assessed = quality(calibration, now, source)
    blockers = []
    if not approval(calibration, "calibration", source, now):
        blockers.append("calibration_quality_approval_missing")
    if assessed["judge_checks"]["verdict"] != "PASS":
        blockers.append("judge_not_calibrated")
    if any(
        approval(calibration, stage, source, now) is None
        for stage in ("materials", "results")
    ):
        blockers.append("calibration_user_approval_missing")
    if len(calibration["results"]) != len(calibration["cases"]) or len(
        calibration["grades"]
    ) != len(calibration["cases"]):
        blockers.append("calibration_incomplete")
    if any(d["status"] == "pending" for d in assessed["review_disputes"]):
        blockers.append("calibration_adjudication_required")
    # Calibration may establish product failures; it may not omit review/facts.
    # Numeric product thresholds are determined AFTER calibration.
    excluded = {"aggregation_policy_missing", "user_thresholds_approval_missing"}
    blockers.extend(
        "calibration_evidence:" + b for b in assessed["blockers"] if b not in excluded
    )
    return blockers


def release_blockers(batch, now, store, source):
    blockers = []
    if store is None:
        blockers.append("supplement_sources_not_verified")
    else:
        store._validate_links(batch, ())
    if batch["phase"] == "formal":
        if store is None or batch["calibration"] is None:
            blockers.append("supplement_calibration_missing")
        else:
            calibration = store.read(batch["calibration"]["batch_id"])
            blockers.extend(calibration_blockers(calibration, now, source))
    return blockers


def validate_execution(batch, store, started_at):
    from .supplement_decisions import instant, validate_chronology

    if started_at is None:
        raise ValueError("execution_start_required")
    now = instant(started_at)
    validate_chronology(batch, started_at)
    if material_blockers(batch, now, store.decision_source):
        raise ValueError("supplement_materials_not_ready")
    if batch["phase"] != "formal":
        return
    if batch["aggregation_policy"] is None or not approval(
        batch, "thresholds", store.decision_source, now
    ):
        raise ValueError("approved_aggregation_missing")
    if not batch["calibration"]:
        raise ValueError("supplement_calibration_missing")
    calibration = store.read(batch["calibration"]["batch_id"])
    if calibration_blockers(calibration, now, store.decision_source):
        raise ValueError("supplement_calibration_not_ready")
