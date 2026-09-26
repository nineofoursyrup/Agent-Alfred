"""Eligibility of referenced real calibration; failed products remain usable data."""

from datetime import timedelta

from .budget import binding, validate_authorization
from .report import instant
from .schema import judge_identity, judge_model, scoring_rubric


def _authorized_requests(source, store):
    ancestry = [source]
    while ancestry[-1]["parent"]:
        ancestry.append(store.read(ancestry[-1]["parent"]["batch_id"]))
    by_id = {b["batch_id"]: b for b in ancestry}
    recorded = source.get("request_history", []) + source.get("requests", [])
    if not recorded or len({r["attempt_id"] for r in recorded}) != len(recorded):
        raise ValueError("calibration_requests_missing")
    covered = {}
    for batch in ancestry:
        auth = batch.get("authorization")
        if not auth:
            continue
        origin = by_id.get(auth.get("binding", {}).get("batch_id"))
        if origin is None:
            continue
        try:
            validate_authorization(auth, binding(origin))
            expected = binding(batch)
            expected["batch_id"] = origin["batch_id"]
            if expected != auth["binding"]:
                continue
            start = instant(batch["budget_started_at"])
            end = start + timedelta(seconds=auth["total_seconds"])
            active = batch.get("requests", [])
            if len(active) > auth["max_requests"]:
                continue
            for request in active:
                at = instant(request["started_at"])
                if not max(start, instant(auth["at"])) <= at < end:
                    continue
                models = (
                    [m for p in batch["profiles"] for m in p["product_models"]]
                    if request["role"] == "product"
                    else [judge_model(batch, p) for p in batch["profiles"]]
                    if request["role"] == "judge"
                    else []
                )
                if not any(
                    request["model"] == {k: m[k] for k in ("endpoint_id", "model_id")}
                    for m in models
                ):
                    continue
                covered[request["attempt_id"]] = request
        except KeyError, TypeError, ValueError:
            continue
    if any(covered.get(r["attempt_id"]) != r for r in recorded):
        raise ValueError("calibration_authorization_or_requests_invalid")
    return covered


def validate_source(source, store):
    """Qualify a formal reference; structural audit validity is not permission."""
    validate_source_facts(source, store)
    from .authorization_history import assess

    authorization = assess(source, store=store)
    if not authorization["release_eligible"]:
        raise ValueError(
            authorization["blockers"][0]
            if authorization["blockers"] else "approval_source_unverifiable"
        )


def validate_source_facts(source, store):
    """Read historical structure/chronology without granting formal eligibility."""
    from .authorization_history import assess

    authorization = assess(source, store=store)
    if authorization["validity"] == "INVALID":
        raise ValueError("execution_authorization_invalid")
    from .budget import validate_material_approval_timing

    validate_material_approval_timing(source)
    proof = source.get("case_set_approval")
    from .semantic_rules import case_approval_valid

    if not case_approval_valid(source):
        raise ValueError("calibration_case_approval_missing")
    requests = _authorized_requests(source, store)
    grades = {g["result_id"]: g for g in source["grades"]}
    profiles = {p["id"]: p for p in source["profiles"]}
    seen = set()

    def referenced(ids, role, allowed, start, end):
        if len(ids) != len(set(ids)) or seen.intersection(ids):
            raise ValueError("calibration_request_reused")
        seen.update(ids)
        for identity in ids:
            request = requests.get(identity)
            if (
                not request
                or request["role"] != role
                or request["model"] not in allowed
            ):
                raise ValueError("calibration_request_reference_invalid")
            if not start <= instant(request["started_at"]) <= end:
                raise ValueError("calibration_request_time_mismatch")

    for result in source["results"]:
        if source["schema_version"] == 3 and instant(
            scoring_rubric(source)["approval"]["at"]
        ) > instant(result["sampled_at"]):
            raise ValueError("calibration_semantic_approval_after_sample")
        evidence = result["evidence"]
        completed = result["outcome"] == "completed" and bool(result["output"])
        if (
            result["source"] != "online"
            or not result["run_id"]
            or evidence.get("run_id") != result["run_id"]
            or evidence.get("recording_state") != "recorded"
            or evidence.get("trace_status") != "available"
            or evidence.get("recorded", True) is not True
            or evidence.get("outcome", result["outcome"]) != result["outcome"]
            or (completed and not result["recorded"])
            or not instant(proof["at"])
            <= instant(result["sampled_at"])
            <= instant(result["finished_at"])
            <= instant(source["calibration_approval"]["at"])
        ):
            raise ValueError("calibration_execution_evidence_invalid")
        profile = profiles[result["profile_id"]]
        ids = [a["attempt_id"] for a in evidence.get("attempts", [])]
        if not ids and completed:
            raise ValueError("calibration_product_requests_missing")
        referenced(
            ids,
            "product",
            [
                {k: m[k] for k in ("endpoint_id", "model_id")}
                for m in profile["product_models"]
            ],
            instant(result["sampled_at"]),
            instant(result["finished_at"]),
        )
        grade = grades[result["id"]]
        model = judge_model(source, profile)
        if grade.get("judge_id") != judge_identity(source, profile):
            raise ValueError("calibration_judge_identity_missing")
        judge_ids = grade.get("attempt_ids", [])
        if grade["status"] == "scored" and (not grade.get("raw") or not judge_ids):
            raise ValueError("calibration_judge_evidence_missing")
        if grade["status"] != "scored" and not grade.get("error"):
            raise ValueError("calibration_judge_error_missing")
        referenced(
            judge_ids,
            "judge",
            [{k: model[k] for k in ("endpoint_id", "model_id")}],
            instant(result["finished_at"]),
            instant(source["calibration_approval"]["at"]),
        )
