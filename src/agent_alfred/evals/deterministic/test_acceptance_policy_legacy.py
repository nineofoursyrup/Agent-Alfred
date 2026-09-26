"""Synthetic public-store counterexamples, never actual human rulings."""

import hashlib
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.reviews import binding
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_schema3 import scored_v3


@pytest.mark.parametrize("real_shape", [False, True])
@pytest.mark.parametrize("outcome", ["dismissed", "confirmed_violation"])
def test_schema3_rejects_legacy_review_ruling_before_append(
    tmp_path, real_shape, outcome
):
    batch = scored_v3(
        "calibration" if real_shape else "offline_fixture",
        5 if real_shape else 1,
        "policy-boundary",
    )
    if real_shape:
        from agent_alfred.evals.acceptance.judge import judge_result
        from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
            approve_cases,
            material_approval,
            rehash,
        )
        from agent_alfred.model import ScriptedModel

        batch["simulation"] = False
        for case in batch["cases"]:
            case.pop("script")
        batch["semantic_rubric"]["approval"] = material_approval(batch)
        rehash(batch["semantic_rubric"])
        approve_cases(batch)
        raw = [grade["raw"] for grade in batch["grades"]]
        batch["grades"] = []
        batch["authorization"] = {"max_output_tokens": 256}
        for case, result, text in zip(
            batch["cases"], batch["results"], raw, strict=True
        ):
            batch["grades"].append(
                judge_result(batch, case, result, ScriptedModel([text]))
            )
        batch["authorization"] = None
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    case, result, grade = (batch[key][0] for key in ("cases", "results", "grades"))
    content = "SYNTHETIC policy-boundary test only"
    review = {
        "version": 1,
        "kind": "agent_review_dispute",
        "binding": binding(batch, grade["id"]),
        "item": {"kind": "prohibitions", "name": case["forbidden"][0]},
        "reason": content,
        "evidence": ["result:" + result["id"] + "#/output"],
        "at": "2026-09-23T00:05:00Z",
        "source": {
            "kind": "agent",
            "name": "fixture",
            "reference": content,
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
    }
    review["id"] = digest(review)
    reviewed = store.revise(batch["batch_id"], "reviewed", reviews=[review])
    ruling = {
        "version": 1,
        "kind": "human_review_adjudication",
        "review_id": review["id"],
        "human": "SYNTHETIC legacy actor",
        "at": "2026-09-23T00:40:00Z",
        "reason": content,
        "outcome": outcome,
        "evidence": review["evidence"],
    }
    ruling["id"] = digest(ruling)
    with pytest.raises(ValueError, match="agent_adjudication_required"):
        store.revise("reviewed", "must-not-exist", review_adjudications=[ruling])
    assert not (store.root / "must-not-exist").exists()
    assert store.read("reviewed") == reviewed
    assert store.read(batch["batch_id"]) == batch
    quality = report(reviewed, store=store, now=datetime(2026, 9, 23, 1, tzinfo=UTC))[
        "quality"
    ]
    assert quality["review_disputes"][0]["status"] == "pending"
    assert "adjudication_required:" + case["id"] in quality["blockers"]
