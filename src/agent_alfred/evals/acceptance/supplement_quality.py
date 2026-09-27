"""Independent critical gates and fixed case denominators for the new contract."""

from datetime import timedelta

from .schema import GROUPS
from .supplement_decisions import approval, instant
from .supplement_reviews import (
    active_reviews,
    assess_disputes,
    expected_items,
    parse_review,
)


def quality(
    batch,
    now,
    source=None,
    *,
    summary_mode=False,
    calibration=None,
    _legacy_summary=False,
):
    from .exact_format import check
    from .report import axis

    failures, blockers, counts, details = [], [], {}, []
    legacy = summary_mode and _legacy_summary
    decisions = {
        stage: None
        if summary_mode and (legacy or stage in ("results", "calibration"))
        else approval(batch, stage, source, now, _calibration=calibration)
        for stage in ("materials", "results", "calibration", "thresholds")
    }
    for stage in ("materials", "results"):
        if decisions[stage] is None:
            blockers.append("user_" + stage + "_approval_missing")
    all_reviews = active_reviews(batch)
    if any(instant(r["compared_at"]) > now for r in all_reviews):
        blockers.append("review_after_report_time")
    reviews = [r for r in all_reviews if instant(r["compared_at"]) <= now]
    material_reviews = [r for r in reviews if r["target"]["type"] == "materials"]
    if not material_reviews:
        blockers.append("independent_material_review_missing")
    # Only immutable version-1 displays replay the old interpretation. Current
    # quality/admission always retain blind concerns and judge-check blockers.
    disputes = assess_disputes(batch, source, now, _include_blind=not legacy)
    for row in disputes:
        if (
            row["target"]["type"] == "materials"
            and row["status"] == "confirmed_violation"
        ):
            blockers.append("material_review_failed:" + row["id"])
        if row["status"] == "pending":
            blockers.append("adjudication_required:" + row["id"])
    results = {r["case_id"]: r for r in batch["results"]}
    grades = {g["result_id"]: g for g in batch["grades"]}

    def score(grade, kind, name):
        item = grade.get(kind, {}).get(name)
        if grade["status"] != "scored" or item is None:
            return "unknown"
        opinions = [
            parse_review(r["comparison_output"], expected_items(batch, r["target"]))[
                kind + ":" + name
            ]
            for r in reviews
            if r["target"]["type"] == "grade" and r["target"]["id"] == grade["id"]
        ]
        related = [
            d
            for d in disputes
            if d["target"]["type"] == "grade"
            and d["target"]["id"] == grade["id"]
            and d["item"] == kind + ":" + name
        ]
        resolved = [d["status"] for d in related if d["status"] != "pending"]
        if "confirmed_violation" in resolved:
            return "fail"
        if related and len(resolved) == len(related) and set(resolved) == {"dismissed"}:
            return "pass"
        supported = opinions and all(
            o["support"] == "supported" and o["status"] == item["status"]
            for o in opinions
        )
        if not supported:
            blockers.append(
                "semantic_support_missing:" + grade["id"] + ":" + kind + ":" + name
            )
            return "unknown"
        # A corroborated failure remains visible while its final dispute is pending.
        if item["status"] == "fail":
            return "fail"
        return (
            "unknown"
            if any(d["status"] == "pending" for d in related)
            else item["status"]
        )

    for group in GROUPS:
        counts[group] = {
            d: {"numerator": 0, "denominator": 0, "failed": 0, "missing": 0}
            for d in ("completion", "correctness", "selection")
        }
    for case in batch["cases"]:
        cid = case["id"]
        result = results.get(cid)
        grade = grades.get(result["id"]) if result else None
        product_failed = bool(
            result
            and (
                result["outcome"] != "completed"
                or not isinstance(result["output"], str)
                or not result["output"].strip()
                or result["evidence"].get("outcome")
                in ("failed", "interrupted", "max_steps")
            )
        )
        if result is None:
            blockers.append("not_run:" + cid)
        else:
            if product_failed:
                failures.append("product_failed:" + cid)
            if (
                not instant(result["sampled_at"])
                <= instant(result["finished_at"])
                <= now
            ):
                blockers.append("invalid_sample_time:" + cid)
            if now - instant(result["sampled_at"]) > timedelta(days=7):
                blockers.append("stale_sample:" + cid)
            evidence = result["evidence"]
            if (
                not result["recorded"]
                or evidence.get("trace_status") != "available"
                or evidence.get("recording_state") != "recorded"
                or evidence.get("run_id") != result["run_id"]
                or evidence.get("outcome", result["outcome"]) != result["outcome"]
            ):
                blockers.append("execution_evidence_missing:" + cid)
            if not batch["simulation"] and result["source"] != "online":
                blockers.append("real_sample_missing:" + cid)
        if grade is None or grade["status"] != "scored":
            blockers.append("judge_missing:" + cid)
        elif not any(
            r["target"]["type"] == "grade" and r["target"]["id"] == grade["id"]
            for r in reviews
        ):
            blockers.append("independent_grade_review_missing:" + cid)
        states = {}
        mechanical = check(case, result) if result else {"status": "unknown"}
        if mechanical["status"] == "fail":
            failures.append("exact_format_failed:" + cid)
        for obligation in case["obligations"]:
            oid = obligation["id"]
            state = (
                "na"
                if not obligation["applies"]
                else score(grade, "obligations", oid)
                if grade
                else "unknown"
            )
            if obligation["applies"] and obligation["check"] == "exact_format":
                state = mechanical["status"]
            if (
                product_failed
                and obligation["dimension"] == "completion"
                and obligation["applies"]
            ):
                state = "fail"
            states[oid] = state
            details.append({"case_id": cid, **obligation, "status": state})
            if obligation["applies"] and state not in ("pass", "fail"):
                blockers.append("obligation_unknown:" + cid + ":" + oid)
            if obligation["kind"] == "critical" and state == "fail":
                failures.append("critical_obligation_failed:" + cid + ":" + oid)
        for dimension, applies in case["applicability"].items():
            if not applies:
                continue
            bucket = counts[case["group"]][dimension]
            bucket["denominator"] += 1
            applicable = [
                states[o["id"]]
                for o in case["obligations"]
                if o["dimension"] == dimension and o["applies"]
            ]
            dimension_state = (
                score(grade, "dimensions", dimension) if grade else "unknown"
            )
            if dimension == "completion" and product_failed:
                dimension_state = "fail"
            all_states = [*applicable, dimension_state]
            if "fail" in all_states:
                bucket["failed"] += 1
            elif all(s == "pass" for s in all_states):
                bucket["numerator"] += 1
            else:
                bucket["missing"] += 1
        for name in case["forbidden"]:
            state = score(grade, "prohibitions", name) if grade else "unknown"
            if state == "fail":
                failures.append("prohibition_failed:" + cid + ":" + name)
            elif state != "pass" or product_failed:
                blockers.append("prohibition_unknown:" + cid + ":" + name)
    aggregation = batch["aggregation_policy"]
    if aggregation is None:
        blockers.append("aggregation_policy_missing")
    elif decisions["thresholds"] is None:
        blockers.append("user_thresholds_approval_missing")
    for group, dimensions in counts.items():
        for name, bucket in dimensions.items():
            n, d = bucket["numerator"], bucket["denominator"]
            bucket["ratio"] = n / d if d else None
            if aggregation is not None and decisions["thresholds"] and d:
                threshold = aggregation["groups"][group][name]
                if (d - bucket["failed"]) / d < threshold:
                    failures.append("threshold_failed:" + group + ":" + name)
                elif n / d < threshold:
                    blockers.append("threshold_unproven:" + group + ":" + name)
    checks = judge_checks(batch, reviews, decisions, disputes, _legacy_summary=legacy)
    if batch["phase"] == "formal":
        observed = checks
        if calibration is None:
            checks = {
                **axis(blockers=["calibration_package_not_verified"]),
                "rows": [],
                "product_sample_count": 0,
                "admission_threshold": None,
            }
        else:
            inherited = quality(calibration, now, source)
            checks = {
                **inherited["judge_checks"],
                "source_batch": calibration["batch_id"],
            }
        # Qualification can be reused, but new observations cannot be erased by
        # an earlier PASS. Unrun formal checks need no duplicate calibration.
        if any(row["result_id"] is not None for row in observed["rows"]):
            checks = {
                **checks,
                **axis(
                    checks["failures"] + observed["failures"],
                    checks["blockers"] + observed["blockers"],
                ),
                "observed_checks": observed,
            }
    failures.extend(checks["failures"])
    blockers.extend(checks["blockers"])
    critical = [row for row in details if row["kind"] == "critical" and row["applies"]]
    return {
        **axis(failures, blockers),
        "groups": counts,
        "obligations": details,
        "critical_gate": axis(
            [f for f in failures if f.startswith("critical_")],
            ["critical_evidence_missing"]
            if any(r["status"] not in ("pass", "fail") for r in critical)
            else [],
        ),
        "judge_checks": checks,
        "user_approvals": decisions,
        "review_disputes": disputes,
        "review_history": batch.get("review_disputes", []),
    }


def judge_checks(batch, reviews, decisions, disputes, *, _legacy_summary=False):
    from .report import axis
    from .supplement_judge import FAILURE_MODES

    failures, blockers, rows = [], [], []
    covered = {t["failure_mode"] for t in batch["judge_tests"]}
    if covered != set(FAILURE_MODES) or not any(
        t["kind"] == "correct_control" for t in batch["judge_tests"]
    ):
        blockers.append("judge_check_coverage_missing")
    for test in batch["judge_tests"]:
        results = [
            r
            for r in batch["judge_test_results"]
            if r["test_id"] == test["id"]
            and r["judge_profile_id"] == batch["judge_profile"]["id"]
            and r["semantic_rubric_id"] == batch["semantic_rubric"]["id"]
        ]
        # Every response is retained. No best-of selection or automatic retry.
        for result in results or [None]:
            parsed = result["parsed"] if result else None
            label = parsed["label"] if parsed else "unknown"
            reviewed = [
                r
                for r in reviews
                if r["target"]["type"] == "judge_test"
                and result
                and r["target"]["id"] == result["id"]
            ]
            support = [
                parse_review(r["comparison_output"], {"judgment": label})["judgment"][
                    "support"
                ]
                for r in reviewed
            ]
            if not support or any(s != "supported" for s in support):
                blockers.append("judge_check_semantic_support_missing:" + test["id"])
            if parsed is None:
                blockers.append("judge_check_result_missing:" + test["id"])
            blockers.extend(
                "adjudication_required:" + d["id"]
                for d in disputes
                if not _legacy_summary
                and result
                and d["target"]["type"] == "judge_test"
                and d["target"]["id"] == result["id"]
                and d["status"] == "pending"
            )
            missed = (
                test["failure_mode"] == "missed_prohibition"
                and test["expected"] == "fail"
                and label == "pass"
            )
            confirmed = any(
                d["target"]["type"] == "judge_test"
                and result
                and d["target"]["id"] == result["id"]
                and d["status"] == "confirmed_violation"
                for d in disputes
            )
            if confirmed:
                failures.append("judge_check_violation:" + test["id"])
            if missed and (
                confirmed
                or (
                    decisions["materials"]
                    and support
                    and all(s == "supported" for s in support)
                )
            ):
                failures.append("judge_missed_prohibition:" + test["id"])
            rows.append(
                {
                    "test_id": test["id"],
                    "failure_mode": test["failure_mode"],
                    "result_id": result["id"] if result else None,
                    "raw": result["raw"] if result else None,
                    "label_agrees": label == test["expected"] if parsed else None,
                    "citation_structure_valid": parsed is not None,
                    "semantic_support": support or ["unknown"],
                }
            )
    if decisions["calibration"] is None:
        blockers.append("judge_quality_approval_missing")
    if any(
        d["status"] == "pending" and d["target"]["type"] in ("materials", "judge_test")
        for d in disputes
    ):
        blockers.append("judge_check_adjudication_required")
    return {
        **axis(failures, blockers),
        "rows": rows,
        "agreement": {
            mode: {
                "numerator": sum(
                    r["label_agrees"] is True for r in rows if r["failure_mode"] == mode
                ),
                "denominator": sum(r["failure_mode"] == mode for r in rows),
            }
            for mode in FAILURE_MODES
        },
        "product_sample_count": 0,
        "admission_threshold": None,
    }
