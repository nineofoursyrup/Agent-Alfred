"""Public persistence faults survive recovery without inventing send facts."""

from datetime import timedelta

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.controlled.control import (
    ControlClient,
    ControlService,
)
from agent_alfred.evals.acceptance.controlled_diagnostics import DiagnosticDriver
from agent_alfred.evals.acceptance.controlled_persistence import (
    PersistentControlledAuthority,
)
from agent_alfred.evals.acceptance.schema import digest
from agent_alfred.model import ModelCallInterrupted

from .test_controlled_execution import prepare
from .test_controlled_persistence import control_envelope, durable_fixture, reopen


def unavailable(*args, **kwargs):
    raise OSError("fixture persistence unavailable")


@pytest.mark.parametrize(
    "entry,method",
    [
        ("prepare", "put_object"),
        ("prepare", "get_object"),
        ("claim", "claim_control"),
        ("complete", "complete_control"),
        ("lookup", "control_status"),
        ("reconcile", "control_status"),
        ("recover", "read_events"),
        ("restore", "get_object"),
        ("settle", "get_object"),
        ("observation", "put_object"),
        ("status", "list_attempts"),
        ("client", "get_object"),
        ("diagnostic_report", "list_attempts"),
    ],
)
def test_public_io_failure_is_retained_after_backend_and_handler_recovery(
    tmp_path, monkeypatch, entry, method
):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    prepared = None
    if entry in ("claim", "complete", "lookup", "reconcile", "client"):
        _, prepared = prepare(f)
    service = ControlService(auth)
    envelope = control_envelope(f, prepared) if prepared else None
    if entry == "reconcile":
        f["ledger"].claim_control("controlled-job", "control-1", envelope)
    calls = {
        "prepare": lambda: prepare(f),
        "claim": lambda: service.call(envelope, principal="simulation:worker"),
        "complete": lambda: service.call(envelope, principal="simulation:worker"),
        "lookup": lambda: service.lookup(
            "controlled-job", "control-1", principal="simulation:worker"
        ),
        "reconcile": lambda: service.reconcile(
            envelope, principal="simulation:controller"
        ),
        "recover": lambda: auth.recover(
            "controlled-job", principal="simulation:controller"
        ),
        "restore": lambda: auth.restore_materials(
            "controlled-job", tmp_path / "restored", principal="simulation:controller"
        ),
        "settle": lambda: auth.settle_attempt(
            "controlled-job", "original", digest({}), principal="simulation:controller"
        ),
        "observation": lambda: auth.record_observation(
            "controlled-job", {"kind": "blocker"}, principal="simulation:controller"
        ),
        "status": lambda: auth.status("controlled-job"),
        "client": lambda: auth.client("controlled-job", "diagnose", prepared),
        "diagnostic_report": lambda: DiagnosticDriver(
            auth, "controlled-job", principal="simulation:controller"
        ).report(),
    }
    with monkeypatch.context() as patch:
        patch.setattr(f["ledger"], method, unavailable)
        with pytest.raises(OSError, match="fixture persistence unavailable"):
            calls[entry]()
    assert f["anchor"].faults("controlled-job"), entry
    auth = reopen(f, tmp_path)
    recovered = auth.recover("controlled-job", principal="simulation:controller")
    assert recovered["state"]["state"] == "SUSPENDED"
    before = len(f["sends"])
    assert before == (1 if entry == "complete" else 0)
    with pytest.raises(ValueError):
        prepare(f)
    assert len(f["sends"]) == before


def test_unauthenticated_controller_call_and_unknown_job_create_no_fault(
    tmp_path, monkeypatch
):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    with monkeypatch.context() as patch:
        patch.setattr(f["ledger"], "get", unavailable)
        with pytest.raises(ValueError, match="controller_identity_required"):
            auth.prepare_request(
                "controlled-job",
                "diagnose",
                f["request"],
                principal="untrusted",
                data_scope_sha256=digest({}),
            )
        with pytest.raises(ValueError, match="controller_identity_required"):
            auth.phase_event(
                "controlled-job",
                principal="untrusted",
                expected_phase="diagnostic",
                next_phase="checkpoint",
                evidence={},
            )
    with pytest.raises(ValueError, match="job_unknown"):
        auth.status("unknown-job")
    assert f["anchor"].faults("controlled-job") == []
    assert f["anchor"].faults("unknown-job") == []
    assert auth.status("controlled-job")["state"]["state"] == "ACTIVE"


def outage(backend, reason, patch):
    def fail(*args, **kwargs):
        raise ValueError(reason)

    for method in ("read", "scan", "transaction"):
        patch.setattr(backend, method, fail)


def replacement(f):
    return PersistentControlledAuthority(**vars(f["authority"]))


def test_unrecordable_failure_and_handler_loss_cannot_revive_grant(
    tmp_path, monkeypatch
):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    original = auth.submit_job(f["submission"])["state"]
    with monkeypatch.context() as patch:
        outage(f["ledger"].backend, "both_domains_unavailable", patch)
        outage(f["anchor"].backend, "both_domains_unavailable", patch)
        with pytest.raises(ValueError, match="both_domains_unavailable"):
            prepare(f)
    assert f["ledger"].faults("controlled-job") == []
    assert f["anchor"].faults("controlled-job") == []
    auth = reopen(f, tmp_path)
    recovered = auth.recover("controlled-job", principal="simulation:controller")
    assert recovered["state"]["state"] == "SUSPENDED"
    assert recovered["state"]["stop_reason"] == "handler_recovery_unverifiable"
    assert recovered["state"]["started_at"] == original["started_at"]
    with pytest.raises(ValueError):
        prepare(f)
    assert f["sends"] == []


def test_clean_handoff_is_consumed_once_and_retires_previous_handler(tmp_path):
    f = durable_fixture(tmp_path)
    old = f["authority"]
    original = old.submit_job(f["submission"])["state"]
    token = old.handoff("controlled-job", principal="simulation:controller")
    with pytest.raises(ValueError, match="handler_recovery_required"):
        prepare(f)
    new = replacement(f)
    f["authority"] = new
    recovered = new.recover(
        "controlled-job", principal="simulation:controller", handoff=token
    )
    for field in ("started_at", "spent_units", "pending_units", "counts", "cap_units"):
        assert recovered["state"][field] == original[field]
    assert recovered["state"]["state"] == "ACTIVE"
    with pytest.raises(ValueError, match="clean_handoff_unverifiable"):
        new.recover("controlled-job", principal="simulation:controller", handoff=token)
    f["authority"] = old
    with pytest.raises(ValueError, match="handler_recovery_required"):
        prepare(f)
    f["authority"] = new
    client, _ = prepare(f)
    assert client.respond(f["request"]).final_error is None
    assert len(f["sends"]) == 1


def test_replacement_cannot_bypass_recovery_using_public_execution(tmp_path):
    f = durable_fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    _, prepared = prepare(f)
    f["authority"] = replacement(f)
    with pytest.raises(ValueError, match="handler_recovery_required"):
        f["authority"].invoke(
            "controlled-job",
            "diagnose",
            "replacement-attempt",
            {**prepared, "timeout_seconds": 120},
        )
    assert f["sends"] == []


@pytest.mark.parametrize("restart_between", [False, True])
def test_first_fault_uses_causal_witness_or_reports_unknown_order(
    tmp_path, monkeypatch, restart_between
):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    with monkeypatch.context() as patch:
        outage(f["anchor"].backend, "first_anchor_outage", patch)
        with pytest.raises(ValueError, match="first_anchor_outage"):
            auth.status("controlled-job")
    if restart_between:
        auth = reopen(f, tmp_path)
    f["clock"].value -= timedelta(seconds=60)
    with monkeypatch.context() as patch:
        outage(f["ledger"].backend, "second_store_outage", patch)
        with pytest.raises(ValueError, match="second_store_outage"):
            auth.status("controlled-job")
    auth = reopen(f, tmp_path)
    recovered = auth.recover("controlled-job", principal="simulation:controller")
    report = auth.stop_report("controlled-job")
    assert recovered["state"]["state"] == "SUSPENDED"
    assert report["first_stop_reason"] == (
        "fault_order_unverifiable" if restart_between else "first_anchor_outage"
    )
    assert report["first_fault_order_verified"] is (not restart_between)
    assert len(f["sends"]) == 0


def test_unreadable_attempt_inventory_does_not_report_zero_sends(tmp_path, monkeypatch):
    def dropped(request):
        raise httpx.ReadTimeout("fixture response lost", request=request)

    f = durable_fixture(tmp_path, handler=dropped)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ModelCallInterrupted):
        client.respond(f["request"])
    assert len(f["sends"]) == 1
    assert auth.status("controlled-job")["state"]["pending_units"] > 0
    with monkeypatch.context() as patch:
        patch.setattr(f["ledger"], "list_attempts", unavailable)
        report = auth.stop_report("controlled-job")
    assert report["attempts"] is None
    assert report["send_accounting"] is None
    assert report["ledger_and_anchor_verified"] is False


@pytest.mark.parametrize("failure_kind", ["timeout", "http_503", "cancelled"])
def test_completed_control_result_read_failure_keeps_receipt_and_read_only_retry(
    tmp_path, failure_kind
):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    _, prepared = prepare(f)
    request = control_envelope(f, prepared)
    service = ControlService(auth)
    failed = [True]

    class Transport:
        def exchange(self, value, *, timeout):
            return service.call(value, principal="simulation:worker")

        def read_result(self, identity, *, timeout):
            if failed[0]:
                if failure_kind == "timeout":
                    raise TimeoutError("fixture read timeout")
                if failure_kind == "http_503":
                    response = httpx.Response(
                        503, request=httpx.Request("GET", "https://fixture.invalid")
                    )
                    response.raise_for_status()
                raise KeyboardInterrupt("fixture read cancelled")
            return service.read_result(identity, principal="simulation:worker")

    client = ControlClient(Transport(), now=f["clock"])
    with pytest.raises(BaseException) as caught:
        client.call(request)
    error = caught.value
    assert getattr(error, "request_id", None) == request["request_id"]
    assert error.delivery == "COMPLETED"
    assert error.result_state == "UNREAD"
    assert error.receipt["state"] == "COMPLETED"
    assert error.cancelled is (failure_kind == "cancelled")
    failed[0] = False
    assert client.read_completed(request, error.receipt)["state"] == "COMPLETED"
    assert client.call(request)["state"] == "COMPLETED"
    assert len(f["sends"]) == 1
