"""Public admission boundaries: synthetic inputs never establish user consent."""

import os

import pytest

from agent_alfred.evals.acceptance.runner import execute
from agent_alfred.evals.deterministic.test_acceptance_execution_policy import (
    authorized_fixture,
)


class UnreadableCredentials:
    def values(self):
        raise AssertionError("credential_read_before_admission")


def test_product_rejects_executor_filled_source_before_credentials(tmp_path):
    batch, root = authorized_fixture()
    batch["simulation"] = False
    with pytest.raises(ValueError, match="^approval_source_unverifiable$"):
        execute(
            batch,
            tmp_path / "runtime",
            credentials=UnreadableCredentials(),
            candidate_root=root,
        )
    assert not (tmp_path / "runtime").exists()


def test_synthetic_receipt_activates_once_and_cannot_authorize_online(tmp_path):
    from datetime import timedelta

    from agent_alfred.clock import SystemClock
    from agent_alfred.evals.acceptance.admission import proposal
    from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority

    batch, _ = authorized_fixture()
    clock = SystemClock()
    authority = SimulationAuthority(tmp_path / "authority", clock=clock)
    request = proposal(
        batch, output_scope=tmp_path / "run", operations=["product", "judge"]
    )
    assert all(
        request[k] is None
        for k in (
            "subject",
            "confirmed_at",
            "cost_acceptance",
            "receipt",
        )
    )
    receipt = authority.issue(
        request,
        subject="simulation:user",
        source_event_id="synthetic-confirmation",
        source_event_digest="a" * 64,
        activation_deadline=(clock.wall_utc() + timedelta(seconds=60)).isoformat(),
        cost_acceptance={"amount": None, "accept_unknown": True},
    )
    session = authority.activate(receipt, request)
    assert session.snapshot()["state"] == "ACTIVE"
    with pytest.raises(ValueError, match="grant_already_activated"):
        authority.activate(receipt, request)
    batch["simulation"] = False
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        session.bind(batch, operation="product", output_scope=tmp_path / "run")


def simulated_session(tmp_path, batch, *, clock=None, activate=True):
    from datetime import timedelta

    from agent_alfred.clock import SystemClock
    from agent_alfred.evals.acceptance.admission import proposal
    from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority

    clock = clock or SystemClock()
    authority = SimulationAuthority(tmp_path / "authority", clock=clock)
    request = proposal(
        batch,
        output_scope=tmp_path / "evidence",
        operations=["product", "judge"],
    )
    receipt = authority.issue(
        request,
        subject="simulation:user",
        source_event_id="synthetic-confirmation",
        source_event_digest="a" * 64,
        activation_deadline=(clock.wall_utc() + timedelta(seconds=60)).isoformat(),
        cost_acceptance={"amount": None, "accept_unknown": True},
    )
    return (
        authority,
        request,
        receipt,
        (authority.activate(receipt, request) if activate else None),
    )


def test_public_product_judge_share_authority_budget_and_ledger(tmp_path):
    import json

    import httpx2 as httpx

    from agent_alfred.evals.acceptance.examples import SKIP
    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    seen = []

    def send(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "id": "simulation",
                "model": body["model"],
                "choices": [
                    {
                        "message": {"content": SKIP if len(seen) == 1 else "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    store = EvidenceStore(tmp_path / "evidence")
    sampled = execute(
        batch,
        tmp_path / "runtime",
        simulation_session=session,
        mock_transport=httpx.MockTransport(send),
        candidate_root=root,
        store=store,
    )
    store.import_batch(sampled)
    graded = grade_batch(
        sampled,
        sampled["authorization"],
        simulation_session=session,
        mock_transport=httpx.MockTransport(send),
        candidate_root=root,
        store=store,
        new_batch="synthetic-judged",
    )
    ledger = session.snapshot()
    assert len(seen) == len(ledger["requests"]) == 3  # auxiliary + product + judge
    assert graded["requests"] == ledger["requests"]
    assert sampled["budget_started_at"] == graded["budget_started_at"]
    assert graded["budget_started_at"] == ledger["budget_started_at"]
    assert all(r["usage"]["total_input_tokens"] == 1 for r in ledger["requests"])
    assert all(r["send_state"] == "SEND_INTENT" for r in ledger["requests"])
    with pytest.raises(ValueError, match="operation_already_consumed"):
        grade_batch(
            sampled,
            sampled["authorization"],
            simulation_session=session,
            mock_transport=httpx.MockTransport(send),
            candidate_root=root,
            store=store,
            new_batch="replay",
        )


def test_unsettled_product_blocks_judge_until_original_request_settles(tmp_path):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime

    import httpx2 as httpx

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.evals.acceptance.schema import judge_model
    from agent_alfred.messages import Message, TextBlock
    from agent_alfred.model import (
        ClientSnapshot,
        ModelAssignment,
        ModelRef,
        ModelRequest,
    )

    batch, _ = authorized_fixture()
    clock = FakeClock(wall=datetime(2026, 9, 25, tzinfo=UTC))
    _, _, _, session = simulated_session(tmp_path, batch, clock=clock)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    entered, release = threading.Event(), threading.Event()
    observed = []

    def client_for(role):
        model = (
            batch["profiles"][0]["product_models"][0]
            if role == "product"
            else judge_model(batch, batch["profiles"][0])
        )

        def send(req):
            observed.append(role)
            if role == "product":
                entered.set()
                assert release.wait(10)
            body = json.loads(req.content)
            return httpx.Response(
                200,
                json={
                    "id": "synthetic",
                    "model": body["model"],
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        budget = AuthorizedBatch(
            batch, session=session, mock_transport=httpx.MockTransport(send)
        )
        snapshot = ClientSnapshot(
            batch["profiles"][0]["id"],
            ModelAssignment(
                model["endpoint_id"], model["model_id"], model["wire_style"]
            ),
            None,
            "synthetic",
            False,
            False,
            30,
            30,
        )
        request = ModelRequest(
            ModelRef(model["endpoint_id"], model["model_id"]),
            None,
            (Message("user", (TextBlock("synthetic"),)),),
            max_tokens=1,
        )
        return budget, budget.client(snapshot, role=role), request

    product, product_client, product_request = client_for("product")
    judge = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(product_client.respond, product_request)
            try:
                assert entered.wait(10)
                session.finish("product")
                with pytest.raises(ValueError, match="request_state_unresolved"):
                    session.bind(
                        batch, operation="judge", output_scope=tmp_path / "evidence"
                    )
                assert observed == ["product"]
            finally:
                release.set()
            assert pending.result(timeout=10).response is not None
        session.bind(batch, operation="judge", output_scope=tmp_path / "evidence")
        judge, judge_client, judge_request = client_for("judge")
        assert judge_client.respond(judge_request).response is not None
        assert observed == ["product", "judge"]
        assert [r["role"] for r in session.snapshot()["requests"]] == [
            "product",
            "judge",
        ]
    finally:
        if judge is not None:
            judge.close()
        product.close()


def test_revocation_allows_inflight_settlement_but_stops_next_dispatch(tmp_path):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx2 as httpx

    from agent_alfred.evals.acceptance.examples import SKIP
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    admitted, finish = threading.Event(), threading.Event()
    sent = []

    def send(req):
        body = json.loads(req.content)
        sent.append(body)
        admitted.set()
        assert finish.wait(10)
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "model": body["model"],
                "choices": [{"message": {"content": SKIP}, "finish_reason": "stop"}],
            },
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            execute,
            batch,
            tmp_path / "runtime",
            simulation_session=session,
            mock_transport=httpx.MockTransport(send),
            candidate_root=root,
            store=EvidenceStore(tmp_path / "evidence"),
        )
        try:
            assert admitted.wait(10)
            authority.revoke(receipt["grant_id"])
        finally:
            finish.set()
        sampled = future.result(timeout=10)
    assert len(sent) == len(sampled["requests"]) == 1
    assert session.snapshot()["state"] == "REVOKED"
    assert session.snapshot()["requests"][0]["outcome"] == "committed"
    with pytest.raises(ValueError, match="grant_not_active"):
        session.check()


@pytest.mark.parametrize("entry", ["product", "judge"])
@pytest.mark.parametrize(
    "variant",
    [
        "missing",
        "unsigned",
        "unknown_not_accepted",
        "binding_mismatch",
        "future_at",
        "operator_filled",
        "operator_filled_with_disclaimer",
    ],
)
def test_original_public_source_scenarios_reject_before_factories(
    tmp_path, entry, variant
):
    from datetime import UTC, datetime, timedelta

    from agent_alfred.evals.acceptance.online_judge import grade_batch
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    batch["simulation"] = False
    auth = batch["authorization"]
    if variant == "missing":
        batch["authorization"] = None
    elif variant == "unsigned":
        auth.update(by=None, at=None, accept_unknown_cost=None)
    elif variant == "unknown_not_accepted":
        auth["accept_unknown_cost"] = False
    elif variant == "binding_mismatch":
        auth["binding"]["candidate_id"] = "wrong"
    elif variant == "future_at":
        auth["at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    elif variant == "operator_filled_with_disclaimer":
        (tmp_path / "disclaimer.txt").write_text("仅模板；本轮没有签署")
    calls = []

    def forbidden_factory():
        calls.append("factory")
        raise AssertionError("factory_before_admission")

    with pytest.raises(ValueError, match="^approval_source_unverifiable$"):
        if entry == "product":
            execute(
                batch,
                tmp_path / "runtime",
                credentials=UnreadableCredentials(),
                product_factory_builder=forbidden_factory,
            )
        else:
            grade_batch(
                batch,
                batch["authorization"],
                factory_builder=forbidden_factory,
                store=EvidenceStore(tmp_path / "evidence"),
                candidate_root=root,
                new_batch="judge",
            )
    assert calls == []
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("variant", ["unsigned", "operator_filled_with_disclaimer"])
def test_original_cli_scenarios_reject_without_environment_read(
    tmp_path,
    variant,
    monkeypatch,
    capsys,
):
    import json
    import sys

    from agent_alfred.evals.acceptance.__main__ import main

    class NoEnvironment(dict):
        def keys(self):
            raise AssertionError("credentials_read")

    batch, root = authorized_fixture()
    batch["simulation"] = False
    if variant == "unsigned":
        batch["authorization"].update(by=None, at=None, accept_unknown_cost=None)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(batch))
    monkeypatch.setattr(os, "environ", NoEnvironment())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acceptance",
            "execute",
            "--input",
            str(path),
            "--store",
            str(tmp_path / "store"),
            "--workspace",
            str(tmp_path / "runtime"),
            "--candidate-root",
            str(root),
        ],
    )
    assert main() == 2
    assert "approval_source_unverifiable" in capsys.readouterr().out
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("defect", ["delete", "rollback", "corrupt", "replace"])
def test_authority_state_loss_never_restores_a_permission(tmp_path, defect):
    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    path = tmp_path / "authority/state.json"
    original = path.read_bytes()
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    current = path.read_bytes()
    if defect == "delete":
        path.unlink()
    elif defect == "rollback":
        path.write_bytes(original)
    elif defect == "corrupt":
        path.write_text("broken")
    else:
        replacement = path.with_name("replacement.json")
        replacement.write_bytes(current)
        replacement.replace(path)
    with pytest.raises(ValueError, match="authority_state_unverifiable"):
        session.check()
    path.write_bytes(current)
    with pytest.raises(ValueError, match="authority_state_unverifiable"):
        session.check()


@pytest.mark.parametrize(
    "field",
    [
        "issuer",
        "subject",
        "source_event_id",
        "source_event_digest",
        "signature",
        "issued_at",
        "activation_deadline",
        "proposal_digest",
        "operations",
        "output_scope",
        "cost_acceptance",
        "limits_digest",
        "version",
        "grant_id",
    ],
)
def test_forged_or_tampered_receipts_never_activate(tmp_path, field):
    from copy import deepcopy

    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    tampered = deepcopy(receipt)
    tampered[field] = "forged"
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        authority.activate(tampered, request)
    del tampered[field]
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        authority.activate(tampered, request)


def test_revoke_before_public_dispatch_reserves_nothing(tmp_path):
    import httpx2 as httpx

    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    authority.revoke(receipt["grant_id"])
    with pytest.raises(ValueError, match="grant_not_active"):
        execute(
            batch,
            tmp_path / "runtime",
            simulation_session=session,
            mock_transport=httpx.MockTransport(lambda r: pytest.fail("dispatch")),
            candidate_root=root,
            store=EvidenceStore(tmp_path / "evidence"),
        )
    assert session.snapshot()["requests"] == []


def test_quarantine_after_activation_blocks_dispatch(tmp_path):
    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    authority.quarantine(batch["candidate_id"])
    with pytest.raises(ValueError, match="execution_authorization_invalid"):
        session.check()


def test_duplicate_confirmation_cannot_mint_a_second_budget(tmp_path):
    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    with pytest.raises(ValueError, match="proposal_already_issued"):
        authority.issue(
            request,
            subject=receipt["subject"],
            source_event_id=receipt["source_event_id"],
            source_event_digest=receipt["source_event_digest"],
            activation_deadline=receipt["activation_deadline"],
            cost_acceptance=receipt["cost_acceptance"],
        )


def test_revocation_wins_at_transport_preflight_without_phantom_reservation(tmp_path):

    import httpx2 as httpx

    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.messages import Message, TextBlock
    from agent_alfred.model import (
        ClientSnapshot,
        ModelAssignment,
        ModelRef,
        ModelRequest,
    )

    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    model = batch["profiles"][0]["product_models"][0]
    assignment = ModelAssignment(model["endpoint_id"], model["model_id"], "openai")
    budget = AuthorizedBatch(batch, session=session)
    try:
        client = budget.client(
            ClientSnapshot(
                "synthetic",
                assignment,
                None,
                api_key="synthetic-only",
                stream=False,
                stream_fallback=False,
                overall_deadline_s=60,
                per_attempt_timeout_s=60,
            ),
            role="product",
            mock_transport=httpx.MockTransport(lambda r: pytest.fail("dispatch")),
        )
        request = ModelRequest(
            model=ModelRef(assignment.endpoint_id, assignment.model_id),
            system=None,
            messages=(Message("user", (TextBlock("offline"),)),),
            max_tokens=100,
            on_attempt_preflight=lambda *args: authority.revoke(receipt["grant_id"]),
        )
        from agent_alfred.runtime.telemetry import AttemptObservationFailed

        with pytest.raises(AttemptObservationFailed):
            client.respond(request)
    finally:
        budget.close()
    assert budget.requests == session.snapshot()["requests"] == []


def test_seen_receipt_cannot_be_reissued_by_an_unknown_issuer(tmp_path):
    from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority

    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    other = SimulationAuthority(tmp_path / "other-authority")
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        other.activate(receipt, request)


def test_reservation_crash_consumes_budget_without_usage_or_recovery(tmp_path):
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
    from agent_alfred.model import ModelRef

    batch, _ = authorized_fixture()
    batch["authorization"]["max_requests"] = 1
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    budget = AuthorizedBatch(batch, session=session)
    model = batch["profiles"][0]["product_models"][0]
    budget.start(
        "reserved-before-crash",
        "product",
        ModelRef(
            model["endpoint_id"],
            model["model_id"],
        ),
    )
    resumed = AuthorizedBatch(batch, session=session)
    with pytest.raises(ValueError, match="request_limit"):
        resumed.check()
    row = session.snapshot()["requests"][0]
    assert row["outcome"] == "unknown" and row["usage"] is None
    with pytest.raises(ValueError, match="authority_state_unverifiable"):
        SimulationAuthority(tmp_path / "authority")


def test_concurrent_activation_has_one_winner(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    batch, _ = authorized_fixture()
    authority, request, receipt, _ = simulated_session(tmp_path, batch, activate=False)
    ready = threading.Barrier(2)

    def activate():
        ready.wait(timeout=10)
        try:
            authority.activate(receipt, request)
            return "ACTIVE"
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: activate(), range(2)))
    assert sorted(outcomes) == ["ACTIVE", "grant_already_activated"]


@pytest.mark.parametrize("advance", [-1, 61])
def test_receipt_activation_checks_real_event_window(tmp_path, advance):
    from datetime import UTC, datetime, timedelta

    from agent_alfred.clock import FakeClock

    clock = FakeClock(wall=datetime(2026, 9, 25, tzinfo=UTC))
    batch, _ = authorized_fixture()
    authority, request, receipt, _ = simulated_session(
        tmp_path,
        batch,
        clock=clock,
        activate=False,
    )
    clock.wall += timedelta(seconds=advance)
    with pytest.raises(ValueError, match="approval_time_invalid"):
        authority.activate(receipt, request)


def test_judge_cannot_restart_deadline_or_copy_output_scope(tmp_path):
    from datetime import UTC, datetime

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch

    clock = FakeClock(wall=datetime(2026, 9, 25, tzinfo=UTC))
    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(
        tmp_path, batch, clock=clock
    )
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    start = session.snapshot()["budget_started_at"]
    with pytest.raises(ValueError, match="approval_binding_mismatch"):
        session.bind(batch, operation="judge", output_scope=tmp_path / "copy")
    clock.monotonic_value += batch["authorization"]["total_seconds"]
    resumed = AuthorizedBatch(batch, session=session)
    assert resumed.started_at == start
    with pytest.raises(ValueError, match="batch_deadline"):
        resumed.check()


def test_copied_authority_process_cannot_restore_permission(tmp_path):
    import select
    import signal

    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    start_r, start_w = os.pipe()
    result_r, result_w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(start_w)
        os.close(result_r)
        try:
            assert os.read(start_r, 1) == b"S"
            try:
                session.check()
                result = "unexpected_acceptance"
            except ValueError as error:
                result = str(error)
            os.write(result_w, result.encode())
        finally:
            os._exit(0)
    os.close(start_r)
    os.close(result_w)
    reaped = False
    try:
        # The parent confirms its live grant before releasing the copied child.
        session.check()
        os.write(start_w, b"S")
        assert select.select([result_r], [], [], 10)[0]
        assert os.read(result_r, 1024) == b"authority_state_unverifiable"
        _, status = os.waitpid(child, 0)
        reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
        session.check()
    finally:
        os.close(start_w)
        os.close(result_r)
        if not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)


def test_state_checked_bytes_are_the_bytes_used_for_admission(tmp_path, monkeypatch):
    from pathlib import Path

    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    path = tmp_path / "authority/state.json"
    active = path.read_bytes()
    authority.revoke(receipt["grant_id"])
    read_bytes = Path.read_bytes
    reads = []

    def swap_after_verification(self):
        if self == path:
            reads.append(True)
            if len(reads) == 2:
                return active
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", swap_after_verification)
    with pytest.raises(
        ValueError, match="grant_not_active|authority_state_unverifiable"
    ):
        session.check()


def test_completed_grant_retains_ledger_and_cannot_reactivate(tmp_path):
    batch, _ = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    for operation in ("product", "judge"):
        session.bind(batch, operation=operation, output_scope=tmp_path / "evidence")
        session.finish(operation)
    assert session.snapshot()["state"] == "CONSUMED"
    with pytest.raises(ValueError, match="grant_already_activated"):
        authority.activate(receipt, request)


def test_concurrent_reservation_has_one_ledger_entry_and_no_phantom(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.acceptance.budget import AuthorizedBatch
    from agent_alfred.model import ModelRef

    class SynchronizedClock(FakeClock):
        barrier = None

        def wall_utc(self):
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            return super().wall_utc()

    clock = SynchronizedClock(wall=datetime(2026, 9, 25, tzinfo=UTC))
    batch, _ = authorized_fixture()
    batch["authorization"]["max_requests"] = 1
    authority, request, receipt, session = simulated_session(
        tmp_path, batch, clock=clock
    )
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    budgets = [AuthorizedBatch(batch, session=session) for _ in range(2)]
    clock.barrier = threading.Barrier(2)
    model = batch["profiles"][0]["product_models"][0]

    def reserve(index):
        try:
            budgets[index].start(
                str(index),
                "product",
                ModelRef(
                    model["endpoint_id"],
                    model["model_id"],
                ),
            )
            return "reserved"
        except ValueError as error:
            assert budgets[index].requests == []
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, range(2)))
    assert sorted(outcomes) == ["request_limit", "reserved"]
    assert len(session.snapshot()["requests"]) == 1


@pytest.mark.parametrize("cost", [None, {}, {"amount": -1, "source": "synthetic"}])
def test_synthetic_issuer_rejects_missing_or_invalid_monetary_terms(tmp_path, cost):
    from datetime import timedelta

    from agent_alfred.evals.acceptance.admission import proposal
    from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority

    batch, _ = authorized_fixture()
    request = proposal(
        batch, output_scope=tmp_path / "evidence", operations=["product"]
    )
    request["cost"] = cost
    authority = SimulationAuthority(tmp_path / "authority")
    with pytest.raises(ValueError, match="cost_acceptance_missing"):
        authority.issue(
            request,
            subject="simulation:user",
            source_event_id="synthetic",
            source_event_digest="a" * 64,
            activation_deadline=(
                authority.clock.wall_utc() + timedelta(seconds=60)
            ).isoformat(),
            cost_acceptance={"amount": -1, "accept_unknown": False},
        )


def test_cli_proposal_never_copies_executor_consent(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from agent_alfred.evals.acceptance.__main__ import main

    batch, _ = authorized_fixture()
    path = tmp_path / "input.json"
    path.write_text(json.dumps(batch))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acceptance",
            "proposal",
            "--input",
            str(path),
            "--output-scope",
            str(tmp_path / "evidence"),
            "--operations",
            "product",
            "judge",
        ],
    )
    assert main() == 0
    document = json.loads(capsys.readouterr().out)
    assert document["state"] == "PENDING_USER_CONFIRMATION"
    assert all(
        document[k] is None
        for k in (
            "subject",
            "confirmed_at",
            "cost_acceptance",
            "receipt",
        )
    )


def test_quarantined_session_rejects_public_factory_construction(tmp_path, monkeypatch):
    import httpx2 as httpx

    from agent_alfred import endpoint_factory
    from agent_alfred.evals.acceptance.store import EvidenceStore

    batch, root = authorized_fixture()
    authority, request, receipt, session = simulated_session(tmp_path, batch)
    authority.quarantine(batch["candidate_id"])

    def forbidden_factory(**kwargs):
        pytest.fail("quarantine reached factory")

    monkeypatch.setattr(endpoint_factory, "EndpointClientFactory", forbidden_factory)
    with pytest.raises(ValueError, match="execution_authorization_invalid"):
        execute(
            batch,
            tmp_path / "runtime",
            simulation_session=session,
            mock_transport=httpx.MockTransport(lambda r: pytest.fail("dispatch")),
            store=EvidenceStore(tmp_path / "evidence"),
            candidate_root=root,
        )
    assert not (tmp_path / "evidence").exists()


def test_p1_proposal_binds_inputs_without_minting_fee_or_worker_authority(tmp_path):
    from copy import deepcopy
    from datetime import timedelta

    from agent_alfred.evals.acceptance.admission import (
        UnconfiguredAuthority,
        proposal,
    )
    from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority

    batch, _ = authorized_fixture()
    terms = {
        "currency": "USD",
        "maximum_amount": "1.00",
        "pricing_basis": "synthetic-unverified",
        "enforcement": "executor-claim",
        "broker_verified": True,
    }
    request = proposal(
        batch,
        output_scope=tmp_path / "evidence",
        operations=["product", "judge"],
        fee_terms=terms,
    )
    assert request["version"] == 2 and len(request["nonce"]) == 32
    assert request["worker_identity"] is None
    assert request["request_roles"] == ["product", "auxiliary", "judge"]
    assert request["fee_condition"]["mode"] == "hard_cap_required"
    assert request["fee_condition"]["broker_verified"] is False
    assert all(
        request[field] is None
        for field in ("subject", "confirmed_at", "cost_acceptance", "receipt")
    )
    with pytest.raises(ValueError, match="approval_source_unverifiable"):
        UnconfiguredAuthority().submit_job(request)

    authority = SimulationAuthority(tmp_path / "authority")
    receipt = authority.issue(
        request,
        subject="simulation:user",
        source_event_id="synthetic-p1",
        source_event_digest="a" * 64,
        activation_deadline=(
            authority.clock.wall_utc() + timedelta(seconds=60)
        ).isoformat(),
        cost_acceptance={"amount": None, "accept_unknown": True},
    )
    forged_fee = deepcopy(request)
    forged_fee["fee_condition"]["broker_verified"] = True
    with pytest.raises(ValueError, match="proposal_not_unsigned"):
        authority.issue(
            forged_fee,
            subject="simulation:user",
            source_event_id="synthetic-p1-forgery",
            source_event_digest="b" * 64,
            activation_deadline=(
                authority.clock.wall_utc() + timedelta(seconds=60)
            ).isoformat(),
            cost_acceptance={"amount": None, "accept_unknown": True},
        )
    session = authority.submit_job({"proposal": request, "receipt": receipt})
    for changed in ("cases", "candidate", "profiles"):
        modified = deepcopy(batch)
        if changed == "cases":
            modified["cases"][0]["input"] += " changed"
        elif changed == "candidate":
            modified["candidate"]["files"]["src/agent_alfred/__init__.py"]["sha256"] = (
                "0" * 64
            )
        else:
            modified["profiles"][0]["parameters"]["max_tokens"] = 1
        with pytest.raises(ValueError, match="approval_binding_mismatch"):
            session.bind(
                modified, operation="product", output_scope=tmp_path / "evidence"
            )
    assert authority.status(receipt["grant_id"])["requests"] == []


@pytest.mark.parametrize(
    "interruption,reason",
    [("revoke", "grant_not_active"), ("quarantine", "execution_authorization_invalid")],
)
def test_revoke_or_quarantine_before_send_intent_cancels_without_ticket(
    tmp_path, monkeypatch, interruption, reason
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_alfred.evals.acceptance.simulation_authority import SimulationSession

    batch, _ = authorized_fixture()
    authority, _, receipt, session = simulated_session(tmp_path, batch)
    session.bind(batch, operation="product", output_scope=tmp_path / "evidence")
    model = batch["profiles"][0]["product_models"][0]
    attempt_id = "synthetic-race-attempt"
    descriptor = {
        "batch": batch,
        "max_tokens": 1,
        "row": {
            "attempt_id": attempt_id,
            "role": "product",
            "model": {
                "endpoint_id": model["endpoint_id"],
                "model_id": model["model_id"],
            },
            "started_at": authority.clock.wall_utc().isoformat(),
            "usage": None,
            "outcome": "unknown",
            "request_digest": "b" * 64,
        },
    }
    reserved, proceed = threading.Event(), threading.Event()
    original = SimulationSession.send_intent

    def held_intent(self, identity):
        reserved.set()
        assert proceed.wait(10)
        return original(self, identity)

    monkeypatch.setattr(SimulationSession, "send_intent", held_intent)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.invoke, "product", attempt_id, descriptor)
        try:
            assert reserved.wait(10)
            assert (
                authority.status(receipt["grant_id"])["requests"][0]["send_state"]
                == "RESERVED"
            )
            if interruption == "revoke":
                authority.revoke(receipt["grant_id"])
            else:
                authority.quarantine(batch["candidate_id"])
        finally:
            proceed.set()
        with pytest.raises(ValueError, match=reason):
            future.result(timeout=10)
    state = authority.status(receipt["grant_id"])
    assert len(state["requests"]) == 1
    assert state["requests"][0]["send_state"] == "CANCELLED_BEFORE_SEND"
    assert session.invoke("product", attempt_id, descriptor) == {
        "already_recorded": True,
        "send_state": "CANCELLED_BEFORE_SEND",
    }


def test_missing_usage_stops_next_request_and_worker_gets_only_restricted_client(
    tmp_path,
):
    from agent_alfred.evals.deterministic.test_acceptance_profile_binding import (
        bound_client,
    )

    budget, client, request, wire, _ = bound_client(tmp_path, usage=False)
    try:
        assert all(
            not hasattr(client, name)
            for name in ("http", "factory", "inner", "api_key", "mock_transport")
        )
        assert client.respond(request).response is not None
        with pytest.raises(ValueError, match="unknown_usage"):
            client.respond(request)
        assert len(wire) == 1
        state = budget.session.authority.status(budget.session.grant_id)
        assert state["stop_reason"] == "unknown_usage"
        assert len(state["requests"]) == 1
        assert state["requests"][0]["send_state"] == "SEND_INTENT"
        assert state["requests"][0]["usage"]["total_input_tokens"] is None
    finally:
        budget.close()


def test_send_intent_persistence_failure_never_reaches_mock_transport(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.deterministic.test_acceptance_profile_binding import (
        bound_client,
    )
    from agent_alfred.runtime.telemetry import AttemptObservationFailed

    budget, client, request, wire, _ = bound_client(tmp_path)
    authority = budget.session.authority
    original_save = authority._save

    def fail_send_intent(state):
        if any(
            row["send_state"] == "SEND_INTENT"
            for grant in state["grants"].values()
            for row in grant["requests"]
        ):
            raise OSError("synthetic send intent persistence failure")
        return original_save(state)

    monkeypatch.setattr(authority, "_save", fail_send_intent)
    try:
        with pytest.raises(AttemptObservationFailed):
            client.respond(request)
        assert wire == []
        state = authority.status(budget.session.grant_id)
        assert state["requests"][0]["send_state"] == "RESERVED"
        with pytest.raises(ValueError, match="request_state_unresolved"):
            budget.session.check()
    finally:
        budget.close()
