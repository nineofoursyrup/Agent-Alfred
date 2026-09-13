"""SKILL-SPEC-r1: persisted evidence, public entrances and copied documentation."""

import hashlib
import io
import json
import shutil
import socket
import sqlite3
import threading
import time
from pathlib import Path

import httpx2
import pytest

from agent_alfred import cli, schema
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_runtime_skills import (
    SKIP,
    runtime,
    skill,
    submit,
    system,
)
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.model import ModelAssignment, ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.trace import RunBundleTraceSink
from agent_alfred.wiring import build_dashboard, build_default_host


def test_ce22_historic_trace_is_redacted_and_never_rebuilt_from_current_catalog(
    tmp_path,
):
    secret = "fixture-secret-for-skill"
    body = f"OLD PROCEDURE {secret}"
    source = skill(tmp_path / "builtin", "A", body)
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="skills-test",
    )
    with runtime(tmp_path, [SKIP, "done"], extra_sinks=(trace,), secrets=(secret,)) as (
        host,
        model,
        _,
    ):
        accepted, result = submit(host, "/skills A\nhello")
        assert result.outcome == "completed"
        assert body in system(model.requests[-1])
        evidence = host.read_run_evidence(
            accepted.run_id, trace_root=tmp_path / "traces"
        )
        assert evidence["trace_status"] == "available"
        serialized = json.dumps(evidence, ensure_ascii=False)
        assert "OLD PROCEDURE" in serialized and secret not in serialized
        metadata = evidence["memory"]["skills"]
        assert body not in json.dumps(metadata)
        assert (
            metadata["loaded"][0]["body_sha256"]
            == hashlib.sha256(body.encode()).hexdigest()
        )
    source.unlink()
    skill(tmp_path / "builtin", "A", "NEW PROCEDURE")
    with runtime(tmp_path, [], secrets=(secret,)) as (host, _, _):
        historic = host.read_run_evidence(
            accepted.run_id, trace_root=tmp_path / "traces"
        )
        text = json.dumps(historic)
        assert (
            "OLD PROCEDURE" in text
            and "NEW PROCEDURE" not in text
            and secret not in text
        )
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute(
                "UPDATE runs SET telemetry=json_set(telemetry, "
                "'$.trace_incomplete', json('true')) WHERE run_id=?",
                (accepted.run_id,),
            )
            conn.commit()
        incomplete = host.read_run_evidence(
            accepted.run_id, trace_root=tmp_path / "traces"
        )
        assert incomplete["trace_incomplete"] is True
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            schema.record_trace_prune(
                conn,
                run_id=accepted.run_id,
                prune_requested_at="2026-09-14T00:00:00Z",
                absence_confirmed_at="2026-09-14T00:00:00Z",
                prune_reason="manual",
            )
            conn.commit()
        pruned = host.read_run_evidence(accepted.run_id, trace_root=tmp_path / "traces")
        assert pruned["trace_status"] == "pruned" and pruned["events"] == []
        assert "NEW PROCEDURE" not in json.dumps(pruned)


@pytest.mark.parametrize(
    "control,mode,error",
    [
        ("A", "explicit", None),
        ("off", "disabled", None),
        ("Missing", "explicit", "skill_preparation_failed"),
    ],
)
def test_ce28_cli_and_real_http_share_the_same_host_contract(
    tmp_path, control, mode, error
):
    skill(tmp_path / "builtin", "A", "SAME HOST PROCEDURE")
    model = ScriptedModel(
        [SKIP, "web answer", SKIP, "cli answer"] if error is None else []
    )
    with socket.socket() as port_source:
        port_source.bind(("127.0.0.1", 0))
        port = port_source.getsockname()[1]
    dashboard = build_dashboard(
        state_dir=tmp_path / "state",
        skill_builtin=tmp_path / "builtin",
        port=port,
        factory=ScriptedModelFactory(model),
    )
    try:
        dashboard.start()
        host = dashboard.host
        prompt = f"/skills {control}\nhello"
        with httpx2.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as http:
            entry = http.get("/api/entry")
            assert entry.status_code == 200
            http.headers["x-agent-alfred-csrf"] = entry.json()["csrf_token"]
            created = http.post("/api/sessions", json={})
            assert created.status_code == 201
            session = created.json()["session_id"]
            sent = http.post(
                "/api/runs", json={"message": prompt, "session_id": session}
            )
            assert sent.status_code == 202, sent.text
            until = time.monotonic() + 5
            while True:
                response = http.get("/api/runs?filter=chat&limit=25")
                assert response.status_code == 200
                row = (
                    next(
                        r
                        for r in response.json()["runs"]
                        if r["run_id"] == sent.json()["run_id"]
                    )
                    if response.json()["runs"]
                    else None
                )
                if row and row["phase"] == "finished":
                    break
                assert time.monotonic() < until, response.text
                threading.Event().wait(0.01)
        cli_session = host.create_session()
        output = io.StringIO()
        code = cli.run_injected(host, prompt, session_id=cli_session, out=output)
        assert code == (1 if error else 0)
        assert ("Skill 准备失败" in output.getvalue()) is bool(error)
        with sqlite3.connect(tmp_path / "state/db.sqlite3") as conn:
            rows = conn.execute(
                "SELECT session_id, telemetry, outcome FROM runs "
                "WHERE purpose='chat' ORDER BY accepted_at, rowid"
            ).fetchall()
            assert len(rows) == 2 and rows[0][0] != rows[1][0]
            metadata = [json.loads(row[1])["memory"]["skills"] for row in rows]
            assert metadata[0] == metadata[1]
            assert metadata[0]["mode"] == mode
            assert [row[2] for row in rows] == ["failed" if error else "completed"] * 2
            saved = conn.execute(
                "SELECT content FROM agent_log WHERE role='user'"
            ).fetchall()
            assert [json.loads(row[0])[0]["text"] for row in saved] == [prompt, prompt]
        for index, request in enumerate(model.requests):
            assert ("SAME HOST PROCEDURE" in system(request)) is (
                control == "A" and index in (1, 3)
            )
    finally:
        assert dashboard.close()


def test_ce29_copied_example_and_gate_route_are_used_by_both_judgments(tmp_path):
    example = (
        Path(__file__).resolve().parents[4] / "docs/examples/skills/checklist/SKILL.md"
    )
    target = tmp_path / "state/skills/checklist/SKILL.md"
    target.parent.mkdir(parents=True)
    shutil.copyfile(example, target)
    from agent_alfred.evals.deterministic.test_check_skills import _load_check_skills

    assert _load_check_skills().validate_skill_text(target.read_text()) == []
    provider = MutableAssignmentProvider(
        endpoint_id="answer",
        model_id="a",
        wire_style="openai",
        api_key="fixture",
        retrieval_gate=ModelAssignment("judgment", "j", "openai"),
    )
    model = ScriptedModel(['{"skills":["checklist"]}', SKIP, "auto", SKIP, "explicit"])
    factory = ScriptedModelFactory(model)
    with runtime(tmp_path, [], factory=factory, snapshot_provider=provider) as (
        host,
        _,
        _,
    ):
        _, auto = submit(host, "organize")
        _, explicit = submit(host, "/skills checklist\norganize")
        assert auto.outcome == explicit.outcome == "completed"
        assert [(r.model.endpoint_id, r.model.model_id) for r in model.requests] == [
            ("judgment", "j"),
            ("judgment", "j"),
            ("answer", "a"),
            ("judgment", "j"),
            ("answer", "a"),
        ]
        assert auto.memory_telemetry["skills"]["loaded"][0]["source"] == "user"
        assert host.skill_catalog.load("checklist") in system(model.requests[-1])


def test_ce15_invalid_catalog_configuration_fails_startup_instead_of_empty(tmp_path):
    (tmp_path / "broken").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="invalid_skill_directory"):
        build_default_host(
            state_dir=tmp_path / "state",
            skill_builtin=tmp_path / "broken",
            factory=ScriptedModelFactory(ScriptedModel([])),
        )


def test_ce29_unavailable_assigned_gate_never_silently_uses_primary(tmp_path):
    from agent_alfred.model import EndpointUnconfigured

    skill(tmp_path / "builtin", "A", "procedure")
    answer = ScriptedModel(["answer"])
    snapshots = []

    class Factory:
        def create(self, snapshot):
            snapshots.append(snapshot)
            if snapshot.endpoint_id == "missing":
                raise EndpointUnconfigured("private provider diagnostic")
            return answer

    provider = MutableAssignmentProvider(
        endpoint_id="primary",
        model_id="answer",
        wire_style="openai",
        api_key="key",
        retrieval_gate=ModelAssignment("missing", "gate", "openai"),
    )
    with runtime(tmp_path, [], factory=Factory(), snapshot_provider=provider) as (
        host,
        _,
        _,
    ):
        _, result = submit(host, "使用 A")
        assert result.outcome == "completed" and result.step_count == 1
        assert len(answer.requests) == 1
        assert result.memory_telemetry["skills"]["reason"] == "model_unavailable"
        assert result.memory_telemetry["gate"]["fallback_reason"] == "model_unavailable"
        assert [s.endpoint_id for s in snapshots] == ["primary", "missing", "missing"]
        assert "private provider diagnostic" not in json.dumps(result.memory_telemetry)


def test_ce19_later_tool_growth_preserves_skill_and_does_not_repeat_action(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import TextBlock, ToolCallBlock

    skill(tmp_path / "builtin", "A", "FIXED SKILL")
    action = ToolCallBlock(
        "create",
        "create_event",
        {
            "title": "one appointment",
            "starts_at": "2026-09-10T12:00:00+08:00",
        },
    )
    with runtime(
        tmp_path, ['{"skills":["A"]}', SKIP, calls(action, TextBlock("x" * 64001))]
    ) as (host, model, capture):
        accepted, result = submit(host, "create appointment")
        assert result.error == "input_limit_exceeded"
        assert len(model.requests) == 3 and result.step_count == 4
        assert "FIXED SKILL" in system(model.requests[-1])
        assert sum(e.payload.name == "tool.finished" for e in capture.events) == 1
        evidence = host.read_run_evidence(accepted.run_id, trace_root=tmp_path)
        assert [a["purpose"] for a in evidence["memory"]["input_attempts"]] == [
            "skill_selector",
            "gate",
            "answer",
        ]
        assert evidence["memory"]["skills"]["loaded"][0]["name"] == "A"
        assert evidence["memory"]["input_failure"]["characters"] > 64000
