"""System Run consolidation generation on real RuntimeHost fixtures."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
    CONTEXT,
    PLAN,
    _complete_chat,
)
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.memory.types import FactQuery
from agent_alfred.messages import message_plain_text
from agent_alfred.model import (
    ModelCallInterrupted,
    ScriptedModel,
    ScriptedModelFactory,
)
from agent_alfred.runtime.config import (
    MutableAssignmentProvider,
    StoreBackedSnapshotProvider,
)
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.model_settings import ModelSettingsStore
from agent_alfred.settings import OPENCODE_API_KEY_ENV, Settings
from agent_alfred.trace import RunBundleTraceSink


def _host(tmp_path: Path, script=None, *, threshold=2):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    store = ModelSettingsStore(tmp_path / "model_settings.json")
    store.load()
    settings = Settings(consolidation_source_threshold=threshold)
    env = {OPENCODE_API_KEY_ENV: "sk-test"}
    model = ScriptedModel(script or [PLAN])
    factory = ScriptedModelFactory(model)
    host = RuntimeHost(
        conn=conn,
        factory=factory,
        settings=settings,
        clock=FakeClock(),
        fanout=FanOutSink(
            [CapturingSink(name="c", flush_at_run_end=True)],
            process_instance_id="proc-cons",
        ),
        process_instance_id="proc-cons",
        snapshot_provider=StoreBackedSnapshotProvider(store, settings, environ=env),
        model_settings=store,
        secrets=("sk-test",),
    )
    host.start()
    return host, model, conn


def _traced_host(tmp_path: Path, script, *, threshold=2):
    clock = FakeClock()
    settings = Settings(consolidation_source_threshold=threshold)
    model = ScriptedModel(script)
    conn = sqlite3.connect(tmp_path / "db", check_same_thread=False)
    schema.migrate(conn)
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=clock,
        process_instance_id="chain-probe",
    )
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(model),
        settings=settings,
        clock=clock,
        process_instance_id="chain-probe",
        fanout=FanOutSink(
            [trace, CapturingSink()],
            process_instance_id="chain-probe",
        ),
        snapshot_provider=MutableAssignmentProvider(
            endpoint_id="test",
            model_id="m",
            wire_style="openai",
            api_key="offline-fixture",
            settings=settings,
        ),
    )
    host.start()
    return host, model, conn


def test_eligible_sources_run_one_sessionless_system_run(tmp_path: Path):
    host, model, conn = _host(tmp_path)
    memory = host.memory_service
    _complete_chat(memory, conn, "s1", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s1", "r2", "Still coriander", "Ok.")
    result = host.generate_consolidation("s1")
    assert result.kind == "accepted"
    finished = host.wait(result.run_id, timeout=5)
    assert finished.outcome == "completed"
    assert finished.step_count == 1
    assert result.session_id is None
    row = conn.execute(
        "SELECT purpose, session_id FROM runs WHERE run_id=?", (result.run_id,)
    ).fetchone()
    assert row == ("consolidation", None)
    assert conn.execute(
        "SELECT count(*) FROM agent_log WHERE run_id=?", (result.run_id,)
    ).fetchone() == (0,)
    batch = conn.execute(
        "SELECT status, generation_run_id FROM memory_consolidation_batches"
    ).fetchone()
    assert batch[0] == "succeeded"
    assert batch[1] == result.run_id
    assert len(model.requests) == 1
    sent = model.requests[0]
    assert sent.system[0].text.startswith("You consolidate")
    assert sent.tool_choice == "none"
    assert sent.tools == ()
    host.close()


def test_generic_submit_cannot_use_consolidation_purpose():
    from agent_alfred.gateway.web.api import DashboardApi, SubmitOutcome

    class _Facade:
        pass

    api = DashboardApi(facade=_Facade())
    outcome = api.submit({"message": "s1", "purpose": "consolidation"})
    assert isinstance(outcome, SubmitOutcome)
    assert outcome.status == 400
    assert outcome.code == "unsupported_purpose"


def test_below_threshold_does_not_send(tmp_path: Path):
    host, model, conn = _host(tmp_path, threshold=2)
    memory = host.memory_service
    _complete_chat(memory, conn, "s1", "r1", "I like coriander", "Noted.")
    result = host.generate_consolidation("s1")
    assert result.kind == "accepted"
    finished = host.wait(result.run_id, timeout=5)
    assert finished.outcome == "completed"
    assert finished.error == "below_threshold"
    assert model.requests == []
    host.close()


def _adapter_host(
    tmp_path: Path, handle, clock, *, stream=False, support_rule=None, traced=False
):
    import httpx2
    from openai import OpenAI

    from agent_alfred.model import ModelRef
    from agent_alfred.openai_compatible import OpenAICompatibleAdapter
    from agent_alfred.retry import RetryPolicy
    from agent_alfred.stream_fallback import StreamFallback

    http = httpx2.Client(transport=httpx2.MockTransport(handle))
    sdk = OpenAI(api_key="offline-fixture", http_client=http, max_retries=0)
    adapter = OpenAICompatibleAdapter(client=sdk, model=ModelRef("test", "m"))
    streaming = OpenAICompatibleAdapter(
        client=sdk, model=ModelRef("test", "m"), stream=True
    )
    client = RetryPolicy(
        StreamFallback(
            streaming if stream else adapter,
            nonstream=adapter,
            stream=stream,
            clock=clock,
        ),
        clock=clock,
        sleeper=SimpleNamespace(sleep=lambda _: None),
        max_retries=1,
    )
    conn = sqlite3.connect(tmp_path / "db", check_same_thread=False)
    schema.migrate(conn)
    settings = Settings(consolidation_source_threshold=2)
    sinks = [CapturingSink(name="c", flush_at_run_end=not traced)]
    if traced:
        sinks.append(RunBundleTraceSink(
            root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
            clock=clock, process_instance_id="proc-cons",
        ))
    host = RuntimeHost(
        conn=conn,
        factory=SimpleNamespace(create=lambda snapshot: client),
        settings=settings,
        clock=clock,
        support_rule=support_rule,
        fanout=FanOutSink(
            sinks, process_instance_id="proc-cons",
        ),
        process_instance_id="proc-cons",
        snapshot_provider=MutableAssignmentProvider(
            endpoint_id="test",
            model_id="m",
            wire_style="openai",
            api_key="offline-fixture",
            settings=settings,
        ),
    )
    host.start()
    return host, conn, http


def _completion(content):
    return {
        "id": "fixture",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
    }


def _seed_eligible(host, conn):
    memory = host.memory_service
    old = memory.execute(
        dict(
            operation_id="old",
            kind="semantic",
            action="save",
            payload=dict(subject="food", fact="coriander"),
        ),
        CONTEXT,
    )
    assert old["status"] == "saved", old
    _complete_chat(memory, conn, "s", "r1", "I like food", "Noted.")
    _complete_chat(memory, conn, "s", "r2", "I like coriander", "Noted.")
    return memory, old


def test_late_registration_does_not_send_after_deadline(tmp_path: Path):
    import httpx2

    clock = FakeClock()
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=_completion(PLAN))

    host, conn, http = _adapter_host(tmp_path, handle, clock)
    try:
        memory, _old = _seed_eligible(host, conn)

        def expire():
            clock.monotonic_value = 61
            return 0

        conn.create_function("expire", 0, expire)
        conn.execute(
            "CREATE TRIGGER expire_preflight AFTER INSERT ON run_input_explanations "
            "WHEN NEW.kind='attempt' BEGIN SELECT expire(); END"
        )
        conn.commit()
        admitted = host.generate_consolidation("s")
        result = host.wait(admitted.run_id, timeout=5)
        batch = conn.execute(
            "SELECT status, error_code FROM memory_consolidation_batches"
        ).fetchone()
        attempts = [a.attempt_id for r in result.model_results for a in r.attempts]
        assert sent == []
        assert attempts == []
        assert batch[0] == "failed"
        batch_id = conn.execute(
            "SELECT batch_id FROM memory_consolidation_batches"
        ).fetchone()[0]
        retry = memory.consolidation.retry(
            batch_id,
            1,
            context=CONTEXT,
            operation_id="retry-late",
        )
        assert retry.get("error", {}).get("code") != "invalid_batch_state"
        inputs = conn.execute(
            "SELECT explanation FROM run_input_explanations WHERE run_id=?",
            (admitted.run_id,),
        ).fetchall()
        states = [json.loads(row[0]).get("dispatch_state") for row in inputs]
        assert "sent" not in states
        assert result.outcome == "failed"
    finally:
        host.close()
        conn.close()
        http.close()


def test_observation_fault_keeps_sent_attempt_uses(tmp_path: Path):
    import httpx2

    clock = FakeClock()
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            return httpx2.Response(500, json={"error": {"message": "offline failure"}})
        return httpx2.Response(200, json=_completion(PLAN))

    def support_fault(*_args):
        raise RuntimeError("offline support observation fault")

    host, conn, http = _adapter_host(
        tmp_path, handle, clock, support_rule=support_fault
    )
    try:
        _seed_eligible(host, conn)
        admitted = host.generate_consolidation("s")
        result = host.wait(admitted.run_id, timeout=5)
        attempts = [a.attempt_id for r in result.model_results for a in r.attempts]
        uses = conn.execute(
            "SELECT attempt_id FROM memory_uses WHERE consumer=?",
            (admitted.run_id,),
        ).fetchall()
        inputs = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT explanation FROM run_input_explanations WHERE run_id=?",
                (admitted.run_id,),
            )
        ]
        batch = conn.execute(
            "SELECT status FROM memory_consolidation_batches"
        ).fetchone()[0]
        assert len(attempts) == len(sent) == 2
        assert {row[0] for row in uses} == set(attempts)
        assert {item["attempt_id"] for item in inputs} == set(attempts)
        assert all(item["dispatch_state"] == "sent" for item in inputs)
        assert batch == "failed"
        assert result.outcome == "failed"
    finally:
        host.close()
        conn.close()
        http.close()


def test_control_interrupt_settles_batch_and_sent_inputs(tmp_path: Path):
    clock = FakeClock()
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        raise KeyboardInterrupt("offline transport control")

    host, conn, http = _adapter_host(tmp_path, handle, clock)
    try:
        memory, _old = _seed_eligible(host, conn)
        admitted = host.generate_consolidation("s")
        result = host.wait(admitted.run_id, timeout=5)
        attempts = [a.attempt_id for r in result.model_results for a in r.attempts]
        uses = conn.execute(
            "SELECT attempt_id FROM memory_uses WHERE consumer=?",
            (admitted.run_id,),
        ).fetchall()
        batch = conn.execute(
            "SELECT status FROM memory_consolidation_batches"
        ).fetchone()[0]
        assert len(attempts) == len(sent) == 1
        assert {row[0] for row in uses} == set(attempts)
        assert batch == "failed"
        assert result.outcome == "interrupted"
        batch_id = conn.execute(
            "SELECT batch_id FROM memory_consolidation_batches"
        ).fetchone()[0]
        retry = memory.consolidation.retry(
            batch_id,
            1,
            context=CONTEXT,
            operation_id="retry-control",
        )
        assert retry.get("error", {}).get("code") != "invalid_batch_state"
    finally:
        host.close()
        conn.close()
        http.close()


def test_stream_fallback_confirms_both_sent_attempts(tmp_path: Path):
    import httpx2

    clock = FakeClock()
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if payload.get("stream"):
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: [DONE]\n\n",
            )
        return httpx2.Response(200, json=_completion(PLAN))

    host, conn, http = _adapter_host(tmp_path, handle, clock, stream=True)
    try:
        _seed_eligible(host, conn)
        admitted = host.generate_consolidation("s")
        result = host.wait(admitted.run_id, timeout=5)
        attempts = [a.attempt_id for r in result.model_results for a in r.attempts]
        uses = conn.execute(
            "SELECT attempt_id FROM memory_uses WHERE consumer=?",
            (admitted.run_id,),
        ).fetchall()
        assert len(attempts) == len(sent) == 2
        assert {row[0] for row in uses} == set(attempts)
        assert result.outcome == "completed"
    finally:
        host.close()
        conn.close()
        http.close()


def test_interrupted_scripted_attempt_is_sent(tmp_path: Path):
    host, model, conn = _host(tmp_path)
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted")
        original = model.respond

        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            raise ModelCallInterrupted(
                RuntimeError("local failure after actual Attempt"), result
            )

        model.respond = interrupted
        accepted = host.generate_consolidation("s")
        outcome = host.wait(accepted.run_id, timeout=5)
        batch = conn.execute(
            "SELECT status FROM memory_consolidation_batches"
        ).fetchone()[0]
        telemetry = json.loads(
            conn.execute(
                "SELECT telemetry FROM runs WHERE run_id=?", (accepted.run_id,)
            ).fetchone()[0]
        )
        actual = [attempt["attempt_id"] for attempt in telemetry["attempts"]]
        inputs = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT explanation FROM run_input_explanations "
                "WHERE run_id=? AND kind=?",
                (accepted.run_id, "attempt"),
            )
        ]
        reads = conn.execute(
            "SELECT source FROM history_reads WHERE consumer=?",
            (accepted.run_id,),
        ).fetchall()
        assert actual and inputs and inputs[0]["attempt_id"] in actual
        assert inputs[0]["dispatch_state"] == "sent"
        assert {row[0] for row in reads} == {"r1", "r2"}
        assert batch == "failed"
        assert outcome.outcome == "failed"
    finally:
        host.close()


def test_interrupted_scripted_control_settles_batch(tmp_path: Path):
    host, model, conn = _host(tmp_path)
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted")
        original = model.respond

        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            raise ModelCallInterrupted(KeyboardInterrupt(), result)

        model.respond = interrupted
        accepted = host.generate_consolidation("s")
        outcome = host.wait(accepted.run_id, timeout=5)
        batch = conn.execute(
            "SELECT status FROM memory_consolidation_batches"
        ).fetchone()[0]
        telemetry = json.loads(
            conn.execute(
                "SELECT telemetry FROM runs WHERE run_id=?", (accepted.run_id,)
            ).fetchone()[0]
        )
        actual = [attempt["attempt_id"] for attempt in telemetry["attempts"]]
        inputs = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT explanation FROM run_input_explanations "
                "WHERE run_id=? AND kind=?",
                (accepted.run_id, "attempt"),
            )
        ]
        reads = conn.execute(
            "SELECT source FROM history_reads WHERE consumer=?",
            (accepted.run_id,),
        ).fetchall()
        assert actual and inputs and inputs[0]["attempt_id"] in actual
        assert inputs[0]["dispatch_state"] == "sent"
        assert {row[0] for row in reads} == {"r1", "r2"}
        assert batch == "failed"
        assert outcome.outcome == "interrupted"
    finally:
        host.close()


def test_typed_finish_read_failure_fails_batch(tmp_path: Path):
    host, model, conn = _host(tmp_path)
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted")
        fired = []

        def authorizer(action, table, column, database, trigger):
            del column, database, trigger
            if action == sqlite3.SQLITE_READ and table == "agent_log" and not fired:
                fired.append(table)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        original = model.respond

        def respond(*args, **kwargs):
            result = original(*args, **kwargs)
            conn.set_authorizer(authorizer)
            return result

        model.respond = respond
        submitted = host.generate_consolidation("s")
        result = host.wait(submitted.run_id, timeout=5)
        conn.set_authorizer(None)
        assert fired
        batch = conn.execute(
            "SELECT batch_id, revision, status, error_code "
            "FROM memory_consolidation_batches"
        ).fetchone()
        retry = memory.consolidation.retry(
            batch[0], batch[1], context=CONTEXT, operation_id="retry"
        )
        assert result.outcome == "failed"
        assert batch[2] == "failed"
        assert retry.get("error", {}).get("code") != "invalid_batch_state"
    finally:
        host.close()


def test_sent_resolution_fault_keeps_remaining_attempts(tmp_path: Path):
    import httpx2

    clock = FakeClock()
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            return httpx2.Response(500, json={"error": {"message": "offline failure"}})
        return httpx2.Response(200, json=_completion(PLAN))

    host, conn, http = _adapter_host(tmp_path, handle, clock)
    try:
        _seed_eligible(host, conn)
        calls = [0]

        def fail_once():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("one transient resolution failure")
            return 0

        conn.create_function("resolve_fault", 0, fail_once)
        conn.execute(
            "CREATE TRIGGER fail_resolution BEFORE UPDATE ON "
            "run_input_explanations WHEN NEW.kind='attempt' AND "
            "json_extract(NEW.explanation,'$.dispatch_state')='sent' "
            "BEGIN SELECT resolve_fault(); END"
        )
        conn.commit()
        admitted = host.generate_consolidation("s")
        result = host.wait(admitted.run_id, timeout=5)
        attempts = [a.attempt_id for r in result.model_results for a in r.attempts]
        uses = conn.execute(
            "SELECT attempt_id FROM memory_uses WHERE consumer=?",
            (admitted.run_id,),
        ).fetchall()
        assert len(sent) == len(attempts) == 2
        assert {row[0] for row in uses} == set(attempts)
    finally:
        host.close()
        conn.close()
        http.close()


def test_unsent_resolution_fault_keeps_not_sent_receipt(tmp_path: Path):
    import httpx2

    clock = FakeClock()
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=_completion(PLAN))

    host, conn, http = _adapter_host(tmp_path, handle, clock)
    try:
        memory, _old = _seed_eligible(host, conn)

        def expire():
            clock.monotonic_value = 61
            return 0

        calls = [0]

        def fail_once():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("one transient resolution failure")
            return 0

        conn.create_function("expire", 0, expire)
        conn.create_function("resolve_fault", 0, fail_once)
        conn.execute(
            "CREATE TRIGGER expire_preflight AFTER INSERT ON run_input_explanations "
            "WHEN NEW.kind='attempt' BEGIN SELECT expire(); END"
        )
        conn.execute(
            "CREATE TRIGGER fail_resolution BEFORE UPDATE ON "
            "run_input_explanations WHEN NEW.kind='attempt' AND "
            "json_extract(NEW.explanation,'$.dispatch_state')='not_sent' "
            "BEGIN SELECT resolve_fault(); END"
        )
        conn.commit()
        admitted = host.generate_consolidation("s")
        result = host.wait(admitted.run_id, timeout=5)
        attempts = [a.attempt_id for r in result.model_results for a in r.attempts]
        assert sent == []
        assert attempts == []
        memory.forgetting.recover_input_registrations()
        recovered = conn.execute(
            "SELECT json_extract(explanation,'$.dispatch_state') "
            "FROM run_input_explanations WHERE run_id=?",
            (admitted.run_id,),
        ).fetchall()
        assert recovered == [("not_sent",)]
        telemetry = json.loads(
            conn.execute(
                "SELECT telemetry FROM runs WHERE run_id=?", (admitted.run_id,)
            ).fetchone()[0]
        )
        assert telemetry.get("memory", {}).get("input_not_sent_attempts")
    finally:
        host.close()
        conn.close()
        http.close()


def test_generated_product_consumer_isolated_after_source_delete(tmp_path: Path):
    clock = FakeClock()
    settings = Settings(consolidation_source_threshold=2)
    model = ScriptedModel(
        [
            PLAN,
            '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}',
            "You like coriander.",
        ]
    )
    conn = sqlite3.connect(tmp_path / "db", check_same_thread=False)
    schema.migrate(conn)
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=clock,
        process_instance_id="chain-probe",
    )
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(model),
        settings=settings,
        clock=clock,
        process_instance_id="chain-probe",
        fanout=FanOutSink(
            [trace, CapturingSink()],
            process_instance_id="chain-probe",
        ),
        snapshot_provider=MutableAssignmentProvider(
            endpoint_id="test",
            model_id="m",
            wire_style="openai",
            api_key="offline-fixture",
            settings=settings,
        ),
    )
    host.start()
    try:
        memory = host.memory_service
        old = memory.execute(
            {
                "operation_id": "save-A",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "food", "fact": "PRIVATE-BASIL-CONTEXT"},
            },
            CONTEXT,
        )
        assert old["status"] == "saved", old
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        generation = host.generate_consolidation("s")
        generated = host.wait(generation.run_id, timeout=5)
        assert generated.outcome == "completed", generated
        request_text = " ".join(
            message_plain_text(message) for message in model.requests[0].messages
        )
        assert "PRIVATE-BASIL-CONTEXT" in request_text
        with memory.reading_stores() as (facts, _):
            product = facts.search(FactQuery(text="coriander"))[0].record.id
        independent = memory.execute(
            {
                "operation_id": "save-independent",
                "kind": "semantic",
                "action": "save",
                "payload": {
                    "subject": "independent",
                    "fact": "An independently saved fact",
                },
            },
            CONTEXT,
        )
        assert independent["status"] == "saved"
        reader = host.submit(SubmitRequest("What herb do I like?"))
        read_result = host.wait(reader.run_id, timeout=5)
        assert read_result.outcome == "completed", read_result
        actual_use = conn.execute(
            "SELECT memory_id FROM memory_uses WHERE consumer=?",
            (reader.run_id,),
        ).fetchall()
        assert (product,) in actual_use, actual_use
        provenance = memory.get_provenance("semantic", product, 1)
        assert generation.run_id in provenance["source_groups"], provenance
        deletion = memory.execute(
            {
                "operation_id": "delete-A",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": old["memory_id"]},
            },
            CONTEXT,
        )
        assert deletion["status"] == "deleted", deletion
        evaluated = memory.forgetting.evaluate_history(
            [generation.run_id, reader.run_id], purpose="working_window"
        )
        assert "error" not in evaluated and "allowed" in evaluated, evaluated
        assert not evaluated["allowed"]
        progress = memory.forgetting.get_forgetting("delete-A")
        isolated = {
            item["group_id"]
            for item in progress["limits"]
            if item["mode"] == "isolated"
        }
        assert {generation.run_id, reader.run_id} <= isolated
        assert memory.get("semantic", old["memory_id"]) is None
        assert memory.get("semantic", independent["memory_id"]) is not None
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_log WHERE run_id IN ('r1','r2')"
            ).fetchone()[0]
            == 4
        )
    finally:
        host.close()
        conn.close()


def _seed_product_chain(host, conn):
    memory = host.memory_service
    old = memory.execute(
        {
            "operation_id": "save-A",
            "kind": "semantic",
            "action": "save",
            "payload": {"subject": "food", "fact": "PRIVATE-BASIL-CONTEXT"},
        },
        CONTEXT,
    )
    assert old["status"] == "saved", old
    _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
    _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
    generation = host.generate_consolidation("s")
    generated = host.wait(generation.run_id, timeout=5)
    assert generated.outcome == "completed", generated
    with memory.reading_stores() as (facts, _):
        product = facts.search(FactQuery(text="coriander"))[0].record.id
    independent = memory.execute(
        {
            "operation_id": "save-independent",
            "kind": "semantic",
            "action": "save",
            "payload": {
                "subject": "independent",
                "fact": "An independently saved fact",
            },
        },
        CONTEXT,
    )
    assert independent["status"] == "saved"
    reader = host.submit(SubmitRequest("What herb do I like?"))
    read_result = host.wait(reader.run_id, timeout=5)
    assert read_result.outcome == "completed", read_result
    deletion = memory.execute(
        {
            "operation_id": "delete-A",
            "kind": "semantic",
            "action": "delete",
            "expected_version": 1,
            "payload": {"id": old["memory_id"]},
        },
        CONTEXT,
    )
    assert deletion["status"] == "deleted", deletion
    return memory, old, product, independent, generation, reader


def test_future_chat_does_not_send_isolated_product(tmp_path: Path):
    host, model, conn = _traced_host(
        tmp_path,
        [
            PLAN,
            '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}',
            "You like coriander.",
            '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}',
            "Again coriander.",
        ],
    )
    try:
        memory, old, product, independent, generation, reader = _seed_product_chain(
            host, conn
        )
        next_reader = host.submit(SubmitRequest("What herb do I like now?"))
        next_result = host.wait(next_reader.run_id, timeout=5)
        new_text = " ".join(
            message_plain_text(message)
            for request in model.requests[3:]
            for message in request.messages
        )
        assert product not in new_text or "likes coriander" not in new_text
        assert "likes coriander" not in new_text
        assert memory.get("semantic", independent["memory_id"]) is not None
        assert memory.get("semantic", old["memory_id"]) is None
        evaluated = memory.forgetting.evaluate_history(
            [generation.run_id, reader.run_id], purpose="working_window"
        )
        assert not evaluated["allowed"]
        assert next_result.outcome in ("completed", "failed")
    finally:
        host.close()
        conn.close()


def test_future_consolidation_does_not_send_isolated_product(tmp_path: Path):
    host, model, conn = _traced_host(
        tmp_path,
        [
            PLAN,
            '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}',
            "You like coriander.",
            PLAN,
        ],
    )
    try:
        memory, _old, _product, independent, _generation, _reader = _seed_product_chain(
            host, conn
        )
        _complete_chat(
            memory, conn, "later-session", "r3", "I enjoy gardening", "Noted."
        )
        _complete_chat(
            memory, conn, "later-session", "r4", "I grow tomatoes", "Noted."
        )
        later = host.generate_consolidation("later-session")
        later_result = host.wait(later.run_id, timeout=5)
        carried = any(
            "likes coriander"
            in " ".join(message_plain_text(m) for m in request.messages)
            for request in model.requests[3:]
        )
        assert not carried
        assert later_result.outcome == "completed"
        assert memory.get("semantic", independent["memory_id"]) is not None
    finally:
        host.close()
        conn.close()


def test_late_source_reaches_existing_consumer(tmp_path: Path):
    host, model, conn = _traced_host(
        tmp_path,
        [
            '{"retrieve":true,"query":"BASILORIGIN","reason_code":"personal_information"}',
            "I used the historical origin.",
            '{"retrieve":true,"query":"CORIANDERPRODUCT","reason_code":"personal_information"}',
            "I used the derived product.",
        ],
    )
    try:
        memory = host.memory_service
        old = memory.execute(
            {
                "operation_id": "A",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "origin", "fact": "BASILORIGIN"},
            },
            CONTEXT,
        )
        assert old["status"] == "saved"
        first = host.submit(SubmitRequest("Recall the historical origin"))
        assert host.wait(first.run_id, timeout=5).outcome == "completed"
        product = memory.execute(
            {
                "operation_id": "P",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "product", "fact": "CORIANDERPRODUCT"},
            },
            CONTEXT,
        )
        assert product["status"] == "saved"
        second = host.submit(SubmitRequest("Recall the derived product"))
        assert host.wait(second.run_id, timeout=5).outcome == "completed"
        added = memory.forgetting.register_sources(
            "semantic",
            product["memory_id"],
            1,
            groups=(first.run_id,),
            evidence_id="late-confirmed-origin",
            context=CONTEXT,
        )
        assert added.get("status") == "registered", added
        deletion = memory.execute(
            {
                "operation_id": "delete-A",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": old["memory_id"]},
            },
            CONTEXT,
        )
        assert deletion.get("status") == "deleted", deletion
        evaluated = memory.forgetting.evaluate_history(
            [first.run_id, second.run_id], purpose="working_window"
        )
        assert not evaluated["allowed"]
    finally:
        host.close()
        conn.close()


def test_command_late_source_isolates_existing_reader(tmp_path: Path):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    host, _model, conn = _traced_host(
        tmp_path,
        [
            '{"retrieve":true,"query":"BASILORIGIN","reason_code":"personal_information"}',
            "Origin recalled.",
            '{"retrieve":true,"query":"CORIANDERPRODUCT","reason_code":"personal_information"}',
            "Product recalled.",
        ],
    )
    try:
        memory = host.memory_service
        origin = memory.execute(
            {
                "operation_id": "A",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "origin", "fact": "BASILORIGIN"},
            },
            CONTEXT,
        )
        first = host.submit(SubmitRequest("Recall the historical origin"))
        assert host.wait(first.run_id, timeout=5).outcome == "completed"
        memory.execute(
            {
                "operation_id": "P",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "product", "fact": "CORIANDERPRODUCT"},
            },
            CONTEXT,
        )
        reader = host.submit(SubmitRequest("Recall the derived product"))
        assert host.wait(reader.run_id, timeout=5).outcome == "completed"
        late = memory.execute(
            {
                "operation_id": "P-late",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "product", "fact": "CORIANDERPRODUCT"},
            },
            CommandContext(
                ManualOrigin("web"), "web", source_groups=(first.run_id,)
            ),
        )
        assert late["status"] == "already_exists"
        assert late["record_version"] == 1
        deleted = memory.execute(
            {
                "operation_id": "delete-A",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": origin["memory_id"]},
            },
            CONTEXT,
        )
        assert deleted["status"] == "deleted"
        evaluated = memory.forgetting.evaluate_history(
            (first.run_id, reader.run_id), purpose="working_window"
        )
        assert reader.run_id in evaluated["denied"]
    finally:
        host.close()
        conn.close()


def test_command_late_source_invalidates_awaiting_batch(tmp_path: Path):
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import (
        _complete_chat,
    )
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    host, model, conn = _traced_host(
        tmp_path,
        [
            '{"retrieve":true,"query":"BASILORIGIN","reason_code":"personal_information"}',
            "Origin recalled.",
            '{"retrieve":true,"query":"CORIANDERPRODUCT","reason_code":"personal_information"}',
            "Product recalled.",
        ],
    )
    try:
        memory = host.memory_service
        origin = memory.execute(
            {
                "operation_id": "A",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "origin", "fact": "BASILORIGIN"},
            },
            CONTEXT,
        )
        first = host.submit(SubmitRequest("Recall the historical origin"))
        assert host.wait(first.run_id, timeout=5).outcome == "completed"
        product = memory.execute(
            {
                "operation_id": "P",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "product", "fact": "CORIANDERPRODUCT"},
            },
            CONTEXT,
        )
        reader = host.submit(SubmitRequest("Recall the derived product"))
        assert host.wait(reader.run_id, timeout=5).outcome == "completed"
        _complete_chat(
            memory, conn, reader.session_id, "r2", "I changed the product", "Noted"
        )
        deleted = memory.execute(
            {
                "operation_id": "delete-A",
                "kind": "semantic",
                "action": "delete",
                "expected_version": 1,
                "payload": {"id": origin["memory_id"]},
            },
            CONTEXT,
        )
        assert deleted["status"] == "deleted"
        plan = json.dumps(
            {
                "semantic": [
                    {
                        "action": "update",
                        "id": product["memory_id"],
                        "subject": "product",
                        "fact": "SENSITIVE-CANDIDATE",
                    }
                ],
                "episode_summary": "Product changed",
            }
        )
        model._script.append(plan)
        generating = host.generate_consolidation(reader.session_id)
        assert host.wait(generating.run_id, timeout=5).outcome == "completed"
        batch_id = conn.execute(
            "SELECT batch_id FROM memory_consolidation_batches WHERE session_id=?",
            (reader.session_id,),
        ).fetchone()[0]
        assert memory.consolidation.get_batch(batch_id)["status"] == "awaiting_approval"
        late = memory.execute(
            {
                "operation_id": "P-late",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "product", "fact": "CORIANDERPRODUCT"},
            },
            CommandContext(ManualOrigin("web"), "web", source_groups=(first.run_id,)),
        )
        assert late["status"] == "already_exists"
        view = memory.consolidation.get_batch(batch_id)
        assert view["status"] == "invalidated"
        assert "plan" not in view
    finally:
        host.close()
        conn.close()


def test_invalid_plan_retry_starts_new_run(tmp_path: Path):
    host, model, conn = _host(tmp_path, script=["not-json"])
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        first = host.generate_consolidation("s")
        finished = host.wait(first.run_id, timeout=5)
        assert finished.outcome == "failed"
        batch = conn.execute(
            "SELECT batch_id, revision, status FROM memory_consolidation_batches"
        ).fetchone()
        assert batch[2] == "failed"
        model._script.append(PLAN)
        retried = host.retry_consolidation(
            batch[0], batch[1], operation_id="retry-invalid"
        )
        assert retried.kind == "accepted"
        assert retried.run_id != first.run_id
        second = host.wait(retried.run_id, timeout=5)
        assert second.outcome == "completed"
        later = conn.execute(
            "SELECT revision, status FROM memory_consolidation_batches "
            "WHERE batch_id=?",
            (batch[0],),
        ).fetchone()
        assert later[0] == batch[1] + 1
        replay = host.retry_consolidation(
            batch[0], batch[1], operation_id="retry-invalid"
        )
        assert replay == memory.consolidation.get_batch(batch[0])["receipt"]
        assert len(model.requests) == 2
    finally:
        host.close()


def test_prepare_failure_leaves_failed_retryable_batch(tmp_path: Path):
    host, model, conn = _host(tmp_path)
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        def authorizer(action, table, column, database, trigger):
            del column, database, trigger
            if action == sqlite3.SQLITE_READ and table == "facts":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        result = host.generate_consolidation("s")
        finished = host.wait(result.run_id, timeout=5)
        conn.set_authorizer(None)
        assert finished.outcome == "failed"
        batch = conn.execute(
            "SELECT batch_id, revision, status, error_code "
            "FROM memory_consolidation_batches"
        ).fetchone()
        assert batch is not None
        assert batch[2] == "failed"
        assert model.requests == []
        retried = host.retry_consolidation(
            batch[0], batch[1], operation_id="retry-prepare"
        )
        assert retried.kind == "accepted"
        done = host.wait(retried.run_id, timeout=5)
        assert done.outcome == "completed"
        assert model.requests
    finally:
        host.close()


def _abandoned_generation_child(folder: str) -> None:
    import threading

    host, model, conn = _traced_host(Path(folder), ["unused"])

    def hold(request, *, events=None, deadline=None):
        request.notify_attempt_started("owned-crash-attempt", deadline)
        print("READY", flush=True)
        threading.Event().wait()

    model.respond = hold
    _complete_chat(
        host.memory_service, conn, "restart-session", "r1", "I like coriander", "Noted."
    )
    _complete_chat(
        host.memory_service, conn, "restart-session", "r2", "Still coriander", "Noted."
    )
    result = host.generate_consolidation("restart-session")
    host.wait(result.run_id, timeout=30)
    raise AssertionError("owned child unexpectedly resumed")


def test_restart_fails_abandoned_running_batch(tmp_path: Path):
    import select
    import subprocess
    import sys

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from agent_alfred.evals.deterministic.test_memory_consolidation_runtime "
            "import _abandoned_generation_child; _abandoned_generation_child(%r)"
            % str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 15)
        assert ready, child.stderr.read() if child.stderr else "no stdout"
        assert child.stdout.readline().strip() == "READY"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
    host, model, conn = _traced_host(tmp_path, ["unexpected startup call"])
    try:
        rows = conn.execute(
            "SELECT status FROM memory_consolidation_batches"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] not in ("running", "queued")
        assert model.requests == []
    finally:
        host.close()
        conn.close()


def test_retry_identity_mismatch_and_readback(tmp_path: Path):
    host, model, conn = _host(tmp_path, script=["not-json"])
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        first = host.generate_consolidation("s")
        assert host.wait(first.run_id, timeout=5).outcome == "failed"
        batch_id, revision = conn.execute(
            "SELECT batch_id, revision FROM memory_consolidation_batches"
        ).fetchone()
        model._script.append(PLAN)
        retry = host.retry_consolidation(
            batch_id, revision, operation_id="stable-retry"
        )
        assert retry.kind == "accepted"
        assert host.wait(retry.run_id, timeout=5).outcome == "completed"
        exact = host.retry_consolidation(
            batch_id, revision, operation_id="stable-retry"
        )
        wrong_batch = host.retry_consolidation(
            "different-batch", revision, operation_id="stable-retry"
        )
        wrong_revision = host.retry_consolidation(
            batch_id, revision + 30, operation_id="stable-retry"
        )
        assert exact == memory.consolidation.get_batch(batch_id)["receipt"]
        assert wrong_batch.get("error", {}).get("code") == "operation_mismatch"
        assert wrong_revision.get("error", {}).get("code") == "operation_mismatch"
        assert len(model.requests) == 2
    finally:
        host.close()


def test_commit_retry_readback_includes_products(tmp_path: Path):
    host, model, conn = _host(tmp_path, script=[PLAN])
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        conn.execute(
            "CREATE TEMP TRIGGER commit_fault AFTER INSERT ON episodes "
            "BEGIN SELECT RAISE(ABORT, 'commit fault'); END"
        )
        first = host.generate_consolidation("s")
        assert host.wait(first.run_id, timeout=5).outcome == "failed"
        batch = conn.execute(
            "SELECT batch_id, revision FROM memory_consolidation_batches"
        ).fetchone()
        conn.execute("DROP TRIGGER IF EXISTS commit_fault")
        committed = host.retry_consolidation(
            batch[0], batch[1], operation_id="retry-stable"
        )
        assert committed["status"] == "succeeded" and committed["products"]
        replay = host.retry_consolidation(
            batch[0], batch[1], operation_id="retry-stable"
        )
        assert replay == committed
        assert len(model.requests) == 1
    finally:
        host.close()


def test_host_target_edit_invalidates_awaiting_batch(tmp_path: Path):
    host, model, conn = _host(tmp_path, script=["placeholder"])
    try:
        memory = host.memory_service
        target = memory.execute(
            {
                "operation_id": "seed",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "food", "fact": "likes basil"},
            },
            CONTEXT,
        )
        plan = json.dumps(
            {
                "semantic": [
                    {
                        "action": "update",
                        "id": target["memory_id"],
                        "subject": "food",
                        "fact": "likes coriander",
                    }
                ],
                "episode_summary": "Changed preference",
            }
        )
        model._script[:] = [plan]
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        first = host.generate_consolidation("s")
        assert host.wait(first.run_id, timeout=5).outcome == "completed"
        batch = conn.execute(
            "SELECT batch_id, revision FROM memory_consolidation_batches"
        ).fetchone()
        assert memory.consolidation.get_batch(batch[0])["status"] == "awaiting_approval"
        edit = memory.execute(
            {
                "operation_id": "edit",
                "kind": "semantic",
                "action": "update",
                "expected_version": 1,
                "payload": {
                    "id": target["memory_id"],
                    "subject": "food",
                    "fact": "likes mint",
                },
            },
            CONTEXT,
        )
        assert edit["status"] == "updated"
        view = memory.consolidation.get_batch(batch[0])
        assert view["status"] == "invalidated" and "plan" not in view
        retried = host.retry_consolidation(
            batch[0], batch[1], operation_id="retry-invalidated"
        )
        assert retried.get("error", {}).get("code") == "batch_invalidated"
    finally:
        host.close()


def test_mutation_during_generation_returns_busy(tmp_path: Path):
    import threading

    host, model, conn = _host(tmp_path)
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, "s", "r1", "I like coriander", "Noted.")
        _complete_chat(memory, conn, "s", "r2", "Still coriander", "Noted.")
        original = model.respond
        started = threading.Event()
        release = threading.Event()

        def hold(request, *, events=None, deadline=None):
            request.notify_attempt_started("busy-attempt", deadline)
            started.set()
            release.wait(5)
            return original(request, events=events, deadline=deadline)

        model.respond = hold
        admitted = host.generate_consolidation("s")
        assert started.wait(5)
        blocked = memory.execute(
            {
                "operation_id": "during",
                "kind": "semantic",
                "action": "save",
                "payload": {"subject": "other", "fact": "concurrent"},
            },
            CONTEXT,
        )
        assert blocked.get("error", {}).get("code") == "busy"
        release.set()
        assert host.wait(admitted.run_id, timeout=5).outcome == "completed"
    finally:
        host.close()


def test_retry_unadmitted_intent_resumes_after_mutation(tmp_path, monkeypatch):
    import threading

    host, model, conn = _host(tmp_path, script=['not-json', PLAN])
    try:
        memory = host.memory_service
        _complete_chat(memory, conn, 's', 'r1', 'I like coriander', 'Noted.')
        _complete_chat(memory, conn, 's', 'r2', 'Still coriander', 'Noted.')
        first = host.generate_consolidation('s')
        assert host.wait(first.run_id, timeout=5).outcome == 'failed'
        batch = memory.consolidation.session_status('s')['batch']
        entered, release = threading.Event(), threading.Event()
        failures = []

        def mutation():
            try:
                def held():
                    entered.set()
                    assert release.wait(5)
                host.execute_mutation(held)
            except BaseException as error:
                failures.append(error)

        original = host.submit

        def collide(request):
            worker = threading.Thread(target=mutation)
            worker.start()
            try:
                assert entered.wait(5)
                return original(request)
            finally:
                release.set()
                worker.join(5)
                assert not worker.is_alive() and not failures

        with monkeypatch.context() as patch:
            patch.setattr(host, 'submit', collide)
            refused = host.retry_consolidation(
                batch['batch_id'], 1, operation_id='stable-busy'
            )
        assert refused.kind == 'mutation_in_flight'
        assert len(model.requests) == 1
        resumed = host.retry_consolidation(
            batch['batch_id'], 1, operation_id='stable-busy'
        )
        assert getattr(resumed, 'kind', None) == 'accepted', resumed
        assert host.wait(resumed.run_id, timeout=5).outcome == 'completed'
        replay = host.retry_consolidation(
            batch['batch_id'], 1, operation_id='stable-busy'
        )
        assert replay['status'] == 'succeeded' and replay['revision'] == 2
        assert len(model.requests) == 2
    finally:
        host.close()
        conn.close()
