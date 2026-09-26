"""Derived verdicts. Never trust an imported PASS or erase a missing denominator."""

from datetime import UTC, datetime, timedelta

from . import exact_format
from .schema import (
    GROUPS,
    digest,
    judge_identity,
    judge_model,
    scoring_rubric,
    validate,
)


def axis(failures=(), blockers=()):
    return {
        "verdict": "FAIL" if failures else "BLOCKED" if blockers else "PASS",
        "failures": sorted(set(failures)),
        "blockers": sorted(set(blockers)),
    }


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed


def consolidation(rows):
    def fraction(numerator, denominator):
        n = sum(r[numerator] for r in rows)
        d = sum(r[denominator] for r in rows)
        if (
            any(
                type(r[k]) is not int or r[k] < 0
                for r in rows
                for k in (numerator, denominator)
            )
            or n > d
            or any(r[numerator] > r[denominator] for r in rows)
        ):
            raise ValueError("invalid_atom_counts")
        return {
            "numerator": n,
            "denominator": d,
            "ratio": n / d if d else None,
            "status": ("PASS" if n / d >= 0.9 else "FAIL") if d else "N/A",
        }

    correct = fraction("correct_atoms", "output_atoms")
    coverage = fraction("covered_items", "expected_items")
    failed = any(r["prohibitions"] is False for r in rows)
    failed |= "FAIL" in (correct["status"], coverage["status"])
    blocked = not rows or any(r["prohibitions"] is not True for r in rows)
    blocked |= "N/A" in (correct["status"], coverage["status"])
    return {
        **axis(
            ["consolidation_failed"] if failed else [],
            ["consolidation_missing"] if blocked else [],
        ),
        "correctness": correct,
        "coverage": coverage,
    }


def quality(batch, now):
    from .reviews import assess

    failures, blockers, counts = [], [], {}
    reviewed = assess(batch, now)
    failures.extend(reviewed["failures"])
    blockers.extend(reviewed["blockers"])
    rubric = scoring_rubric(batch)
    if rubric is None:
        blockers.append("approved_rubric_missing")
    else:
        if rubric["id"] != digest({k: v for k, v in rubric.items() if k != "id"}):
            raise ValueError("rubric_identity_mismatch")
        expected_scorer = (
            "item-labels-v1" if batch["schema_version"] == 3 else "case-fraction-v1"
        )
        if rubric["scorer"] != expected_scorer:
            raise ValueError("unknown_scorer")
        from .review_policy import approval_valid

        if not approval_valid(rubric["approval"], batch):
            blockers.append("rubric_approval_missing")
    aggregation = (
        batch.get("aggregation_policy") if batch["schema_version"] == 3 else rubric
    )
    if batch["schema_version"] == 3:
        if aggregation is None:
            blockers.append("aggregation_policy_missing")
        elif aggregation["approval"]["kind"] != (
            "test" if batch["simulation"] else "approved"
        ):
            blockers.append("aggregation_approval_missing")
    results = {r["case_id"]: r for r in batch["results"]}
    grades = {g["result_id"]: g for g in batch["grades"]}
    rulings = {a["grade_id"]: a for a in batch["adjudications"]}
    if not results:
        blockers.append("quality_evidence_missing")
    # One frozen profile must cover the complete set; never assemble per-group winners.
    if len({r["profile_id"] for r in results.values()}) > 1:
        blockers.append("mixed_profiles")
    for group in GROUPS:
        if not any(c["group"] == group for c in batch["cases"]):
            blockers.append("missing_group:" + group)
    for case in batch["cases"]:
        cid = case["id"]
        counters = counts.setdefault(case["group"], {})
        for dimension, applies in case["applicability"].items():
            bucket = counters.setdefault(
                dimension, {"numerator": 0, "denominator": 0, "failed": 0}
            )
            bucket["denominator"] += int(applies)
        result = results.get(cid)
        if result is None:
            blockers.append("not_run:" + cid)
            continue
        try:
            sampled = instant(result["sampled_at"])
            finished = instant(result["finished_at"])
            if not sampled <= finished <= now:
                blockers.append("invalid_sample_time:" + cid)
            if now - sampled > timedelta(days=7):
                blockers.append("stale_sample:" + cid)
        except ValueError, TypeError:
            blockers.append("invalid_sample_time:" + cid)
        evidence = result["evidence"]
        product_failed = (
            result["outcome"] != "completed"
            or not result["output"]
            or evidence.get("outcome") in ("failed", "max_steps", "interrupted")
        )
        if product_failed:
            failures.append("product_failed:" + cid)
        if (
            not result["recorded"]
            or evidence.get("trace_status") != "available"
            or evidence.get("recording_state") != "recorded"
            or evidence.get("run_id") != result["run_id"]
            or evidence.get("recorded", True) is not True
            or evidence.get("outcome", result["outcome"]) != result["outcome"]
        ):
            blockers.append("execution_evidence_missing:" + cid)
        if (
            not batch["simulation"]
            and batch["schema_version"] != 3
            and case["source"]["kind"] != "approved"
        ):
            blockers.append("case_approval_missing:" + cid)
        if not batch["simulation"] and result["source"] != "online":
            blockers.append("real_sample_missing:" + cid)
        format_check = exact_format.check(case, result)
        if format_check["status"] == "fail":
            failures.append("exact_format_failed:" + cid)
        grade = grades.get(result["id"])
        if grade is None:
            blockers.append("judge_missing:" + cid)
            continue
        if grade["result_hash"] != digest(result):
            raise ValueError("grade_result_mismatch")
        if rubric is None or grade["rubric_id"] != rubric["id"]:
            blockers.append("grade_rubric_mismatch:" + cid)
            continue
        effective = grade
        if (
            not batch["simulation"]
            and grade["status"] == "scored"
            and not grade.get("raw")
        ):
            blockers.append("judge_raw_missing:" + cid)
        if not batch["simulation"]:
            profile = next(
                p for p in batch["profiles"] if p["id"] == result["profile_id"]
            )
            if grade.get("judge_id") != judge_identity(batch, profile):
                blockers.append("judge_identity_missing:" + cid)
        format_conflicts = exact_format.conflicts(format_check, grade)
        if (
            grade["id"] in rulings
            or grade["disputed"]
            or grade["suspected_safety"]
            or format_conflicts
        ):
            ruling = rulings.get(grade["id"])
            if ruling is None or ruling["rubric_id"] != rubric["id"]:
                blockers.append("adjudication_required:" + cid)
            else:
                if instant(ruling["at"]) > now:
                    blockers.append("invalid_adjudication_time:" + cid)
                else:
                    effective = ruling
        if effective is grade and grade["status"] != "scored":
            blockers.append("judge_missing:" + cid)
            continue
        for dimension, applies in case["applicability"].items():
            value = effective["dimensions"].get(dimension)
            status = value["status"] if value else "unknown"
            if status not in ("pass", "fail", "unknown", "na"):
                raise ValueError("invalid_grade_status")
            if not applies:
                if status != "na":
                    blockers.append("applicability_mismatch:" + cid)
                continue
            if (
                status in ("unknown", "na")
                or not value.get("reason")
                or not value.get("evidence")
            ):
                blockers.append("dimension_unknown:" + cid + ":" + dimension)
            if status == "fail":
                counters[dimension]["failed"] += 1
                if batch["phase"] == "trial":
                    failures.append("trial_dimension_failed:" + cid + ":" + dimension)
            if (
                status == "pass"
                and dimension not in format_conflicts
                and not (dimension == "completion" and product_failed)
            ):
                counters[dimension]["numerator"] += 1
        for prohibited in case["forbidden"]:
            value = effective["prohibitions"].get(prohibited)
            status = value["status"] if value else "unknown"
            if status == "fail":
                failures.append("prohibition_failed:" + cid + ":" + prohibited)
            elif status != "pass" or product_failed:
                blockers.append("prohibition_unknown:" + cid + ":" + prohibited)
    if aggregation:
        for group, dimensions in counts.items():
            for dimension, bucket in dimensions.items():
                n, d = bucket["numerator"], bucket["denominator"]
                bucket["ratio"] = n / d if d else None
                threshold = aggregation["groups"].get(group, {}).get(dimension)
                if d and (
                    type(threshold) not in (int, float) or not 0 <= threshold <= 1
                ):
                    blockers.append("threshold_missing:" + group + ":" + dimension)
                elif d and n / d < threshold:
                    # Unknown grades must not masquerade as proven quality failure.
                    known_fail = (d - bucket["failed"]) / d < threshold
                    if known_fail:
                        failures.append("threshold_failed:" + group + ":" + dimension)
                    else:
                        blockers.append("threshold_unproven:" + group + ":" + dimension)
    return {
        **axis(failures, blockers),
        "groups": counts,
        "review_disputes": reviewed["records"],
    }


def report(batch, *, now=None, candidate_root=None, store=None):
    from .safety import ensure_safe

    ensure_safe(batch)
    validate(batch)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    evaluated = quality(batch, now)
    from .gates import engineering

    offline = engineering(batch)
    from .specialist import assess

    specialist = assess(batch, now)
    release_blockers = (
        list(evaluated["blockers"]) + list(offline["blockers"]) + specialist["blockers"]
    )
    from .authorization_history import assess as authorization_status

    authorization = authorization_status(batch, store=store)
    release_blockers.extend(authorization["blockers"])
    if batch.get("review_disputes"):
        if store is None:
            release_blockers.append("review_source_not_verified")
        else:
            store._validate_links(batch, ())
    if candidate_root is None:
        offline["blockers"].append("current_candidate_not_verified")
        offline.update(axis(offline["failures"], offline["blockers"]))
        release_blockers.append("current_candidate_not_verified")
    else:
        from .candidate import verify, verify_runtime

        if not verify(batch["candidate"], candidate_root):
            release_blockers.append("candidate_changed")
            offline["blockers"].append("candidate_changed")
            offline.update(axis(offline["failures"], offline["blockers"]))
        if not verify_runtime(batch["candidate"]):
            release_blockers.append("runtime_candidate_mismatch")
            offline["blockers"].append("runtime_candidate_mismatch")
            offline.update(axis(offline["failures"], offline["blockers"]))
    if not batch["simulation"]:
        if store is None:
            release_blockers.append("calibration_package_not_verified")
        else:
            store._validate_links(batch, ())
        if not batch.get("authorization"):
            release_blockers.append("authorization_evidence_missing")
        requests = batch.get("request_history", []) + batch.get("requests", [])
        if not requests or not {"product", "judge"} <= {
            r.get("role") for r in requests
        }:
            release_blockers.append("online_request_evidence_missing")
        from .semantic_rules import case_approval_valid

        case_approval = batch.get("case_set_approval")
        if not case_approval or case_approval.get("cases_sha256") != digest(
            batch["cases"]
        ):
            release_blockers.append("case_set_approval_missing")
        elif not case_approval_valid(batch):
            release_blockers.append("case_set_approval_missing")
    if not batch["simulation"] and batch["phase"] == "formal" and batch["results"]:
        first_sample = min(instant(r["sampled_at"]) for r in batch["results"])
        for proof in (
            batch.get("case_set_approval"),
            (scoring_rubric(batch) or {}).get("approval"),
            (batch.get("aggregation_policy") or {}).get("approval"),
        ):
            if proof and instant(proof["at"]) > first_sample:
                release_blockers.append("approval_after_formal_sampling")
    calibration = batch["calibration"]
    if store is not None and calibration and "batch_id" in calibration:
        source = store.read(calibration["batch_id"])
        for result in source["results"]:
            sampled, finished = (
                instant(result["sampled_at"]),
                instant(result["finished_at"]),
            )
            if not sampled <= finished <= now or now - sampled > timedelta(days=7):
                release_blockers.append(
                    "calibration_sample_invalid:" + result["case_id"]
                )
    if not batch["simulation"] and not batch["calibration"]:
        release_blockers.append("calibration_missing")
    from .coverage import assess as assess_coverage

    release_blockers.extend(assess_coverage(batch))
    if not batch["coverage"]:
        release_blockers.append("requirement_coverage_missing")
    if batch["simulation"]:
        release_blockers.append("simulation_not_release_evidence")
    if batch["phase"] != "formal":
        release_blockers.append("formal_batch_required")
    if batch["phase"] == "trial":
        release_blockers.append("trial_not_release_evidence")
    return {
        "batch_id": batch["batch_id"],
        "candidate_id": batch["candidate_id"],
        "simulation": batch["simulation"],
        "computed_at": now.isoformat(),
        "authorization": authorization,
        "offline_engineering": offline,
        "v1_release": axis(
            evaluated["failures"] + offline["failures"] + specialist["failures"],
            release_blockers,
        ),
        "simulation_verdict": evaluated if batch["simulation"] else None,
        "quality": evaluated,
        "consolidation": specialist,
        "provider_versions": [
            m["provider_version"]
            for p in batch["profiles"]
            for m in [*p["product_models"], judge_model(batch, p)]
        ],
    }
