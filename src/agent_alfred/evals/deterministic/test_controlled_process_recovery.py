"""Actual local process competition/crash, still exclusively synthetic IO."""

import json
import multiprocessing
import os
from threading import Thread

import httpx2 as httpx
import pytest

from agent_alfred.evals.acceptance.controlled.control import ControlService
from agent_alfred.evals.acceptance.controlled.persistence import (
    SQLiteExecutionAnchor,
    SQLiteExecutionStore,
)
from agent_alfred.evals.acceptance.controlled.runtime import (
    SyntheticCredentials,
    SyntheticRuntime,
)
from agent_alfred.evals.acceptance.controlled_persistence import (
    PersistentControlledAuthority,
)
from agent_alfred.evals.acceptance.materials import ProtectedMaterialStore
from agent_alfred.evals.acceptance.simulation_authority import SimulationAuthority
from agent_alfred.model import ModelCallInterrupted

from .test_controlled_execution import Clock, prepare
from .test_controlled_execution import response as provider_response
from .test_controlled_persistence import control_envelope, durable_fixture, reopen


def process_service(root, submission, send, connection):
    clock = Clock()
    # The original simulator intentionally cannot be restored from copied files.
    # Relay each fresh decision read to that same live source in the parent.
    source = SimulationAuthority(
        root / f"source-read-fixture-{os.getpid()}", clock=clock
    )

    def read_decision(reference):
        connection.send(reference)
        reply = connection.recv()
        if reply["error"]:
            raise ValueError(reply["error"])
        return reply["value"]

    source.read_decision = read_decision
    runtime = SyntheticRuntime(
        source=source,
        transport=httpx.MockTransport(send),
        credentials=SyntheticCredentials(),
    )
    return PersistentControlledAuthority(
        runtime=runtime,
        store=SQLiteExecutionStore(root / "authority.sqlite"),
        anchor=SQLiteExecutionAnchor(root / "anchor.sqlite"),
        vault=ProtectedMaterialStore(root / "vault", scope="synthetic-full-materials"),
        allowed_objects={submission["material_ref"]["object_sha256"]},
        expected_manifest=submission["material_ref"]["manifest_sha256"],
        output_root=root / "received",
        worker="simulation:worker",
        controller="simulation:controller",
        subject="simulation:user",
        now=clock,
    )


def prepared_control(auth, submission, model_request):
    fixture = {
        "authority": auth,
        "operation": submission["plan"]["operations"][0],
        "request": model_request,
        "clock": auth.now,
    }
    _, prepared = prepare(fixture)
    return control_envelope(fixture, prepared)


def control_process(
    root, submission, model_request, handoff, ready, barrier, output, connection
):
    def send(request):
        output.put(("sent", os.getpid()))
        return provider_response(request)

    auth = process_service(root, submission, send, connection)
    try:
        if handoff is not None:
            auth.recover(
                "controlled-job", principal="simulation:controller", handoff=handoff
            )
            request = prepared_control(auth, submission, model_request)
            (root / "fixture-control.json").write_text(json.dumps(request))
            ready.set()
        else:
            assert ready.wait(timeout=10)
            request = json.loads((root / "fixture-control.json").read_text())
        barrier.wait(timeout=10)
        receipt = ControlService(auth).call(request, principal="simulation:worker")
        output.put(("receipt", receipt))
    except BaseException as error:
        output.put(("error", str(error)))
    finally:
        auth.store.close()
        auth.anchor.close()
        connection.close()


def crashing_process(root, submission, model_request, handoff, connection):
    def send(request):
        (root / "fixture-send-observed").write_text("one synthetic send entered")
        os._exit(23)

    auth = process_service(root, submission, send, connection)
    auth.recover("controlled-job", principal="simulation:controller", handoff=handoff)
    request = prepared_control(auth, submission, model_request)
    (root / "fixture-control.json").write_text(json.dumps(request))
    ControlService(auth).call(request, principal="simulation:worker")


def source_channel(f, context):
    parent, child = context.Pipe()

    def serve():
        try:
            while True:
                reference = parent.recv()
                try:
                    value = f["source"].read_decision(reference)
                    reply = {"value": value, "error": None}
                except Exception as error:
                    reply = {"value": None, "error": str(error)}
                parent.send(reply)
        except EOFError, OSError:
            pass
        finally:
            parent.close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    return child, thread


def test_two_process_control_competition_has_one_ledger_attempt_and_one_send(tmp_path):
    f = durable_fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    handoff = f["authority"].handoff(
        "controlled-job", principal="simulation:controller"
    )
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    ready = context.Event()
    channels = [source_channel(f, context) for _ in range(2)]
    processes = [
        context.Process(
            target=control_process,
            args=(
                tmp_path,
                f["submission"],
                f["request"],
                handoff if index == 0 else None,
                ready,
                barrier,
                output,
                channel,
            ),
        )
        for index, (channel, _) in enumerate(channels)
    ]
    try:
        for process in processes:
            process.start()
        for channel, _ in channels:
            channel.close()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        observations = [output.get(timeout=5) for _ in range(3)]
        assert [kind for kind, _ in observations].count("sent") == 1
        assert [kind for kind, _ in observations].count("receipt") >= 1
        assert all(
            value == "handler_recovery_required"
            for kind, value in observations
            if kind == "error"
        )
        status = reopen(f, tmp_path).status("controlled-job")
        assert len(status["attempts"]) == status["state"]["counts"]["total"] == 1
        assert status["state"]["spent_units"] == 5400000
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output.close()
        output.join_thread()
        for _, thread in channels:
            thread.join(timeout=5)


def test_process_exit_after_send_intent_recovers_debt_without_model_retry(tmp_path):
    f = durable_fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    handoff = f["authority"].handoff(
        "controlled-job", principal="simulation:controller"
    )
    context = multiprocessing.get_context("spawn")
    channel, thread = source_channel(f, context)
    process = context.Process(
        target=crashing_process,
        args=(tmp_path, f["submission"], f["request"], handoff, channel),
    )
    process.start()
    channel.close()
    try:
        process.join(timeout=20)
        assert process.exitcode == 23
        request = json.loads((tmp_path / "fixture-control.json").read_text())
        auth = reopen(f, tmp_path)
        before = auth.status("controlled-job")
        assert before["attempts"][0]["send_state"] == "SENDING"
        after = auth.recover("controlled-job", principal="simulation:controller")
        assert after["attempts"][0]["send_state"] == "MAY_HAVE_SENT"
        assert after["state"]["pending_units"] == before["state"]["pending_units"] > 0
        assert after["state"]["started_at"] == before["state"]["started_at"]
        reply = ControlService(auth).call(request, principal="simulation:worker")
        assert reply["state"] == "PENDING"
        original = after["attempts"][0]
        prepared = auth.read_object(original["prepared_ref"])
        raw = provider_response(
            httpx.Request(
                "POST",
                "https://api.deepseek.com/chat/completions",
                json=prepared["payload"],
            )
        )
        reference = auth.store.put_object({"status_code": 200, "raw": raw.text})
        auth.settle_attempt(
            "controlled-job",
            original["attempt_id"],
            reference,
            principal="simulation:controller",
        )
        service = ControlService(auth)
        completed = service.reconcile(request, principal="simulation:controller")
        assert completed["state"] == "COMPLETED"
        assert service.call(request, principal="simulation:worker") == completed
        assert len(f["sends"]) == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        thread.join(timeout=5)


def test_original_network_failure_survives_anchor_failure_during_error_readback(
    tmp_path, monkeypatch
):
    f = durable_fixture(tmp_path)

    def disconnected(job):
        raise OSError("anchor readback failed after original send")

    def failed_send(request):
        monkeypatch.setattr(f["anchor"], "read", disconnected)
        raise TimeoutError("original wire failure retained")

    f["runtime"].transport = httpx.MockTransport(failed_send)
    f["authority"].submit_job(f["submission"])
    client, _ = prepare(f)
    with pytest.raises(ModelCallInterrupted) as error:
        client.respond(f["request"])
    report = f["authority"].stop_report("controlled-job")
    assert (
        error.value.result.attempts[0].attempt_id == report["attempts"][0]["attempt_id"]
    )
    assert report["send_accounting"]["possibly_sent"] == 1
    assert report["pending_units"] > 0 and report["ledger_and_anchor_verified"] is False
    assert "original wire failure retained" in json.dumps(report["faults"])
    assert "anchor readback failed" in json.dumps(report["blockers"])
