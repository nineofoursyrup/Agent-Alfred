"""Schema3 synthetic mechanism fixtures; no execution approval or real gold."""

from .examples import offline_batch
from .judge_protocol import current_profile, descriptor
from .review_policy import descriptor as agent_policy
from .schema import digest


def controlled_batch(candidate=None):
    batch = offline_batch(candidate)
    batch.update(
        schema_version=3,
        batch_id="controlled-offline-example",
        review_policy=agent_policy(),
        aggregation_policy=None,
        seen_families=["legacy-six-development-families"],
    )
    profile = batch["profiles"][0]
    for model in [*profile["product_models"], profile["judge_model"]]:
        model.update(endpoint_id="deepseek", thinking="disabled")
    profile["judge_model"]["response_format"] = "json_object"
    profile["parameters"].update(stream=False, stream_fallback=False)
    profile["local_tool_allowlist"] = ["draft_message"]
    profile["execution_policy"] = {
        "version": 1,
        "max_retries": 0,
        "sdk_max_retries": 0,
        "stream": False,
        "stream_fallback": False,
        "credential_scope": ["deepseek"],
        "stop_on_infrastructure_error": True,
        "require_case_tool_mask": True,
        "allow_external_business_effects": False,
    }
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    batch["judge_profile"] = current_profile(profile["judge_model"])
    semantic = {
        "version": "semantic-rubric-v1",
        "scorer": "item-labels-v1",
        "dimensions": {
            "completion": "Check all predeclared task obligations.",
            "correctness": "Check claims against supplied evidence.",
            "selection": "Check applicable source/tool/skill/routing selection.",
        },
        "prohibitions": {
            "evidence_required": (
                "Unknown when facts do not support a label; "
                "never infer execution from prose."
            )
        },
        "review_policy_id": batch["review_policy"]["id"],
        "judge_protocol_id": descriptor()["id"],
        "approval": {
            "kind": "test",
            "by": "fixture",
            "at": "2026-09-23T00:00:00+00:00",
            "reference": "synthetic-only",
        },
    }
    batch["semantic_rubric"] = {**semantic, "id": digest(semantic)}
    for case in batch["cases"]:
        old = case["setup"]
        setup = {"version": 1, "local_tool_allowlist": []}
        if "fact" in old:
            setup["memory"] = [
                {
                    "ref": "fact1",
                    "kind": "semantic",
                    "payload": {"subject": "fixture", "fact": old["fact"]},
                }
            ]
        if "skill" in old:
            setup["skills"] = [
                {
                    "name": "Fixture",
                    "description": "Synthetic procedure",
                    "body": old["skill"],
                }
            ]
        if old.get("routing"):
            setup["routing"] = True
        if case["group"] == "tools":
            setup["local_tool_allowlist"] = ["draft_message"]
        if case["operation"] == "aggregate":
            setup["aggregate"] = {"keywords": "coffee", "sources": ["semantic"]}
        case["setup"] = setup
        case["source_family_id"] = "fixture-" + case["group"]
    return batch
