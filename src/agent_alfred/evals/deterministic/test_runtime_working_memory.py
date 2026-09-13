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
            assert [p["memory_revision"] for p in notifications] == [0]
            notifications.clear()
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
    target = {"active": False}
    input_notifications = []
    group_notifications = []
    group_target = {"active": False}

    def notify(payload):
        notifications.append(payload)
        if group_target["active"]:
            group_notifications.append(payload)
        if target["active"]:
            assert not host._conn.in_transaction
            assert payload["memory_revision"] == host.memory_service.memory_revision
            input_notifications.append(payload)

    with runtime([SKIP, "answer"], memory_notifier=notify) as (host, _, _):
        assert [p["memory_revision"] for p in notifications] == [0]
        register_group = host.memory_service.forgetting.register_group

        def group(*args, **kwargs):
            group_target["active"] = True
            try:
                return register_group(*args, **kwargs)
            finally:
                group_target["active"] = False

        host.memory_service.forgetting.register_group = group
        original = host.memory_service.forgetting.resolve_input_registration

        def resolve(*args, **kwargs):
            target["active"] = True
            try:
                return original(*args, **kwargs)
            finally:
                target["active"] = False

        host.memory_service.forgetting.resolve_input_registration = resolve
        submitted = host.submit(SubmitRequest("hello"))
        assert host.wait(submitted.run_id).outcome == "completed"
        # Each gate/answer registration announces its committed revision;
        # startup and saved-chat maintenance have separate identities.
        assert len(group_notifications) == 1
        assert len(input_notifications) == 2
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
    target = {"active": False, "effects": 0}

    def notify(payload):
        notifications.append(payload)
        if target["active"]:
            target["effects"] += 1
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
            assert [p["memory_revision"] for p in notifications] == [0]
            original = host.memory_service.forgetting.resolve_input_registration

            def resolve(*args, **kwargs):
                target["active"] = True
                try:
                    return original(*args, **kwargs)
                finally:
                    target["active"] = False

            host.memory_service.forgetting.resolve_input_registration = resolve
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
            assert target["effects"] == 1


@pytest.mark.parametrize("delay_at", ["registration", "commit"])
@pytest.mark.parametrize(
    "gate_s,attempt_s,registration_s,purpose",
    [(5, 60, 6, "gate"), (20, 2, 3, "gate"), (20, 2, 3, "answer")],
)
def test_effective_input_deadline_rolls_back_before_transport(
    tmp_path, gate_s, attempt_s, registration_s, purpose, delay_at
):
    import httpx2
    from openai import OpenAI

    from agent_alfred.clock import FakeClock
    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.stream_fallback import StreamFallback

    clock = FakeClock()
    sent = []

    def handle(request):
        sent.append(clock.monotonic())
        return httpx2.Response(500)

    with httpx2.Client(transport=httpx2.MockTransport(handle)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        client = StreamFallback(
            OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m")),
            clock=clock,
            per_attempt_timeout_s=attempt_s,
        )

        class Factory:
            def create(self, snapshot):
                return client

        with runtime(
            [],
            factory=Factory(),
            clock=clock,
            settings=Settings(
                overall_deadline_s=30,
                gate_model_budget_s=gate_s,
                per_attempt_timeout_s=attempt_s,
                max_steps=2,
            ),
        ) as (host, _, _):
            original = host.memory_service.forgetting.register_read

            def slow_registration(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs["purpose"] == purpose:
                    if delay_at == "registration":
                        clock.monotonic_value += registration_s
                    else:
                        conn = kwargs["transaction"]

                        def delay_commit(sql):
                            if sql.upper() == "COMMIT":
                                conn.set_trace_callback(None)
                                clock.monotonic_value += registration_s

                        conn.set_trace_callback(delay_commit)
                return result

            host.memory_service.forgetting.register_read = slow_registration
            submitted = host.submit(SubmitRequest("hello"))
            result = host.wait(submitted.run_id)
            inputs = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)[
                "memory"
            ]["input_attempts"]
            if purpose == "gate":
                # CE-16 reserves a remaining answer Step after the unsent gate.
                assert sent == [registration_s]
                assert [item["purpose"] for item in inputs] == ["answer"]
                assert (
                    result.memory_telemetry["gate"]["fallback_reason"]
                    == "model_deadline"
                )
                assert result.outcome == "failed"
            else:
                assert sent == [0.0]
                assert [item["purpose"] for item in inputs] == ["gate"]
                assert result.outcome == "failed"
                assert result.error == "input_deadline_exceeded"


@pytest.mark.parametrize("receipt", [False, True])
@pytest.mark.parametrize("sent", [False, True])
def test_failed_input_resolution_stays_unknown_and_recovers_from_durable_receipt(
    tmp_path, sent, receipt
):
    import sqlite3

    import httpx2
    from openai import OpenAI

    from agent_alfred.clock import FakeClock
    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.stream_fallback import StreamFallback

    path = tmp_path / "input-recovery.sqlite"
    clock = FakeClock()
    requests = []

    def dispatch(request):
        requests.append(clock.monotonic())
        return httpx2.Response(500)

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        client = StreamFallback(
            OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m")),
            clock=clock,
        )

        class Factory:
            def create(self, snapshot):
                return client

        with runtime([], database=path, factory=Factory(), clock=clock) as (host, _, _):
            original = host.memory_service.forgetting.register_read

            def failing_resolution(*args, **kwargs):
                value = original(*args, **kwargs)
                conn = kwargs["transaction"]

                def authorize(action, table, *rest):
                    if (
                        action == sqlite3.SQLITE_UPDATE
                        and table == "run_input_explanations"
                    ):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                def delayed_commit(sql):
                    if sql.upper() == "COMMIT":
                        conn.set_trace_callback(None)
                        if not sent:
                            clock.monotonic_value += 6

                if not receipt:
                    conn.execute("""CREATE TRIGGER fail_run_receipt
                        BEFORE UPDATE OF telemetry ON runs
                        WHEN NEW.telemetry IS NOT NULL
                        BEGIN SELECT RAISE(FAIL, 'fixture receipt unavailable'); END""")
                conn.set_authorizer(authorize)
                conn.set_trace_callback(delayed_commit)
                return value

            host.memory_service.forgetting.register_read = failing_resolution
            submitted = host.submit(SubmitRequest("hello"))
            result = host.wait(submitted.run_id)
            assert result.outcome == "failed"
            assert result.error == "input_resolution_unavailable"
            memory = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)[
                "memory"
            ]
            assert memory["input_attempts"] == []
            assert len(memory["input_unconfirmed"]) == 1
            assert len(requests) == int(sent)
            assert host.memory_service.forgetting.evaluate_history(
                [submitted.run_id], purpose="working_window"
            )["denied"] == [submitted.run_id]

    if not receipt:
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TRIGGER fail_run_receipt")
    with runtime([], database=path) as (host, _, _):
        memory = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)["memory"]
        if receipt:
            assert memory["input_unconfirmed"] == []
            assert len(memory["input_attempts"]) == int(sent)
        else:
            assert len(memory["input_unconfirmed"]) == 1
            assert memory["input_attempts"] == []
        evaluated = host.memory_service.forgetting.evaluate_history(
            [submitted.run_id], purpose="working_window"
        )
        assert evaluated["allowed" if receipt else "denied"] == [submitted.run_id]


@pytest.mark.parametrize("source_kind", ["history", "memory"])
@pytest.mark.parametrize("earlier_sent", [False, True])
def test_unsent_input_never_joins_confirmed_forgetting_closure(
    source_kind, earlier_sent
):
    import sqlite3

    from agent_alfred.evals.deterministic.test_forgetting import (
        CONTEXT,
        delete,
        save,
        service,
    )

    with sqlite3.connect(":memory:") as conn:
        memory = service(conn)
        forgetting = memory.forgetting
        for group in ("source", "consumer"):
            forgetting.register_group(
                group,
                kind="run",
                container_id="session",
                evidence="complete",
                evidence_id="fixture",
                context=CONTEXT,
            )
        record = save(memory, groups=("source",))
        refs = (
            {"sources": ("source",)}
            if source_kind == "history"
            else {"memories": (("semantic", record["memory_id"], 1),)}
        )
        if earlier_sent:
            assert "error" not in forgetting.register_read(
                "consumer",
                **refs,
                attempt_id="actual",
                purpose="answer",
                context=CONTEXT,
            )
        assert "error" not in forgetting.register_read(
            "consumer",
            **refs,
            attempt_id="unsent",
            purpose="answer",
            context=CONTEXT,
            input_explanation={
                "attempt_id": "unsent",
                "purpose": "answer",
                "step_index": 1,
            },
            provisional=True,
        )
        conn.execute("""CREATE TRIGGER reject_resolution
            BEFORE UPDATE ON run_input_explanations
            BEGIN SELECT RAISE(FAIL, 'fixture resolution failure'); END""")
        conn.commit()
        assert "error" in forgetting.resolve_input_registration(
            "consumer",
            "unsent",
            sent=False,
            context=CONTEXT,
        )
        assert delete(memory, record)["status"] == "deleted"
        assert forgetting.evaluate_history(["consumer"], purpose="working_window")[
            "denied"
        ] == ["consumer"]
        conn.execute("DROP TRIGGER reject_resolution")
        conn.commit()
        assert (
            forgetting.resolve_input_registration(
                "consumer",
                "unsent",
                sent=False,
                context=CONTEXT,
            )["status"]
            == "not_sent"
        )
        result = forgetting.evaluate_history(["consumer"], purpose="working_window")
        assert result["denied" if earlier_sent else "allowed"] == ["consumer"]


@pytest.mark.parametrize("purpose", ["gate", "answer"])
def test_input_resolution_failure_preserves_transport_control_and_error_chain(
    tmp_path, purpose
):
    import sqlite3

    import httpx2
    from openai import OpenAI

    from agent_alfred.clock import FakeClock
    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.runtime.memory import InputResolutionError
    from agent_alfred.stream_fallback import StreamFallback

    interruption = KeyboardInterrupt("transport interrupted")
    sent = []

    def dispatch(request):
        sent.append(request)
        if purpose == "answer" and len(sent) == 1:
            return httpx2.Response(500)
        raise interruption

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        clock = FakeClock()
        client = StreamFallback(
            OpenAICompatibleAdapter(
                client=OpenAI(api_key="fixture", http_client=http, max_retries=0),
                model=ModelRef("test", "m"),
            ),
            clock=clock,
        )

        class Factory:
            def create(self, snapshot):
                return client

        with runtime([], factory=Factory(), clock=clock) as (host, _, _):
            original = host.memory_service.forgetting.register_read

            def register(*args, **kwargs):
                value = original(*args, **kwargs)
                if kwargs["purpose"] == purpose:

                    def authorize(action, table, *rest):
                        if (
                            action == sqlite3.SQLITE_UPDATE
                            and table == "run_input_explanations"
                        ):
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    kwargs["transaction"].set_authorizer(authorize)
                return value

            host.memory_service.forgetting.register_read = register
            submitted = host.submit(SubmitRequest("hello"))
            result = host.wait(submitted.run_id)
            assert result.outcome == "interrupted"
            assert sum(len(r.attempts) for r in result.model_results) == len(sent)
            memory = host.read_run_evidence(submitted.run_id, trace_root=tmp_path)[
                "memory"
            ]
            assert len(memory["input_unconfirmed"]) == 1
            pending, seen, errors = [interruption], set(), []
            while pending:
                error = pending.pop()
                if error is None or id(error) in seen:
                    continue
                seen.add(id(error))
                errors.append(error)
                pending.extend((error.__cause__, error.__context__))
            assert any(isinstance(error, InputResolutionError) for error in errors)


def test_later_input_failure_persists_its_final_exclusions_without_repeating_action(
    tmp_path,
):
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
    with runtime(
        [
            SKIP,
            calls(action),
            "created",
            SKIP,
            calls(action, TextBlock("x" * 64001)),
            "unused",
        ]
    ) as (host, model, capture):
        first = host.submit(SubmitRequest("create appointment"))
        assert host.wait(first.run_id).outcome == "completed"
        second = host.submit(SubmitRequest("create again", session_id=first.session_id))
        result = host.wait(second.run_id)
        assert result.error == "input_limit_exceeded"
        memory = host.read_run_evidence(second.run_id, trace_root=tmp_path)["memory"]
        failure = memory["input_failure"]
        assert failure["budget_omitted_groups"] == 1
        assert failure["history_exclusions"] == {
            "incomplete": 0,
            "unsafe": 0,
            "round_limit": 0,
        }
        assert failure["ledger_omitted"] == 1
        assert failure["ledger_unknown_omitted"] == failure["ledger_excluded"] == 0
        assert failure["working_history_groups"] == failure["ledger_entries"] == []
        assert memory["input_preparation"]["budget_omitted_groups"] == 0
        assert len(memory["input_attempts"]) == 2
        assert len(model.requests) == 5
        assert (
            sum(event.payload.name == "tool.finished" for event in capture.events) == 2
        )


@pytest.mark.parametrize(
    "proof,terminal",
    [
        ("unconfirmed", "committed"),
        ("unconfirmed", "aborted"),
        ("no_ledger", "committed"),
        ("no_ledger", "aborted"),
        ("unsent", "committed"),
        ("no_receipts", "committed"),
        ("no_receipts", "aborted"),
    ],
)
def test_trace_identity_accepts_either_durable_proof_without_confirming_sources(
    tmp_path, proof, terminal
):
    import json
    import sqlite3

    import httpx2
    from openai import OpenAI

    from agent_alfred.clock import FakeClock
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.stream_fallback import StreamFallback
    from agent_alfred.trace import RunBundleTraceSink

    clock = FakeClock()
    root = tmp_path / "trace"
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(root),
        clock=clock,
        process_instance_id="memory-test",
    )
    sent = []
    database = tmp_path / "state.sqlite"
    body = "partial response" if terminal == "aborted" else SKIP

    def dispatch(request):
        sent.append(request)
        if terminal == "aborted":
            chunk = {"choices": [{"delta": {"content": body}, "finish_reason": None}]}
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(chunk)}\n\n".encode(),
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
                        "message": {"role": "assistant", "content": body},
                    }
                ],
            },
        )

    def check_evidence(evidence):
        assert evidence["trace_status"] == "available"
        assert len(sent) == int(proof != "unsent")
        assert len(evidence["attempts"]) == int(proof == "unconfirmed")
        assert len(evidence["memory"]["input_unconfirmed"]) == int(
            proof in ("unconfirmed", "no_receipts")
        )
        assert len(evidence["memory"]["input_attempts"]) == int(proof == "no_ledger")
        terminals = [
            event
            for event in evidence["events"]
            if event["payload"]["name"] in ("attempt.committed", "attempt.aborted")
        ]
        assert len(terminals) == int(proof != "unsent")
        if terminals:
            assert terminals[0]["payload"]["name"] == f"attempt.{terminal}"
            assert terminals[0]["payload"]["blocks"][0]["text"] == body
        else:
            assert all(
                event["envelope"].get("attempt_id") is None
                for event in evidence["events"]
            )

    with httpx2.Client(transport=httpx2.MockTransport(dispatch)) as http:
        sdk = OpenAI(api_key="fixture", http_client=http, max_retries=0)
        client = StreamFallback(
            OpenAICompatibleAdapter(
                client=sdk,
                model=ModelRef("test", "m"),
                stream=terminal == "aborted",
            ),
            clock=clock,
            stream=terminal == "aborted",
            stream_fallback=False,
        )

        class Factory:
            def create(self, snapshot):
                return client

        with runtime(
            [],
            factory=Factory(),
            clock=clock,
            extra_sinks=(trace,),
            settings=Settings(max_steps=1, overall_deadline_s=5),
            database=database,
        ) as (host, _, _):
            original = host.memory_service.forgetting.register_read

            def register(*args, **kwargs):
                value = original(*args, **kwargs)
                conn = kwargs["transaction"]
                if proof in ("unconfirmed", "no_receipts"):
                    if proof == "no_receipts":
                        conn.execute("""CREATE TRIGGER reject_final
                            BEFORE UPDATE OF telemetry ON runs
                            WHEN NEW.telemetry IS NOT NULL
                            BEGIN SELECT RAISE(FAIL,'fixture final recording'); END""")

                    def authorize(action, table, *rest):
                        if (
                            action == sqlite3.SQLITE_UPDATE
                            and table == "run_input_explanations"
                        ):
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    conn.set_authorizer(authorize)
                elif proof == "no_ledger":
                    conn.execute("""CREATE TRIGGER omit_accounting
                        AFTER UPDATE OF telemetry ON runs
                        WHEN json_type(NEW.telemetry,'$.attempts')='array'
                        BEGIN UPDATE runs
                        SET telemetry=json_remove(NEW.telemetry,'$.attempts')
                        WHERE run_id=NEW.run_id; END""")
                else:
                    clock.monotonic_value += 6
                return value

            host.memory_service.forgetting.register_read = register
            submitted = host.submit(SubmitRequest("hello"))
            host.wait(submitted.run_id)
            evidence = host.read_run_evidence(submitted.run_id, trace_root=root)
            if proof != "no_receipts":
                check_evidence(evidence)
    if proof == "no_receipts":
        with sqlite3.connect(database) as conn:
            conn.execute("DROP TRIGGER reject_final")
        with runtime([], database=database) as (host, _, _):
            check_evidence(host.read_run_evidence(submitted.run_id, trace_root=root))
