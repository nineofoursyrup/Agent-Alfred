"""Blind reviewers must have the target's completed output before starting."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from agent_alfred.evals.acceptance.report import report
from agent_alfred.evals.acceptance.reviews import binding
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic.test_acceptance_agent_policy import agent_ruling
from agent_alfred.evals.deterministic.test_acceptance_policy_provenance import (
    panel,
    rehash,
)
from agent_alfred.evals.deterministic.test_acceptance_schema3 import (
    aggregation_for,
    scored_v3,
)

NOW = datetime(2026, 9, 23, 2, tzinfo=UTC)


def scenario(store, route, *, other_finished_at=None):
    batch = scored_v3("offline_fixture", 1, "blind-time-" + route + "-")
    if other_finished_at is not None:
        from agent_alfred.evals.acceptance.judge import judge_result
        from agent_alfred.model import ScriptedModel

        batch["results"][0]["finished_at"] = other_finished_at
        batch["authorization"] = {"max_output_tokens": 256}
        batch["grades"][0] = judge_result(
            batch,
            batch["cases"][0],
            batch["results"][0],
            ScriptedModel([batch["grades"][0]["raw"]]),
        )
        batch["authorization"] = None
    target = len(batch["grades"]) - 1
    grade, result, case = (batch[key][target] for key in ("grades", "results", "cases"))
    if route == "grade":
        raw = json.loads(grade["raw"])
        raw["disputed"] = True
        grade.update(disputed=True, raw=json.dumps(raw))
    batch["aggregation_policy"] = aggregation_for(batch)
    store.import_batch(batch)
    if route == "grade":
        return batch, panel(batch, index=target), "adjudications"
    content = "Synthetic chronology review of the target output."
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
            "name": "synthetic-reviewer",
            "reference": "offline-chronology",
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
    }
    rehash(review)
    reviewed = store.revise(batch["batch_id"], "blind-time-reviewed", reviews=[review])
    return reviewed, agent_ruling(review, outcome="dismissed"), "review_adjudications"


def stored_bytes(store):
    return {
        str(p.relative_to(store.root)): p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    }


def make_early(ruling, which, mutation):
    for index in which:
        owner = ruling["blind_annotations"][index]["actor"]
        if mutation == "early_start":
            owner["started_at"] = "2026-09-22T23:59:59Z"
        elif mutation == "early_finish":
            owner.update(
                started_at="2026-09-22T00:10:00Z", completed_at="2026-09-22T00:20:00Z"
            )
        else:
            owner["started_at"] = "2026-09-23T08:00:01+08:00"
    rehash(ruling)


@pytest.mark.parametrize("route", ["grade", "review"])
@pytest.mark.parametrize(
    "which", [(0,), (1,), (0, 1)], ids=["only-a", "only-b", "both"]
)
@pytest.mark.parametrize("mutation", ["early_start", "early_finish", "offset_before"])
def test_store_rejects_blind_panel_started_before_target_output(
    tmp_path,
    route,
    which,
    mutation,
):
    store = EvidenceStore(tmp_path / "evidence")
    before, ruling, collection = scenario(store, route)
    quality = report(before, store=store, now=NOW)["quality"]
    assert quality["verdict"] == "BLOCKED"
    snapshot = stored_bytes(store)
    make_early(ruling, which, mutation)
    with pytest.raises(ValueError, match="blind_review_before_output"):
        store.revise(before["batch_id"], "rejected-too-early", **{collection: [ruling]})
    assert stored_bytes(store) == snapshot
    assert store.read(before["batch_id"]) == before
    assert (
        report(store.read(before["batch_id"]), store=store, now=NOW)["quality"]
        == quality
    )


@pytest.mark.parametrize("route", ["grade", "review"])
def test_direct_import_of_early_panel_cannot_publish_a_revision(tmp_path, route):
    store = EvidenceStore(tmp_path / "evidence")
    before, ruling, collection = scenario(store, route)
    make_early(ruling, (0, 1), "early_start")
    revision = deepcopy(before)
    revision.update(
        batch_id="rejected-direct-import",
        parent={
            "batch_id": before["batch_id"],
            "sha256": digest(before),
            "relation": "regrade",
        },
    )
    revision.setdefault(collection, []).append(ruling)
    snapshot = stored_bytes(store)
    with pytest.raises(ValueError, match="blind_review_before_output"):
        store.import_batch(revision)
    assert stored_bytes(store) == snapshot
    assert store.read(before["batch_id"]) == before


@pytest.mark.parametrize("route", ["grade", "review"])
@pytest.mark.parametrize(
    "start",
    [
        "2026-09-23T00:00:02Z",
        "2026-09-23T08:00:02+08:00",
        "2026-09-22T19:00:02-05:00",
        "2026-09-23T00:00:02.000001Z",
    ],
)
def test_panel_may_start_when_its_own_target_output_is_complete(tmp_path, route, start):
    store = EvidenceStore(tmp_path / "evidence")
    before, ruling, collection = scenario(
        store,
        route,
        other_finished_at="2026-09-23T00:15:00Z",
    )
    for row in ruling["blind_annotations"]:
        row["actor"]["started_at"] = start
    rehash(ruling)
    assert report(before, store=store, now=NOW)["quality"]["verdict"] == "BLOCKED"
    revised = store.revise(
        before["batch_id"], "accepted-on-time", **{collection: [ruling]}
    )
    saved = store.read(revised["batch_id"])
    assert saved == revised
    assert saved["results"] == before["results"]
    assert saved["grades"] == before["grades"]
    assert report(saved, store=store, now=NOW)["quality"]["verdict"] == "PASS"
    assert store.read(before["batch_id"]) == before
