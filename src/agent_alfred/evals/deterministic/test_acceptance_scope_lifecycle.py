"""Public output scope, terminal operation, and ancestry admission regressions."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from agent_alfred.evals.acceptance.admission import proposal
from agent_alfred.evals.acceptance.authorization_history import assess
from agent_alfred.evals.acceptance.budget import binding
from agent_alfred.evals.acceptance.examples import SKIP
from agent_alfred.evals.acceptance.online_judge import grade_batch
from agent_alfred.evals.acceptance.runner import execute
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.evals.acceptance.store import EvidenceStore
from agent_alfred.evals.deterministic._simulation_test_helpers import (
    session_for,
    text_transport,
)
from agent_alfred.evals.deterministic.test_acceptance_execution_policy import (
    authorized_fixture,
)
from agent_alfred.evals.deterministic.test_acceptance_profile_binding import (
    bound_client,
)
from agent_alfred.evals.deterministic.test_acceptance_trial import authorize
from agent_alfred.runtime.telemetry import AttemptObservationFailed


def test_empty_output_scope_does_not_default_to_current_directory():
    batch, _ = authorized_fixture()
    with pytest.raises(ValueError, match="invalid_proposal_scope"):
        proposal(batch, output_scope="", operations=["product"])


@pytest.mark.parametrize("role", ["product", "judge"])
def test_finished_operation_cannot_reserve_another_request(tmp_path, role):
    budget, client, request, wire, _ = bound_client(tmp_path, role=role)
    try:
        assert client.respond(request).response is not None
        budget.session.finish(role)
        before = budget.session.snapshot()
        assert before["state"] == "ACTIVE"
        with pytest.raises(AttemptObservationFailed):
            client.respond(request)
        assert len(wire) == 1
        assert budget.session.snapshot()["requests"] == before["requests"]
    finally:
        budget.close()


def test_relative_output_scope_cannot_move_product_to_judge(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    batch, root = authorized_fixture()
    monkeypatch.chdir(first)
    store = EvidenceStore("evidence")
    authority = SimulationAuthority(tmp_path / "authority")
    proposed = proposal(batch, output_scope=store.root, operations=["product", "judge"])
    receipt = authority.issue(
        proposed,
        subject="simulation:scope-test",
        source_event_id="synthetic-scope-confirmation",
        source_event_digest="a" * 64,
        activation_deadline=(
            authority.clock.wall_utc() + timedelta(seconds=60)
        ).isoformat(),
        cost_acceptance={"amount": None, "accept_unknown": True},
    )
    session = authority.activate(receipt, proposed)
    wire = []
    sampled = execute(
        batch,
        first / "runtime",
        simulation_session=session,
        mock_transport=text_transport([SKIP, "answer"], wire),
        candidate_root=root,
        store=store,
    )
    store.import_batch(sampled)
    before = session.snapshot()["requests"]
    monkeypatch.chdir(second)
    copied = EvidenceStore("evidence")
    copied.import_batch(sampled)
    with pytest.raises(ValueError, match="approval_binding_mismatch"):
        grade_batch(
            sampled,
            sampled["authorization"],
            simulation_session=session,
            mock_transport=text_transport(["{}"], wire),
            candidate_root=root,
            store=copied,
            new_batch="shifted-judge",
        )
    assert len(wire) == 2
    assert session.snapshot()["requests"] == before
    assert not list((second / "evidence").glob(".journal-judge-*"))
    assert store.read(sampled["batch_id"]) == sampled


def descendant(tmp_path):
    batch, root = authorized_fixture()
    batch["batch_id"] = "calibration-isolated-c25-proposal-r1"
    batch["authorization"]["binding"] = binding(batch)
    store = EvidenceStore(tmp_path / "evidence")
    store.import_batch(batch)
    middle = store.revise(batch["batch_id"], "renamed-middle")
    parent = store.revise(middle["batch_id"], "renamed-parent")
    child = deepcopy(parent)
    child["batch_id"] = "unimported-descendant"
    child["parent"] = {
        "batch_id": parent["batch_id"],
        "sha256": digest(parent),
        "relation": "regrade",
    }
    authorize(child)
    return child, root, store


@pytest.mark.parametrize("operation", ["product", "judge"])
def test_multilevel_quarantine_precedes_public_factory(tmp_path, operation):
    batch, root, store = descendant(tmp_path)
    assert assess(batch, store=store)["validity"] == "INVALID"
    session = session_for(batch, store)
    wire = []
    options = dict(
        simulation_session=session,
        mock_transport=text_transport([SKIP, "answer"], wire),
        candidate_root=root,
        store=store,
    )
    with pytest.raises(ValueError, match="execution_authorization_invalid"):
        if operation == "product":
            execute(batch, tmp_path / "runtime", **options)
        else:
            grade_batch(batch, batch["authorization"], new_batch="new-judge", **options)
    assert wire == []
    assert session.snapshot()["requests"] == []


@pytest.mark.parametrize("missing_store", [True, False])
def test_parent_without_verifiable_store_cannot_obtain_new_permission(
    tmp_path, missing_store
):
    batch, root, store = descendant(tmp_path)
    session = session_for(batch, store)
    wire = []
    if not missing_store:
        (store.root / batch["parent"]["batch_id"] / "batch.json").unlink()
    with pytest.raises(ValueError, match="authorization_ancestry_unverifiable"):
        execute(
            batch,
            tmp_path / "runtime",
            simulation_session=session,
            mock_transport=text_transport([SKIP, "answer"], wire),
            candidate_root=root,
            store=None if missing_store else store,
        )
    assert wire == []
    assert session.snapshot()["requests"] == []


def test_finish_at_public_preflight_prevents_reservation(tmp_path):
    budget, client, request, wire, _ = bound_client(tmp_path)
    try:
        request = replace(
            request,
            on_attempt_preflight=lambda *args: budget.session.finish("product"),
        )
        with pytest.raises(AttemptObservationFailed):
            client.respond(request)
        assert wire == []
        assert budget.session.snapshot()["requests"] == []
    finally:
        budget.close()


def test_finish_allows_existing_inflight_settlement_only(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    budget, client, request, wire, _ = bound_client(tmp_path)
    entered, complete = threading.Event(), threading.Event()
    send = budget.mock_transport.handler

    def held_transport(request):
        response = send(request)
        entered.set()
        assert complete.wait(10)
        return response

    budget.mock_transport.handler = held_transport
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.respond, request)
            try:
                assert entered.wait(10)
                budget.session.finish("product")
            finally:
                complete.set()
            assert pending.result(timeout=10).response is not None
        with pytest.raises(AttemptObservationFailed):
            client.respond(request)
        ledger = budget.session.snapshot()["requests"]
        assert len(wire) == len(ledger) == 1
        assert ledger[0]["outcome"] == "committed"
    finally:
        complete.set()
        budget.close()
