"""ROUTING-SPEC-r1: real Host/Graph/SQLite public request paths."""

import pytest

from agent_alfred.evals.deterministic.test_runtime_skills import (
    SKIP,
    runtime,
    submit,
)
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.messages import message_plain_text


def enable(host):
    api = DashboardApi(facade=host)
    state = api.behaviour()[1]
    status, result = api.mutate_behaviour(
        dict(action="save", expected_revision=state["revision"], enabled=True)
    )
    assert status == 200, result


@pytest.mark.parametrize(
    "task,category,route,reply",
    [
        ("你好", "greeting", "quick", "你好！"),
        ("谢谢", "thanks", "quick", "不客气。"),
        ("无需回复，也不需要做任何事", "no_reply", "no_action", None),
        ("你好，帮我查上次保存的地址", "greeting", "fallback", "actual answer"),
        ("谢谢", "greeting", "fallback", "actual answer"),
        ("你好!!!!", "greeting", "fallback", "actual answer"),
        ("你好", "```greeting```", "fallback", "actual answer"),
        ("你好", "full", "full", "actual answer"),
    ],
)
def test_ce02_03_04_actual_routing_and_classifier_input(
    tmp_path, task, category, route, reply
):
    with runtime(tmp_path, [SKIP, category, "actual answer"]) as (host, model, _):
        enable(host)
        _, result = submit(host, task)
        assert result.outcome == "completed"
        assert (
            None if result.reply is None else message_plain_text(result.reply)
        ) == reply
        routing = result.memory_telemetry["routing"]
        assert routing["route"] == route
        request = model.requests[1]
        assert len(request.messages) == 1
        assert message_plain_text(request.messages[0]) == task
        assert request.tools == ()
        assert len(model.requests) == (3 if route in ("full", "fallback") else 2)
        if route in ("full", "fallback"):
            assert [message_plain_text(m) for m in model.requests[-1].messages] == [
                task
            ]


def test_ce09_12_no_reply_recovery_and_next_window(tmp_path):
    with runtime(
        tmp_path, [SKIP, "full", "A answer", SKIP, "no_reply", SKIP, "full", "C answer"]
    ) as (host, model, _):
        enable(host)
        a, _ = submit(host, "A")
        b, quiet = submit(host, "不用回复", a.session_id)
        api = DashboardApi(facade=host)
        status, recovered = api.recover_reply(
            dict(
                process_instance_id="skills-test",
                session_id=a.session_id,
                run_id=b.run_id,
            )
        )
        assert status == 200, recovered
        assert recovered["reply_disposition"] == "no_reply"
        assert recovered["reply_text"] is None
        _, c = submit(host, "C", a.session_id)
        assert [message_plain_text(m) for m in model.requests[-1].messages] == [
            "A",
            "A answer",
            "C",
        ]
        exclusions = c.memory_telemetry["input_preparation"]["history_exclusions"]
        assert exclusions["intentional_no_reply"] == 1
        assert exclusions["incomplete"] == 0

    # Rebuild the real Host on the same SQLite/state files, then query the
    # consolidation reader and the next answer's complete working window.
    with runtime(tmp_path, [SKIP, "full", "D answer"]) as (host, model, _):
        assert (
            host.memory_service.consolidation.session_status(a.session_id)[
                "unprocessed_count"
            ]
            == 2
        )
        _, result = submit(host, "D", a.session_id)
        assert [message_plain_text(m) for m in model.requests[-1].messages] == [
            "A",
            "A answer",
            "C",
            "C answer",
            "D",
        ]
        assert (
            result.memory_telemetry["input_preparation"]["history_exclusions"][
                "intentional_no_reply"
            ]
            == 1
        )


@pytest.mark.parametrize("category,no_reply", [("full", False), ("no_reply", True)])
@pytest.mark.parametrize("broken_recovery", [False, True])
def test_ce05_11_real_projection_failure_and_recovery_boundary(
    tmp_path, category, no_reply, broken_recovery
):
    from agent_alfred.graph import NodeOutcome
    from agent_alfred.runtime.routing import (
        CONTEXT_FAILURE,
        build_routing_graph,
        project_context,
        recover_context,
    )

    def corrupt(snapshot):
        return project_context({**snapshot, "context_version": 999})

    def build(tools):
        return build_routing_graph(
            tools,
            projection=corrupt,
            recovery=(lambda s, c: NodeOutcome())
            if broken_recovery
            else recover_context,
        )

    with runtime(
        tmp_path, [SKIP, category, "must not run"], routing_graph_builder=build
    ) as (host, model, _):
        enable(host)
        _, result = submit(host, "不用回复" if no_reply else "task")
        routing = result.memory_telemetry["routing"]
        assert len(model.requests) == 2
        if broken_recovery:
            assert result.error == "context_invalid"
            assert routing["fallback"]["decision"] == "blocked"
            assert routing["fallback"]["reason"] == "context_invalid"
        else:
            assert result.outcome == "completed"
            assert routing["graph_result"] == (
                "NoAction" if no_reply else "CompletedWithRecovery"
            )
            assert routing["recoveries"][0]["node_id"] == "project_context"
            assert (
                result.reply is None
                if no_reply
                else message_plain_text(result.reply) == CONTEXT_FAILURE
            )


@pytest.mark.parametrize(
    "category,outcome,requests",
    [
        ("greeting", "completed", 1),
        ("no_reply", "completed", 1),
        ("full", "max_steps", 1),
        (RuntimeError("offline"), "max_steps", 1),
    ],
)
def test_ce10_last_classifier_step_allows_zero_step_terminals(
    tmp_path, category, outcome, requests
):
    from agent_alfred.settings import Settings

    # Gate has its existing two-Step guard and therefore does not run.
    with runtime(tmp_path, [category], settings=Settings(max_steps=1)) as (
        host,
        model,
        _,
    ):
        enable(host)
        _, result = submit(host, "不用回复" if category == "no_reply" else "你好")
        assert result.outcome == outcome
        assert len(model.requests) == requests
        if outcome == "max_steps":
            assert result.memory_telemetry["routing"]["fallback"]["entered"] is False


def test_ce04_08_failed_classifier_fallback_is_clean_and_notice_is_persistent(tmp_path):
    with runtime(tmp_path, [SKIP, RuntimeError("offline"), "fallback answer"]) as (
        host,
        model,
        sink,
    ):
        enable(host)
        _, result = submit(host, "task")
        assert result.outcome == "completed"
        assert message_plain_text(result.reply) == "fallback answer"
        assert [message_plain_text(m) for m in model.requests[-1].messages] == ["task"]
        notices = [
            e
            for e in sink.events
            if e.payload.name == "notice" and e.payload.code == "routing_fallback"
        ]
        assert len(notices) == 1 and notices[0].trace_policy == "persist"
        assert result.memory_telemetry["routing"]["fallback"]["model_requests"] == 1


def test_ce13_invalid_settings_runs_ordinary_with_notice(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "behaviour.json").write_text("broken")
    with runtime(tmp_path, [SKIP, "ordinary answer"]) as (host, model, sink):
        _, result = submit(host, "你好")
        assert message_plain_text(result.reply) == "ordinary answer"
        assert len(model.requests) == 2
        assert (
            result.memory_telemetry["routing"]["fallback"]["reason"]
            == "routing_unavailable"
        )
        assert not any(e.payload.name == "graph.started" for e in sink.events)
        assert any(
            getattr(e.payload, "code", None) == "routing_unavailable"
            for e in sink.events
        )


def test_ce08_loaded_skill_forces_full_and_classifier_has_no_skill(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_skills import skill, system

    skill(tmp_path / "builtin", "A", "unique procedure body")
    with runtime(tmp_path, [SKIP, "greeting", "answer"]) as (host, model, _):
        enable(host)
        _, result = submit(host, "/skills A\n你好")
        assert (
            result.memory_telemetry["routing"]["decision_reason"]
            == "skills_require_full"
        )
        assert "unique procedure body" not in system(model.requests[1])
        assert "unique procedure body" in system(model.requests[2])
        attempts = result.memory_telemetry["input_attempts"]
        classification = next(
            a for a in attempts if a["purpose"] == "message_classifier"
        )
        assert classification["skills"] == []
        assert classification["working_history_groups"] == []
        assert classification["references"] == []


def test_ce06_real_local_write_then_failure_never_repeats(tmp_path):
    from agent_alfred.messages import ToolCallBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelRef,
        ModelResponse,
        ModelResult,
        Usage,
    )

    call = ToolCallBlock("save-one", "save_fact", {"subject": "用户", "fact": "喜欢茶"})
    response = ModelResult(
        (AttemptRecord("tool-attempt", False, "committed", Usage()),),
        ModelResponse((call,), "tool_use", ModelRef("test", "m")),
        None,
    )
    with runtime(
        tmp_path,
        [SKIP, "full", response, RuntimeError("fail after save"), "must not repeat"],
    ) as (host, model, _):
        enable(host)
        _, result = submit(host, "请保存我喜欢茶")
        assert result.outcome == "failed"
        routing = result.memory_telemetry["routing"]
        assert routing["fallback"]["reason"] == "side_effect_occurred"
        assert routing["fallback"]["entered"] is False
        assert len(model.requests) == 4


def test_ce07_recording_pending_rejects_behaviour_change(tmp_path):
    import threading

    from agent_alfred.runtime.host import SubmitRequest

    gate, pending = threading.Event(), threading.Event()

    def observed(snapshot):
        if snapshot.coordinator_state == "recording_pending":
            pending.set()

    with runtime(
        tmp_path,
        [SKIP, "greeting"],
        before_recording_commit=gate,
        snapshot_listener=observed,
    ) as (host, model, _):
        enable(host)
        accepted = host.submit(SubmitRequest("你好"))
        try:
            assert pending.wait(5)
            assert DashboardApi(facade=host).mutate_behaviour(
                dict(action="save", enabled=False, expected_revision=1)
            ) == (409, {"code": "mutation_in_flight"})
        finally:
            gate.set()
        result = host.wait(accepted.run_id)
        assert result.memory_telemetry["routing"]["settings"]["enabled"] is True


@pytest.mark.parametrize("overall", [False, True])
def test_ce10_classifier_local_deadline_and_overall_are_distinct(tmp_path, overall):
    from agent_alfred.clock import FakeClock
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.settings import Settings

    clock = FakeClock()

    class TimedModel(ScriptedModel):
        def respond(self, request, *, events=None, deadline=None):
            result = super().respond(request, events=events, deadline=deadline)
            if "Classify only" in str(request.system):
                clock.monotonic_value += 6
            return result

    model = TimedModel([SKIP, "full", "fallback"])
    settings = Settings(overall_deadline_s=4 if overall else 30)
    with runtime(
        tmp_path,
        [],
        clock=clock,
        settings=settings,
        factory=ScriptedModelFactory(model),
    ) as (host, _, sink):
        enable(host)
        _, result = submit(host, "task")
        if overall:
            assert result.outcome == "failed"
            assert result.error == "overall_deadline"
            assert len(model.requests) == 2
        else:
            assert result.outcome == "completed"
            assert len(model.requests) == 3
            assert (
                result.memory_telemetry["routing"]["classification"]["status"]
                == "failed"
            )


def test_ce06_external_unknown_keeps_fixed_receipt_and_never_retries(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.tools import Tool, ToolFailure, ToolPolicy
    from agent_alfred.tools.calendar import object_schema

    sent = []

    def transport(args, context):
        sent.append(context.call_id)
        return ToolFailure(
            "execution_error",
            (TextBlock("unconfirmed"),),
            stop_reason="tool_result_unverified",
        )

    tool = Tool(
        "external_test",
        "controlled external IO",
        object_schema({}, ()),
        transport,
        "external",
    )
    with runtime(
        tmp_path,
        [
            SKIP,
            "full",
            calls(ToolCallBlock("one", "external_test", {})),
            "must not run",
        ],
        extra_tools=(tool,),
        tool_policies={
            "external_test": ToolPolicy(configured=True, authorization="allowed")
        },
    ) as (host, model, _):
        enable(host)
        catalog = host.tools_catalog()
        identity = next(
            t["identity"] for t in catalog["tools"] if t["name"] == tool.name
        )
        assert "error" not in host.save_tool_authorization(
            identity, "allowed", catalog["revision"]
        )
        _, result = submit(host, "do external action")
        assert sent == ["one"]
        assert result.error == "tool_result_unverified"
        assert len(model.requests) == 3
        assert result.memory_telemetry["routing"]["side_effect_state"] == "unknown"
        assert result.memory_telemetry["routing"]["fallback"]["entered"] is False


def test_ce14_startup_without_valid_graph_is_visibly_unavailable(tmp_path):
    def broken(tools):
        raise ValueError("static routing graph unavailable")

    with runtime(tmp_path, [SKIP, "ordinary"], routing_graph_builder=broken) as (
        host,
        model,
        sink,
    ):
        enable(host)
        _, result = submit(host, "task")
        assert result.outcome == "completed"
        assert "graph" not in result.memory_telemetry
        assert (
            result.memory_telemetry["routing"]["fallback"]["reason"]
            == "routing_unavailable"
        )
        assert any(
            getattr(e.payload, "code", None) == "routing_unavailable"
            for e in sink.events
        )


def test_ce10_zero_budget_does_not_prepare_or_invent_graph(tmp_path):
    from agent_alfred.settings import Settings

    with runtime(tmp_path, [], settings=Settings(max_steps=0)) as (host, model, sink):
        enable(host)
        _, result = submit(host, "task")
        assert result.outcome == "max_steps"
        assert model.requests == []
        assert "graph" not in result.memory_telemetry


@pytest.mark.parametrize("category", ["no_reply", "full", "unknown"])
def test_ce03_save_then_no_reply_is_not_swallowed(tmp_path, category):
    import sqlite3

    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock

    script = [
        SKIP,
        category,
        calls(
            ToolCallBlock(
                "save",
                "save_fact",
                {
                    "subject": "用户",
                    "fact": "address fixture",
                },
            )
        ),
        "saved with evidence",
    ]
    with runtime(tmp_path, script) as (host, model, _):
        enable(host)
        accepted, result = submit(host, "请保存我的地址，保存后不要回复")
        assert result.outcome == "completed"
        assert result.memory_telemetry["routing"]["route"] in ("full", "fallback")
        assert host.tool_requests(accepted.run_id)[0]["result"] == "succeeded"
    with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM facts WHERE fact = 'address fixture'"
            ).fetchone()[0]
            == 1
        )


def test_ce07_explicit_unavailable_classifier_never_switches_to_primary(tmp_path):
    from agent_alfred.model import EndpointUnconfigured, ModelAssignment, ScriptedModel
    from agent_alfred.runtime.config import MutableAssignmentProvider

    answer = ScriptedModel(["answer"])

    class Factory:
        def create(self, snapshot):
            if snapshot.endpoint_id == "missing":
                raise EndpointUnconfigured("unavailable")
            return answer

    provider = MutableAssignmentProvider(
        endpoint_id="primary",
        model_id="answer",
        wire_style="openai",
        api_key="key",
        retrieval_gate=ModelAssignment("missing", "classifier", "openai"),
    )
    with runtime(tmp_path, [], factory=Factory(), snapshot_provider=provider) as (
        host,
        _,
        _,
    ):
        enable(host)
        _, result = submit(host, "task")
        classification = result.memory_telemetry["routing"]["classification"]
        assert classification["status"] == "failed"
        assert classification["planned_model"]["endpoint_id"] == "missing"
        assert "actual_model" not in classification
        assert len(answer.requests) == 1
        assert result.memory_telemetry["routing"]["fallback"]["model_requests"] == 1


def test_ce10_full_classifier_input_limit_has_no_classifier_request(tmp_path):
    from agent_alfred.runtime.memory import GATE_SYSTEM
    from agent_alfred.runtime.routing_classifier import CLASSIFIER_SYSTEM
    from agent_alfred.settings import Settings

    assert len(CLASSIFIER_SYSTEM) > len(GATE_SYSTEM) + 80
    with runtime(
        tmp_path,
        [SKIP, "answer"],
        settings=Settings(
            gate_input_character_limit=len(GATE_SYSTEM) + 250,
        ),
    ) as (host, model, _):
        enable(host)
        _, result = submit(host, "task")
        assert result.outcome == "completed"
        assert len(model.requests) == 2
        preparation = result.memory_telemetry["classification_input_preparation"]
        assert preparation["status"] == "failed"
        assert result.memory_telemetry["routing"]["fallback"]["model_requests"] == 1


def test_ce10_routing_notice_real_trace_write_failure_keeps_business_result(tmp_path):
    import sqlite3

    from agent_alfred.clock import FakeClock
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.trace import RunBundleTraceSink

    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="skills-test",
    )
    original = trace._write_item
    failures = []

    def write(item):
        if getattr(item.event.payload, "code", None) == "routing_fallback":
            failures.append(True)
            raise OSError("notice write failure")
        original(item)

    trace._write_item = write
    with runtime(
        tmp_path, [SKIP, RuntimeError("offline"), "answer"], extra_sinks=(trace,)
    ) as (host, model, _):
        enable(host)
        accepted, result = submit(host, "task")
        assert result.outcome == "completed"
        assert failures == [True]
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            row = conn.execute(
                "SELECT telemetry FROM runs WHERE run_id=?", (accepted.run_id,)
            ).fetchone()
        import json

        assert json.loads(row[0])["trace_incomplete"] is True
        assert "routing" in row[0]
        assert len(model.requests) == 3


@pytest.mark.parametrize("phase", ["classifier", "agent"])
def test_ce06_10_cancellation_is_never_a_recoverable_failure(tmp_path, phase):
    script = [SKIP]
    if phase == "agent":
        script.append("full")
    script.extend([KeyboardInterrupt(), "must not run"])
    with runtime(tmp_path, script) as (host, model, _):
        enable(host)
        _, result = submit(host, "task")
        assert result.outcome == "interrupted"
        assert len(model.requests) == (2 if phase == "classifier" else 3)
        assert result.memory_telemetry["routing"]["fallback"]["reason"] == "cancelled"
        assert result.memory_telemetry["routing"]["fallback"]["entered"] is False


@pytest.mark.parametrize("withheld", [False, True])
def test_f04_routing_reply_endpoint_distinguishes_withheld_projection(
    tmp_path, withheld
):
    import threading

    from agent_alfred.redact import Redactor
    from agent_alfred.runtime.host import SubmitRequest

    pending, release = threading.Event(), threading.Event()

    class ReplyRedactor(Redactor):
        def redact_text(self, value):
            if withheld and value == "fixture answer":
                raise ValueError("controlled redaction boundary failure")
            return super().redact_text(value)

    def observed(snapshot):
        if snapshot.coordinator_state == "recording_pending":
            pending.set()

    with runtime(
        tmp_path,
        [SKIP, "full", "fixture answer"],
        redactor=ReplyRedactor(()),
        before_recording_commit=release,
        snapshot_listener=observed,
    ) as (host, _, _):
        enable(host)
        accepted = host.submit(SubmitRequest("task"))
        try:
            assert pending.wait(5)
            code, payload = DashboardApi(facade=host).recover_reply(
                dict(
                    process_instance_id="skills-test",
                    session_id=accepted.session_id,
                    run_id=accepted.run_id,
                )
            )
            assert code == (503 if withheld else 200)
            assert payload["reply_disposition"] == (
                "reply_withheld" if withheld else "reply"
            )
        finally:
            release.set()
        host.wait(accepted.run_id)
