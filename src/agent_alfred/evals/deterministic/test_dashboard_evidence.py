"""The presentation endpoint against a real Run, trace and loopback HTTP."""

import json
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
    free_loopback_port,
)
from agent_alfred.events import AttemptCommitted, AttemptStarted
from agent_alfred.messages import TextBlock, ThinkingBlock
from agent_alfred.model import (
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
)
from agent_alfred.runtime.work import SubmitRequest
from agent_alfred.wiring import build_dashboard


class EvidenceModel(ScriptedModel):
    def respond(self, request, *, events=None, deadline=None):
        result = ScriptedModel(["正文快照"]).respond(request, deadline=deadline)
        attempt = result.attempts[0]
        blocks = (TextBlock("正文快照"), ThinkingBlock("不得进入界面的思考正文"))
        if events is not None:
            events.emit(AttemptStarted(attempt_id=attempt.attempt_id))
            events.emit(AttemptCommitted(
                attempt_id=attempt.attempt_id, blocks=blocks, usage=attempt.usage,
            ))
        return ModelResult(
            attempts=result.attempts,
            response=ModelResponse(blocks, "end_turn", request.model),
            final_error=None,
        )


@pytest.fixture
def recorded(tmp_path):
    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=tmp_path, port=port,
        factory=ScriptedModelFactory(EvidenceModel([])),
    )
    try:
        dashboard.start()
        host = dashboard.host
        submitted = host.submit(SubmitRequest(
            message="证据测试", session_id=host.create_session(),
        ))
        host.wait(submitted.run_id)
        yield port, submitted.run_id, next(tmp_path.rglob("trace.jsonl"))
    finally:
        assert dashboard.close()


def read(port, run_id):
    with urlopen(
        f"http://127.0.0.1:{port}/api/run-evidence?"
        + urlencode({"run_id": run_id}), timeout=3,
    ) as response:
        assert response.status == 200
        return json.load(response)


def test_evidence_http_exposes_text_and_types_without_thinking_body(recorded):
    port, run_id, _trace = recorded
    body = read(port, run_id)
    assert body["trace_status"] == "available"
    assert body["recording_state"] == "recorded"
    assert body["trace_incomplete"] is False
    encoded = json.dumps(body, ensure_ascii=False)
    assert "正文快照" in encoded
    assert '"type": "thinking"' in encoded
    assert "不得进入界面的思考正文" not in encoded
    assert body["attempts"][0]["cost"] == {"state": "unknown"}


@pytest.mark.parametrize("damage", ["missing", "tail", "interior", "shape"])
def test_unavailable_evidence_does_not_rewrite_saved_facts(recorded, damage):
    port, run_id, trace = recorded
    if damage == "missing":
        trace.unlink()
    elif damage == "tail":
        with trace.open("ab") as target:
            target.write(b'{"unfinished":')
    elif damage == "shape":
        lines = trace.read_text().splitlines()
        event = json.loads(lines[-1])
        event["payload"] = None
        lines[-1] = json.dumps(event)
        trace.write_text("\n".join(lines) + "\n")
    else:
        with trace.open("ab") as target:
            target.write(b'not json\n{"another":"record"}\n')
    body = read(port, run_id)
    assert body["trace_status"] == ("partial" if damage == "tail" else "unavailable")
    assert body["trace_incomplete"] is False
    assert body["recording_state"] == "recorded"
    assert body["attempts"][0]["outcome"] == "committed"


def test_active_evidence_uses_existing_sse_not_the_trace(tmp_path):
    from threading import Event

    entered, release = Event(), Event()

    class PausedModel(EvidenceModel):
        def respond(self, request, *, events=None, deadline=None):
            entered.set()
            assert release.wait(5)
            return super().respond(request, events=events, deadline=deadline)

    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=tmp_path, port=port,
        factory=ScriptedModelFactory(PausedModel([])),
    )
    try:
        dashboard.start()
        submitted = dashboard.host.submit(SubmitRequest(
            message="运行中的证据", session_id=dashboard.host.create_session(),
        ))
        assert entered.wait(5)
        body = read(port, submitted.run_id)
        assert body["trace_status"] == "live"
        assert body["events"] == []
        assert body["attempts"] == []
    finally:
        release.set()
        assert dashboard.close()


def test_historic_interrupted_without_telemetry_is_not_live(recorded):
    import sqlite3

    port, run_id, trace = recorded
    database = next(parent / "db.sqlite3" for parent in trace.parents
                    if (parent / "db.sqlite3").exists())
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE runs SET outcome = 'interrupted', telemetry = NULL "
            "WHERE run_id = ?", (run_id,),
        )
    body = read(port, run_id)
    assert body["trace_status"] == "available"
    assert body["events"]
    assert body["attempts"] == []
    assert body["recording_state"] is None


def test_failed_run_keeps_recorded_accounting_and_string_terminal_error(tmp_path):
    from agent_alfred.events import AttemptAborted
    from agent_alfred.model import AttemptRecord, ModelError, Usage

    class FailedModel(ScriptedModel):
        def respond(self, request, *, events=None, deadline=None):
            error = ModelError(False, None, "offline failure", "failed-attempt",
                               "offline_failure")
            usage = Usage(output_tokens=7)
            if events is not None:
                events.emit(AttemptStarted(attempt_id="failed-attempt"))
                events.emit(AttemptAborted(
                    attempt_id="failed-attempt", error=error, usage=usage,
                ))
            return ModelResult(
                attempts=(AttemptRecord("failed-attempt", False, "aborted",
                                        usage, error),),
                response=None, final_error=error,
            )

    port = free_loopback_port()
    dashboard = build_dashboard(
        state_dir=tmp_path, port=port,
        factory=ScriptedModelFactory(FailedModel([])),
    )
    try:
        dashboard.start()
        submitted = dashboard.host.submit(SubmitRequest(
            message="失败调用", session_id=dashboard.host.create_session(),
        ))
        dashboard.host.wait(submitted.run_id)
        trace = next(tmp_path.rglob("trace.jsonl"))
        terminal = json.loads(trace.read_text().splitlines()[-1])
        assert terminal["payload"]["outcome"] == "failed"
        assert isinstance(terminal["payload"]["error"], str)
        body = read(port, submitted.run_id)
        assert body["trace_status"] == "available"
        assert body["recording_state"] == "recorded"
        assert body["trace_incomplete"] is False
        assert body["attempts"][0]["usage"]["output_tokens"] == 7
        assert body["attempts"][0]["outcome"] == "aborted"
        assert body["events"][-1]["payload"]["error"]
    finally:
        assert dashboard.close()
