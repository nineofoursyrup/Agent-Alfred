"""Explicit synthetic grants for public MockTransport acceptance tests."""

import json
from datetime import timedelta

import httpx2 as httpx

from agent_alfred.clock import SystemClock
from agent_alfred.evals.acceptance.admission import proposal
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority


def session_for(batch, store, *, clock=None, operations=("product", "judge")):
    clock = clock or SystemClock()
    authority = SimulationAuthority(
        store.root.parent / (store.root.name + "-synthetic-authority"), clock=clock
    )
    request = proposal(batch, output_scope=store.root, operations=operations)
    receipt = authority.issue(
        request,
        subject="simulation:test-fixture",
        source_event_id="explicit-synthetic-confirmation",
        source_event_digest="a" * 64,
        activation_deadline=(clock.wall_utc() + timedelta(seconds=60)).isoformat(),
        cost_acceptance={"amount": request["cost"]["amount"], "accept_unknown": True},
    )
    return authority.activate(receipt, request)


def text_transport(texts, seen):
    replies = iter(texts)

    def send(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "id": "synthetic-public-fixture",
                "model": body["model"],
                "choices": [
                    {"message": {"content": next(replies)}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    return httpx.MockTransport(send)


def budget_client(batch, root, transport, *, role="product", clock=None):
    """Bind the public budget seam to an explicit synthetic grant and transport."""
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.evals.acceptance.schema import judge_model
    from agent_alfred.evals.acceptance.store import EvidenceStore
    from agent_alfred.model import ClientSnapshot, ModelAssignment

    store = EvidenceStore(root / "evidence")
    session = session_for(batch, store, clock=clock)
    session.bind(batch, operation=role, output_scope=store.root)
    budget = AuthorizedBatch(batch, session=session, mock_transport=transport)
    profile = batch["profiles"][0]
    model = (
        profile["product_models"][0]
        if role == "product"
        else judge_model(batch, profile)
    )
    config = ClientSnapshot(
        "synthetic-budget",
        ModelAssignment(model["endpoint_id"], model["model_id"], model["wire_style"]),
        None,
        api_key="synthetic",
        stream=False,
        stream_fallback=False,
        overall_deadline_s=batch["authorization"]["total_seconds"],
        per_attempt_timeout_s=batch["authorization"]["total_seconds"],
    )
    return budget, budget.client(config, role=role)
