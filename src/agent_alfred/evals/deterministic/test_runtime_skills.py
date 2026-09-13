"""SKILL-SPEC-r1 P1: real Host, catalog, SQLite and model input evidence."""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.config import MutableAssignmentProvider
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.settings import Settings

SKIP = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


def skill(path, name, body, description="A self-contained procedure"):
    target = path / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n" + body,
        encoding="utf-8",
    )
    return target


@contextmanager
def runtime(
    path,
    script,
    *,
    settings=None,
    factory=None,
    clock=None,
    snapshot_provider=None,
    secrets=(),
    extra_tools=(),
    extra_sinks=(),
    **options,
):
    path.mkdir(parents=True, exist_ok=True)
    state = ManagedStateDirectory.acquire(path / "state")
    conn = sqlite3.connect(path / "runs.sqlite3", check_same_thread=False)
    schema.migrate(conn)
    settings = settings or Settings()
    clock = clock or FakeClock()
    capture = CapturingSink()
    model = ScriptedModel(script)
    host = RuntimeHost(
        conn=conn,
        factory=factory or ScriptedModelFactory(model),
        settings=settings,
        clock=clock,
        fanout=FanOutSink([capture, *extra_sinks], process_instance_id="skills-test"),
        process_instance_id="skills-test",
        file_state=state,
        skill_builtin=path / "builtin",
        secrets=secrets,
        extra_tools=extra_tools,
        snapshot_provider=snapshot_provider
        or MutableAssignmentProvider(
            endpoint_id="test",
            model_id="m",
            wire_style="openai",
            api_key="test-key",
            settings=settings,
        ),
        **options,
    )
    host.start()
    try:
        yield host, model, capture
    finally:
        assert host.close()
        state.close()
        conn.close()


def submit(host, message, session_id=None):
    accepted = host.submit(SubmitRequest(message, session_id=session_id))
    assert accepted.kind == "accepted"
    return accepted, host.wait(accepted.run_id)


def system(request):
    return "\n".join(block.text for block in request.system or ())


@pytest.mark.parametrize(
    "control,names",
    [
        ("A A B", ["A", "B"]),
        ("off", []),
        ('"off"', ["off"]),
    ],
)
def test_ce01_explicit_and_off_use_only_requested_bodies(tmp_path, control, names):
    for name in ("A", "B", "off"):
        skill(tmp_path / "builtin", name, f"Body marker {name} end.\n")
    with runtime(tmp_path, [SKIP, "answer"]) as (host, model, _):
        accepted, result = submit(host, f"/skills {control}\nhello")
        assert result.outcome == "completed"
        assert len(model.requests) == 2
        assert message_plain_text(model.requests[-1].messages[-1]) == "hello"
        assert [s["name"] for s in result.memory_telemetry["skills"]["loaded"]] == names
        for name in ("A", "B", "off"):
            marker = f"Body marker {name} end.\n"
            assert (marker in system(model.requests[-1])) == (name in names)
            assert marker not in system(model.requests[0])
        assert all(
            a["purpose"] != "skill_selector"
            for a in result.memory_telemetry["input_attempts"]
        )
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            stored = conn.execute(
                "SELECT content FROM agent_log WHERE run_id=? AND role='user'",
                (accepted.run_id,),
            ).fetchone()[0]
            assert json.loads(stored)[0]["text"] == f"/skills {control}\nhello"


@pytest.mark.parametrize(
    "control",
    [
        "A Missing\nhello",
        "A B C D\nhello",
        "\nhello",
        '"A\nhello',
        "A",
        "../A\nhello",
        "A;B\nhello",
        '"A B"\nhello',
    ],
)
def test_ce02_invalid_explicit_request_stops_before_models_and_recovers(
    tmp_path, control
):
    for name in ("A", "B", "C", "D"):
        skill(tmp_path / "builtin", name, f"{name} procedure")
    with runtime(tmp_path, [SKIP, "fixed"]) as (host, model, _):
        _, failed = submit(host, "/skills " + control)
        assert failed.outcome == "failed"
        assert failed.error == "skill_preparation_failed"
        assert failed.memory_telemetry["skills"]["loaded"] == []
        assert model.requests == []
        _, fixed = submit(host, "/skills A\nhello")
        assert message_plain_text(fixed.reply) == "fixed"


def test_ce03_ce12_body_exactness_and_startup_user_override(tmp_path):
    skill(tmp_path / "builtin", "A", "builtin-marker")
    body = "\n  user procedure\r\n😀e\u0301\n\n"
    path = skill(tmp_path / "state/skills", "A", body)
    with runtime(tmp_path, [SKIP, "first", SKIP, "second"]) as (host, model, _):
        _, first = submit(host, "/skills A\nhello")
        assert body in system(model.requests[-1])
        metadata = first.memory_telemetry["skills"]["loaded"][0]
        assert metadata["source"] == "user" and metadata["overrides_builtin"]
        path.unlink()
        submit(host, "/skills A\nhello")
        assert body in system(model.requests[-1])
    with runtime(tmp_path, [SKIP, "after restart"]) as (host, model, _):
        submit(host, "/skills A\nhello")
        assert "builtin-marker" in system(model.requests[-1])
        assert body not in system(model.requests[-1])


@pytest.mark.parametrize("length,works", [(8000, True), (8001, False)])
def test_ce09_explicit_body_codepoint_boundary(tmp_path, length, works):
    body = "😀中e\u0301" * (length // 4) + "x" * (length % 4)
    skill(tmp_path / "builtin", "A", body)
    with runtime(tmp_path, [SKIP, "answer"]) as (host, model, _):
        _, result = submit(host, "/skills A\nhello")
        assert (result.outcome == "completed") == works
        if works:
            assert body in system(model.requests[-1])
        else:
            assert model.requests == []
            assert "body_limit_exceeded" in message_plain_text(result.reply)


@pytest.mark.parametrize(
    "max_steps,purposes,script",
    [
        (0, [], []),
        (1, ["answer"], ["answer"]),
        (2, ["skill_selector", "answer"], ['{"skills":["A"]}', "answer"]),
        (3, ["skill_selector", "gate", "answer"], ['{"skills":["A"]}', SKIP, "answer"]),
    ],
)
def test_ce16_selector_gate_preserve_the_last_answer_step(
    tmp_path, max_steps, purposes, script
):
    skill(tmp_path / "builtin", "A", "A body")
    with runtime(tmp_path, script, settings=Settings(max_steps=max_steps)) as (
        host,
        model,
        _,
    ):
        _, result = submit(host, "hello")
        assert result.outcome == ("max_steps" if max_steps == 0 else "completed")
        assert [
            a["purpose"] for a in result.memory_telemetry["input_attempts"]
        ] == purposes
        assert len(model.requests) == len(purposes)
        if max_steps:
            assert message_plain_text(result.reply) == "answer"


def test_ce04_selector_reads_only_catalog_and_safe_history_not_bodies(tmp_path):
    skill(tmp_path / "builtin", "A", "SECRET_BODY_A", "alpha-task")
    skill(tmp_path / "builtin", "B", "SECRET_BODY_B", "beta-task")
    with runtime(
        tmp_path,
        [
            SKIP,
            "previous answer",
            '{"skills":["B","A"]}',
            SKIP,
            "answer",
            '{"skills":[]}',
            SKIP,
            "plain",
        ],
    ) as (host, model, _):
        first, _ = submit(host, "/skills off\nhello previous")
        _, result = submit(host, "hello now", first.session_id)
        selector = model.requests[2]
        assert selector.tools == () and selector.tool_choice == "none"
        whole = system(selector) + " ".join(
            message_plain_text(m) for m in selector.messages
        )
        assert "alpha-task" in whole and "beta-task" in whole
        assert "SECRET_BODY" not in whole
        assert "hello previous" in whole and "previous answer" in whole
        assert "/skills off" not in whole
        assert [s["name"] for s in result.memory_telemetry["skills"]["loaded"]] == [
            "B",
            "A",
        ]
        assert result.memory_telemetry["input_attempts"][0][
            "working_history_groups"
        ] == [first.run_id]
        _, empty = submit(host, "hello", first.session_id)
        assert empty.memory_telemetry["skills"]["mode"] == "model"
        assert empty.memory_telemetry["skills"]["loaded"] == []
        assert "reason" not in empty.memory_telemetry["skills"]


@pytest.mark.parametrize(
    "text",
    [
        '{"skills":["Missing"]}',
        '{"skills":["A","A"]}',
        '{"skills":["A","B","C","D"]}',
        '{"skills":[1]}',
        '{"skills":[],"why":"yes"}',
        '{"skills":[],"skills":["B"]}',
        '```json\n{"skills":["B"]}\n```',
        'Explanation {"skills":["B"]}',
    ],
)
def test_ce05_invalid_model_selection_is_wholly_replaced_by_fallback(tmp_path, text):
    for name in ("A", "B", "C", "D"):
        skill(tmp_path / "builtin", name, name + " body")
    with runtime(tmp_path, [text, SKIP, "answer"]) as (host, model, _):
        _, result = submit(host, "使用 A")
        evidence = result.memory_telemetry["skills"]
        assert evidence["mode"] == "fallback" and evidence["reason"] == "invalid_output"
        assert evidence["selected"] == ["A"]
        assert len(model.requests) == 3
        assert len(result.model_results) == 3


@pytest.mark.parametrize(
    "task,names",
    [
        ("使用 A", ["A"]),
        ("用 B", ["B"]),
        ("按 AB", ["AB"]),
        ("use A", ["A"]),
        ("USE A", ["A"]),
        ("use a", []),
        ("普通问题", []),
        ("使用 ABC", []),
        ("使用 A_suffix", []),
        ("不要使用 A", []),
        ("use A, do not use B", ["A"]),
        ("不要使用 A；使用 B", ["B"]),
        ("no use A", []),
        ("不用 A", []),
        ("without use A", []),
        ("don't use A", []),
        ("`使用 A`", []),
        ("```\n使用 A\n```", []),
        ("~~~\n使用 A\n~~~", []),
        ("    使用 A", []),
        ("> 使用 A", []),
        ("abuse A", []),
        ("use A; 使用 B; 按 AB; use D", ["A", "B", "AB"]),
    ],
)
def test_ce06_ce07_fallback_depends_on_current_task_and_real_index(
    tmp_path, task, names
):
    for name in ("A", "AB", "B", "D"):
        skill(tmp_path / "builtin", name, name + " procedure")
    with runtime(tmp_path, [OSError("private-error"), SKIP, "answer"]) as (host, _, _):
        _, result = submit(host, task)
        evidence = result.memory_telemetry["skills"]
        assert evidence["selected"] == names
        assert "private-error" not in json.dumps(evidence)
        if "use D" in task:
            assert evidence["excluded"] == [
                {"name": "D", "reason": "count_limit_exceeded"}
            ]


def test_ce10_automatic_loading_skips_middle_long_document_and_continues(tmp_path):
    skill(tmp_path / "builtin", "A", "A body")
    skill(tmp_path / "builtin", "B", "B" * 8001)
    skill(tmp_path / "builtin", "C", "C body")
    with runtime(tmp_path, ['{"skills":["A","B","C"]}', SKIP, "answer"]) as (
        host,
        model,
        _,
    ):
        _, result = submit(host, "hello")
        evidence = result.memory_telemetry["skills"]
        assert [s["name"] for s in evidence["loaded"]] == ["A", "C"]
        assert evidence["excluded"] == [{"name": "B", "reason": "body_limit_exceeded"}]
        assert "A body" in system(model.requests[-1]) and "C body" in system(
            model.requests[-1]
        )
        assert "B" * 8001 not in system(model.requests[-1])
        attempts = result.memory_telemetry["input_attempts"]
        assert [s["name"] for s in attempts[-1]["skills"]] == ["A", "C"]
        assert attempts[0]["skills"] == [] and attempts[1]["skills"] == []


@pytest.mark.parametrize("count,selector", [(100, True), (101, False)])
def test_ce08_full_catalog_count_limit_does_not_limit_explicit_lookup(
    tmp_path, count, selector
):
    for number in reversed(range(count)):
        skill(tmp_path / "builtin", f"s{number:03d}", "procedure")
    script = (['{"skills":[]}'] if selector else []) + [
        SKIP,
        "answer",
        SKIP,
        "explicit",
    ]
    with runtime(tmp_path, script) as (host, model, _):
        _, result = submit(host, "hello")
        evidence = result.memory_telemetry["skills"]
        assert evidence["mode"] == ("model" if selector else "fallback")
        if selector:
            listed = json.loads(model.requests[0].system[0].text.split("Catalog: ")[1])
            assert [m["name"] for m in listed] == [f"s{n:03d}" for n in range(count)]
        else:
            assert evidence["reason"] == "catalog_count_limit"
        submit(host, f"/skills s{count - 1:03d}\nhello")
        assert "procedure" in system(model.requests[-1])


def test_ce11_load_failure_is_atomic_for_explicit_and_visible_for_auto(
    tmp_path, monkeypatch
):
    for name in ("A", "B"):
        skill(tmp_path / "builtin", name, name + " body")
    with runtime(tmp_path, ['{"skills":["A","B"]}', SKIP, "answer"]) as (
        host,
        model,
        _,
    ):
        load = host.skill_catalog.load

        def fault(name):
            if name == "B":
                raise OSError("private-loaded-secret")
            return load(name)

        monkeypatch.setattr(host.skill_catalog, "load", fault)
        _, explicit = submit(host, "/skills A B\nhello")
        assert explicit.outcome == "failed" and model.requests == []
        _, auto = submit(host, "hello")
        assert auto.outcome == "completed"
        assert [s["name"] for s in auto.memory_telemetry["skills"]["loaded"]] == ["A"]
        assert "private-loaded-secret" not in json.dumps(auto.memory_telemetry)
        assert auto.step_count == 3


@pytest.mark.parametrize("size", [16000, 16001])
def test_ce08_serialized_catalog_character_boundary(tmp_path, size):
    overhead = len('[{"name":"A","description":""}]')
    skill(tmp_path / "builtin", "A", "body", "中" * (size - overhead))
    script = (['{"skills":["A"]}'] if size == 16000 else []) + [SKIP, "answer"]
    with runtime(tmp_path, script) as (host, model, _):
        _, result = submit(host, "hello")
        assert result.outcome == "completed"
        if size == 16000:
            assert len(model.requests[0].system[0].text.split("Catalog: ")[1]) == size
        else:
            assert (
                result.memory_telemetry["skills"]["reason"] == "catalog_character_limit"
            )
            assert len(model.requests) == 2


def test_ce09_ce10_section_capacity_uses_actual_wrapper_and_continues(tmp_path):
    for name in ("A", "B", "C"):
        skill(tmp_path / "builtin", name, "x")
    with runtime(tmp_path, [SKIP, "baseline"]) as (host, model, _):
        submit(host, "/skills A B\nhello")
        section = model.requests[-1].system[-1].text
        overhead = len(section) - 2
    # Calibrate packaging separately, then verify real requests at the contract limits.
    for offset in (0, 1):
        skill(tmp_path / "builtin", "A", "a" * 8000)
        skill(tmp_path / "builtin", "B", "b" * (16000 - overhead - 8000 + offset))
        with runtime(
            tmp_path,
            [SKIP, "equal"] if offset == 0 else [],
            settings=Settings(input_character_limit=100000),
        ) as (host, model, _):
            _, result = submit(host, "/skills A B\nhello")
            assert result.outcome == ("completed" if offset == 0 else "failed")
            if offset == 0:
                assert len(model.requests[-1].system[-1].text) == 16000
            else:
                assert model.requests == []
                assert (
                    result.memory_telemetry["skills"]["excluded"][0]["reason"]
                    == "section_limit_exceeded"
                )
    with runtime(tmp_path, ['{"skills":["A","B","C"]}', SKIP, "answer"]) as (
        host,
        model,
        _,
    ):
        _, result = submit(host, "hello")
        assert [s["name"] for s in result.memory_telemetry["skills"]["loaded"]] == [
            "A",
            "C",
        ]


def test_ce14_control_projection_keeps_original_invalid_and_nonfirst_lines(tmp_path):
    skill(tmp_path / "builtin", "A", "body")
    script = [SKIP, "one", SKIP, "two", '{"skills":[]}', SKIP, "three"]
    with runtime(tmp_path, script) as (host, model, _):
        first, _ = submit(host, "/skills A\nhello one")
        submit(host, "/skills off\nhello two", first.session_id)
        third, _ = submit(host, "hello\n/skills A", first.session_id)
        texts = [message_plain_text(m) for m in model.requests[4].messages]
        assert texts == ["hello one", "one", "hello two", "two", "hello\n/skills A"]
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            texts = [
                json.loads(row[0])[0]["text"]
                for row in conn.execute(
                    "SELECT content FROM agent_log WHERE role='user' ORDER BY id"
                )
            ]
        assert texts == [
            "/skills A\nhello one",
            "/skills off\nhello two",
            "hello\n/skills A",
        ]
        assert third.session_id == first.session_id


def test_ce15_off_survives_catalog_failure_and_empty_catalog_is_normal(
    tmp_path, monkeypatch
):
    with runtime(tmp_path, [SKIP, "off", SKIP, "auto"]) as (host, model, _):
        _, empty = submit(host, "hello")
        assert empty.memory_telemetry["skills"]["mode"] == "empty"

        def unavailable():
            raise OSError("private unavailable")

        monkeypatch.setattr(host.skill_catalog, "list", unavailable)
        _, disabled = submit(host, "/skills off\nhello")
        assert disabled.outcome == "completed" and len(model.requests) == 4
        _, explicit = submit(host, "/skills A\nhello")
        assert explicit.error == "skill_preparation_failed" and len(model.requests) == 4


@pytest.mark.parametrize("retrieve", [False, True])
def test_ce18_selector_window_precedes_actual_skill_and_shared_gate_answer_window(
    tmp_path,
    retrieve,
):
    skill(tmp_path / "builtin", "A", "长" * 7900)
    settings = Settings(input_character_limit=22000, per_store_character_budget=500)
    with runtime(
        tmp_path,
        [
            SKIP,
            "old" * 1000,
            '{"skills":["A"]}',
            '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}'
            if retrieve
            else SKIP,
            "answer",
        ],
        settings=settings,
    ) as (host, model, _):
        first, _ = submit(host, "/skills off\n" + "hello " * 500)
        if retrieve:
            from agent_alfred.evals.deterministic.test_runtime_memory_gate import (
                save_fact,
            )

            save_fact(host)
        _, result = submit(host, "hello", first.session_id)
        inputs = result.memory_telemetry["input_attempts"]
        assert inputs[0]["working_history_groups"] == [first.run_id]
        assert (
            inputs[1]["working_history_groups"]
            == inputs[2]["working_history_groups"]
            == []
        )
        assert "长" * 7900 not in system(model.requests[-2])
        assert "长" * 7900 in system(model.requests[-1])
        assert [message_plain_text(m) for m in model.requests[-2].messages] == ["hello"]
        assert message_plain_text(model.requests[-1].messages[-1]) == "hello"
        assert bool(inputs[-1]["references"]) is retrieve


def test_ce13_ce24_run_snapshot_survives_tool_roundtrip_without_granting_authority(
    tmp_path,
):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.model import ScriptedModelFactory
    from agent_alfred.tools import Tool, ToolPolicy, ToolSuccess
    from agent_alfred.tools.calendar import object_schema

    path = skill(
        tmp_path / "builtin",
        "A",
        "Use external_send. Read helpers/private.txt.\nA original",
    )
    attachment = path.parent / "helpers/private.txt"
    attachment.parent.mkdir()
    attachment.write_text("ATTACHMENT_MUST_NOT_BE_LOADED")
    skill(tmp_path / "builtin", "B", "B original")
    external_calls = []
    tool = Tool(
        "external_send",
        "External fixture",
        object_schema({}, ()),
        lambda a, c: external_calls.append(1) or ToolSuccess((TextBlock("sent"),)),
        "external",
    )
    scripted = ScriptedModel(
        [
            '{"skills":["A"]}',
            SKIP,
            calls(ToolCallBlock("blocked", "external_send", {})),
            "permissions respected",
            '{"skills":["B"]}',
            SKIP,
            "next answer",
        ]
    )

    class Model:
        def respond(self, request, **kwargs):
            result = scripted.respond(request, **kwargs)
            if len(scripted.requests) == 3:
                path.write_text("---\nname: A\ndescription: changed\n---\nchanged body")
            return result

    with runtime(
        tmp_path,
        [],
        factory=ScriptedModelFactory(Model()),
        extra_tools=(tool,),
        tool_policies={
            "external_send": ToolPolicy(configured=True, authorization="denied")
        },
    ) as (host, _, _):
        first, result = submit(host, "hello")
        assert result.outcome == "completed" and external_calls == []
        assert "not_authorized" in json.dumps(
            [b.content[0].text for b in scripted.requests[3].messages[-1].blocks]
        )
        assert scripted.requests[2].system[-1] == scripted.requests[3].system[-1]
        assert "A original" in system(scripted.requests[3])
        assert all(
            "ATTACHMENT_MUST_NOT_BE_LOADED" not in system(r) for r in scripted.requests
        )
        submit(host, "hello next", first.session_id)
        assert "B original" in system(scripted.requests[-1])
        assert "A original" not in system(scripted.requests[-1])
        assert len([r for r in scripted.requests if "Catalog: " in system(r)]) == 2


def test_ce27_probe_and_consolidation_do_not_inherit_skill_or_selector(tmp_path):
    import threading

    from agent_alfred.evals.deterministic.test_memory_consolidation_service import PLAN

    finished = threading.Event()
    observed = []

    def snapshot_changed(snapshot):
        run = snapshot.active_run
        if run and run.purpose == "consolidation" and run.recording_state == "recorded":
            observed.append(run)
            finished.set()

    skill(tmp_path / "builtin", "A", "NONINHERITED_SKILL_MARKER")
    with runtime(
        tmp_path,
        [SKIP, "First safe fact", SKIP, "Second safe fact", PLAN, "probe"],
        settings=Settings(consolidation_source_threshold=2),
        snapshot_listener=snapshot_changed,
    ) as (host, model, _):
        first, _ = submit(host, "/skills A\nhello fact one")
        submit(host, "/skills A\nhello fact two", first.session_id)
        assert finished.wait(5), "actual consolidation did not finish"
        assert observed[-1].outcome == "completed"
        probe = host.submit(
            SubmitRequest(
                "probe", purpose="inference_probe", endpoint_id="test", model_id="m"
            )
        )
        assert host.wait(probe.run_id).outcome == "completed"
        assert len(model.requests) == 6
        for request in model.requests[-2:]:
            assert "NONINHERITED_SKILL_MARKER" not in system(request)
            assert "Catalog: " not in system(request)
        evidence = host.read_run_evidence(observed[-1].run_id, trace_root=tmp_path)
        assert "skills" not in evidence["memory"]
