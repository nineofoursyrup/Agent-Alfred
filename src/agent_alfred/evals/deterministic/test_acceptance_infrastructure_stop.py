"""Infrastructure failures halt the shared batch through SDK and store entries."""

import json
from copy import deepcopy

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.budget import binding
from agent_alfred.evals.acceptance.examples import SKIP
from agent_alfred.evals.acceptance.examples_v3 import controlled_batch
from agent_alfred.evals.acceptance.online_judge import grade_batch
from agent_alfred.evals.acceptance.runner import execute
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_execution_policy import (
    authorized_fixture,
    synthetic_environment,  # noqa: F401
)


@pytest.mark.parametrize(
    "origin,failed_dispatch",
    [
        ("auxiliary", 1),
        ("product", 2),
        ("judge", 5),
    ],
)
@pytest.mark.parametrize("failure", [429, 500, 503, "timeout", "connection"])
def test_first_infrastructure_error_stops_all_later_dispatch_and_judge_resume(
    tmp_path,
    origin,
    failed_dispatch,
    failure,
):
    batch, root = authorized_fixture()
    batch["cases"].append(controlled_batch()["cases"][1])
    batch["authorization"]["binding"] = binding(batch)
    judge_id = batch["judge_profile"]["model"]["model_id"]
    wire = []

    def send(request):
        body = json.loads(request.content)
        wire.append(body)
        if len(wire) == failed_dispatch:
            if failure == "timeout":
                raise httpx.ReadTimeout("offline timeout", request=request)
            if failure == "connection":
                raise httpx.ConnectError("offline connection", request=request)
            return httpx.Response(failure, json={"error": {"message": "offline"}})
        content = SKIP if len(wire) % 2 else "Controlled product response"
        return httpx.Response(
            200,
            json={
                "id": "controlled",
                "model": body["model"],
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    store = EvidenceStore(tmp_path / "evidence")
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
    original_results = deepcopy(sampled["results"])
    store.import_batch(sampled)
    if origin == "judge":
        graded = grade_batch(
            store.read(sampled["batch_id"]),
            sampled["authorization"],
            simulation_session=session,
            mock_transport=transport,
            candidate_root=root,
            store=store,
            new_batch="stopped-judged",
        )
    else:
        with pytest.raises(ValueError, match="execution_infrastructure_failure"):
            grade_batch(
                sampled,
                sampled["authorization"],
                simulation_session=session,
                mock_transport=transport,
                candidate_root=root,
                store=store,
                new_batch="stopped-judged",
            )
        graded = sampled
    saved = store.revise(
        sampled["batch_id"],
        "stopped-judged",
        grades=graded["grades"],
        execution=graded,
    )
    readback = store.read(saved["batch_id"])
    assert len(wire) == len(graded["requests"]) == failed_dispatch
    assert graded["stop_reason"] == "execution_infrastructure_failure"
    assert readback["stop_reason"] == graded["stop_reason"]
    assert readback["results"] == original_results
    assert readback["requests"] == graded["requests"]
    assert graded["budget_started_at"] == sampled["budget_started_at"]
    if origin == "judge":
        assert wire[-1]["model"] == judge_id
        assert len(graded["grades"]) == 1
        assert graded["grades"][0]["error"] == "judge_unavailable"
        with pytest.raises(
            ValueError, match="operation_already_consumed|grant_not_active"
        ):
            grade_batch(
                graded,
                graded["authorization"],
                simulation_session=session,
                mock_transport=transport,
                candidate_root=root,
                store=store,
                new_batch="forbidden-resume",
            )
    else:
        assert graded["grades"] == []
        assert sampled["results"][0]["outcome"] == "failed"
        # A separate store cannot turn a persisted product stop into a
        # new judge opportunity or reset the shared accounting window.
        resumed_store = EvidenceStore(tmp_path / "resumed-evidence")
        resumed_store.import_batch(sampled)
        with pytest.raises(ValueError, match="approval_binding_mismatch"):
            grade_batch(
                resumed_store.read(sampled["batch_id"]),
                sampled["authorization"],
                simulation_session=session,
                mock_transport=transport,
                candidate_root=root,
                store=resumed_store,
                new_batch="resumed-stopped",
            )
        assert session.snapshot()["requests"] == sampled["requests"]
        assert session.snapshot()["budget_started_at"] == sampled["budget_started_at"]
    assert len(wire) == failed_dispatch
