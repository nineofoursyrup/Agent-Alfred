"""Public budget regressions from the independent c27 review."""

import pytest

from agent_alfred.evals.acceptance.budget import AuthorizedBatch
from agent_alfred.evals.deterministic.test_acceptance_execution_policy import (
    authorized_fixture,
)


def configured_budget(tmp_path):
    from agent_alfred.evals.deterministic.test_acceptance_admission import (
        simulated_session,
    )
    from agent_alfred.model import ClientSnapshot, ModelAssignment

    batch, _ = authorized_fixture()
    _, _, _, session = simulated_session(tmp_path, batch)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    model = batch["profiles"][0]["product_models"][0]
    config = ClientSnapshot(
        "synthetic",
        ModelAssignment(model["endpoint_id"], model["model_id"], model["wire_style"]),
        None,
        "synthetic",
        False,
        False,
        60,
        60,
    )
    return AuthorizedBatch(batch, session=session), config


@pytest.mark.parametrize("field", ["stream", "stream_fallback"])
def test_budget_rejects_unsigned_stream_options(tmp_path, field):
    from dataclasses import replace

    import httpx2 as httpx

    budget, config = configured_budget(tmp_path)
    seen = []
    try:
        with pytest.raises(ValueError, match="approval_profile_mismatch"):
            budget.client(
                replace(config, **{field: True}),
                role="product",
                mock_transport=httpx.MockTransport(
                    lambda request: seen.append(request)
                ),
            )
        assert seen == []
        assert budget.session.snapshot()["requests"] == []
    finally:
        budget.close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_budget_factory_construction_failure_closes_owned_http(
    tmp_path,
    monkeypatch,
    failure,
):
    import httpx2 as httpx

    import agent_alfred.endpoint_factory as endpoint_factory

    budget, config = configured_budget(tmp_path)
    captured = []

    def fail_factory(**kwargs):
        captured.append(kwargs["http_client"])
        raise failure("synthetic_factory_failure")

    monkeypatch.setattr(endpoint_factory, "EndpointClientFactory", fail_factory)
    with pytest.raises(failure, match="synthetic_factory_failure"):
        budget.client(
            config, role="product", mock_transport=httpx.MockTransport(lambda _: None)
        )
    budget.close()
    assert len(captured) == 1
    assert captured[0].is_closed


def test_budget_close_retains_failed_transport_and_closes_other_clients(
    tmp_path,
    monkeypatch,
):
    import httpx2 as httpx

    budget, config = configured_budget(tmp_path)
    calls = []
    first = httpx.MockTransport(lambda _: None)
    second = httpx.MockTransport(lambda _: None)

    def close_first():
        calls.append("first")
        if calls.count("first") == 1:
            raise OSError("synthetic_transport_close_failure")

    monkeypatch.setattr(first, "close", close_first)
    monkeypatch.setattr(second, "close", lambda: calls.append("second"))
    budget.client(config, role="product", mock_transport=first)
    budget.client(config, role="product", mock_transport=second)
    with pytest.raises(OSError, match="synthetic_transport_close_failure"):
        budget.close()
    assert "second" in calls
    budget.close()
    assert calls.count("first") == 2
    assert calls.count("second") == 1
    budget.close()
    assert calls.count("first") == 2


def test_budget_rejects_unsigned_wire_style(tmp_path):
    from dataclasses import replace

    import httpx2 as httpx

    budget, config = configured_budget(tmp_path)
    try:
        with pytest.raises(ValueError, match="approval_profile_mismatch"):
            budget.client(
                replace(
                    config, primary=replace(config.primary, wire_style="anthropic")
                ),
                role="product",
                mock_transport=httpx.MockTransport(lambda _: None),
            )
    finally:
        budget.close()


def test_budget_without_session_rejects_builder_before_invocation():
    batch, _ = authorized_fixture()
    called = []
    with pytest.raises(ValueError, match="simulation_session_and_mock_required"):
        AuthorizedBatch(batch).client(lambda: called.append(True), role="product")
    assert called == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_output_tokens", 1000),
        ("max_requests", 1000),
        ("total_seconds", 1000),
        ("cost", {"amount": None, "source": "changed"}),
    ],
)
def test_budget_revalidates_authority_binding(tmp_path, field, value):
    from copy import deepcopy

    from agent_alfred.evals.deterministic.test_acceptance_admission import (
        simulated_session,
    )

    batch, _ = authorized_fixture()
    _, _, _, session = simulated_session(tmp_path, batch)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    changed = deepcopy(batch)
    changed["authorization"][field] = value
    with pytest.raises(ValueError, match="approval_binding_mismatch"):
        AuthorizedBatch(changed, session=session)


def test_concurrent_budget_snapshots_settle_only_their_own_attempts(tmp_path):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx2 as httpx

    from agent_alfred.evals.deterministic.test_acceptance_admission import (
        simulated_session,
    )
    from agent_alfred.messages import Message, TextBlock
    from agent_alfred.model import (
        ClientSnapshot,
        ModelAssignment,
        ModelRef,
        ModelRequest,
    )

    batch, _ = authorized_fixture()
    _, _, _, session = simulated_session(tmp_path, batch)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    admitted = [threading.Event(), threading.Event()]
    finish = [threading.Event(), threading.Event()]

    def send(request):
        body = json.loads(request.content)
        i = int(body["messages"][-1]["content"])
        admitted[i].set()
        assert finish[i].wait(10)
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "model": body["model"],
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    model = batch["profiles"][0]["product_models"][0]
    config = ClientSnapshot(
        "synthetic",
        ModelAssignment(model["endpoint_id"], model["model_id"], model["wire_style"]),
        None,
        api_key="synthetic",
        stream=False,
        stream_fallback=False,
        overall_deadline_s=60,
        per_attempt_timeout_s=60,
    )

    def request(i):
        return ModelRequest(
            model=ModelRef(model["endpoint_id"], model["model_id"]),
            system=None,
            messages=(Message("user", (TextBlock(str(i)),)),),
            max_tokens=100,
        )

    transport = httpx.MockTransport(send)
    budgets = []

    def new_client():
        budget = AuthorizedBatch(batch, session=session, mock_transport=transport)
        budgets.append(budget)
        return budget.client(config, role="product")

    try:
        first = new_client()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first.respond, request(0))
            try:
                assert admitted[0].wait(10)
                second = new_client()
                b = pool.submit(second.respond, request(1))
                assert admitted[1].wait(10)
                finish[0].set()
                assert a.result(timeout=10).attempts[0].outcome == "committed"
                finish[1].set()
                assert b.result(timeout=10).attempts[0].outcome == "committed"
            finally:
                for event in finish:
                    event.set()
    finally:
        for budget in budgets:
            budget.close()
    ledger = session.snapshot()["requests"]
    assert [r["outcome"] for r in ledger] == ["committed", "committed"]
    assert all(
        r["usage"]["total_input_tokens"] == 1 and r["usage"]["output_tokens"] == 1
        for r in ledger
    )


def test_budget_with_session_still_rejects_arbitrary_builder(tmp_path):
    from agent_alfred.evals.deterministic.test_acceptance_admission import (
        simulated_session,
    )

    batch, _ = authorized_fixture()
    _, _, _, session = simulated_session(tmp_path, batch)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    called = []
    with pytest.raises(ValueError, match="simulation_mock_transport_required"):
        AuthorizedBatch(batch, session=session).client(
            lambda: called.append(True), role="product"
        )
    assert called == []


def test_atomic_reservation_enforces_grant_token_cap(tmp_path):
    from agent_alfred.evals.deterministic._simulation_test_helpers import (
        budget_client,
        text_transport,
    )
    from agent_alfred.messages import Message, TextBlock
    from agent_alfred.model import ModelRef, ModelRequest

    batch, _ = authorized_fixture()
    batch["authorization"]["max_output_tokens"] = 100
    seen = []
    budget, client = budget_client(batch, tmp_path, text_transport([], seen))
    try:
        budget.max_output_tokens = 1000
        model = batch["profiles"][0]["product_models"][0]
        request = ModelRequest(
            ModelRef(model["endpoint_id"], model["model_id"]),
            None,
            (Message("user", (TextBlock("offline"),)),),
            max_tokens=1000,
        )
        with pytest.raises(ValueError, match="approval_profile_mismatch"):
            client.respond(request)
        with pytest.raises(ValueError, match="output_token_limit"):
            budget.start(
                "synthetic-oversized-reservation",
                "product",
                request.model,
                max_tokens=request.max_tokens,
            )
        assert seen == []
        assert budget.session.snapshot()["requests"] == []
    finally:
        budget.close()
