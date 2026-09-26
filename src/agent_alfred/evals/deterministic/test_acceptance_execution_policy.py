"""Schema3 execution contracts through public entry points and offline SDK IO."""

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.schema import digest, validate
from agent_alfred.evals.deterministic._simulation_test_helpers import session_for


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch):
    # No inherited provider or integration credential reaches these tests.
    monkeypatch.setattr(
        os,
        "environ",
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DEEPSEEK_API_KEY": "synthetic-wire-credential-84",
            "TAVILY_API_KEY": "synthetic-unrelated-integration-84",
        },
    )


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "extra",
        "retry",
        "sdk_retry",
        "stream",
        "fallback",
        "credentials",
        "infra",
        "mask",
        "external",
        "boolean_version",
    ],
)
def test_public_validate_rejects_unfrozen_execution_policy(defect):
    batch = controlled_batch()
    profile = batch["profiles"][0]
    changes = {
        "extra": {"unbound_option": True},
        "retry": {"max_retries": 1},
        "sdk_retry": {"sdk_max_retries": 1},
        "stream": {"stream": True},
        "fallback": {"stream_fallback": True},
        "credentials": {"credential_scope": ["deepseek", "tavily"]},
        "infra": {"stop_on_infrastructure_error": False},
        "mask": {"require_case_tool_mask": False},
        "external": {"allow_external_business_effects": True},
        "boolean_version": {"version": True},
    }
    if defect == "missing":
        del profile["execution_policy"]
    else:
        profile["execution_policy"].update(changes[defect])
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    original = deepcopy(batch)
    with pytest.raises(ValueError, match="invalid_execution_policy"):
        validate(batch)
    assert batch == original


def authorized_fixture():
    from agent_alfred.evals.acceptance.candidate import capture
    from agent_alfred.evals.deterministic.test_acceptance_trial import authorize

    root = Path(__file__).resolve().parents[4]
    batch = controlled_batch(capture(root))
    batch["cases"] = batch["cases"][:1]
    return authorize(batch), root


@pytest.mark.parametrize("failure", ["429", "timeout"])
def test_public_execute_and_grade_never_retry_a_failed_sdk_dispatch(tmp_path, failure):
    import httpx2 as httpx

    from agent_alfred.clock import SystemClock
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    observed = []

    def send(request):
        body = json.loads(request.content)
        observed.append(body)
        assert body["stream"] is False
        assert not body.get("tools")
        assert (
            request.headers["authorization"]
            == "Bearer offline-synthetic-credential-admission"
        )
        if failure == "timeout":
            raise httpx.ReadTimeout("offline controlled timeout", request=request)
        return httpx.Response(429, json={"error": {"message": "offline limit"}})

    store = EvidenceStore(tmp_path / "evidence")
    clock = SystemClock()
    transport = httpx.MockTransport(send)
    session = session_for(batch, store, clock=clock)
    sampled = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=transport,
        clock=clock,
        candidate_root=root,
        store=store,
    )
    assert sampled["results"][0]["outcome"] == "failed"
    assert len(observed) == len(sampled["requests"]) == 1
    assert sampled["stop_reason"] == "execution_infrastructure_failure"
    store.import_batch(sampled)
    with pytest.raises(ValueError, match="execution_infrastructure_failure"):
        grade_batch(
            sampled,
            sampled["authorization"],
            simulation_session=session,
            mock_transport=transport,
            clock=clock,
            candidate_root=root,
            store=store,
            new_batch="single-attempt-judge-v3",
        )
    graded = sampled
    assert graded["grades"] == []
    assert graded["requests"] == sampled["requests"]
    assert len(observed) == 1


@pytest.mark.parametrize("failure", ["401", "model_identity", "invalid_response"])
def test_infrastructure_failure_stops_later_steps_cases_and_judges(tmp_path, failure):
    import httpx2 as httpx

    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    batch["cases"].append(controlled_batch()["cases"][1])
    batch["authorization"]["binding"] = binding(batch)
    observed = []

    def send(request):
        body = json.loads(request.content)
        observed.append(body)
        if failure == "401":
            return httpx.Response(401, json={"error": {"message": "offline auth"}})
        if failure == "invalid_response":
            return httpx.Response(200, json={"id": "missing-choices"})
        return httpx.Response(
            200,
            json={
                "id": "offline-response",
                "model": "unapproved-actual-model",
                "choices": [
                    {
                        "message": {"content": "untrusted output"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    store = EvidenceStore(tmp_path / "evidence")
    transport = httpx.MockTransport(send)
    session = session_for(batch, store)
    sampled = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=transport,
        candidate_root=root,
        store=store,
    )
    assert len(observed) == len(sampled["requests"]) == 1
    assert len(sampled["results"]) == 1
    assert sampled["stop_reason"] == "execution_infrastructure_failure"
    assert sampled["results"][0]["output"] != "untrusted output"
    if failure == "model_identity":
        assert sampled["requests"][0]["provider_model_id"] == "unapproved-actual-model"
    store.import_batch(sampled)
    with pytest.raises(ValueError, match="execution_infrastructure_failure"):
        grade_batch(
            sampled,
            sampled["authorization"],
            simulation_session=session,
            mock_transport=transport,
            candidate_root=root,
            store=store,
            new_batch="stopped-judge-v3",
        )
    graded = sampled
    assert graded["grades"] == []
    assert graded["requests"] == sampled["requests"]
    assert len(observed) == 1


@pytest.mark.parametrize("boundary", ["request_limit", "original_deadline"])
def test_product_auxiliary_and_judge_share_budget_and_original_deadline(
    tmp_path,
    boundary,
):
    import time
    from datetime import UTC, datetime, timedelta

    import httpx2 as httpx

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.acceptance.budget import binding
    from agent_alfred.evals.acceptance.examples import SKIP
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    batch["cases"].append(controlled_batch()["cases"][1])
    profile = batch["profiles"][0]
    primary = profile["product_models"][0]
    profile["product_models"].append({**primary, "model_id": "deepseek-flash"})
    profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    batch["authorization"].update(binding=binding(batch), max_requests=5)
    observed = []

    def send(request):
        body = json.loads(request.content)
        observed.append(body)
        content = (
            SKIP
            if body["model"] == "deepseek-flash"
            else "{}"
            if body["model"] == profile["judge_model"]["model_id"]
            else "Controlled transport response"
        )
        return httpx.Response(
            200,
            json={
                "id": "offline-shared-budget",
                "model": body["model"],
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    clock = FakeClock(monotonic_value=time.monotonic(), wall=datetime.now(UTC))
    store = EvidenceStore(tmp_path / "evidence")
    transport = httpx.MockTransport(send)
    session = session_for(batch, store, clock=clock)
    sampled = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=transport,
        clock=clock,
        candidate_root=root,
        store=store,
    )
    assert len(sampled["results"]) == 2
    assert all(row["outcome"] == "completed" for row in sampled["results"])
    assert len(observed) == len(sampled["requests"]) == 4
    assert [body["model"] for body in observed] == [
        "deepseek-flash",
        primary["model_id"],
        "deepseek-flash",
        primary["model_id"],
    ]
    store.import_batch(sampled)
    if boundary == "original_deadline":
        elapsed = batch["authorization"]["total_seconds"] + 1
        clock.wall += timedelta(seconds=elapsed)
        clock.monotonic_value += elapsed
    if boundary == "original_deadline":
        with pytest.raises(ValueError, match="batch_deadline"):
            grade_batch(
                sampled,
                sampled["authorization"],
                simulation_session=session,
                mock_transport=transport,
                candidate_root=root,
                store=store,
                new_batch="expired-judge",
            )
        assert len(observed) == 4
        assert session.snapshot()["requests"] == sampled["requests"]
        assert session.snapshot()["budget_started_at"] == sampled["budget_started_at"]
        return
    graded = grade_batch(
        sampled,
        sampled["authorization"],
        simulation_session=session,
        mock_transport=transport,
        clock=clock,
        candidate_root=root,
        store=store,
        new_batch="shared-budget-judge-v3",
    )
    saved = store.revise(
        sampled["batch_id"],
        "shared-budget-judge-v3",
        grades=graded["grades"],
        execution=graded,
    )
    count = 5 if boundary == "request_limit" else 4
    assert len(graded["requests"]) == len(observed) == count
    assert len(graded["grades"]) == count - 4
    assert graded["budget_started_at"] == sampled["budget_started_at"]
    assert graded["requests"][:4] == sampled["requests"]
    assert graded["stop_reason"] == "batch_budget_exhausted"
    assert store.read(saved["batch_id"])["requests"] == graded["requests"]


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("authorization", "authorization_missing"),
        ("binding", "authorization_mismatch"),
        ("unknown_cost", "unknown_cost_not_accepted"),
        ("stream_parameters", "execution_policy_parameter_mismatch"),
        ("endpoint", "execution_policy_model_scope"),
    ],
)
def test_execute_rejects_unapproved_dispatch_before_http(tmp_path, defect, reason):
    import httpx2 as httpx

    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    if defect == "authorization":
        batch["authorization"] = None
    elif defect == "binding":
        batch["cases"][0]["setup"]["local_tool_allowlist"] = ["draft_message"]
    elif defect == "unknown_cost":
        batch["authorization"]["accept_unknown_cost"] = False
    else:
        profile = batch["profiles"][0]
        if defect == "stream_parameters":
            profile["parameters"]["stream"] = True
        else:
            profile["product_models"][0].pop("thinking")
            profile["product_models"][0]["endpoint_id"] = "opencode-go"
        profile["id"] = digest({k: v for k, v in profile.items() if k != "id"})
    observed = []

    def send(request):
        observed.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        from agent_alfred.evals.acceptance.budget import AuthorizedBatch

        with pytest.raises(ValueError, match=reason):
            AuthorizedBatch(batch)
        with pytest.raises(ValueError, match="simulation_session_and_mock_required"):
            execute(
                batch,
                tmp_path / "runtime",
                http_client=http,
                candidate_root=root,
                store=EvidenceStore(tmp_path / "evidence"),
            )
    assert observed == []


def test_injected_sdk_retry_cannot_escape_schema3_dispatch_policy(tmp_path):
    import httpx2 as httpx

    from agent_alfred.clock import SystemClock
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.endpoint_factory import EndpointClientFactory
    from agent_alfred.evals.acceptance.runner import execute
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    observed = []

    def send(request):
        observed.append(json.loads(request.content))
        return httpx.Response(429, json={"error": {"message": "offline limit"}})

    clock = SystemClock()
    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        with pytest.raises(ValueError, match="simulation_session_and_mock_required"):
            execute(
                batch,
                tmp_path / "runtime",
                clock=clock,
                product_factory_builder=lambda: EndpointClientFactory(
                    clock=clock,
                    http_client=http,
                    max_retries=1,
                ),
                credentials=CredentialOverlay(os.environ, None),
                candidate_root=root,
                store=EvidenceStore(tmp_path / "evidence"),
            )
    assert observed == []
