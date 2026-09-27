"""Persistent public dispatch, conservative recovery and control retransmission."""

import json
import sqlite3
from copy import deepcopy
from datetime import timedelta

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.controlled.control import (
    ControlClient,
    ControlDeliveryUnknown,
    ControlService,
    HTTPControlTransport,
    control_request,
)
from agent_alfred.evals.acceptance.controlled.persistence import (
    SQLiteExecutionAnchor,
    SQLiteExecutionStore,
)
from agent_alfred.evals.acceptance.controlled_persistence import (
    PersistentControlledAuthority,
)
from agent_alfred.evals.acceptance.schema import digest, encode
from agent_alfred.model import ModelCallInterrupted
from agent_alfred.resource_rollback import IncompleteRollback

from .test_controlled_execution import fixture, prepare, terms
from .test_controlled_execution import response as provider_response


def durable_fixture(tmp_path, **options):
    f = fixture(tmp_path, **options)
    f["ledger"] = SQLiteExecutionStore(tmp_path / "authority.sqlite")
    f["anchor"] = SQLiteExecutionAnchor(tmp_path / "anchor.sqlite")
    original = f["authority"]
    f["authority"] = PersistentControlledAuthority(
        **{
            key: value
            for key, value in vars(original).items()
            if key not in ("store", "anchor")
        },
        store=f["ledger"],
        anchor=f["anchor"],
    )
    return f


def reopen(f, root):
    f["ledger"].close()
    f["anchor"].close()
    f["ledger"] = SQLiteExecutionStore(root / "authority.sqlite")
    f["anchor"] = SQLiteExecutionAnchor(root / "anchor.sqlite")
    f["authority"] = PersistentControlledAuthority(
        **{
            key: value
            for key, value in vars(f["authority"]).items()
            if key not in ("store", "anchor")
        },
        store=f["ledger"],
        anchor=f["anchor"],
    )
    return f["authority"]


@pytest.mark.parametrize("fail_close", [False, True])
def test_sqlite_initialization_failure_releases_connection_and_preserves_cleanup(
    tmp_path, monkeypatch, fail_close
):
    path = tmp_path / "invalid.sqlite"
    path.write_bytes(b"This is not a SQLite database.")
    opened = []
    close_calls = []
    cleanup_failure = OSError("fixture close temporarily unavailable")
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def close(self):
            close_calls.append(self)
            if fail_close and len(close_calls) == 1:
                raise cleanup_failure
            super().close()

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=TrackedConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        with pytest.raises(
            sqlite3.DatabaseError, match="file is not a database"
        ) as error:
            SQLiteExecutionStore(path)
        assert len(opened) == 1
        assert close_calls == opened
        if fail_close:
            pending = error.value.__cause__
            assert isinstance(pending, IncompleteRollback)
            assert pending.failure is error.value
            assert pending.errors == (cleanup_failure,)
            assert pending.retry() is True
            assert close_calls == opened * 2
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            opened[0].execute("SELECT 1")
    finally:
        for connection in opened:
            sqlite3.Connection.close(connection)


def test_restart_keeps_original_clock_money_and_entire_event_chain(tmp_path):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    initial = auth.submit_job(f["submission"])["state"]
    client, _ = prepare(f)
    assert client.respond(f["request"]).final_error is None
    before = auth.status("controlled-job")
    handoff = auth.handoff("controlled-job", principal="simulation:controller")
    f["clock"].value += timedelta(seconds=100)
    restarted = reopen(f, tmp_path)
    after = restarted.recover(
        "controlled-job", principal="simulation:controller", handoff=handoff
    )
    transfer_fields = {"revision", "event_digest", "handler_epoch", "clean_handoff"}
    assert {k: v for k, v in after["state"].items() if k not in transfer_fields} == {
        k: v for k, v in before["state"].items() if k not in transfer_fields
    }
    assert after["state"]["started_at"] == initial["started_at"]
    assert after["state"]["spent_units"] == 5400000
    assert after["state"]["counts"]["total"] == 1
    assert len(f["ledger"].read_events("controlled-job")) == 17
    assert len(f["anchor"].read_events("controlled-job")) == 17
    with pytest.raises(ValueError, match="job_already_submitted"):
        restarted.submit_job(f["submission"])
    f["clock"].value += timedelta(seconds=10700)
    with pytest.raises(ValueError, match="batch_deadline"):
        restarted.finish("controlled-job", "diagnose")
    f["clock"].value -= timedelta(seconds=10800)
    stopped = reopen(f, tmp_path).status("controlled-job")
    assert stopped["state"]["stop_reason"] == "batch_deadline"


def test_unknown_send_settles_once_after_restart_and_retains_first_error(tmp_path):
    def dropped(request):
        raise TimeoutError("original provider response lost")

    f = durable_fixture(tmp_path, handler=dropped)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ModelCallInterrupted) as failure:
        client.respond(f["request"])
    assert "original provider response lost" in str(failure.value.cause)
    before = auth.status("controlled-job")
    assert before["attempts"][0]["send_state"] == "MAY_HAVE_SENT"
    assert before["state"]["pending_units"] > 0
    auth = reopen(f, tmp_path)
    auth.recover("controlled-job", principal="simulation:controller")
    row = before["attempts"][0]
    response = {"status_code": 200, "raw": provider_response(f["sends"][0]).text}
    reference = f["ledger"].put_object(response)
    first = auth.settle_attempt(
        "controlled-job",
        row["attempt_id"],
        reference,
        principal="simulation:controller",
    )
    repeated = auth.settle_attempt(
        "controlled-job",
        row["attempt_id"],
        reference,
        principal="simulation:controller",
    )
    assert first == repeated
    state = auth.status("controlled-job")["state"]
    assert state["spent_units"] == 5400000 and state["pending_units"] == 0
    assert state["stop_reason"] == "request_state_unresolved"
    assert first["attempt"]["error"] == "TimeoutError"
    with pytest.raises(ValueError, match="original_response_immutable"):
        auth.settle_attempt(
            "controlled-job",
            row["attempt_id"],
            f["ledger"].put_object({**response, "raw": "{}"}),
            principal="simulation:controller",
        )
    with pytest.raises(ValueError, match="request_state_unresolved"):
        prepare(f)
    assert len(f["sends"]) == 1


def test_anchor_outage_restored_cannot_revive_and_report_keeps_failure_cleanup(
    tmp_path, monkeypatch
):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    for observation in (
        {
            "kind": "failure",
            "status": "FAIL",
            "reason": "old product obligation failure",
        },
        {"kind": "resource", "status": "retained", "resource": "fixture:database"},
        {
            "kind": "cleanup",
            "status": "cleanup_failed",
            "reason": "fixture:delete denied",
        },
    ):
        auth.record_observation(
            "controlled-job", observation, principal="simulation:controller"
        )
    original = f["anchor"].read

    def disconnected(job):
        raise OSError("independent anchor unavailable")

    monkeypatch.setattr(f["anchor"], "read", disconnected)
    with pytest.raises(OSError):
        auth.status("controlled-job")
    report = auth.stop_report("controlled-job")
    assert report["ledger_and_anchor_verified"] is False
    assert len(report["observations"]) == 3
    assert report["blockers"] and report["faults"]
    monkeypatch.setattr(f["anchor"], "read", original)
    state = reopen(f, tmp_path).status("controlled-job")["state"]
    assert state["state"] == "SUSPENDED" and state["stop_reason"] == "OSError"
    with pytest.raises(ValueError, match="OSError"):
        prepare(f)
    assert f["credentials"].reads == 0


def test_missing_authority_store_restore_and_new_directory_do_not_reset(tmp_path):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    started = auth.submit_job(f["submission"])["state"]["started_at"]
    f["ledger"].close()
    path = tmp_path / "authority.sqlite"
    backup = tmp_path / "saved-authority.sqlite"
    path.rename(backup)
    empty = SQLiteExecutionStore(path)
    auth.store = empty
    with pytest.raises(ValueError, match="original_job_ledger_required"):
        auth.submit_job(f["submission"])
    empty.close()
    path.rename(tmp_path / "empty-authority.sqlite")
    backup.rename(path)
    f["ledger"] = auth.store = SQLiteExecutionStore(path)
    state = auth.status("controlled-job")["state"]
    assert state["started_at"] == started
    assert state["stop_reason"] == "original_job_ledger_required"
    assert f["credentials"].reads == 0


def test_source_failure_and_clock_rollback_are_permanent_after_reopen(tmp_path):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    f["runtime"].available = False
    with pytest.raises(ValueError, match="runtime_readback_unverifiable"):
        prepare(f)
    f["runtime"].available = True
    auth = reopen(f, tmp_path)
    with pytest.raises(ValueError, match="runtime_readback_unverifiable"):
        prepare(f)
    assert (
        auth.status("controlled-job")["state"]["stop_reason"]
        == "runtime_readback_unverifiable"
    )
    assert f["credentials"].reads == 0


def test_material_cache_migration_preserves_same_grant_budget_and_clock(tmp_path):
    f = durable_fixture(tmp_path, product=True)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    client.respond(f["request"])
    before = auth.status("controlled-job")["state"]
    handoff = auth.handoff("controlled-job", principal="simulation:controller")
    original = tmp_path / "received" / "controlled-job"
    original.rename(tmp_path / "offline-old-handler-cache")
    f["clock"].value += timedelta(seconds=90)
    auth = reopen(f, tmp_path)
    after = auth.recover(
        "controlled-job",
        principal="simulation:controller",
        material_output=tmp_path / "new-handler-cache",
        handoff=handoff,
    )["state"]
    for key in (
        "plan_ref",
        "binding_ref",
        "run_source_ref",
        "run_request_ref",
        "started_at",
        "counts",
        "spent_units",
        "pending_units",
    ):
        assert after[key] == before[key]
    client, _ = prepare(f)
    assert client.respond(f["request"]).final_error is None
    assert len(f["sends"]) == 2


def control_envelope(f, descriptor, *, identity="control-1", action="invoke"):
    return control_request(
        request_id=identity,
        job_id="controlled-job",
        action=action,
        arguments={
            "operation_id": f["operation"]["id"],
            "attempt_id": "attempt-1",
            "descriptor": {**descriptor, "timeout_seconds": 120},
        }
        if action == "invoke"
        else {"operation_id": f["operation"]["id"]},
        deadline=f["clock"].value + timedelta(seconds=120),
    )


def test_control_120_seconds_lost_ack_retry_and_duplicate_finish_never_resend(tmp_path):
    f = durable_fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    _, descriptor = prepare(f)
    server = ControlService(f["authority"])
    observed = []
    lose = [True]

    def http_control(request):
        observed.append(request.extensions["timeout"]["read"])
        if request.url.path == "/control":
            result = server.call(
                json.loads(request.content), principal="simulation:worker"
            )
            if lose.pop() if lose else False:
                raise httpx.ReadTimeout("control reply dropped after settlement")
        else:
            result = server.read_result(
                request.url.path.rsplit("/", 1)[1], principal="simulation:worker"
            )
        return httpx.Response(200, json=result)

    transport = HTTPControlTransport(
        "https://control.invalid",
        httpx.Client(transport=httpx.MockTransport(http_control), trust_env=False),
    )
    client = ControlClient(transport, now=f["clock"])
    envelope = control_envelope(f, descriptor)
    with pytest.raises(ControlDeliveryUnknown) as failure:
        client.call(envelope)
    assert failure.value.possibly_sent is True and failure.value.cancelled is False
    assert observed[0] == 150  # 120-second Attempt plus 30-second control margin.
    handoff = server.authority.handoff(
        "controlled-job", principal="simulation:controller"
    )
    server.authority = reopen(f, tmp_path)
    server.authority.recover(
        "controlled-job", principal="simulation:controller", handoff=handoff
    )
    done = client.call(envelope)
    assert done["ok"] is True and len(f["sends"]) == 1
    finish = control_envelope(f, descriptor, identity="finish-1", action="finish")
    assert client.call(finish) == client.call(finish)
    assert len(f["sends"]) == 1
    changed = deepcopy(envelope)
    changed["arguments"]["descriptor"]["timeout_seconds"] = 119
    with pytest.raises(ValueError, match="control_id_reused"):
        server.call(changed, principal="simulation:worker")


@pytest.mark.parametrize("boundary", ["price_expiry", "last_clock_rollback"])
def test_final_dispatch_time_sample_closes_expired_quote_and_rollback(
    tmp_path, boundary
):
    pricing = None
    if boundary == "price_expiry":
        pricing = {
            group: terms(model, valid_until="2026-09-27T00:00:01+00:00")
            for group, model in (
                ("flash", "deepseek-flash"),
                ("pro", "deepseek-v4-pro"),
            )
        }
    f = durable_fixture(tmp_path, pricing=pricing)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    original = f["runtime"].quote
    quotes, samples = [], []

    def quote(plan, operation, payload, now):
        result = original(plan, operation, payload, now)
        quotes.append(now)
        if len(quotes) == 4 and boundary == "price_expiry":
            f["clock"].value += timedelta(seconds=2)
        return result

    def clock():
        value = f["clock"].value
        if len(quotes) == 4:
            samples.append(value)
            if len(samples) == 2 and boundary == "last_clock_rollback":
                return value - timedelta(seconds=1)
        return value

    f["runtime"].quote = quote
    f["authority"].now = clock
    with pytest.raises(ValueError):
        client.respond(f["request"])
    state = f["authority"].status("controlled-job")["state"]
    assert len(f["sends"]) == 0
    assert state["state"] == "SUSPENDED" and state["pending_units"] > 0
    assert state["stop_reason"] == (
        "billing_price_expired"
        if boundary == "price_expiry"
        else "authority_clock_unverifiable"
    )
    f["authority"].now = f["clock"]
    with pytest.raises(ValueError, match=state["stop_reason"]):
        prepare(f)


def test_stale_attempt_row_and_missing_history_are_rejected_on_public_readback(
    tmp_path,
):
    captured = {}

    def before_intent(job, attempt):
        captured["row"] = f["ledger"].get_attempt(job, attempt)
        captured["revision"] = f["ledger"].get(job)["revision"]

    f = durable_fixture(tmp_path, before_intent=before_intent)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    client, _ = prepare(f)
    client.respond(f["request"])
    old = {"row": captured["row"], "revision": captured["revision"]}
    with sqlite3.connect(tmp_path / "authority.sqlite") as attack:
        attack.execute(
            "UPDATE documents SET sha256=?,raw=? WHERE part=? AND key=?",
            (
                digest(old),
                encode(old),
                "JOB#controlled-job",
                "ATTEMPT#" + old["row"]["attempt_id"],
            ),
        )
    with pytest.raises(ValueError, match="attempt_ledger_unverifiable"):
        auth.status("controlled-job")
    assert auth.stop_report("controlled-job")["ledger_and_anchor_verified"] is False
    assert len(f["sends"]) == 1
    with sqlite3.connect(tmp_path / "authority.sqlite") as attack:
        attack.execute("DELETE FROM documents WHERE key='EVENT#000002'")
    with pytest.raises(ValueError, match="authority_event_unverifiable"):
        prepare(f)


def test_control_allows_119_second_attempt_and_distinguishes_client_cancellation(
    tmp_path,
):
    def slow(request):
        f["clock"].value += timedelta(seconds=119)
        return provider_response(request)

    f = durable_fixture(tmp_path, handler=slow)
    f["authority"].submit_job(f["submission"])
    _, prepared = prepare(f)
    server = ControlService(f["authority"])
    envelope = control_envelope(f, prepared)
    receipt = server.call(envelope, principal="simulation:worker")
    assert (
        server.read_result(receipt["result_ref"], principal="simulation:worker")["ok"]
        is True
    )
    state = f["authority"].status("controlled-job")["state"]
    assert state["state"] == "ACTIVE" and len(f["sends"]) == 1
    assert 0 < f["sends"][0].extensions["timeout"]["read"] <= 120

    class Cancelled:
        def exchange(self, request, *, timeout):
            raise KeyboardInterrupt()

    with pytest.raises(ControlDeliveryUnknown) as failure:
        ControlClient(Cancelled(), now=f["clock"]).call(envelope)
    assert failure.value.cancelled is True and failure.value.possibly_sent is True
    assert failure.value.request_id == envelope["request_id"]
