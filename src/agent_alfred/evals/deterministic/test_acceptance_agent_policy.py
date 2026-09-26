"""Agent roles are explicit provenance; synthetic fixtures are not real rulings."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.review_policy import descriptor
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance import scored_fixture
from agent_alfred.evals.deterministic.test_acceptance_disputes import opinion


def actor(role, output="synthetic-only"):
    return {
        "kind": "agent",
        "role": role,
        "agent_id": "fixture-" + role,
        "model": "gpt-6-astra",
        "reasoning_effort": "xhigh",
        "fork_turns": "none",
        "input_sha256": "0" * 64,
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "started_at": "2026-09-23T00:10:00+00:00",
        "completed_at": "2026-09-23T00:20:00+00:00",
        "reference": "OFFLINE SYNTHETIC PROVENANCE ONLY",
    }


def agent_ruling(review, outcome="insufficient_evidence"):
    owner = actor("adjudicator")
    owner.update(
        started_at="2026-09-23T00:30:00+00:00", completed_at="2026-09-23T00:40:00+00:00"
    )
    value = {
        "version": 2,
        "kind": "agent_review_adjudication",
        "review_id": review["id"],
        "policy": descriptor(),
        "actor": owner,
        "blind_annotations": [
            {"actor": actor(r), "output": "synthetic-only"}
            for r in ("blind_a", "blind_b")
        ],
        "at": owner["completed_at"],
        "reason": "Synthetic insufficient evidence.",
        "outcome": outcome,
        "evidence": review["evidence"],
    }
    return {**value, "id": digest(value)}


def test_agent_insufficient_disposition_stays_pending_without_rewriting_judge(tmp_path):
    store = EvidenceStore(tmp_path)
    batch = scored_fixture()
    store.import_batch(batch)
    review = opinion(batch)
    store.revise(batch["batch_id"], "reviewed", reviews=[review])
    ruling = agent_ruling(review)
    revised = store.revise(
        "reviewed", "agent-adjudicated", review_adjudications=[ruling]
    )
    assert revised["grades"] == batch["grades"]
    assert "human" not in revised["review_adjudications"][0]
    summary = report(revised, store=store, now=datetime(2026, 9, 23, 1, tzinfo=UTC))
    assert "adjudication_required:conversation" in summary["quality"]["blockers"]
    assert not summary["quality"]["failures"]
    assert summary["quality"]["review_disputes"][0]["status"] == "pending"
    assert store.read(batch["batch_id"]) == batch


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["actor"].update(model="fallback"),
        lambda r: r["actor"].update(reasoning_effort="high"),
        lambda r: r["actor"].update(fork_turns="all"),
        lambda r: r["actor"].update(agent_id="fixture-blind_a"),
        lambda r: r.update(human="agent pretending to be human"),
        lambda r: r["blind_annotations"][1]["actor"].update(input_sha256="1" * 64),
        lambda r: r["blind_annotations"][0].update(output="changed after sealing"),
        lambda r: r["actor"].update(started_at="2026-09-23T00:15:00+00:00"),
    ],
)
def test_import_rejects_nonindependent_or_tampered_agent_roles(tmp_path, mutation):
    store = EvidenceStore(tmp_path)
    batch = scored_fixture()
    store.import_batch(batch)
    review = opinion(batch)
    store.revise(batch["batch_id"], "reviewed", reviews=[review])
    ruling = deepcopy(agent_ruling(review))
    mutation(ruling)
    ruling["id"] = digest({k: v for k, v in ruling.items() if k != "id"})
    with pytest.raises(ValueError):
        store.revise("reviewed", "bad", review_adjudications=[ruling])


def test_legacy_approval_contract_does_not_silently_gain_agent_authority():
    from agent_alfred.evals.acceptance.schema import validate

    batch = scored_fixture()
    batch["rubric"]["approval"]["kind"] = "agent_approved"
    batch["rubric"]["id"] = digest(
        {k: v for k, v in batch["rubric"].items() if k != "id"}
    )
    batch["grades"] = []
    with pytest.raises(ValueError, match="approval_identity_missing"):
        validate(batch)
