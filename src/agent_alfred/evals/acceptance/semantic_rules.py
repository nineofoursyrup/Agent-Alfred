"""Schema3 scoring meaning and post-calibration aggregation are separate inputs."""

from .review_policy import (
    approval_valid,
    validate_material_approval,
    validate_policy,
)
from .schema import GROUPS, approval, digest, hash_value


def identity(value, fields, error):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError(error)
    if value["id"] != digest({k: v for k, v in value.items() if k != "id"}):
        raise ValueError(error)


def validate(batch):
    validate_shapes(batch)
    validate_policy(batch.get("review_policy"))
    if batch["rubric"] is not None:
        raise ValueError("legacy_rubric_in_schema3")
    rules = batch.get("semantic_rubric")
    identity(
        rules,
        "id version scorer dimensions prohibitions review_policy_id "
        "judge_protocol_id approval",
        "invalid_semantic_rubric",
    )
    from .judge_protocol import known_descriptor

    selected = batch.get("judge_profile", {}).get("protocol")
    if (
        rules["version"] != "semantic-rubric-v1"
        or rules["scorer"] != "item-labels-v1"
        or rules["review_policy_id"] != batch["review_policy"]["id"]
        or not known_descriptor(selected)
        or rules["judge_protocol_id"] != selected["id"]
    ):
        raise ValueError("semantic_policy_mismatch")
    if (
        set(rules["dimensions"]) != {"completion", "correctness", "selection"}
        or set(rules["prohibitions"]) != {"evidence_required"}
        or not all(
            isinstance(v, str) and v.strip()
            for v in [*rules["dimensions"].values(), *rules["prohibitions"].values()]
        )
    ):
        raise ValueError("invalid_semantic_criteria")
    validate_material_approval(rules["approval"], batch)
    families = batch.get("seen_families")
    if (
        not isinstance(families, list)
        or len(set(families)) != len(families)
        or not all(isinstance(v, str) and v.strip() for v in families)
    ):
        raise ValueError("invalid_seen_families")
    for case in batch["cases"]:
        if (
            not isinstance(case.get("source_family_id"), str)
            or not case["source_family_id"]
        ):
            raise ValueError("source_family_missing")
        if batch["phase"] == "formal" and case["source_family_id"] in families:
            raise ValueError("seen_family_in_formal")
    aggregation = batch.get("aggregation_policy")
    if aggregation is None:
        return
    identity(
        aggregation,
        "id version scorer semantic_rubric_id "
        "calibration_evidence_sha256 groups denominator unknown_policy "
        "forbidden_policy approval",
        "invalid_aggregation_policy",
    )
    if (
        aggregation["version"] != 1
        or aggregation["scorer"] != "case-fraction-v1"
        or aggregation["semantic_rubric_id"] != rules["id"]
        or aggregation["denominator"] != "all_predeclared_applicable"
        or aggregation["unknown_policy"] != "block"
        or aggregation["forbidden_policy"] != "zero_confirmed_violations"
    ):
        raise ValueError("unsupported_aggregation_policy")
    hash_value(aggregation["calibration_evidence_sha256"])
    approval(aggregation["approval"])
    if aggregation["approval"]["kind"] not in ("approved", "test"):
        raise ValueError("threshold_requires_user_approval")
    if set(aggregation["groups"]) != set(GROUPS):
        raise ValueError("invalid_aggregation_groups")
    for values in aggregation["groups"].values():
        if set(values) != {"completion", "correctness", "selection"} or any(
            type(v) not in (int, float) or not 0 <= v <= 1 for v in values.values()
        ):
            raise ValueError("invalid_aggregation_threshold")


def case_approval_valid(batch):
    proof = batch.get("case_set_approval")
    # Legacy case material always required approved, even in source validation.
    approved = (
        approval_valid(proof, batch)
        if batch["schema_version"] == 3
        else bool(proof) and proof.get("kind") == "approved"
    )
    if not approved or proof.get("cases_sha256") != digest(batch["cases"]):
        return False
    if batch["schema_version"] == 3:
        return proof.get("semantic_rubric_id") == batch["semantic_rubric"][
            "id"
        ] and proof.get("seen_families_sha256") == digest(batch["seen_families"])
    return all(c["source"]["kind"] == "approved" for c in batch["cases"])


def validate_shapes(batch):
    """Reject unconsumed execution/scoring inputs in the new closed version."""
    allowed = set(
        "schema_version contract batch_id phase simulation parent candidate "
        "candidate_id profiles cases rubric semantic_rubric review_policy "
        "aggregation_policy seen_families coverage gates results grades "
        "adjudications authorization references calibration judge_profile "
        "review_disputes review_adjudications case_set_approval "
        "calibration_approval specialist requests request_history budget_scope "
        "budget_started_at stop_reason".split()
    )
    if set(batch) - allowed:
        raise ValueError("unknown_schema3_field")
    if set(batch["judge_profile"]) != {"id", "model", "protocol"}:
        raise ValueError("unknown_judge_profile_field")
    for profile in batch["profiles"]:
        if set(profile) != set(
            "id product_models judge_model parameters inputs "
            "local_tool_allowlist execution_policy".split()
        ):
            raise ValueError("unknown_profile_field")
        if set(profile["inputs"]) != {"persona"}:
            raise ValueError("unknown_profile_input")
        from agent_alfred.settings import Settings

        try:
            Settings(**profile["parameters"], persona=profile["inputs"]["persona"])
        except TypeError, ValueError:
            raise ValueError("invalid_profile_parameters") from None
        for model in [
            *profile["product_models"],
            profile["judge_model"],
            batch["judge_profile"]["model"],
        ]:
            if set(model) - set(
                "endpoint_id model_id wire_style provider_version "
                "thinking response_format".split()
            ):
                raise ValueError("unknown_model_field")
    for case in batch["cases"]:
        if set(case) - set(
            "id group input gold forbidden source applicability operation "
            "setup script material_id source_family_id".split()
        ):
            raise ValueError("unknown_case_field")
        if not batch["simulation"] and "script" in case:
            raise ValueError("online_case_contains_script")
