"""Public regressions for interruption and calibration request time identity."""

from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.budget import binding
from agent_alfred.evals.acceptance.candidate import capture
from agent_alfred.evals.acceptance.online_judge import grade_batch
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance import scored_fixture
from agent_alfred.evals.deterministic.test_acceptance_c4 import (
    declared_online_fixture,
    import_pair,
)


def test_judge_control_interruption_stops_batch_and_preserves_first_attempt(tmp_path):
    root = Path(__file__).resolve().parents[4]
    batch = scored_fixture()
    batch["grades"] = []
    from agent_alfred.evals.acceptance.judge_protocol import current_profile

    batch["judge_profile"] = current_profile(batch["profiles"][0]["judge_model"])
    batch["candidate"] = capture(root)
    batch["candidate_id"] = digest(batch["candidate"])
    auth = {
        "binding": binding(batch),
        "by": "fixture",
        "at": "2026-09-20T00:00:00Z",
        "max_requests": 20,
        "max_output_tokens": 256,
        "total_seconds": 60,
        "cost": {"amount": None, "source": "unknown"},
        "accept_unknown_cost": True,
    }
    import httpx2 as httpx

    from agent_alfred.evals.deterministic._simulation_test_helpers import session_for

    dispatched = []

    def interrupt(request):
        dispatched.append(request)
        raise KeyboardInterrupt()

    store = EvidenceStore(tmp_path)
    store.import_batch(batch)
    session = session_for(
        {**batch, "authorization": auth}, store, operations=("judge",)
    )
    actual = grade_batch(
        batch,
        auth,
        simulation_session=session,
        mock_transport=httpx.MockTransport(interrupt),
        candidate_root=root,
        store=store,
        new_batch="interrupted",
    )
    saved = store.revise(
        batch["batch_id"], "interrupted", grades=actual["grades"], execution=actual
    )
    assert len(dispatched) == len(saved["requests"]) == len(saved["grades"]) == 1
    assert saved["stop_reason"] == "judge_interrupted"
    assert saved["grades"][0]["attempt_ids"] == [saved["requests"][0]["attempt_id"]]
    assert saved["grades"][0]["error"] == "judge_interrupted"
    assert len(saved["cases"]) == 6 and saved["results"] == batch["results"]
    assert store.read(batch["batch_id"]) == batch


@pytest.mark.parametrize("defect", ["old_product", "early_judge", "late_judge"])
def test_calibration_request_time_must_match_product_and_judgment_windows(
    tmp_path, defect
):
    source = declared_online_fixture("calibration", 5, "calibration")
    if defect == "old_product":
        source["authorization"]["at"] = "2026-09-01T00:00:00Z"
        source["budget_started_at"] = "2026-09-01T00:00:00Z"
        for request in source["requests"]:
            request["started_at"] = "2026-09-01T00:00:01Z"
    elif defect == "early_judge":
        source["requests"][1]["started_at"] = source["results"][0]["sampled_at"]
    else:
        source["requests"][1]["started_at"] = "2026-09-20T00:00:04Z"
    with pytest.raises(ValueError, match="calibration_request_time_mismatch"):
        import_pair(EvidenceStore(tmp_path), source)
