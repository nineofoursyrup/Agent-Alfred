"""Imported Issue 18 specialist evidence; no synthetic consolidation execution."""

from datetime import timedelta

from .schema import approval, digest, hash_value, required, unique


def validate_specialist(document):
    required(
        document,
        (
            "schema_version",
            "id",
            "candidate_id",
            "profile_id",
            "simulation",
            "approval",
            "rows",
        ),
    )
    if document["schema_version"] != 1 or type(document["simulation"]) is not bool:
        raise ValueError("unknown_specialist_schema")
    if document["id"] != digest({k: v for k, v in document.items() if k != "id"}):
        raise ValueError("specialist_identity_mismatch")
    approval(document["approval"])
    unique(document["rows"], "case_id")
    unique(document["rows"], "material_id")
    unique(document["rows"], "run_id")
    for row in document["rows"]:
        required(
            row,
            (
                "case_id",
                "material_id",
                "run_id",
                "input",
                "gold",
                "output",
                "sampled_at",
                "finished_at",
                "source",
                "evidence",
                "review",
                "correct_atoms",
                "output_atoms",
                "covered_items",
                "expected_items",
                "prohibitions",
                "structure_safe",
            ),
        )
        hash_value(row["material_id"])
        if row["material_id"] != digest({"input": row["input"], "gold": row["gold"]}):
            raise ValueError("specialist_material_mismatch")
        approval(row["review"])
        if row["review"]["kind"] != ("test" if document["simulation"] else "approved"):
            raise ValueError("specialist_review_approval_missing")
        if row["prohibitions"] not in (True, False, None) or row[
            "structure_safe"
        ] not in (True, False, None):
            raise ValueError("invalid_specialist_status")
    return document


def assess(batch, now):
    from .report import axis, consolidation, instant

    document = batch.get("specialist")
    if not document:
        return {**axis(blockers=["consolidation_missing"]), "counts": consolidation([])}
    validate_specialist(document)
    counts = consolidation(document["rows"])
    failures, blockers = list(counts["failures"]), list(counts["blockers"])
    if len(document["rows"]) < 30:
        blockers.append("consolidation_cases_insufficient")
    if document["candidate_id"] != batch["candidate_id"]:
        blockers.append("consolidation_candidate_changed")
    profiles = {r["profile_id"] for r in batch["results"]}
    if profiles != {document["profile_id"]}:
        blockers.append("consolidation_profile_mismatch")
    if document["simulation"] != batch["simulation"]:
        blockers.append("consolidation_source_mismatch")
    if document["approval"]["kind"] != ("test" if batch["simulation"] else "approved"):
        blockers.append("consolidation_approval_missing")
    for row in document["rows"]:
        if row["structure_safe"] is False:
            failures.append("consolidation_structure_failed:" + row["case_id"])
        elif row["structure_safe"] is not True:
            blockers.append("consolidation_structure_missing:" + row["case_id"])
        evidence = row["evidence"]
        if (
            (row.get("outcome") == "completed" and not row["output"])
            or row.get("outcome") in ("failed", "max_steps", "interrupted")
            or evidence.get("outcome") in ("failed", "max_steps", "interrupted")
        ):
            failures.append("consolidation_product_failed:" + row["case_id"])
        if (
            row.get("outcome") != "completed"
            or row.get("recorded") is not True
            or not row["output"]
            or evidence.get("trace_status") != "available"
            or evidence.get("run_id") != row["run_id"]
            or evidence.get("recording_state") != "recorded"
            or evidence.get("recorded", True) is not True
            or evidence.get("outcome", row.get("outcome")) != row.get("outcome")
        ):
            blockers.append("consolidation_execution_missing:" + row["case_id"])
        if not batch["simulation"] and row["source"] != "online":
            blockers.append("consolidation_real_sample_missing:" + row["case_id"])
        start, end = instant(row["sampled_at"]), instant(row["finished_at"])
        if not start <= end <= now or now - start > timedelta(days=7):
            blockers.append("consolidation_sample_invalid:" + row["case_id"])
    return {**axis(failures, blockers), "counts": counts}
