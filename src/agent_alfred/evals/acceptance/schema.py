"""Versioned, content-addressed acceptance documents. Hashes are not signatures."""

import hashlib
import json
import re

CONTRACT = "V1-ACCEPTANCE-PHASE-A-SPEC-r1"
GROUPS = ("conversation", "memory", "tools", "skills", "routing", "aggregation")


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise ValueError("invalid_identity")
    return value


def validate_envelope(batch):
    if type(batch.get("schema_version")) is not int:
        raise ValueError("unknown_schema_or_contract")
    if (
        batch.get("schema_version") not in (1, 2, 3)
        or batch.get("contract") != CONTRACT
    ):
        raise ValueError("unknown_schema_or_contract")
    identifier(batch["batch_id"])
    if digest(batch["candidate"]) != batch["candidate_id"]:
        raise ValueError("candidate_identity_mismatch")
    for name in (
        "profiles",
        "cases",
        "coverage",
        "gates",
        "results",
        "grades",
        "adjudications",
    ):
        if not isinstance(batch[name], list):
            raise ValueError("invalid_collection")
    for name, reference in batch["references"].items():
        identifier(name)
        if (
            hashlib.sha256(reference["content"].encode()).hexdigest()
            != reference["sha256"]
        ):
            raise ValueError("reference_integrity_mismatch")
    return batch


def unique(rows, key):
    values = [row[key] for row in rows]
    if len(set(values)) != len(values):
        raise ValueError("duplicate_identity")
    return set(values)


def validate(batch):
    validate_envelope(batch)
    phases = (
        ("trial",)
        if batch["schema_version"] == 2
        else ("offline_fixture", "calibration", "formal")
    )
    if batch["phase"] not in phases:
        raise ValueError("invalid_phase")
    if type(batch["simulation"]) is not bool:
        raise ValueError("invalid_simulation")
    if batch["phase"] == "offline_fixture" and not batch["simulation"]:
        raise ValueError("fixture_is_simulation")
    if batch["phase"] == "trial":
        if batch["simulation"]:
            raise ValueError("trial_requires_real_execution")
        if sorted(c["group"] for c in batch["cases"]) != sorted(GROUPS):
            raise ValueError("trial_requires_six_groups")
    if batch["parent"]:
        identifier(batch["parent"]["batch_id"])
        if batch["parent"]["batch_id"] == batch["batch_id"]:
            raise ValueError("cyclic_parent")
        if batch["parent"]["relation"] not in ("regrade", "retry"):
            raise ValueError("unknown_parent_relation")
    ids = unique(batch["cases"], "id")
    unique(batch["cases"], "material_id")
    profiles = unique(batch["profiles"], "id")
    for case in batch["cases"]:
        from .exact_format import contract

        contract(case)
        identifier(case["id"])
        if case["operation"] not in ("chat", "aggregate", "consolidate"):
            raise ValueError("unknown_operation")
        if not isinstance(case["input"], str) or not case["input"].strip():
            raise ValueError("case_input_missing")
        if not case["forbidden"]:
            raise ValueError("prohibitions_missing")
        if not case["applicability"].get("completion") or not case["applicability"].get(
            "correctness"
        ):
            raise ValueError("mandatory_dimensions_not_applicable")
        if len(case["forbidden"]) != len(set(case["forbidden"])):
            raise ValueError("duplicate_prohibition")
        if case["group"] not in (*GROUPS, "consolidation"):
            raise ValueError("invalid_group")
        if case["material_id"] != digest(
            {"input": case["input"], "gold": case["gold"]}
        ):
            raise ValueError("material_identity_mismatch")
        if case["source"]["kind"] not in ("synthetic", "approved"):
            raise ValueError("unapproved_data")
        if not case["source"]["reference"]:
            raise ValueError("material_source_missing")
        if set(case["applicability"]) != {"completion", "correctness", "selection"}:
            raise ValueError("applicability_missing")
        if any(type(v) is not bool for v in case["applicability"].values()):
            raise ValueError("invalid_applicability")
    minimum = {"formal": 20, "calibration": 5, "offline_fixture": 0, "trial": 1}[
        batch["phase"]
    ]
    for group in GROUPS:
        if sum(c["group"] == group for c in batch["cases"]) < minimum:
            raise ValueError("group_size_insufficient")
    calibration = batch["calibration"]
    if calibration and set(calibration["material_ids"]) & {
        c["material_id"] for c in batch["cases"]
    }:
        raise ValueError("calibration_overlap")
    override = batch.get("judge_profile")
    if override:
        required(override, ("id", "model"))
        if override["id"] != digest({k: v for k, v in override.items() if k != "id"}):
            raise ValueError("judge_profile_identity_mismatch")
        if "protocol" in override:
            from .judge_protocol import known_descriptor

            if not known_descriptor(override["protocol"]):
                raise ValueError("judge_protocol_mismatch")
    for profile in batch["profiles"]:
        if batch["phase"] == "trial" and profile.get("local_tool_allowlist") != [
            "draft_message"
        ]:
            raise ValueError("trial_local_tool_allowlist_required")
        models = profile["product_models"]
        if len(models) not in (1, 2):
            raise ValueError("product_assignments_required")
        judge = judge_model(batch, profile)
        if any(m["model_id"] == judge["model_id"] for m in models):
            raise ValueError("judge_same_model")
        for model in [*models, judge]:
            for key in ("endpoint_id", "model_id", "wire_style", "provider_version"):
                if not model[key]:
                    raise ValueError("model_identity_missing")
            if model["wire_style"] not in ("openai", "anthropic"):
                raise ValueError("unsupported_wire_style")
            if "response_format" in model and (
                model["response_format"] != "json_object"
                or model["wire_style"] != "openai"
                or model["endpoint_id"] != "deepseek"
                or model in models
                or batch["schema_version"] not in (2, 3)
            ):
                raise ValueError("unsupported_response_format")
            if "thinking" in model and (
                model["thinking"] != "disabled"
                or model["wire_style"] != "openai"
                or model["endpoint_id"] != "deepseek"
                or batch["schema_version"] not in (2, 3)
            ):
                raise ValueError("unsupported_thinking_mode")
        if profile["id"] != digest({k: v for k, v in profile.items() if k != "id"}):
            raise ValueError("profile_identity_mismatch")
    if batch["schema_version"] == 3:
        from . import semantic_rules
        from .case_setup import validate_setup
        from .execution_policy import policy, validate_profile

        policy(batch)
        semantic_rules.validate(batch)
        for profile in batch["profiles"]:
            validate_profile(profile)
            for case in batch["cases"]:
                validate_setup(case, profile)
    result_ids = unique(batch["results"], "id")
    unique(batch["results"], "case_id")
    sampled_ids = []
    for result in batch["results"]:
        if result["outcome"] not in ("completed", "failed", "max_steps", "interrupted"):
            raise ValueError("unknown_product_outcome")
        if result["source"] not in ("online", "offline_fixture", "simulation"):
            raise ValueError("unknown_sample_source")
        if type(result["recorded"]) is not bool:
            raise ValueError("invalid_recorded_state")
        if result["case_id"] not in ids or result["profile_id"] not in profiles:
            raise ValueError("broken_result_reference")
        if result["batch_id"] != batch["batch_id"]:
            parent = batch["parent"]
            if not parent or parent["relation"] != "regrade":
                raise ValueError("cross_batch_selection")
        if result.get("run_id"):
            sampled_ids.append(result["run_id"])
    if len(sampled_ids) != len(set(sampled_ids)):
        raise ValueError("duplicate_sample")
    from .score_evidence import validate_evidence, validate_grade_raw

    results_by_id = {r["id"]: r for r in batch["results"]}
    unique(batch["grades"], "id")
    unique(batch["grades"], "result_id")
    for grade in batch["grades"]:
        if grade["status"] not in ("scored", "error", "unknown"):
            raise ValueError("unknown_grade_status")
        if any(
            type(grade[key]) is not bool for key in ("disputed", "suspected_safety")
        ):
            raise ValueError("invalid_judge_flags")
        validate_scores(grade)
        if grade["result_id"] not in result_ids:
            raise ValueError("broken_grade_reference")
        result = results_by_id[grade["result_id"]]
        case = next(c for c in batch["cases"] if c["id"] == result["case_id"])
        from .judge_protocol import validate_grade

        validate_grade(grade, batch, case, result)
        validate_grade_raw(grade, case)
        validate_evidence(grade, batch, results_by_id[grade["result_id"]])
    unique(batch["adjudications"], "id")
    unique(batch["adjudications"], "grade_id")
    grade_ids = {g["id"] for g in batch["grades"]}
    for ruling in batch["adjudications"]:
        validate_scores(ruling)
        if ruling["grade_id"] not in grade_ids:
            raise ValueError("broken_adjudication_reference")
        grade = next(g for g in batch["grades"] if g["id"] == ruling["grade_id"])
        validate_evidence(ruling, batch, results_by_id[grade["result_id"]])
        if ruling.get("kind") == "agent_adjudication":
            from .review_policy import validate_panel

            if set(ruling) != set(
                "id version kind grade_id actor policy blind_annotations "
                "at reason rubric_id dimensions prohibitions".split()
            ):
                raise ValueError("invalid_agent_adjudication")
            if ruling["version"] != 2 or "human" in ruling:
                raise ValueError("invalid_agent_adjudication")
            validate_panel(
                ruling,
                result_finished_at=results_by_id[grade["result_id"]]["finished_at"],
            )
            if (
                batch["schema_version"] != 3
                or ruling["policy"] != batch["review_policy"]
            ):
                raise ValueError("adjudication_policy_mismatch")
            if (
                ruling["rubric_id"] != scoring_rubric(batch)["id"]
                or not ruling["reason"]
            ):
                raise ValueError("adjudication_identity_missing")
        else:
            if batch["schema_version"] == 3:
                raise ValueError("agent_adjudication_required")
            for key in ("human", "at", "reason", "rubric_id"):
                if not ruling[key]:
                    raise ValueError("adjudication_identity_missing")
    if batch.get("specialist") is not None:
        from .specialist import validate_specialist

        validate_specialist(batch["specialist"])
    validate_proofs(batch)
    from .reviews import validate as validate_reviews

    validate_reviews(batch)
    from .review_policy import validate_role_independence

    validate_role_independence(batch)
    return batch


def validate_proofs(batch):
    from .review_policy import validate_material_approval

    if batch.get("calibration_approval"):
        validate_material_approval(batch["calibration_approval"], batch)
        hash_value(batch["calibration_approval"]["evidence_sha256"])
    if batch.get("case_set_approval"):
        validate_material_approval(batch["case_set_approval"], batch)
        hash_value(batch["case_set_approval"]["cases_sha256"])
    calibration = batch["calibration"]
    if calibration:
        required(calibration, ("batch_id", "sha256", "material_ids"))
        identifier(calibration["batch_id"])
        hash_value(calibration["sha256"])
    rubric = batch["rubric"]
    if rubric:
        required(rubric, ("id", "version", "scorer", "approval", "groups"))
        approval(rubric["approval"])
        if rubric["id"] != digest({k: v for k, v in rubric.items() if k != "id"}):
            raise ValueError("rubric_identity_mismatch")
    for row in batch["coverage"]:
        required(
            row,
            (
                "source",
                "source_version",
                "id",
                "obligation",
                "public_path",
                "evidence",
                "applicability",
                "gap",
                "owner",
            ),
        )
        if not all(
            row[k]
            for k in (
                "source",
                "source_version",
                "id",
                "obligation",
                "public_path",
                "owner",
            )
        ):
            raise ValueError("coverage_identity_missing")
        if not isinstance(row["evidence"], list):
            raise ValueError("coverage_evidence_invalid")


def required(value, keys):
    if not isinstance(value, dict) or any(key not in value for key in keys):
        raise ValueError("proof_fields_missing")


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("invalid_hash")


def approval(value, *, allow_agent=False):
    required(value, ("kind", "by", "at", "reference"))
    kinds = (
        ("test", "approved", "agent_approved") if allow_agent else ("test", "approved")
    )
    if value["kind"] not in kinds or not all(value.values()):
        raise ValueError("approval_identity_missing")
    from datetime import datetime

    if datetime.fromisoformat(value["at"]).tzinfo is None:
        raise ValueError("approval_timezone_missing")


def validate_scores(record):
    for kind in ("dimensions", "prohibitions"):
        if not isinstance(record[kind], dict):
            raise ValueError("invalid_scores")
        for item in record[kind].values():
            if item["status"] not in ("pass", "fail", "unknown", "na"):
                raise ValueError("unknown_score_status")
            if not item.get("reason") or not item.get("evidence"):
                raise ValueError("score_evidence_missing")


def judge_model(batch, profile):
    return batch.get("judge_profile", {}).get("model", profile["judge_model"])


def judge_identity(batch, profile):
    protocol = batch.get("judge_profile", {}).get("protocol")
    model = judge_model(batch, profile)
    return digest({"model": model, "protocol": protocol}) if protocol else digest(model)


def calibration_identity(batch):
    evidence = {
        key: batch.get(key)
        for key in (
            "candidate_id",
            "profiles",
            "judge_profile",
            "cases",
            "results",
            "grades",
            "adjudications",
            "rubric",
        )
    }
    if batch["schema_version"] == 3:
        evidence.update(
            semantic_rubric=batch["semantic_rubric"],
            review_policy=batch["review_policy"],
            seen_families=batch["seen_families"],
        )
    # Keep the historical identity byte-for-byte when no review extension exists.
    for key in ("review_disputes", "review_adjudications"):
        if key in batch:
            evidence[key] = batch[key]
    return digest(evidence)


def scoring_rubric(batch):
    """The judge sees semantic rules only; legacy schemas keep the full rubric."""
    return (
        batch["semantic_rubric"]
        if batch.get("schema_version", 1) == 3
        else batch["rubric"]
    )
