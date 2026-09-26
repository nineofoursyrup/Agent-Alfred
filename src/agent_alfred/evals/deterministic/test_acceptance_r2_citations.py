"""Public judge regressions; archived responses are never replacement grades."""

import copy
import json
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.judge import judge_result
from agent_alfred.model import ScriptedModel


def r2():
    return json.loads(
        (Path(__file__).parent / "fixtures/trial_r2_citations.json").read_text()
    )


def test_judge_supplies_actual_case_reference_catalog():
    batch = r2()
    from agent_alfred.evals.acceptance.judge_protocol import current_profile

    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    case, result = batch["cases"][3], batch["results"][3]
    client = ScriptedModel([batch["grades"][3]["raw"]])
    grade = judge_result(batch, case, result, client)
    payload = json.loads(client.requests[0].messages[0].blocks[0].text)
    refs = payload["reference_catalog"]
    prefix = "result:" + result["id"]
    assert prefix + "#/evidence/memory/skills/selected" in refs
    assert prefix + "#/evidence/skills/selected" not in refs
    assert grade["status"] == "error"
    assert grade["raw"] == batch["grades"][3]["raw"]
    assert len(client.requests) == 1
    assert not client.requests[0].tools


@pytest.mark.parametrize("index", range(6))
def test_archived_r2_responses_keep_original_reference_outcome(index):
    batch = r2()
    raw = batch["grades"][index]["raw"]
    grade = judge_result(
        batch, batch["cases"][index], batch["results"][index], ScriptedModel([raw])
    )
    assert grade["status"] == batch["grades"][index]["status"]
    assert grade["raw"] == raw


def protocol_batch():
    from agent_alfred.evals.acceptance.judge_protocol import current_profile
    from agent_alfred.evals.deterministic.test_acceptance import scored_fixture

    batch = scored_fixture()
    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    batch["authorization"] = {"max_output_tokens": 4096}
    batch["grades"] = []
    return batch


def test_new_judge_wire_highlights_real_reference_entrypoints_and_observation_order():
    batch = protocol_batch()
    case, result = batch["cases"][0], batch["results"][0]
    result["evidence"]["aggregation_inputs"] = {"attempts": []}
    ref = "result:" + result["id"] + "#/output"
    client = ScriptedModel([response(case, ref)])

    grade = judge_result(batch, case, result, client)

    payload = json.loads(client.requests[0].messages[0].blocks[0].text)
    guide = payload["reference_guide"]
    assert guide["case_input"] == "case:" + case["id"] + "#/input"
    assert guide["result_output"] == ref
    assert guide["aggregation_inputs"] == (
        "result:" + result["id"] + "#/evidence/aggregation_inputs"
    )
    assert set(guide.values()) <= set(payload["reference_catalog"])
    instructions = client.requests[0].system[0].text
    assert "first observation" in instructions
    assert "later recovery" in instructions
    assert "absence of a record" in instructions
    assert grade["status"] == "scored"


def test_historical_v3_import_remains_readable_but_cannot_start_new_judging(tmp_path):
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    legacy_protocol = {
        "version": "judge-citations-v3",
        "instructions_sha256": (
            "fd0b505207a987063178d5013e737c9720d9c37c9959846f29260275d00397f1"
        ),
        "response_contract": "nested-score-v1",
        "response_schema_sha256": (
            "65c7ac8fc0ada6334d3fb8a13c3a19d886549a4309829dd668039e326f405b83"
        ),
        "source_policy": "visible-case-fields-result-rubric-v1",
        "catalog_policy": "all-nodes-sorted-keys-rfc6901-v1",
        "max_catalog_entries": 10000,
        "id": "13038e6bce70803fbca1deda9626e105e847c7deaf5a2abefcc4978f2ab3a127",
    }
    batch["judge_profile"] = {
        "model": batch["profiles"][0]["judge_model"],
        "protocol": legacy_protocol,
    }
    batch["judge_profile"]["id"] = digest(batch["judge_profile"])
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    assert store.read(batch["batch_id"])["judge_profile"]["protocol"] == legacy_protocol
    case, result = batch["cases"][0], batch["results"][0]
    client = ScriptedModel([response(case, "result:" + result["id"] + "#/output")])
    with pytest.raises(ValueError, match="judge_protocol_mismatch"):
        judge_result(batch, case, result, client)
    assert client.requests == []


def test_historical_v3_schema3_semantic_material_stays_readable(tmp_path):
    from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
    from agent_alfred.evals.acceptance.judge_protocol import V3_DESCRIPTOR
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = controlled_batch()
    batch["judge_profile"]["protocol"] = V3_DESCRIPTOR
    batch["judge_profile"]["id"] = digest(
        {k: v for k, v in batch["judge_profile"].items() if k != "id"}
    )
    batch["semantic_rubric"]["judge_protocol_id"] = V3_DESCRIPTOR["id"]
    batch["semantic_rubric"]["id"] = digest(
        {k: v for k, v in batch["semantic_rubric"].items() if k != "id"}
    )
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    assert (
        store.read(batch["batch_id"])["semantic_rubric"]["judge_protocol_id"]
        == (V3_DESCRIPTOR["id"])
    )


def response(case, ref):
    return json.dumps(
        {
            "dimensions": {
                k: {
                    "status": "pass" if v else "na",
                    "reason": "fixture only",
                    "evidence": ref,
                }
                for k, v in case["applicability"].items()
            },
            "prohibitions": {
                k: {"status": "pass", "reason": "fixture only", "evidence": ref}
                for k in case["forbidden"]
            },
            "disputed": False,
            "suspected_safety": False,
        }
    )


@pytest.mark.parametrize(
    "defect", ["protocol", "catalog", "model", "legacy", "result", "case"]
)
def test_public_import_rejects_protocol_grade_identity_drift(tmp_path, defect):
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    case, result = batch["cases"][0], batch["results"][0]
    grade = judge_result(
        batch,
        case,
        result,
        ScriptedModel([response(case, "result:" + result["id"] + "#/output")]),
    )
    assert grade["status"] == "scored"
    batch["grades"] = [grade]
    if defect == "protocol":
        grade["protocol_id"] = "0" * 64
    elif defect == "catalog":
        grade["catalog_id"] = "0" * 64
    elif defect == "model":
        grade["judge_id"] = digest(batch["profiles"][0]["judge_model"])
    elif defect == "legacy":
        del batch["judge_profile"]
    elif defect == "result":
        result["output"] = "changed"
    else:
        case["input"] = "another task"
        case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    with pytest.raises(
        ValueError, match="judge_protocol|judge_material|judge_identity"
    ):
        EvidenceStore(tmp_path).import_batch(batch)


@pytest.mark.parametrize(
    "ref,valid",
    [
        ("result:conversation#/evidence/a~1b/~0key/0", True),
        ("result:conversation#/evidence/a~1b/~0key/1", False),
        ("result:conversation#/evidence/a~1b/~0key/-1", False),
        ("result:conversation#/evidence/a~1b/~0key/00", False),
        ("result:conversation#/evidence/a~1b/~0key/-", False),
        ("result:conversation#/evidence/a~1b/~2key", False),
        ("result:conversation#/evidence/", True),
        ("result:conversation#/evidence/empty", True),
        ("result:conversation#/evidence/missing", False),
        ("result:memory#/output", False),
        ("case:memory#/gold", False),
        ("case:conversation#/setup", False),
        ("result:conversation#", True),
        ("", False),
        ([], False),
        ([""], False),
        (None, False),
        (42, False),
    ],
)
def test_reference_boundaries_through_judge_and_public_import(tmp_path, ref, valid):
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    case, result = batch["cases"][0], batch["results"][0]
    result["evidence"].update(
        {"a/b": {"~key": ["actual"]}, "": "empty key", "empty": []}
    )
    raw = response(case, ref)
    client = ScriptedModel([raw])
    grade = judge_result(batch, case, result, client)
    assert grade["status"] == ("scored" if valid else "error")
    assert grade["raw"] == raw and len(client.requests) == 1
    batch["grades"] = [grade]
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    assert store.read(batch["batch_id"])["grades"] == [grade]
    if not valid:
        grade.update(json.loads(raw), status="scored")
        with pytest.raises(ValueError):
            EvidenceStore(tmp_path / "forged").import_batch(batch)


def test_new_judge_execution_requires_explicit_protocol_before_factory(tmp_path):
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    del batch["judge_profile"]
    root = Path(__file__).resolve().parents[4]
    batch["candidate"] = capture(root)
    batch["candidate_id"] = digest(batch["candidate"])
    auth = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 6,
        "max_output_tokens": 4096,
        "total_seconds": 60,
        "cost": {"amount": None, "source": "fixture"},
        "accept_unknown_cost": True,
    }
    constructed = []

    def factory():
        constructed.append(True)
        raise AssertionError("must fail before constructing a client")

    import httpx2 as httpx

    from agent_alfred.evals.deterministic._simulation_test_helpers import session_for

    batch["authorization"] = auth
    store = EvidenceStore(tmp_path / "evidence")
    session = session_for(batch, store, operations=("judge",))
    transport = httpx.MockTransport(lambda request: factory())
    with pytest.raises(ValueError, match="judge_protocol_required"):
        grade_batch(
            batch,
            auth,
            simulation_session=session,
            mock_transport=transport,
            candidate_root=root,
            store=store,
            new_batch="new",
        )
    assert not constructed


def test_catalog_tracks_renamed_case_and_actual_event_positions():
    batch = protocol_batch()
    case, result = batch["cases"][0], batch["results"][0]
    case["id"] = result["case_id"] = "unseen-case"
    result["id"] = "unseen-result"
    result["evidence"]["events"] = [{"payload": []}, {"payload": {"new/field": "fact"}}]
    client = ScriptedModel(
        [response(case, "result:unseen-result#/evidence/events/1/payload/new~1field")]
    )
    grade = judge_result(batch, case, result, client)
    data = json.loads(client.requests[0].messages[0].blocks[0].text)
    assert grade["status"] == "scored"
    assert (
        "result:unseen-result#/evidence/events/1/payload/new~1field"
        in data["reference_catalog"]
    )
    assert (
        "result:unseen-result#/evidence/events/0/payload/new~1field"
        not in data["reference_catalog"]
    )
    assert "setup" not in data["sources"]["case:unseen-case"]
    assert set(data["sources"]) == {
        "case:unseen-case",
        "result:unseen-result",
        "rubric:" + batch["rubric"]["id"],
    }


@pytest.mark.parametrize("defect", ["sensitive_value", "sensitive_field", "capacity"])
def test_unsafe_or_oversize_material_never_reaches_judge(defect):
    batch = protocol_batch()
    case, result = batch["cases"][0], batch["results"][0]
    if defect == "sensitive_value":
        result["output"] = "synthetic-secret-reference-test-84"
    elif defect == "sensitive_field":
        result["evidence"]["password"] = "synthetic"
    else:
        result["evidence"]["large"] = list(range(10001))
    client = ScriptedModel(["{}"])
    with pytest.raises(ValueError, match="sensitive_data|reference_catalog_limit"):
        judge_result(
            batch, case, result, client, secrets=("synthetic-secret-reference-test-84",)
        )
    assert not client.requests


def test_resolvable_wrong_judgment_is_not_semantic_calibration():
    from agent_alfred.evals.acceptance.report import report
    from agent_alfred.evals.acceptance.schema import digest

    batch = protocol_batch()
    case, result = batch["cases"][0], batch["results"][0]
    case["gold"] = {
        "exact_format": {
            "version": 1,
            "kind": "exact_lines",
            "value": ["alpha", "beta"],
        }
    }
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    result["output"] = "extra\nalpha\nbeta"
    grade = judge_result(
        batch,
        case,
        result,
        ScriptedModel([response(case, "result:" + result["id"] + "#/output")]),
    )
    assert grade["status"] == "scored"  # A parseable fixture, not a correct judgment.
    assert grade["judge_conflicts"] == ["completion", "correctness"]
    batch["grades"] = [grade]
    actual = report(batch)
    assert actual["v1_release"]["verdict"] == "FAIL"
    assert "adjudication_required:conversation" in actual["v1_release"]["blockers"]
    assert batch["calibration"] is None


def test_new_protocol_cannot_reuse_old_authorization_or_calibration(tmp_path):
    from agent_alfred.evals.acceptance.budget import binding, validate_authorization
    from agent_alfred.evals.acceptance.judge_protocol import current_profile
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.evals.deterministic.test_acceptance_c4 import (
        declared_online_fixture,
        import_pair,
    )

    source = declared_online_fixture("calibration", 5, "calibration")
    store = EvidenceStore(tmp_path)
    formal = import_pair(store, source)
    old_auth = {"binding": binding(formal)}
    formal["judge_profile"] = current_profile(formal["profiles"][0]["judge_model"])
    with pytest.raises(ValueError, match="authorization_mismatch"):
        validate_authorization(old_auth, binding(formal))
    # Only the calibration pointer is reused; this formal input has no scores.
    formal["grades"] = []
    formal["batch_id"] = "new-formal"
    for result in formal["results"]:
        result["batch_id"] = formal["batch_id"]
    with pytest.raises(ValueError, match="judge_requires_recalibration"):
        store.import_batch(formal)


def test_current_protocol_missing_rubric_preserves_importable_error(tmp_path):
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    batch["rubric"] = None
    client = ScriptedModel([])
    grade = judge_result(batch, batch["cases"][0], batch["results"][0], client)
    assert grade["error"] == "rubric_missing"
    assert not client.requests
    batch["grades"] = [grade]
    EvidenceStore(tmp_path).import_batch(batch)


def test_public_judge_rejects_case_result_mismatch_before_client_call():
    batch = protocol_batch()
    case = batch["cases"][0]
    result = batch["results"][1]
    client = ScriptedModel([])

    with pytest.raises(ValueError, match="case_result_mismatch"):
        judge_result(batch, case, result, client)

    assert not client.requests


@pytest.mark.parametrize("drift", ["json_number_type", "bool_integer_type", "content"])
def test_public_judge_rejects_same_id_case_identity_drift_before_client_call(drift):
    batch = r2()
    from agent_alfred.evals.acceptance.judge_protocol import current_profile

    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    canonical = batch["cases"][0]
    case = copy.deepcopy(canonical)
    result = batch["results"][0]
    if drift == "json_number_type":
        case["gold"]["exact_format"]["value"]["answer"] = 42.0
    elif drift == "bool_integer_type":
        case["gold"]["extra_text"] = 0
    else:
        case["input"] = case["input"] + " changed"
    assert case["id"] == canonical["id"]
    if drift != "content":
        assert case == canonical
    client = ScriptedModel([])

    with pytest.raises(ValueError, match="case_result_mismatch"):
        judge_result(batch, case, result, client)

    assert not client.requests


def test_public_judge_accepts_unchanged_case_copy():
    batch = protocol_batch()
    canonical = batch["cases"][0]
    case = copy.deepcopy(canonical)
    result = batch["results"][0]
    client = ScriptedModel([response(case, "result:" + result["id"] + "#/output")])

    grade = judge_result(batch, case, result, client)

    assert grade["status"] == "scored"
    assert len(client.requests) == 1


@pytest.mark.parametrize("drift", ["json_type", "content"])
def test_public_judge_rejects_same_id_result_identity_drift_before_client_call(drift):
    batch = protocol_batch()
    case = batch["cases"][0]
    canonical = batch["results"][0]
    result = copy.deepcopy(canonical)
    if drift == "json_type":
        result["recorded"] = 1
    else:
        result["output"] = str(result["output"]) + " changed"
    assert result["id"] == canonical["id"]
    client = ScriptedModel([])

    with pytest.raises(ValueError, match="result_identity_mismatch"):
        judge_result(batch, case, result, client)

    assert not client.requests


def test_public_judge_accepts_unchanged_result_copy():
    batch = protocol_batch()
    case = batch["cases"][0]
    result = copy.deepcopy(batch["results"][0])
    client = ScriptedModel([response(case, "result:" + result["id"] + "#/output")])

    grade = judge_result(batch, case, result, client)

    assert grade["status"] == "scored"
    assert len(client.requests) == 1


def test_legacy_grade_without_case_binding_is_rejected(tmp_path):
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    del batch["judge_profile"]
    case, result = batch["cases"][0], batch["results"][0]
    grade = judge_result(
        batch,
        case,
        result,
        ScriptedModel([response(case, "result:" + result["id"] + "#/output")]),
    )
    grade.pop("case_hash")
    grade.pop("case_material_id")
    batch["grades"] = [grade]

    with pytest.raises(ValueError, match="legacy_grade_unbound"):
        EvidenceStore(tmp_path).import_batch(batch)


def test_legacy_grade_case_binding_rejects_same_id_case_drift(tmp_path):
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    del batch["judge_profile"]
    case, result = batch["cases"][0], batch["results"][0]
    grade = judge_result(
        batch,
        case,
        result,
        ScriptedModel([response(case, "result:" + result["id"] + "#/output")]),
    )
    batch["grades"] = [grade]
    case["input"] = case["input"] + " changed"
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})

    with pytest.raises(ValueError, match="judge_material_mismatch"):
        EvidenceStore(tmp_path).import_batch(batch)


def test_legacy_grade_format_check_drift_is_rejected(tmp_path):
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    del batch["judge_profile"]
    case, result = batch["cases"][0], batch["results"][0]
    case["gold"] = {
        "exact_format": {
            "version": 1,
            "kind": "exact_lines",
            "value": ["alpha", "beta"],
        }
    }
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    result["output"] = "alpha\nbeta"
    grade = judge_result(
        batch,
        case,
        result,
        ScriptedModel([response(case, "result:" + result["id"] + "#/output")]),
    )
    assert grade["format_check"]["status"] == "pass"
    grade["format_check"] = {"status": "not_applicable"}
    batch["grades"] = [grade]

    with pytest.raises(ValueError, match="format_check_mismatch"):
        EvidenceStore(tmp_path).import_batch(batch)


def test_calibration_grade_binding_cannot_be_forged(tmp_path):
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.evals.deterministic.test_acceptance_c4 import (
        declared_online_fixture,
    )

    batch = declared_online_fixture("calibration", 5, "calibration")
    batch["grades"][0]["case_hash"] = "f" * 64
    batch["grades"][0]["case_material_id"] = "e" * 64

    with pytest.raises(ValueError, match="judge_material_mismatch"):
        EvidenceStore(tmp_path).import_batch(batch)


def test_regrade_grade_binding_cannot_be_forged(tmp_path):
    from copy import deepcopy

    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    case, result = batch["cases"][0], batch["results"][0]
    grade = judge_result(
        batch,
        case,
        result,
        ScriptedModel([response(case, "result:" + result["id"] + "#/output")]),
    )
    batch["grades"] = [grade]
    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    revised = deepcopy(batch)
    revised["batch_id"] = "forged-regrade"
    revised["parent"] = {
        "batch_id": batch["batch_id"],
        "relation": "regrade",
        "sha256": digest(batch),
    }
    revised["grades"][0]["case_hash"] = "f" * 64
    with pytest.raises(ValueError, match="judge_material_mismatch"):
        store.import_batch(revised)


def test_protocol_grade_rejects_forged_format_check_reference(tmp_path):
    from agent_alfred.evals.acceptance.judge_protocol import current_profile
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    case, result = batch["cases"][0], batch["results"][0]
    case["gold"] = {
        "exact_format": {
            "version": 1,
            "kind": "json_object",
            "value": {"answer": 42},
        }
    }
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    result["output"] = '{"answer":42}'
    grade = judge_result(
        batch,
        case,
        result,
        ScriptedModel([response(case, "result:" + result["id"] + "#/output")]),
    )
    assert grade["status"] == "scored"
    batch["grades"] = [grade]
    grade["format_check"]["evidence"][0] = "result:" + result["id"] + "#/missing"

    with pytest.raises(ValueError, match="reference|format_check"):
        EvidenceStore(tmp_path).import_batch(batch)


@pytest.mark.parametrize("protocol_value", [None, {"version": "unknown"}])
def test_import_rejects_unknown_or_null_explicit_protocol(tmp_path, protocol_value):
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch = protocol_batch()
    profile = batch["judge_profile"]
    profile["protocol"] = protocol_value
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    with pytest.raises(ValueError, match="judge_protocol_mismatch"):
        EvidenceStore(tmp_path).import_batch(batch)
