"""Public schema3 identity/admission tests; all materials are synthetic."""

from copy import deepcopy

import pytest

from agent_alfred.evals.acceptance.budget import binding
from agent_alfred.evals.acceptance.examples import offline_batch
from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.schema import digest, validate
from agent_alfred.evals.acceptance.store import EvidenceStore


def test_schema3_roundtrip_preserves_legacy_identity(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    old = offline_batch()
    old_id = digest(old)
    store.import_batch(old)
    batch = controlled_batch()
    store.import_batch(batch)
    assert store.read(batch["batch_id"]) == batch
    assert digest(store.read(old["batch_id"])) == old_id


def test_authorization_binds_semantic_policy_and_setup():
    batch = controlled_batch()
    original = binding(batch)
    for key in (
        "semantic_rubric",
        "review_policy",
        "aggregation_policy",
        "seen_families",
    ):
        changed = deepcopy(batch)
        changed[key] = {"changed": True}
        assert binding(changed) != original
    changed = deepcopy(batch)
    changed["cases"][0]["setup"]["routing"] = True
    assert binding(changed) != original


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.pop("review_policy"),
        lambda b: b["review_policy"].update(model="other"),
        lambda b: b["semantic_rubric"].update(groups={}),
        lambda b: b["profiles"][0].pop("execution_policy"),
        lambda b: b["cases"][0]["setup"].update(arbitrary_python="print(1)"),
        lambda b: b["profiles"][0]["product_models"][0].update(
            response_format="json_object"
        ),
    ],
)
def test_schema3_rejects_unknown_or_misbound_inputs(change):
    batch = controlled_batch()
    change(batch)
    with pytest.raises(ValueError):
        validate(batch)


def test_aggregation_is_not_sent_to_judge_and_does_not_change_sample_identity():
    from agent_alfred.evals.acceptance.judge_protocol import material
    from agent_alfred.evals.acceptance.schema import calibration_identity

    batch = controlled_batch()
    case = batch["cases"][0]
    result = {"id": "synthetic-result", "case_id": case["id"], "output": "fixture"}
    batch["results"] = [result]
    before = calibration_identity(batch)
    sources, refs = material(batch, case, result)
    assert (
        sources["rubric:" + batch["semantic_rubric"]["id"]] == batch["semantic_rubric"]
    )
    batch["aggregation_policy"] = {"test_only_not_valid_policy": "not judge input"}
    assert material(batch, case, result) == (sources, refs)
    assert calibration_identity(batch) == before


def test_unapproved_aggregation_blocks_report():
    from agent_alfred.evals.acceptance.report import report

    value = report(controlled_batch())
    assert "aggregation_policy_missing" in value["v1_release"]["blockers"]


def scored_v3(phase, size, prefix):
    import json

    from agent_alfred.evals.acceptance.judge import judge_result
    from agent_alfred.model import ScriptedModel

    batch = controlled_batch()
    originals = batch["cases"]
    batch.update(phase=phase, batch_id=prefix, cases=[])
    batch["authorization"] = {"max_output_tokens": 256}  # Judge-only synthetic seam.
    for source in originals:
        for i in range(size):
            case = deepcopy(source)
            cid = prefix + source["id"] + str(i)
            case.update(
                id=cid,
                input=cid,
                gold="synthetic answer",
                source_family_id=prefix + source["group"],
            )
            case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
            batch["cases"].append(case)
            result = {
                "id": cid,
                "batch_id": prefix,
                "case_id": cid,
                "profile_id": batch["profiles"][0]["id"],
                "source": "simulation",
                "sampled_at": "2026-09-23T00:00:01Z",
                "finished_at": "2026-09-23T00:00:02Z",
                "run_id": cid,
                "outcome": "completed",
                "output": "synthetic answer",
                "recorded": True,
                "evidence": {
                    "run_id": cid,
                    "trace_status": "available",
                    "recording_state": "recorded",
                },
            }
            batch["results"].append(result)
            item = {
                "status": "pass",
                "reason": "Synthetic evidence",
                "evidence": "result:" + cid + "#/output",
            }
            raw = {
                "dimensions": {
                    k: {**item, "status": "pass" if v else "na"}
                    for k, v in case["applicability"].items()
                },
                "prohibitions": {k: dict(item) for k in case["forbidden"]},
                "disputed": False,
                "suspected_safety": False,
            }
            batch["grades"].append(
                judge_result(batch, case, result, ScriptedModel([json.dumps(raw)]))
            )
    batch["authorization"] = None
    return batch


def aggregation_for(source):
    from agent_alfred.evals.acceptance.schema import GROUPS, calibration_identity

    policy = {
        "version": 1,
        "scorer": "case-fraction-v1",
        "semantic_rubric_id": source["semantic_rubric"]["id"],
        "calibration_evidence_sha256": calibration_identity(source),
        "groups": {
            g: dict.fromkeys(("completion", "correctness", "selection"), 1)
            for g in GROUPS
        },
        "denominator": "all_predeclared_applicable",
        "unknown_policy": "block",
        "forbidden_policy": "zero_confirmed_violations",
        "approval": {
            "kind": "test",
            "by": "fixture",
            "at": "2026-09-23T00:01:00Z",
            "reference": "test-only",
        },
    }
    return {**policy, "id": digest(policy)}


def test_thresholds_preserve_scores_but_semantics_require_recalibration(
    tmp_path,
):
    from agent_alfred.evals.acceptance.schema import calibration_identity

    store = EvidenceStore(tmp_path)
    source = scored_v3("calibration", 5, "cal")
    source["calibration_approval"] = {
        "kind": "test",
        "by": "fixture",
        "at": "2026-09-23T00:00:03Z",
        "reference": "test-only",
        "evidence_sha256": calibration_identity(source),
    }
    store.import_batch(source)
    revised = store.revise(
        "cal",
        "thresholds-added",
        configuration={"aggregation_policy": aggregation_for(source)},
    )
    assert revised["grades"] == source["grades"]
    assert revised["results"] == source["results"]
    assert calibration_identity(revised) == calibration_identity(source)
    formal = scored_v3("formal", 20, "formal")
    formal["aggregation_policy"] = aggregation_for(source)
    formal["seen_families"] += sorted({c["source_family_id"] for c in source["cases"]})
    formal["calibration"] = {
        "batch_id": "cal",
        "sha256": digest(source),
        "material_ids": [c["material_id"] for c in source["cases"]],
    }
    store.import_batch(formal)
    with pytest.raises(ValueError, match="aggregation_after_formal_sampling"):
        store.revise(
            "formal",
            "relaxed",
            configuration={"aggregation_policy": aggregation_for(source)},
        )
    changed = deepcopy(formal)
    changed["batch_id"] = "changed-semantics"
    changed["grades"] = []
    changed["results"] = []  # A fresh unsampled batch, not copied foreign results.
    changed["semantic_rubric"]["dimensions"]["correctness"] = "Changed meaning"
    changed["semantic_rubric"]["id"] = digest(
        {k: v for k, v in changed["semantic_rubric"].items() if k != "id"}
    )
    changed["aggregation_policy"] = None
    with pytest.raises(ValueError, match="rubric_requires_recalibration"):
        store.import_batch(changed)


def test_schema3_rejects_external_capability_before_client_construction():
    batch = controlled_batch()
    profile = batch["profiles"][0]
    profile["local_tool_allowlist"].append("web_search")
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    with pytest.raises(ValueError, match="execution_policy_tool_scope"):
        validate(batch)


@pytest.mark.parametrize(
    "parameters",
    [
        {"persona_file": "/outside/private.txt"},
        {"api_key_env": "OTHER_KEY"},
        {"endpoint_id": "other"},
        {"model_id": "other"},
        {"wire_style": "anthropic"},
        {"max_steps": -1},
        {"max_tokens": True},
        {"per_attempt_timeout_s": float("inf")},
    ],
)
def test_schema3_rejects_unapproved_settings_or_invalid_limits(parameters):
    batch = controlled_batch()
    profile = batch["profiles"][0]
    profile["parameters"].update(parameters)
    # Nonfinite inputs are rejected by identity encoding itself.
    with pytest.raises(ValueError):
        profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
        validate(batch)


@pytest.mark.parametrize("build_batch", [offline_batch, controlled_batch])
def test_schema_rejects_unconsumed_third_product_assignment(build_batch):
    batch = build_batch()
    profile = batch["profiles"][0]
    first = profile["product_models"][0]
    profile["product_models"] = [
        deepcopy(first),
        {**first, "model_id": "auxiliary"},
        {**first, "model_id": "silently-ignored"},
    ]
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    with pytest.raises(ValueError, match="product_assignments_required"):
        validate(batch)
