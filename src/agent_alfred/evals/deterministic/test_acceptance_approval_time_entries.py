"""Actual-worktree product/judge preflight rejects approvals outside their window."""

import os
from pathlib import Path

import pytest

from agent_alfred.evals.acceptance.candidate import capture
from agent_alfred.evals.acceptance.online_judge import grade_batch
from agent_alfred.evals.acceptance.runner import execute
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_approval_cutoff import (
    FUTURE,
    formal_pair,
    inventory,
)
from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import rehash
from agent_alfred.evals.deterministic.test_acceptance_trial import authorize


@pytest.mark.parametrize("entry", ["product", "judge"])
@pytest.mark.parametrize(
    "field,when,reason",
    [
        ("aggregation", FUTURE, "aggregation_approval_after_execution"),
        ("calibration", FUTURE, "calibration_approval_after_execution"),
        ("authorization", FUTURE, "authorization_after_execution"),
        (
            "aggregation",
            "2026-09-23T00:00:00Z",
            "aggregation_approval_before_calibration",
        ),
        (
            "aggregation",
            "2026-09-23T01:00:00Z",
            "aggregation_approval_before_calibration",
        ),
        ("test_threshold", "2026-09-23T01:30:00Z", "approved_aggregation_missing"),
    ],
)
def test_formal_entries_reject_invalid_approval_before_factory_or_store_write(
    tmp_path,
    monkeypatch,
    entry,
    field,
    when,
    reason,
):
    monkeypatch.setattr(
        os, "environ", {"DEVELOPER_DIR": "/Library/Developer/CommandLineTools"}
    )
    root = Path(__file__).resolve().parents[4]
    source, batch, clock = formal_pair(field, when, candidate=capture(root))
    if field == "test_threshold":
        batch["aggregation_policy"]["approval"]["kind"] = "test"
        rehash(batch["aggregation_policy"])
        authorize(batch)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(source)
    before = inventory(store)
    constructions = []

    def forbidden_factory():
        constructions.append(True)
        raise AssertionError(
            "approval preflight reached factory; no client constructed"
        )

    from agent_alfred.evals.acceptance.budget import (
        AuthorizedBatch,
        validate_execution_materials,
    )

    # Historical material chronology still has its original public validator.
    # Online entry now fails earlier because local fields cannot prove consent.
    with pytest.raises(ValueError, match=reason):
        budget = AuthorizedBatch(batch, clock=clock)
        validate_execution_materials(batch, store, started_at=budget.started_at)
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        if entry == "product":
            execute(
                batch,
                tmp_path / "runtime",
                product_factory_builder=forbidden_factory,
                credentials={},
                clock=clock,
                candidate_root=root,
                store=store,
            )
        else:
            grade_batch(
                batch,
                batch["authorization"],
                factory_builder=forbidden_factory,
                clock=clock,
                candidate_root=root,
                store=store,
                new_batch="approval-cutoff-judge",
            )
    assert constructions == []
    assert batch.get("requests", []) == []
    assert inventory(store) == before
    assert store.read(source["batch_id"]) == source
    assert not (tmp_path / "runtime").exists()
