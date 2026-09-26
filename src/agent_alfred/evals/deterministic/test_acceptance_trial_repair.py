"""Offline replay of r1 failures; never a replacement online grade or sample."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.judge import judge_result
from agent_alfred.model import ScriptedModel


def r1():
    return json.loads((Path(__file__).parent / "fixtures/trial_r1.json").read_text())


@pytest.mark.parametrize("index", [0, 1, 2, 3, 5])
def test_r1_invalid_judge_bytes_remain_errors(index):
    batch = r1()
    raw = batch["grades"][index]["raw"]
    grade = judge_result(
        batch, batch["cases"][index], batch["results"][index], ScriptedModel([raw])
    )
    assert grade["status"] == "error"
    assert grade["raw"] == raw


def test_judge_request_supplies_complete_nested_schema_and_no_fences():
    batch = r1()
    client = ScriptedModel([batch["grades"][4]["raw"]])
    grade = judge_result(batch, batch["cases"][4], batch["results"][4], client)
    assert grade["status"] == "scored"
    data = json.loads(client.requests[0].messages[0].blocks[0].text)
    schema = data["response_schema"]
    assert set(schema["required"]) == {
        "dimensions",
        "prohibitions",
        "disputed",
        "suspected_safety",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["dimensions"]["required"]) == {
        "completion",
        "correctness",
        "selection",
    }
    assert set(schema["properties"]["prohibitions"]["required"]) == set(
        batch["cases"][4]["forbidden"]
    )
    instructions = client.requests[0].system[0].text
    assert "Markdown fences" in instructions
    assert not client.requests[0].tools


@pytest.mark.parametrize("defect", ["duplicate", "extra", "wrong_applicability"])
def test_judge_rejects_ambiguous_or_inapplicable_scores(defect):
    batch = r1()
    value = json.loads(batch["grades"][4]["raw"])
    if defect == "extra":
        value["dimensions"]["completion"]["disputed"] = False
    elif defect == "wrong_applicability":
        value["dimensions"]["selection"]["status"] = "na"
    raw = json.dumps(value)
    if defect == "duplicate":
        raw = raw.replace('"disputed": false', '"disputed": true, "disputed": false')
    grade = judge_result(
        batch, batch["cases"][4], batch["results"][4], ScriptedModel([raw])
    )
    assert grade["status"] == "error"
    assert grade["raw"] == raw


def test_aggregation_judge_receives_actual_source_body_from_dispatched_request(
    tmp_path,
):
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.runner import run_offline

    batch = offline_batch()
    case = batch["cases"][-1]
    case["setup"] = deepcopy(r1()["cases"][-1]["setup"])
    sampled = run_offline(batch, tmp_path)
    result = sampled["results"][-1]
    inputs = result["evidence"]["aggregation_inputs"]
    assert inputs["status"] == "available"
    source = inputs["attempts"][0]["provided_sources"][0]
    assert source["label"] == "S1"
    assert case["setup"]["fact"] in json.dumps(source, ensure_ascii=False)
    confirmed = result["evidence"]["memory"]["input_attempts"]
    assert inputs["attempts"][0]["attempt_id"] in {
        a["attempt_id"] for a in confirmed if a["purpose"] == "aggregation"
    }
    # Judge input must carry the captured body, not a reconstructed setup value.
    sampled["rubric"] = r1()["rubric"]
    sampled["authorization"] = {"max_output_tokens": 4096}
    client = ScriptedModel(["{}"])
    judge_result(sampled, case, result, client)
    payload = json.loads(client.requests[0].messages[0].blocks[0].text)
    assert payload["product_result"]["evidence"]["aggregation_inputs"] == inputs


def test_aggregation_without_sent_sources_does_not_invent_evidence(tmp_path):
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.runner import run_offline

    batch = offline_batch()
    batch["cases"][-1]["setup"] = {}
    sampled = run_offline(batch, tmp_path)
    result = sampled["results"][-1]
    assert result["evidence"]["aggregation_inputs"] == {
        "status": "unavailable",
        "attempts": [],
    }
    assert result["evidence"]["attempts"] == []


def test_judge_json_mode_reaches_real_wire_without_changing_product_default():
    from dataclasses import replace

    import httpx2 as httpx

    from agent_alfred.clock import FakeClock
    from agent_alfred.endpoint_factory import EndpointClientFactory
    from agent_alfred.evals.deterministic.test_endpoint_factory import snapshot
    from agent_alfred.model import ModelRef, ModelRequest

    batch = r1()
    batch["profiles"][0]["judge_model"]["response_format"] = "json_object"
    seen = []

    def send(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "offline-json-test",
                "choices": [
                    {
                        "message": {"content": batch["grades"][4]["raw"]},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        factory = EndpointClientFactory(clock=FakeClock(), http_client=http)
        config = replace(
            snapshot(endpoint="deepseek", model="deepseek-v4-pro"), stream=False
        )
        try:
            client = factory.create(config)
            grade = judge_result(batch, batch["cases"][4], batch["results"][4], client)
            assert grade["status"] == "scored"
            client.respond(
                ModelRequest(ModelRef("deepseek", "deepseek-v4-pro"), None, ())
            )
        finally:
            factory.close()
    assert seen[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in seen[1]


@pytest.mark.parametrize("defect", ["product", "anthropic", "unknown"])
def test_trial_json_mode_is_bound_and_rejects_unsupported_uses(defect):
    from agent_alfred.evals.acceptance.schema import digest, validate
    from agent_alfred.evals.deterministic.test_acceptance_trial import trial_batch

    batch = trial_batch()
    profile = batch["profiles"][0]
    model = (
        profile["product_models"][0] if defect == "product" else profile["judge_model"]
    )
    model["response_format"] = "json_object"
    if defect == "anthropic":
        model["wire_style"] = "anthropic"
    if defect == "unknown":
        model["response_format"] = "anything"
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    with pytest.raises(ValueError, match="unsupported_response_format"):
        validate(batch)


@pytest.mark.parametrize("variant", ["r1", "other_task", "compliant"])
def test_format_valid_judge_cannot_pass_exact_lines_with_extra_prose(variant):
    batch = r1()
    case, result = batch["cases"][3], batch["results"][3]
    # Synthetic protocol-valid variant for a quality counterexample, NOT a
    # repaired or replacement grade for the historical online batch.
    original = json.loads(batch["grades"][3]["raw"])
    value = {
        "dimensions": {k: original[k] for k in case["applicability"]},
        **{k: original[k] for k in ("prohibitions", "disputed", "suspected_safety")},
    }
    if variant == "other_task":
        case["gold"]["lines"] = ["total: 17", "pending: 2"]
        result["output"] = "Here you go:\ntotal: 17\npending: 2"
    elif variant == "compliant":
        result["output"] = "\n".join(case["gold"]["lines"])
    case["gold"]["exact_format"] = {
        "version": 1,
        "kind": "exact_lines",
        "value": case["gold"]["lines"],
    }
    raw = json.dumps(value)
    grade = judge_result(batch, case, result, ScriptedModel([raw]))
    assert grade["raw"] == raw
    assert grade["status"] == "scored"
    assert grade["dimensions"] == value["dimensions"]
    if variant == "compliant":
        assert grade["format_check"]["status"] == "pass"
        assert grade["judge_conflicts"] == []
    else:
        assert grade["format_check"]["status"] == "fail"
        assert grade["judge_conflicts"] == ["completion", "correctness"]


def test_report_keeps_format_failure_and_requires_resolution_of_judge_conflict():
    from agent_alfred.evals.acceptance.report import report
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.deterministic.test_acceptance import scored_fixture

    batch = scored_fixture()
    case, result, grade = batch["cases"][0], batch["results"][0], batch["grades"][0]
    case["gold"] = {
        "exact_format": {
            "version": 1,
            "kind": "exact_lines",
            "value": ["alpha", "beta"],
        }
    }
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    result["output"] = "Introduction\nalpha\nbeta"
    grade["result_hash"] = digest(result)
    grade["case_hash"] = digest(case)
    grade["case_material_id"] = case["material_id"]
    before = deepcopy(batch)
    evaluated = report(batch)
    assert "exact_format_failed:conversation" in evaluated["v1_release"]["failures"]
    assert "adjudication_required:conversation" in evaluated["v1_release"]["blockers"]
    assert evaluated["v1_release"]["verdict"] == "FAIL"
    assert batch == before


def test_skill_formats_reach_host_without_rewriting_noncompliant_output(tmp_path):
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.runner import run_offline
    from agent_alfred.evals.acceptance.schema import digest

    batch = offline_batch()
    original = r1()
    case = batch["cases"][3]
    case.update(
        input=original["cases"][3]["input"], setup=original["cases"][3]["setup"]
    )
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    case["script"][-1] = original["results"][3]["output"]
    sampled = run_offline(batch, tmp_path)
    result = sampled["results"][3]
    assert result["output"] == original["results"][3]["output"]
    systems = [
        s
        for e in result["evidence"]["events"]
        for s in e["payload"].get("skill_system", [])
    ]
    assert any(
        "format constraints apply to the entire final reply" in s for s in systems
    )
    assert any(case["setup"]["skill"] in s for s in systems)
    assert len(result["evidence"]["attempts"]) == 2


@pytest.mark.parametrize(
    "output,expected",
    [
        ('{"answer":42,"unit":"个"}', "pass"),
        ('{"unit":"个", "answer":42}', "pass"),
        ('```json\n{"answer":42,"unit":"个"}\n```', "fail"),
        ('{"answer":0,"answer":42,"unit":"个"}', "fail"),
        ('{"answer":42,"unit":"个","extra":true}', "fail"),
        ('{"answer":42,"unit":"个"}\nextra', "fail"),
    ],
)
def test_predeclared_json_format_preserves_output_and_records_mechanical_fact(
    output, expected
):
    batch = r1()
    case, result = batch["cases"][4], batch["results"][4]
    case["gold"]["exact_format"] = {
        "version": 1,
        "kind": "json_object",
        "value": {"answer": 42, "unit": "个"},
    }
    result["output"] = output
    grade = judge_result(
        batch, case, result, ScriptedModel([batch["grades"][4]["raw"]])
    )
    assert grade["format_check"]["status"] == expected
    assert result["output"] == output


def test_old_gold_without_format_contract_is_not_reinterpreted():
    batch = r1()
    original = deepcopy(batch)
    grade = judge_result(
        batch,
        batch["cases"][3],
        batch["results"][3],
        ScriptedModel([batch["grades"][3]["raw"]]),
    )
    assert grade["format_check"] == {"status": "not_applicable"}
    assert grade["status"] == "error"
    assert batch == original


@pytest.mark.parametrize(
    "rule",
    [
        {"version": 99, "kind": "exact_lines", "value": ["ok"]},
        {"version": 1, "kind": "regexp", "value": ["ok"]},
        {"version": 1, "kind": "exact_lines", "value": ["one\ntwo"]},
        {"version": True, "kind": "exact_lines", "value": ["ok"]},
    ],
)
def test_invalid_format_contract_is_rejected_before_execution(rule):
    from agent_alfred.evals.acceptance.examples import offline_batch
    from agent_alfred.evals.acceptance.schema import digest, validate

    batch = offline_batch()
    case = batch["cases"][0]
    case["gold"] = {"exact_format": rule}
    case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
    with pytest.raises(ValueError, match="invalid_exact_format"):
        validate(batch)


@pytest.mark.parametrize("defect", ["duplicate", "extra", "wrong_applicability"])
def test_public_import_rejects_same_invalid_judge_protocol_as_live_path(
    tmp_path,
    defect,
):
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.evals.deterministic.test_acceptance import scored_fixture

    batch = scored_fixture()
    grade = batch["grades"][0]
    value = {
        k: deepcopy(grade[k])
        for k in ("dimensions", "prohibitions", "disputed", "suspected_safety")
    }
    if defect == "extra":
        value["comment"] = "must not be accepted"
    elif defect == "wrong_applicability":
        value["dimensions"]["selection"]["status"] = "pass"
        grade["dimensions"]["selection"]["status"] = "pass"
    raw = json.dumps(value)
    if defect == "duplicate":
        raw = raw.replace('"disputed": false', '"disputed": true, "disputed": false')
    grade["raw"] = raw
    with pytest.raises(ValueError, match="grade_raw_mismatch"):
        EvidenceStore(tmp_path).import_batch(batch)
    assert grade["raw"] == raw


def test_simulation_public_product_and_judge_do_not_retry_transport_errors(
    tmp_path, monkeypatch
):
    import httpx2 as httpx

    import agent_alfred.evals.acceptance.online_judge as online_judge
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.schema import digest
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.evals.deterministic.test_acceptance_trial import (
        authorize,
    )

    root = Path(__file__).resolve().parents[4]
    from agent_alfred.evals.acceptance.examples_v3 import controlled_batch

    batch = controlled_batch()
    batch["candidate"] = capture(root)
    batch["candidate_id"] = digest(batch["candidate"])
    profile = batch["profiles"][0]
    for model, name in [
        (profile["product_models"][0], "deepseek-flash"),
        (profile["judge_model"], "deepseek-v4-pro"),
    ]:
        model.update(endpoint_id="deepseek", model_id=name, thinking="disabled")
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    from agent_alfred.evals.acceptance.judge_protocol import current_profile

    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    authorize(batch)
    seen = []

    def send(request):
        seen.append(request.content)
        return httpx.Response(
            503, json={"error": {"message": "offline temporary error"}}
        )

    store = EvidenceStore(tmp_path / "evidence")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-wire-credential-84")
    from agent_alfred.evals.deterministic._simulation_test_helpers import session_for

    session = session_for(batch, store)
    transport = httpx.MockTransport(send)

    sampled = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
    )
    assert len(sampled["results"]) == 1
    assert all(result["outcome"] != "completed" for result in sampled["results"])
    assert seen and len(seen) == len(set(seen)), (
        "product repeated a failed HTTP request"
    )
    product_count = len(seen)
    store.import_batch(sampled)
    with pytest.raises(ValueError, match="execution_infrastructure_failure"):
        online_judge.grade_batch(
            sampled,
            sampled["authorization"],
            simulation_session=session,
            mock_transport=transport,
            candidate_root=root,
            store=store,
            new_batch="single-attempt-judge",
        )
    graded = sampled
    assert graded["grades"] == []
    assert len(seen) == product_count == 1
    assert graded["stop_reason"] == "execution_infrastructure_failure"
    assert len(graded["requests"]) == len(seen)
    assert len(seen) == len(set(seen)), "judge repeated a failed HTTP request"
