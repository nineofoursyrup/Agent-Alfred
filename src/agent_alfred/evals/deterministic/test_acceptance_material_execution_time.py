"""Real-worktree entry points reject future synthetic material approvals."""

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_alfred.clock import FakeClock
from agent_alfred.evals.acceptance.candidate import capture
from agent_alfred.evals.acceptance.online_judge import grade_batch
from agent_alfred.evals.acceptance.runner import execute
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
    approve_cases,
    real_materials,
    rehash,
)
from agent_alfred.evals.deterministic.test_acceptance_trial import authorize


def inventory(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("entry", ["product", "judge"])
@pytest.mark.parametrize("field", ["case_set_approval", "semantic_rubric"])
def test_real_entry_rejects_future_material_before_factory_and_journal(
    tmp_path, monkeypatch, entry, field
):
    monkeypatch.setattr(
        os, "environ", {"DEVELOPER_DIR": "/Library/Developer/CommandLineTools"}
    )
    root = Path(__file__).resolve().parents[4]
    batch = real_materials()
    batch["candidate"] = capture(root)
    batch["candidate_id"] = digest(batch["candidate"])
    proof = (
        batch["semantic_rubric"]["approval"]
        if field == "semantic_rubric"
        else batch["case_set_approval"]
    )
    proof["actor"].update(
        started_at="2026-09-24T00:00:00Z",
        completed_at="2026-09-24T00:01:00Z",
    )
    proof["at"] = proof["actor"]["completed_at"]
    if field == "semantic_rubric":
        rehash(batch["semantic_rubric"])
        approve_cases(batch)
    authorize(batch)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    before = inventory(store.root)
    constructions = []

    def forbidden_factory():
        constructions.append(True)
        raise AssertionError("future approval reached model factory construction")

    clock = FakeClock(wall=datetime(2026, 9, 23, tzinfo=UTC))
    from agent_alfred.evals.acceptance.budget import validate_execution_materials

    with pytest.raises(ValueError, match="approval_after"):
        validate_execution_materials(
            batch, store, started_at=clock.wall_utc().isoformat()
        )
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
                new_batch="future-approval-rejected",
            )
    assert constructions == []
    assert inventory(store.root) == before
    assert store.read(batch["batch_id"]) == batch
    assert not (tmp_path / "runtime").exists()
