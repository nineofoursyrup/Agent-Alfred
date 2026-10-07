"""Local runner projections and transport; all model responses are synthetic."""

import errno
import json
import os
import struct
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

from agent_alfred.evals.acceptance.examples_v4 import supplement_batch


def case_materials():
    batch = supplement_batch(phase="calibration")
    profile = batch["profiles"][0]
    profile["parameters"].update(
        max_steps=12,
        input_character_limit=64000,
        gate_input_character_limit=64000,
        working_memory_rounds=8,
        per_store_limit=8,
        per_store_character_budget=8000,
        max_tokens=8192,
        per_attempt_timeout_s=120.0,
        overall_deadline_s=900.0,
        gate_model_budget_s=120.0,
        stream=False,
        stream_fallback=False,
    )
    profile["product_models"] = [
        {
            "endpoint_id": "deepseek",
            "model_id": "deepseek-flash",
            "wire_style": "openai",
            "thinking": "disabled",
            "provider_version": "synthetic-only",
        }
    ]
    case = deepcopy(batch["cases"][0])
    case.update(
        operation="chat",
        input="Hello",
        setup={
            "version": 1,
            "local_tool_allowlist": [],
        },
    )
    return batch, case


@pytest.mark.parametrize("bridge_fails", [False, True])
@pytest.mark.parametrize("seal_error", [None, "storage", "interrupt", "exit"])
def test_product_bridge_persists_cleanup_first_error_even_after_recovery(
    tmp_path, monkeypatch, bridge_fails, seal_error
):
    """Offline wiring only; no actual installation, native launch or sample."""
    from agent_alfred.evals.acceptance import controlled_calibration
    from agent_alfred.evals.acceptance.controlled import local_runner, native
    from agent_alfred.evals.acceptance.controlled.local_runtime import (
        LocalInstalledRuntime,
    )

    report = {"classification": "OFFLINE_FIXTURE_ONLY",
              "signal_error": {"type": "PermissionError", "errno": 1},
              "group_release": "group_absent", "leader_returncode": 0}
    first = RuntimeError("original offline bridge failure")
    seal_failure = {
        None: None,
        "storage": OSError(errno.ENOSPC, "offline cleanup evidence disk full"),
        "interrupt": KeyboardInterrupt(),
        "exit": SystemExit(2),
    }[seal_error]
    seal_fails = seal_failure is not None

    class Slot:
        root = tmp_path / "actual"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def launch(self):
            pytest.fail("offline wiring must not launch native")

        def cleanup_observation(self):
            return report

    runtime = object.__new__(LocalInstalledRuntime)
    runtime._config = {"bundle_root": str(tmp_path / "bundle"),
                       "protected_root": str(tmp_path / "protected")}
    runtime._bundle_manifest, runtime._bundle_verifier = {}, None
    driver = SimpleNamespace(authority=SimpleNamespace(runtime=runtime),
                             workspace=tmp_path / "workspace", job_id="offline")
    operation = {"id": "product:C01", "kind": "product"}
    monkeypatch.setattr(controlled_calibration, "_operation", lambda *a: operation)
    monkeypatch.setattr(native, "prepare_runner_slot", lambda *a, **k: Slot())
    monkeypatch.setattr(local_runner, "case_input", lambda *a: {})

    def bridge(dto, workspace, **kwargs):
        workspace.mkdir(parents=True)
        if bridge_fails:
            raise first
        return {"evidence": {"classification": "OFFLINE_FIXTURE_ONLY"}}

    monkeypatch.setattr(local_runner, "_run_pair", bridge)
    if seal_fails:
        def failed_seal(*args):
            raise seal_failure
        monkeypatch.setattr(local_runner, "_seal", failed_seal)
    if bridge_fails or seal_fails:
        expected = first if bridge_fails else seal_failure
        if seal_error in {"interrupt", "exit"}:
            expected = seal_failure
        with pytest.raises(type(expected)) as failure:
            local_runner.run_local_case(driver, {"id": "C01"},
                                        {"operations": [operation]}, {})
        assert failure.value is expected
        if bridge_fails and seal_fails:
            from .test_controlled_transport_boundary import exception_graph
            graph = list(exception_graph(failure.value))
            assert first in graph and seal_failure in graph
    else:
        result = local_runner.run_local_case(driver, {"id": "C01"},
                                            {"operations": [operation]}, {})
        assert result["evidence"]["native_cleanup"]["observation"] == report
    path = driver.workspace / driver.job_id / "C01-local/native-cleanup.json"
    if seal_fails:
        assert not path.exists()
    else:
        assert json.loads(path.read_bytes()) == report


def test_local_case_does_not_export_hidden_material_or_allow_path_configuration():
    from agent_alfred.evals.acceptance.controlled.local_projection import (
        case_input,
        validate_case_input,
    )

    batch, case = case_materials()
    batch["judge_tests"][0]["expected"] = "SECRET_JUDGE_ANSWER"
    case["gold"] = "SECRET_GOLD"
    dto = case_input(batch, case)
    assert "SECRET" not in str(dto)
    assert "judge_model" not in dto["profile"]
    assert dto["case"]["input"] == "Hello"
    validate_case_input(dto)
    invalid = deepcopy(dto)
    invalid["profile"]["parameters"]["persona_file"] = "/etc/passwd"
    with pytest.raises(ValueError, match="local_case"):
        validate_case_input(invalid)


def test_synthetic_pair_uses_real_hosts_and_preserves_simulation(tmp_path):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    batch, case = case_materials()
    result = run_synthetic_case(
        batch,
        case,
        tmp_path / "pair",
        model=ScriptedModel(
            [
                '{"retrieve":false,"query":null,"reason_code":"greeting"}',
                "Hello there",
            ]
        ),
    )
    assert result["output"] == "Hello there"
    assert result["recorded"] is True
    assert result["sampled_at"] is None
    assert result["source"] == "simulation"
    bridge = result["evidence"]["local_bridge"]
    assert bridge["mode"] == "SYNTHETIC_PIPE_ONLY"
    assert bridge["real_sample"] is False
    assert bridge["independent_business_readback"]["run"][:2] == [
        "finished",
        "completed",
    ]


def test_foreign_response_ref_stops_dispatch_and_preserves_first_attempt(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.acceptance.controlled import local_runner
    from agent_alfred.model import ScriptedModel

    class ForeignRefFrames(local_runner.Frames):
        poisoned = False

        def write(self, value, *, deadline):
            if value.get("kind") == "result" and not self.poisoned:
                self.poisoned = True
                value = deepcopy(value)
                value["payload"]["session"] = "0" * 32
            super().write(value, deadline=deadline)

    monkeypatch.setattr(local_runner, "Frames", ForeignRefFrames)
    batch, case = case_materials()
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            "This response must not be dispatched after the protocol failure",
        ]
    )
    root = tmp_path / "foreign-ref"
    with pytest.raises((ValueError, EOFError)):
        local_runner.run_synthetic_case(batch, case, root, model=model)
    assert len(model.requests) == 1
    failure = json.loads((root / "trusted/first-bridge-failure.json").read_bytes())
    assert len(failure["attempt_observations"]) == 1
    assert failure["automatic_retry_permitted"] is False
    responses = list((root / "trusted").glob("first-model-response-*.json"))
    assert len(responses) == 1
    first_response = json.loads(responses[0].read_bytes())
    assert first_response["attempts"][0]["attempt_id"] == (
        failure["attempt_observations"][0]["attempt_id"]
    )


@pytest.mark.parametrize("gate_kind", ["invalid_output", "model_error"])
def test_valid_model_failure_still_allows_conservative_gate_fallback(
    tmp_path, gate_kind
):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import (
        AttemptRecord,
        ModelError,
        ModelResult,
        ScriptedModel,
        Usage,
    )

    batch, case = case_materials()
    gate = "invalid gate JSON"
    if gate_kind == "model_error":
        error = ModelError(False, 503, None, "synthetic-gate")
        gate = ModelResult(
            (AttemptRecord(error.attempt_id, False, "aborted", Usage(), error),),
            None,
            error,
        )
    model = ScriptedModel([gate, "Normal fallback answer"])
    result = run_synthetic_case(batch, case, tmp_path / "fallback", model=model)
    assert result["output"] == "Normal fallback answer"
    assert result["recorded"] is True
    assert len(model.requests) == 2
    assert result["source"] == "simulation"


def test_protocol_failure_is_sticky_before_another_frame_is_written(tmp_path):
    from agent_alfred.evals.acceptance.controlled.local_ipc import CaseData
    from agent_alfred.evals.acceptance.controlled.local_runner import _WorkerBridge
    from agent_alfred.runtime.memory import InputEvidenceError

    class WrongScope:
        writes = []

        def write(self, value, *, deadline):
            self.writes.append(value)

        def read(self, *, deadline):
            return {**self.writes[-1], "session": "2" * 32}

    frames = WrongScope()
    bridge = _WorkerBridge(
        frames, "1" * 32, monotonic() + 5, CaseData(tmp_path, "1" * 32)
    )
    with pytest.raises(InputEvidenceError) as first:
        bridge.local_now()
    assert isinstance(first.value.__cause__, ValueError)
    assert str(first.value.__cause__) == "local_reply_scope_mismatch"
    with pytest.raises(InputEvidenceError) as second:
        bridge.exchange("request")
    assert second.value is first.value
    assert len(frames.writes) == 1
    assert bridge.sequence == 1


def test_shadow_verifies_draft_receipt_path_and_actual_file(tmp_path):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelRef,
        ModelResponse,
        ModelResult,
        ScriptedModel,
        Usage,
    )

    batch, case = case_materials()
    case["setup"]["local_tool_allowlist"] = ["draft_message"]
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            ModelResult(
                (AttemptRecord("draft-attempt", False, "committed", Usage()),),
                ModelResponse(
                    (
                        ToolCallBlock(
                            "draft-call",
                            "draft_message",
                            {
                                "subject": "First draft",
                                "body": "Keep this exact first content",
                            },
                        ),
                    ),
                    "tool_use",
                    ModelRef("deepseek", "deepseek-flash"),
                ),
                None,
            ),
            "Draft saved",
        ]
    )
    result = run_synthetic_case(batch, case, tmp_path / "draft", model=model)
    assert result["output"].startswith("Draft saved\n\n系统操作回执：\n")
    bridge = result["evidence"]["local_bridge"]
    mappings = [m for step in bridge["projection"] for m in step["receipt_mappings"]]
    assert len(mappings) == 1
    relative = mappings[0]["target"]
    actual = tmp_path / "draft" / "actual" / case["id"] / relative
    assert actual.read_text() == "# First draft\n\nKeep this exact first content\n"
    assert (
        bridge["independent_business_readback"]["file_contents"][relative]["content"]
        == actual.read_text()
    )


def _attack_worker(monkeypatch, script, *, before_import=""):
    """An ordinary untrusted subprocess, explicitly no native-isolation claim."""
    import agent_alfred
    from agent_alfred.evals.acceptance.controlled import local_runner

    source = str(Path(agent_alfred.__file__).resolve().parent.parent)
    program = (
        "import sys,os\nfrom pathlib import Path\nsys.path.insert(0,"
        + repr(source)
        + ")\n" + before_import
        + "\nfrom agent_alfred.evals.acceptance.controlled "
        "import local_runner as runner\n" + script + "\nrunner.worker_main()\n"
    )

    def launch(root):
        return subprocess.Popen(
            [sys.executable, "-s", "-c", program],
            cwd=root,
            env={"PYTHONNOUSERSITE": "1"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    monkeypatch.setattr(local_runner, "_synthetic_launch", launch)


def test_bridge_worker_completes_without_host_network_libraries(tmp_path, monkeypatch):
    """A fresh worker uses only IPC, even when provider/TLS imports are unavailable."""
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    _attack_worker(
        monkeypatch,
        "",
        before_import="""
class NoHostNetwork:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {
            'openai', 'anthropic', 'httpx2', 'httpcore2', 'truststore',
        }:
            raise ImportError('host network libraries unavailable: ' + fullname)
sys.meta_path.insert(0, NoHostNetwork())
""",
    )
    batch, case = case_materials()
    model = ScriptedModel(
        ['{"retrieve":false,"query":null,"reason_code":"greeting"}', "IPC only"]
    )
    result = run_synthetic_case(batch, case, tmp_path / "no-host-network", model=model)
    assert result["output"] == "IPC only"
    assert len(model.requests) == 2


def test_worker_payload_changes_are_rejected_before_model_dispatch(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    _attack_worker(
        monkeypatch,
        """
original_wire = runner.wire_payload
def forged(request):
    value = original_wire(request)
    value['model'] = 'unapproved-model'
    return value
runner.wire_payload = forged
""",
    )
    batch, case = case_materials()
    model = ScriptedModel([])
    with pytest.raises(ValueError, match="local_full_projection_mismatch"):
        run_synthetic_case(batch, case, tmp_path / "forged", model=model)
    assert model.requests == []
    failure = json.loads(
        (tmp_path / "forged/trusted/first-bridge-failure.json").read_text()
    )
    assert failure["automatic_retry_permitted"] is False


def test_duplicate_frame_reads_first_reply_without_another_model_attempt(
    tmp_path, monkeypatch
):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    _attack_worker(
        monkeypatch,
        """
class RepeatFrames(runner.Frames):
    last = None
    def write(self, value, *, deadline):
        if value.get('kind') == 'request':
            self.last = value
        super().write(value, deadline=deadline)
    def read(self, *, deadline):
        value = super().read(deadline=deadline)
        if value.get('kind') == 'result':
            super().write(self.last, deadline=deadline)
            assert super().read(deadline=deadline) == value
        return value
runner.Frames = RepeatFrames
""",
    )
    batch, case = case_materials()
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            "First output",
        ]
    )
    result = run_synthetic_case(batch, case, tmp_path / "replay", model=model)
    assert result["output"] == "First output"
    assert len(model.requests) == 2


def test_extra_actual_file_cannot_hide_outside_the_tool_ledger(tmp_path, monkeypatch):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    _attack_worker(
        monkeypatch,
        """
original_run = runner._run_case
def mutate(*args, **kwargs):
    record = original_run(*args, **kwargs)
    target = Path.cwd() / args[1]['id'] / 'outbox'
    target.mkdir(exist_ok=True)
    (target / 'undeclared.md').write_text('invisible side effect')
    return record
runner._run_case = mutate
""",
    )
    batch, case = case_materials()
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            "First output",
        ]
    )
    with pytest.raises(ValueError, match="local_business_projection_mismatch"):
        run_synthetic_case(batch, case, tmp_path / "mutation", model=model)
    assert len(model.requests) == 2
    raw = json.loads(
        (tmp_path / "mutation/trusted/first-worker-record.untrusted.json").read_text()
    )
    assert raw["trusted"] is False
    assert raw["record"]["output"] == "First output"


def test_arbitrary_worker_evidence_is_only_an_untrusted_sidecar(tmp_path, monkeypatch):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    _attack_worker(
        monkeypatch,
        """
original_run = runner._run_case
def forged(*args, **kwargs):
    record = original_run(*args, **kwargs)
    record['evidence'] = {'forged_approval': 'WORKER_ASSERTION'}
    record['tools'] = [{'forged_tool': True}]
    return record
runner._run_case = forged
""",
    )
    batch, case = case_materials()
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
            "First output",
        ]
    )
    result = run_synthetic_case(batch, case, tmp_path / "sidecar", model=model)
    assert "WORKER_ASSERTION" not in json.dumps(result)
    assert result["tools"] == []
    assert result["evidence"]["trace_status"] == "available"
    assert result["evidence"]["independent_readback"]["worker_record_used"] is False
    raw = json.loads(
        (tmp_path / "sidecar/trusted/first-worker-record.untrusted.json").read_text()
    )
    assert raw["record"]["evidence"]["forged_approval"] == "WORKER_ASSERTION"


@pytest.mark.parametrize(
    "raw, error",
    [
        (struct.pack("!I", 9000000), "local_frame_size"),
        (struct.pack("!I", 7) + b'{"x":1', "local_frame_incomplete"),
        (struct.pack("!I", 13) + b'{"x":1,"x":2}', "duplicate"),
    ],
)
def test_frames_reject_oversize_truncated_and_duplicate_json(raw, error):
    from agent_alfred.evals.acceptance.controlled.local_ipc import Frames

    read, write = os.pipe()
    os.write(write, raw)
    os.close(write)
    with os.fdopen(read, "rb") as incoming:
        with pytest.raises((ValueError, EOFError), match=error):
            Frames(incoming, None).read(deadline=monotonic() + 1)


@pytest.mark.parametrize("scenario", ["backpressure", "roundtrip", "broken_pipe"])
def test_frames_pipe_writes_respect_deadline_and_preserve_complete_payload(
    tmp_path, scenario
):
    """An ordinary Python child bounds the regression itself and is always reaped."""
    program = r'''
import json, os, select, sys, threading, time
from types import SimpleNamespace
from agent_alfred.evals.acceptance.controlled.local_ipc import Frames

reader, writer = os.pipe()
incoming = SimpleNamespace(fileno=lambda: reader)
outgoing = SimpleNamespace(fileno=lambda: writer)
scenario = sys.argv[1]
try:
    pipe_buf = os.fpathconf(writer, "PC_PIPE_BUF")
    if scenario == "backpressure":
        os.set_blocking(writer, False)
        try:
            while True:
                os.write(writer, b"x" * pipe_buf)
        except BlockingIOError:
            pass
        assert len(os.read(reader, pipe_buf)) == pipe_buf
        assert select.select([], [writer], [], 0)[1]
        os.set_blocking(writer, True)
        started = time.monotonic()
        try:
            Frames(None, outgoing).write(
                {"payload": "x" * max(3000, pipe_buf * 3)},
                deadline=started + 0.05,
            )
        except TimeoutError as failure:
            assert str(failure) == "local_frame_deadline"
        else:
            raise AssertionError("backpressured frame did not honor its deadline")
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, elapsed
        assert os.get_blocking(writer)
        print(json.dumps({"scenario": scenario, "pipe_buf": pipe_buf,
                          "elapsed": elapsed, "outcome": "deadline"}))
    elif scenario == "roundtrip":
        value = {"payload": "汉字🧩\n" * 2000, "tail": {"complete": True}}
        observed, failures = [], []
        deadline = time.monotonic() + 2
        def receive():
            try:
                observed.append(Frames(incoming, None).read(deadline=deadline))
            except BaseException as failure:
                failures.append(failure)
        thread = threading.Thread(target=receive)
        thread.start()
        try:
            Frames(None, outgoing).write(value, deadline=deadline)
        finally:
            thread.join(2)
        assert not thread.is_alive()
        assert failures == [] and observed == [value]
        print(json.dumps({"scenario": scenario, "outcome": "complete"}))
    else:
        os.close(reader)
        reader = -1
        try:
            Frames(None, outgoing).write({"payload": "closed"},
                                         deadline=time.monotonic() + 1)
        except BrokenPipeError:
            pass
        else:
            raise AssertionError("closed pipe was reported as a successful send")
        print(json.dumps({"scenario": scenario, "outcome": "broken_pipe"}))
finally:
    if reader >= 0:
        os.close(reader)
    os.close(writer)
'''
    # subprocess.run kills and waits on timeout, including the broken r1 writer.
    result = subprocess.run(
        [sys.executable, "-B", "-s", "-c", program, scenario],
        cwd=tmp_path,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[3])},
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["scenario"] == scenario
    assert report["outcome"] == {
        "backpressure": "deadline",
        "roundtrip": "complete",
        "broken_pipe": "broken_pipe",
    }[scenario]


def test_large_data_uses_scoped_refs_while_control_remains_64k(tmp_path):
    from agent_alfred.evals.acceptance.controlled.control import MAX_CONTROL_BYTES
    from agent_alfred.evals.acceptance.controlled.local_ipc import CaseData, Frames
    from agent_alfred.evals.acceptance.schema import encode

    data = CaseData(tmp_path, "1" * 32, create=True)
    value = {"text": "x" * (MAX_CONTROL_BYTES + 1)}
    ref = data.put(value, direction="host", sequence=4)
    assert len(encode(ref)) < MAX_CONTROL_BYTES
    assert data.read(ref, direction="host", sequence=4) == value
    with pytest.raises(ValueError, match="local_frame_size"):
        Frames(None, None).write(value, deadline=monotonic() + 1)
    for wrong in (
        {**ref, "sequence": 5},
        {**ref, "direction": "worker"},
        {**ref, "path": "/etc/passwd"},
    ):
        with pytest.raises(ValueError, match="local_data_ref_invalid"):
            data.read(wrong, direction="host", sequence=4)
    target = next((tmp_path / ".alfred-ipc" / ("1" * 32)).iterdir())
    target.write_bytes(b"!" * target.stat().st_size)
    with pytest.raises(ValueError, match="local_data_digest_mismatch"):
        data.read(ref, direction="host", sequence=4)


def test_disconnect_after_attempt_receipt_preserves_first_attempt_without_resend(
    tmp_path,
    monkeypatch,
):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    _attack_worker(
        monkeypatch,
        """
class DisconnectFrames(runner.Frames):
    def write(self, value, *, deadline):
        super().write(value, deadline=deadline)
        if value.get('kind') == 'ack':
            os._exit(0)
runner.Frames = DisconnectFrames
""",
    )
    batch, case = case_materials()
    model = ScriptedModel(
        [
            '{"retrieve":false,"query":null,"reason_code":"greeting"}',
        ]
    )
    root = tmp_path / "unknown"
    with pytest.raises((EOFError, BrokenPipeError)):
        run_synthetic_case(batch, case, root, model=model)
    assert len(model.requests) == 1
    failure = json.loads((root / "trusted/first-bridge-failure.json").read_text())
    assert len(failure["attempt_observations"]) == 1
    assert failure["automatic_retry_permitted"] is False
    with pytest.raises(FileExistsError):
        run_synthetic_case(batch, case, root, model=model)
    assert len(model.requests) == 1


def test_declared_memory_persona_calendar_and_prior_session_are_seeded_once(tmp_path):
    from agent_alfred.evals.acceptance.controlled.local_runner import run_synthetic_case
    from agent_alfred.model import ScriptedModel

    batch, case = case_materials()
    case["setup"].update(
        persona="Use short answers.",
        memory=[
            {
                "ref": "tea",
                "kind": "semantic",
                "payload": {"subject": "drink", "fact": "I prefer tea."},
            }
        ],
        session_seed=[{"input": "Remember this context", "output": "Context saved"}],
        calendar=[{"title": "Review", "starts_at": "2026-09-30T08:00:00Z"}],
    )
    result = run_synthetic_case(
        batch,
        case,
        tmp_path / "seeded",
        model=ScriptedModel(
            [
                '{"retrieve":false,"query":null,"reason_code":"greeting"}',
                "Ready",
            ]
        ),
    )
    actual = result["evidence"]["local_bridge"]["independent_business_readback"]
    assert len(actual["facts"]) == 1
    assert len(actual["calendar"]) == 1
    assert len(actual["seed_messages"]) == 2
    assert result["recorded"] is True
    assert result["evidence"]["setup"]["seed_copied_once"] is True


def test_display_clock_does_not_change_real_deadlines():
    from datetime import UTC, datetime

    from agent_alfred.evals.acceptance.controlled.local_runner import _PromptClock

    clock = _PromptClock(lambda: "2001-01-01T00:00:00+00:00")
    before = datetime.now(UTC)
    assert clock.local_now().year == 2001
    assert clock.wall_utc() >= before
    assert abs(clock.monotonic() - monotonic()) < 1
