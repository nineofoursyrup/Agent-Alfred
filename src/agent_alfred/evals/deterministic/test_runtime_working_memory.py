"""Complete conversation selection through admitted and recorded Runs."""

import pytest

from agent_alfred.evals.deterministic.test_runtime_memory_gate import SKIP, runtime
from agent_alfred.messages import message_plain_text
from agent_alfred.runtime.host import SubmitRequest
from agent_alfred.settings import Settings


def test_forgetting_excludes_used_history_and_its_successor_but_allows_new_input(
    tmp_path,
):
    import json

    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin
    from agent_alfred.messages import ToolCallBlock

    save = ToolCallBlock(
        "remember",
        "save_fact",
        {
            "subject": "private-subject",
            "fact": "private-forgotten-body",
        },
    )
    from agent_alfred.clock import FakeClock
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.trace import RunBundleTraceSink

    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="memory-test",
    )
    with runtime(
        [SKIP, calls(save), "saved", SKIP, "successor", SKIP, "fresh"],
        extra_sinks=(trace,),
    ) as (
        host,
        model,
        _,
    ):
        first = host.submit(SubmitRequest("remember my fact"))
        assert host.wait(first.run_id).outcome == "completed"
        receipt = json.loads(model.requests[-1].messages[-1].blocks[0].content[0].text)
        second = host.submit(SubmitRequest("continue", session_id=first.session_id))
        assert host.wait(second.run_id).outcome == "completed"
        deleted = host.memory_service.execute(
            {
                "operation_id": "forget-test",
                "kind": "semantic",
                "action": "delete",
                "payload": {"id": receipt["memory_id"]},
                "expected_version": 1,
            },
            CommandContext(origin=ManualOrigin("web"), source="web"),
        )
        assert "error" not in deleted
        third = host.submit(SubmitRequest("new question", session_id=first.session_id))
        result = host.wait(third.run_id)
        assert result.outcome == "completed"
        assert (
            result.memory_telemetry["input_attempts"][-1]["working_history_groups"]
            == []
        )
        assert all(
            "private-forgotten-body" not in message_plain_text(message)
            for request in model.requests[-2:]
            for message in request.messages
        )


def test_restart_retains_complete_window_and_other_sessions_do_not_contribute(tmp_path):
    import sqlite3

    from agent_alfred import schema
    from agent_alfred.evals.deterministic.test_runtime import _host
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory

    path = tmp_path / "session.sqlite"
    conn = sqlite3.connect(path, check_same_thread=False)
    schema.migrate(conn)
    host, _, _ = _host(conn=conn)
    host.start()
    first = host.submit(SubmitRequest("original"))
    host.wait(first.run_id)
    host.close()
    conn.close()
    conn = sqlite3.connect(path, check_same_thread=False)
    model = ScriptedModel([SKIP, "other", SKIP, "restored"])
    host, _, _ = _host(conn=conn, factory=ScriptedModelFactory(model))
    host.start()
    try:
        host.wait(host.submit(SubmitRequest("unrelated")).run_id)
        next_run = host.submit(SubmitRequest("continue", session_id=first.session_id))
        assert host.wait(next_run.run_id).outcome == "completed"
        assert [message_plain_text(m) for m in model.requests[-1].messages] == [
            "original",
            "pong",
            "continue",
        ]
    finally:
        host.close()
        conn.close()


def test_full_retrieval_budgets_include_second_json_escaping():
    import json

    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    retrieve = (
        '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}'
    )
    context = CommandContext(origin=ManualOrigin("web"), source="web")
    with runtime([retrieve, "answer"]) as (host, model, _):
        for kind in ("semantic", "episodic"):
            field = "fact" if kind == "semantic" else "summary"
            payload = {field: "coriander"}
            if kind == "semantic":
                payload["subject"] = "herb"
            else:
                payload.update(
                    occurred_at="2026-09-09T12:00:00+00:00", occurred_until=None
                )
            saved = host.memory_service.execute(
                {
                    "operation_id": "seed-" + kind,
                    "kind": kind,
                    "action": "save",
                    "payload": payload,
                },
                context,
            )
            assert saved["status"] == "saved"
            # Fixed public reference fields: build a fixture exactly at 4000,
            # with quotes whose second encoding must consume extra capacity.
            item = {
                "id": saved["memory_id"],
                "version": 2,
                **payload,
                "origin": {"type": "manual", "source": "web"},
            }
            remaining = 4000 - len(
                json.dumps([item], ensure_ascii=False, separators=(",", ":"))
            )
            body = "coriander" + '"' * (remaining // 2) + "a" * (remaining % 2)
            changed = host.memory_service.execute(
                {
                    "operation_id": "fill-" + kind,
                    "kind": kind,
                    "action": "update",
                    "expected_version": 1,
                    "payload": {"id": saved["memory_id"], field: body},
                },
                context,
            )
            assert "error" not in changed
        result = host.wait(host.submit(SubmitRequest("coriander")).run_id)
        assert result.outcome == "completed"
        reference = message_plain_text(model.requests[-1].messages[0])
        for line in reference.splitlines():
            if line.startswith(("semantic=", "episodic=")):
                assert len(line.split("=", 1)[1]) == 4000
        actual = result.memory_telemetry["input_attempts"][-1]
        assert actual["input_characters"] > len(reference) + 3000
        assert actual["input_characters"] <= actual["input_limit"]
        assert len(actual["references"]) == 2


@pytest.mark.parametrize("mode", ["retry", "stream_fallback"])
def test_real_retry_and_stream_fallback_keep_each_actual_input_identity(tmp_path, mode):
    import json
    from types import SimpleNamespace

    import httpx2
    from openai import OpenAI

    from agent_alfred.clock import FakeClock
    from agent_alfred.model import ModelRef, ScriptedModel
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.retry import RetryPolicy
    from agent_alfred.stream_fallback import StreamFallback

    clock = FakeClock()
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if mode == "retry" and len(sent) == 1:
            return httpx2.Response(500, json={"error": {"message": "fixture"}})
        if payload.get("stream"):
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: [DONE]\n\n",
            )
        return httpx2.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": SKIP if len(sent) <= 2 else "answer",
                        },
                    }
                ],
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        adapter = OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m"))
        streaming = OpenAICompatibleAdapter(
            client=sdk,
            model=ModelRef("test", "m"),
            stream=True,
        )
        client = StreamFallback(
            streaming if mode == "stream_fallback" else adapter,
            nonstream=adapter,
            clock=clock,
            stream=mode == "stream_fallback",
        )
        if mode == "retry":
            client = RetryPolicy(
                client, clock=clock, sleeper=SimpleNamespace(sleep=lambda _: None)
            )

        class Factory:
            count = 0

            def create(self, snapshot):
                self.count += 1
                return ScriptedModel([SKIP, "first"]) if self.count == 1 else client

        with runtime([], factory=Factory(), clock=clock) as (host, _, _):
            first = host.submit(SubmitRequest("first"))
            host.wait(first.run_id)
            second = host.submit(SubmitRequest("next", session_id=first.session_id))
            result = host.wait(second.run_id)
            assert result.outcome == "completed"
            inputs = host.read_run_evidence(second.run_id, trace_root=tmp_path)[
                "memory"
            ]["input_attempts"]
            assert len(inputs) == len(sent) == (3 if mode == "retry" else 4)
            assert len({entry["attempt_id"] for entry in inputs}) == len(inputs)
            assert all(
                entry["working_history_groups"] == [first.run_id] for entry in inputs
            )
            assert [entry["attempt_id"] for entry in inputs] == [
                attempt.attempt_id
                for response in result.model_results
                for attempt in response.attempts
            ]


def test_input_registration_deadline_prevents_send_and_rolls_back_evidence(tmp_path):
    import httpx2
    from openai import OpenAI

    from agent_alfred.clock import FakeClock
    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.stream_fallback import StreamFallback

    clock = FakeClock()
    sent = []
    notifications = []

    def handle(request):
        sent.append(clock.monotonic())
        return httpx2.Response(500)

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        client = StreamFallback(
            OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m")),
            clock=clock,
        )

        class Factory:
            def create(self, snapshot):
                return client

        with runtime(
            [],
            factory=Factory(),
            clock=clock,
            settings=Settings(overall_deadline_s=5),
            memory_notifier=notifications.append,
        ) as (host, _, _):
            original = host.memory_service.forgetting.register_read

            def slow_registration(*args, **kwargs):
                result = original(*args, **kwargs)
                clock.monotonic_value += 6
                return result

            host.memory_service.forgetting.register_read = slow_registration
            submitted = host.submit(SubmitRequest("hello"))
            result = host.wait(submitted.run_id)
            assert result.outcome == "failed"
            assert sent == []
            assert len(notifications) == 1  # Only the committed group registration.
            evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
            assert evidence["memory"]["input_attempts"] == []


def test_failed_input_registration_prevents_real_sdk_transport():
    import httpx2
    from openai import OpenAI

    from agent_alfred.evals.deterministic.test_runtime import _host
    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter

    sent = []

    def handle(request):
        sent.append(request)
        return httpx2.Response(500)

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)

        class Factory:
            def create(self, snapshot):
                return OpenAICompatibleAdapter(
                    client=sdk,
                    model=ModelRef(snapshot.endpoint_id, snapshot.model_id),
                )

        host, conn, _ = _host(factory=Factory())
        conn.execute("""CREATE TRIGGER reject_input BEFORE INSERT
            ON run_input_explanations WHEN NEW.kind='attempt'
            BEGIN SELECT RAISE(FAIL,'fixture input failure'); END""")
        conn.commit()
        host.start()
        try:
            result = host.wait(host.submit(SubmitRequest("hello")).run_id)
            assert result.outcome == "failed"
            assert sent == []
            assert result.memory_telemetry["input_attempts"] == []
        finally:
            host.close()


def test_unknown_ledger_prefix_has_complete_entries_and_honest_omission_counts():
    import json

    from agent_alfred.evals.deterministic.test_runtime import _host
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory

    model = ScriptedModel([SKIP, "first", SKIP, "next"])
    host, conn, _ = _host(
        factory=ScriptedModelFactory(model),
        settings=Settings(working_memory_rounds=0),
    )
    host.start()
    try:
        first = host.submit(SubmitRequest("hello"))
        host.wait(first.run_id)
        # Imported ledger fixture, on the same real SQLite/RuntimeHost boundary.
        for number in range(25):
            conn.execute(
                "INSERT INTO tool_ledger "
                "(tool_name,fingerprint,effect,status,run_id,session_id,created_at) "
                "VALUES ('web_search',?,'external',?,?,?,'2026-09-10T00:00:00Z')",
                (
                    str(number),
                    "unknown" if number < 23 else "succeeded",
                    first.run_id,
                    first.session_id,
                ),
            )
        conn.commit()
        second = host.submit(SubmitRequest("hello", session_id=first.session_id))
        result = host.wait(second.run_id)
        assert result.outcome == "completed"
        entry = result.memory_telemetry["input_attempts"][-1]
        selected = entry["ledger_entries"]
        assert 0 < len(selected) <= 20
        assert [item["ledger_id"] for item in selected] == list(
            range(23, 23 - len(selected), -1)
        )
        assert entry["ledger_omitted"] == 25 - len(selected)
        assert entry["ledger_unknown_omitted"] == 23 - len(selected)
        summary = message_plain_text(model.requests[-1].messages[0])
        assert len(summary) <= 4000
        assert json.loads(summary.split("\n", 2)[-1]) == selected
    finally:
        host.close()


def test_actual_input_evidence_survives_a_failed_final_recording(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime import _host
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory

    host, conn, _ = _host(factory=ScriptedModelFactory(ScriptedModel([SKIP, "ok"])))
    conn.execute("""CREATE TRIGGER reject_final BEFORE UPDATE OF phase ON runs
        WHEN NEW.phase='finished' BEGIN SELECT RAISE(FAIL,'fixture recording'); END""")
    conn.commit()
    host.start()
    try:
        submitted = host.submit(SubmitRequest("hello"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "completed"
        evidence = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)
        assert [entry["purpose"] for entry in evidence["memory"]["input_attempts"]] == [
            "gate",
            "answer",
        ]
    finally:
        host.close()


def test_common_window_is_trimmed_before_gate_and_not_refilled_after_skip():
    with runtime(
        [SKIP, "first answer", SKIP, "next answer"],
        settings=Settings(input_character_limit=64000),
    ) as (host, model, _):
        first = host.submit(SubmitRequest("h" * 7000))
        assert host.wait(first.run_id).outcome == "completed"
        second = host.submit(SubmitRequest("n" * 7000, session_id=first.session_id))
        result = host.wait(second.run_id)
        assert result.outcome == "completed"
        for request in model.requests[-2:]:
            assert [message_plain_text(m) for m in request.messages] == ["n" * 7000]
        assert (
            result.memory_telemetry["input_preparation"]["budget_omitted_groups"] == 1
        )


def test_input_serialization_has_a_fixed_whitelist_and_preserves_unicode():
    from agent_alfred.messages import text_message
    from agent_alfred.model import ModelRef, ModelRequest
    from agent_alfred.runtime.input_budget import serialize_input

    request = ModelRequest(
        model=ModelRef("not-input", "not-input"),
        system=None,
        messages=(text_message("user", "中\n e\u0301"),),
        max_tokens=123,
        conversation_id="private-conversation",
    )
    assert serialize_input(request) == (
        '{"messages":[{"blocks":[{"text":"中\\n é","type":"text"}],'
        '"role":"user"}],"system":[],"tool_choice":"auto","tools":[], '
        '"version":"request-input-v1"}'
    ).replace('[], "version"', '[],"version"')


def test_gate_override_can_fail_preparation_before_any_model_call():
    from agent_alfred.settings import load_settings

    settings = load_settings(
        {
            "AGENT_ALFRED_INPUT_CHARACTER_LIMIT": "70000",
            "AGENT_ALFRED_GATE_INPUT_CHARACTER_LIMIT": "100",
        }
    )
    with runtime([SKIP, "unused"], settings=settings) as (host, model, _):
        result = host.wait(host.submit(SubmitRequest("hello")).run_id)
        assert result.outcome == "failed"
        assert result.error == "input_limit_exceeded"
        assert model.requests == []
        assert result.memory_telemetry["input_preparation"]["gate_limit"] == 100


def test_zero_window_keeps_real_prior_tool_evidence_out_of_the_gate():
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock

    action = ToolCallBlock(
        "create",
        "create_event",
        {
            "title": "private appointment",
            "starts_at": "2026-09-10T12:00:00+08:00",
        },
    )
    with runtime(
        [SKIP, calls(action), "created", SKIP, "next"],
        settings=Settings(working_memory_rounds=0),
    ) as (host, model, _):
        first = host.submit(SubmitRequest("create appointment"))
        assert host.wait(first.run_id).outcome == "completed"
        second = host.submit(SubmitRequest("hello", session_id=first.session_id))
        result = host.wait(second.run_id)
        assert result.outcome == "completed"
        assert [message_plain_text(m) for m in model.requests[-2].messages] == ["hello"]
        summary = message_plain_text(model.requests[-1].messages[0])
        assert "create_event" in summary
        assert "succeeded" in summary
        assert "private appointment" not in summary
        assert result.memory_telemetry["input_attempts"][-1]["ledger_entries"]


def test_tool_step_growth_stops_without_repeating_the_action():
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import TextBlock, ToolCallBlock

    action = ToolCallBlock(
        "create",
        "create_event",
        {
            "title": "one appointment",
            "starts_at": "2026-09-10T12:00:00+08:00",
        },
    )
    with runtime([SKIP, calls(action, TextBlock("x" * 64001)), "unused"]) as (
        host,
        model,
        _,
    ):
        submitted = host.submit(SubmitRequest("create appointment"))
        result = host.wait(submitted.run_id)
        assert result.outcome == "failed"
        assert result.error == "input_limit_exceeded"
        assert len(model.requests) == 2
        assert result.memory_telemetry["input_attempts"][-1]["purpose"] == "answer"


def test_required_input_overflow_stops_before_gate_without_inventing_attempts():
    with runtime([SKIP, "unused"]) as (host, model, _):
        submitted = host.submit(SubmitRequest("x" * 64001))
        result = host.wait(submitted.run_id)
        assert result.outcome == "failed"
        assert result.error == "input_limit_exceeded"
        assert model.requests == []
        assert result.memory_telemetry["input_attempts"] == []
        assert result.memory_telemetry["input_preparation"]["status"] == "failed"


def test_unknown_source_is_excluded_before_the_complete_run_limit():
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    with runtime(
        [SKIP, "safe answer", SKIP, "unsafe answer", SKIP, "last answer"],
        settings=Settings(working_memory_rounds=1),
    ) as (host, model, _):
        first = host.submit(SubmitRequest("safe question"))
        host.wait(first.run_id)
        second = host.submit(
            SubmitRequest("unsafe question", session_id=first.session_id)
        )
        host.wait(second.run_id)
        registered = host.memory_service.forgetting.register_group(
            second.run_id,
            kind="run",
            container_id=first.session_id,
            evidence="unknown",
            context=CommandContext(origin=ManualOrigin("web"), source="web"),
        )
        assert "error" not in registered
        last = host.submit(SubmitRequest("last question", session_id=first.session_id))
        result = host.wait(last.run_id)
        assert result.outcome == "completed"
        assert [message_plain_text(m) for m in model.requests[-1].messages] == [
            "safe question",
            "safe answer",
            "last question",
        ]


def test_incomplete_run_does_not_displace_or_split_the_last_complete_run():
    with runtime(
        [SKIP, "first answer", KeyboardInterrupt(), SKIP, "last answer"],
        settings=Settings(working_memory_rounds=1),
    ) as (host, model, _):
        first = host.submit(SubmitRequest("first question"))
        assert host.wait(first.run_id).outcome == "completed"
        interrupted = host.submit(
            SubmitRequest("unfinished question", session_id=first.session_id)
        )
        assert host.wait(interrupted.run_id).outcome == "interrupted"
        last = host.submit(SubmitRequest("last question", session_id=first.session_id))
        result = host.wait(last.run_id)
        assert result.outcome == "completed"
        for request in model.requests[-2:]:
            assert [message_plain_text(m) for m in request.messages] == [
                "first question",
                "first answer",
                "last question",
            ]
        assert result.memory_telemetry["input_attempts"][-1][
            "working_history_groups"
        ] == [first.run_id]


def test_actual_input_registration_notifies_committed_memory_revision():
    notifications = []
    with runtime([SKIP, "answer"], memory_notifier=notifications.append) as (
        host,
        _,
        _,
    ):
        submitted = host.submit(SubmitRequest("hello"))
        assert host.wait(submitted.run_id).outcome == "completed"
        # Group registration emits one change, and each gate/answer read must
        # announce its newly committed provenance revision as well.
        assert len(notifications) == 3
        revisions = [item["memory_revision"] for item in notifications]
        assert revisions == sorted(set(revisions))


@pytest.mark.parametrize("notification_effect", ["interrupt", "slow"])
def test_input_notification_cannot_prevent_registered_transport(
    tmp_path, notification_effect
):
    import httpx2
    from openai import OpenAI

    from agent_alfred.clock import FakeClock
    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.stream_fallback import StreamFallback

    clock = FakeClock()
    sent = []
    notifications = []

    def notify(payload):
        notifications.append(payload)
        if len(notifications) == 2:
            if notification_effect == "interrupt":
                raise KeyboardInterrupt("notification fixture")
            clock.monotonic_value += 6

    def handle(request):
        sent.append(clock.monotonic())
        return httpx2.Response(500)

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        client = StreamFallback(
            OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m")),
            clock=clock,
        )

        class Factory:
            def create(self, snapshot):
                return client

        with runtime(
            [],
            factory=Factory(),
            clock=clock,
            settings=Settings(overall_deadline_s=5),
            memory_notifier=notify,
        ) as (host, _, _):
            submitted = host.submit(SubmitRequest("hello"))
            result = host.wait(submitted.run_id)
            assert result.outcome == (
                "interrupted" if notification_effect == "interrupt" else "failed"
            )
            inputs = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)[
                "memory"
            ]["input_attempts"]
            assert len(inputs) == len(sent) == 1
            assert sent == [0.0]
