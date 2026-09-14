"""#26 review counterexamples through real Host/CLI/SQLite boundaries."""

import json
from io import StringIO

import pytest

from agent_alfred.evals.deterministic.test_message_routing import enable
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, runtime, submit
from agent_alfred.gateway.cli import _send
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.runtime.routing import build_routing_graph, project_context


@pytest.mark.parametrize(
    "text,category,expected",
    [
        ("  HELLO!!!  ", "greeting", "quick"),
        ("thank you。.!", "thanks", "quick"),
        ('"无需回复"', "no_reply", "fallback"),
        ("```\n无需回复\n```", "no_reply", "fallback"),
        ("你好?", "greeting", "fallback"),
        ("hello!!!!", "greeting", "fallback"),
        ("hello\nworld", "greeting", "fallback"),
        ("ｈｉ", "greeting", "fallback"),
        ("你好", "Greeting", "fallback"),
    ],
)
def test_full_guard(tmp_path, text, category, expected):
    with runtime(tmp_path, [SKIP, category, "answer"]) as (host, model, _):
        enable(host)
        _, result = submit(host, text)
        assert result.memory_telemetry["routing"]["route"] == expected
        assert len(model.requests) == (2 if expected == "quick" else 3)


@pytest.mark.parametrize("fault", ["raise", "missing_projection"])
def test_recovery_boundaries(tmp_path, fault):
    def projection(snapshot):
        if fault == "missing_projection":
            return {"context_version": 1}
        return project_context({**snapshot, "context_version": 999})

    def recovery(s, c):
        raise RuntimeError("controlled recovery callback")

    def build(tools):
        return build_routing_graph(
            tools,
            projection=projection,
            **({"recovery": recovery} if fault == "raise" else {}),
        )

    with runtime(
        tmp_path, [SKIP, "full", "must not send"], routing_graph_builder=build
    ) as (host, model, _):
        enable(host)
        _, result = submit(host, "task")
        assert len(model.requests) == 2
        if fault == "raise":
            assert (
                result.memory_telemetry["routing"]["fallback"]["reason"]
                == "context_invalid"
            )
        else:
            assert (
                result.memory_telemetry["routing"]["graph_result"]
                == "CompletedWithRecovery"
            )


@pytest.mark.parametrize(
    "task,category,expected",
    [("无需回复", "no_reply", None), ("你好", "greeting", "你好！")],
)
def test_cli_renderer(tmp_path, task, category, expected):
    with runtime(tmp_path, [SKIP, category]) as (host, model, _):
        enable(host)
        out = StringIO()
        rendered = []
        assert (
            _send(
                host,
                task,
                host.create_session(),
                out,
                renderer=lambda text, _: rendered.append(text),
            )
            == 0
        )
        assert rendered == ([] if expected is None else [expected])
        if expected is None:
            assert "按要求未回复" in out.getvalue()


def test_unreadable_config(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    (path / "behaviour.json").mkdir()
    with runtime(tmp_path, [SKIP, "ordinary"]) as (host, model, _):
        api = DashboardApi(facade=host)
        assert api.behaviour()[1]["status"] == "settings_unreadable"
        assert (
            api.mutate_behaviour(
                dict(action="recover", expected_revision=0, fingerprint=None)
            )[0]
            == 400
        )
        _, result = submit(host, "hello")
        assert (
            result.memory_telemetry["routing"]["fallback"]["reason"]
            == "routing_unavailable"
        )
        assert len(model.requests) == 2


def test_fallback_entered_is_actual(tmp_path):
    from agent_alfred.clock import FakeClock
    from agent_alfred.settings import Settings

    clock = FakeClock()
    from agent_alfred.events import CapturingSink

    class DeadlineOnNotice(CapturingSink):
        def commit(self, prepared, event):
            super().commit(prepared, event)
            if (
                event.payload.name == "notice"
                and event.payload.code == "routing_fallback"
            ):
                clock.monotonic_value = 100

    with runtime(
        tmp_path,
        [SKIP, RuntimeError("classifier fail"), "must not send"],
        clock=clock,
        settings=Settings(overall_deadline_s=30),
        extra_sinks=(DeadlineOnNotice(),),
    ) as (host, model, _):
        enable(host)
        _, result = submit(host, "task")
        print(
            json.dumps(
                {
                    "outcome": result.outcome,
                    "error": result.error,
                    "routing": result.memory_telemetry["routing"],
                    "requests": len(model.requests),
                },
                ensure_ascii=False,
            )
        )
        import sqlite3

        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            saved = json.loads(
                conn.execute(
                    "SELECT telemetry FROM runs WHERE phase='finished'"
                ).fetchone()[0]
            )
        assert (
            saved["memory"]["routing"]["fallback"]
            == result.memory_telemetry["routing"]["fallback"]
        )
        from agent_alfred.gateway.cli import _print_result

        cli = StringIO()
        _print_result(result, cli, renderer=lambda *_: None)
        print(
            json.dumps(
                {
                    "persisted_fallback": saved["memory"]["routing"]["fallback"],
                    "cli": cli.getvalue(),
                },
                ensure_ascii=False,
            )
        )
        assert result.error == "overall_deadline"
        # F04 keeps the original graph failure independently of the final stop.
        assert result.memory_telemetry["routing"]["error"] == "node_failed"
        assert saved["memory"]["routing"]["error"] == "node_failed"
        assert len(model.requests) == 2
        assert result.memory_telemetry["routing"]["fallback"]["entered"] is False
        assert (
            result.memory_telemetry["routing"]["fallback"]["reason"]
            == "overall_deadline"
        )


def test_cli_no_reply_recording_failure_visible(tmp_path):
    import sqlite3

    with runtime(tmp_path, [SKIP, "no_reply"]) as (host, model, _):
        enable(host)
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute(
                "CREATE TRIGGER broken_recording BEFORE UPDATE OF phase ON runs "
                "WHEN NEW.phase='finished' "
                "BEGIN SELECT RAISE(ABORT,'IO fault'); END"
            )
        out = StringIO()
        code = _send(
            host, "无需回复", host.create_session(), out, renderer=lambda *_: None
        )
        print(
            json.dumps(
                {
                    "code": code,
                    "cli": out.getvalue(),
                    "state": host.snapshot().coordinator_state,
                },
                ensure_ascii=False,
            )
        )
        assert host.snapshot().coordinator_state == "recording_failed"
        assert "未保存" in out.getvalue()


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("failure_read", [1, 2])
def test_std02_save_publishes_only_a_verified_snapshot(tmp_path, recover, failure_read):
    from agent_alfred.atomic_config import read_bytes, write_atomic
    from agent_alfred.runtime.behaviour import BehaviourStore

    path = tmp_path / "behaviour.json"
    if recover:
        path.write_bytes(b"broken configuration")
    written, reads = [], []

    def writer(target, payload, digest):
        write_atomic(target, payload, digest)
        written.append(True)

    def reader(target):
        if target == path and written:
            reads.append(True)
            if len(reads) == failure_read:
                raise OSError("controlled readback failure")
        return read_bytes(target)

    store = BehaviourStore(path, reader=reader, writer=writer)
    with runtime(tmp_path / "host", [], behaviour_store=store) as (host, _, _):
        api = DashboardApi(facade=host)
        initial = api.behaviour()[1]
        status, body = api.mutate_behaviour(
            dict(
                action="recover" if recover else "save",
                expected_revision=initial["revision"],
                fingerprint=initial["fingerprint"],
                enabled=True,
            )
        )
        disk = json.loads(path.read_bytes())
        assert disk["enabled"] is (not recover)
        if failure_read == 1:
            assert status == 503
            assert body["code"] == "settings_write_failed"
            assert store.snapshot()["status"] == "settings_write_unconfirmed"
        else:
            assert status == 200
            assert body["status"] == "ok"
            assert body["enabled"] == disk["enabled"]
            assert body["revision"] == disk["revision"]
        if recover:
            from pathlib import Path

            assert (
                Path(store.snapshot()["backup_path"]).read_bytes()
                == b"broken configuration"
            )


def test_ce05_actual_sqlite_read_failure_stops_before_graph(tmp_path):
    import sqlite3

    from agent_alfred import schema
    from agent_alfred.clock import FakeClock
    from agent_alfred.events import CapturingSink, FanOutSink
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.behaviour import BehaviourStore
    from agent_alfred.runtime.host import RuntimeHost
    from agent_alfred.settings import Settings

    conn = sqlite3.connect(tmp_path / "runs.sqlite3", check_same_thread=False)
    schema.migrate(conn)
    model, capture = ScriptedModel([]), CapturingSink()
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(model),
        clock=FakeClock(),
        settings=Settings(),
        fanout=FanOutSink([capture], process_instance_id="read-fault"),
        process_instance_id="read-fault",
        behaviour_store=BehaviourStore(tmp_path / "behaviour.json"),
    )
    host.start()
    blocked = []

    def deny_history(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and table == "agent_log":
            blocked.append((table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        enable(host)
        session = host.create_session()
        conn.set_authorizer(deny_history)
        _, result = submit(host, "task", session)
        assert blocked
        assert result.outcome == "failed"
        assert model.requests == []
        assert "graph" not in result.memory_telemetry
        assert "routing" not in result.memory_telemetry
        assert not any(e.payload.name == "graph.started" for e in capture.events)
    finally:
        conn.set_authorizer(None)
        assert host.close()
        conn.close()


def test_spec01_entered_fallback_failure_does_not_restart_graph_or_recurse(tmp_path):
    with runtime(
        tmp_path,
        [SKIP, RuntimeError("classifier"), RuntimeError("answer"), "must not run"],
    ) as (host, model, _):
        enable(host)
        _, result = submit(host, "task")
        assert result.outcome == "failed"
        assert len(model.requests) == 3
        assert result.memory_telemetry["routing"]["fallback"]["entered"] is True
        assert (
            result.memory_telemetry["routing"]["fallback"]["reason"] == "graph_failed"
        )


def test_ce12_real_missing_assistant_is_not_intentional_no_reply(tmp_path):
    import sqlite3

    with runtime(tmp_path, [SKIP, "full", "A answer", SKIP, "no_reply"]) as (
        host,
        _,
        _,
    ):
        enable(host)
        first, _ = submit(host, "A")
        quiet, _ = submit(host, "不用回复", first.session_id)
    with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
        conn.execute(
            "DELETE FROM agent_log WHERE run_id=? AND role='assistant'", (first.run_id,)
        )
        assert conn.execute(
            "SELECT role FROM agent_log WHERE run_id=?", (quiet.run_id,)
        ).fetchall() == [("user",)]
    with runtime(tmp_path, [SKIP, "full", "C answer"]) as (host, model, _):
        assert (
            host.memory_service.consolidation.session_status(first.session_id)[
                "unprocessed_count"
            ]
            == 0
        )
        _, result = submit(host, "C", first.session_id)
        exclusions = result.memory_telemetry["input_preparation"]["history_exclusions"]
        assert exclusions["incomplete"] == 1
        assert exclusions["intentional_no_reply"] == 1
        from agent_alfred.messages import message_plain_text

        assert [message_plain_text(m) for m in model.requests[-1].messages] == ["C"]


def test_ce01_enabled_routing_never_routes_probe_or_host_command(tmp_path):
    from agent_alfred.runtime.host import SubmitRequest

    with runtime(tmp_path, ["probe answer"]) as (host, model, capture):
        enable(host)
        accepted = host.submit(
            SubmitRequest(
                "probe", purpose="inference_probe", endpoint_id="test", model_id="m"
            )
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed"
        _, command = submit(host, "查看操作 abc")
        assert "routing" not in result.memory_telemetry
        assert "routing" not in command.memory_telemetry
        assert len(model.requests) == 1
        assert not any(e.payload.name == "graph.started" for e in capture.events)
