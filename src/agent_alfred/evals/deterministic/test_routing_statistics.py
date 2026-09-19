"""ROUTING-STATS-SPEC-r1 P1/P2: real admission, graph and durable read API."""

from datetime import timedelta

from agent_alfred.clock import FakeClock
from agent_alfred.evals.deterministic.test_message_routing import enable
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, runtime, submit
from agent_alfred.gateway.web.api import DashboardApi


def read(host, clock, window="7d"):
    clock.wall += timedelta(seconds=1)
    status, body = DashboardApi(facade=host).routing_statistics(window)
    assert status == 200, body
    return body


def test_ce01_committed_decisions_and_actual_fallback_have_separate_denominators(
    tmp_path,
):
    clock = FakeClock()
    script = [
        SKIP,
        "unknown",
        "answer",
        SKIP,
        "full",
        RuntimeError("answer failed"),
        "fallback answer",
        SKIP,
        RuntimeError("classifier failed"),
        "fallback answer",
    ]
    with runtime(tmp_path, script, clock=clock) as (host, model, _):
        enable(host)
        for message in ("first", "second", "third"):
            submit(host, message)
        data = read(host, clock)
        assert data["sample"] == dict(
            admitted=3,
            enabled=3,
            disabled=0,
            unknown=0,
            finished=3,
            pending=0,
            unknown_bypass=0,
        )
        group = data["groups"][0]
        assert group["current"] is True
        assert group["decisions"]["counts"] == dict(
            quick=0,
            full=1,
            fallback=1,
            no_action=0,
            context_failure=0,
        )
        assert group["decisions"]["known"] == 2
        assert group["decisions"]["none"] == 1
        assert group["decisions"]["unknown"] == 0
        assert group["fallback"]["yes"] == 2
        assert group["fallback"]["known"] == 3
        assert group["fallback"]["rate"] == dict(
            numerator=2, denominator=3, value=2 / 3
        )
        assert len(model.requests) == 10
        assert read(host, clock)["sample"] == data["sample"]


def test_ce05_legacy_empty_recovery_is_not_always_negative(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "full", "ok"] * 4, clock=clock) as (host, _, _):
        enable(host)
        ids = [submit(host, f"task {i}")[0].run_id for i in range(4)]
        # Explicit schema1 read fixtures derived from baseline chat_graph.py.
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            for run_id, result in zip(
                ids, ("Failed", "BudgetExhausted", "Completed", "NoAction")
            ):
                routing = dict(
                    schema_version=1,
                    settings=dict(status="ok", enabled=True),
                    graph_id="message_routing",
                    graph_result=result,
                    recoveries=[],
                    route="no_action" if result == "NoAction" else "full",
                    classification=dict(status="not_started"),
                    fallback=dict(decision="not_needed", model_requests=0),
                )
                conn.execute(
                    "UPDATE runs SET routing_admission=NULL, telemetry=? "
                    "WHERE run_id=?",
                    (json.dumps(dict(memory=dict(routing=routing))), run_id),
                )
        data = read(host, clock)
        assert data["sample"]["enabled"] == 4
        group = data["groups"][1]
        assert group["comparable"] is False
        assert group["recovered"]["no"] == 2
        assert group["recovered"]["unknown"] == 2
        assert group["fallback"]["unknown"] == 4
        assert group["decisions"]["known"] == 4
        assert group["decisions"]["ratios"]["full"]["value"] is None


def test_ce06_cancelled_sql_lock_wait_releases_worker(tmp_path):
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "full", "ok"] * 2, clock=clock) as (host, _, _):
        enable(host)
        submit(host, "one")
        clock.wall += timedelta(seconds=1)
        cancel = threading.Event()
        seen = threading.Event()

        def cancelled():
            seen.set()
            return cancel.is_set()

        with sqlite3.connect(tmp_path / "runs.sqlite3") as lock:
            lock.execute("PRAGMA journal_mode=DELETE")
            lock.execute("BEGIN EXCLUSIVE")
            with ThreadPoolExecutor() as pool:
                future = pool.submit(
                    DashboardApi(facade=host).routing_statistics,
                    "all",
                    cancelled=cancelled,
                )
                assert seen.wait(2)
                cancel.set()
                assert future.result(timeout=2) == (499, {"code": "query_cancelled"})
            lock.rollback()
        submit(host, "two")
        assert read(host, clock)["sample"]["admitted"] == 2


def test_ce02_ce10_enablement_is_admission_snapshot(tmp_path):
    clock = FakeClock()
    with runtime(
        tmp_path, [SKIP, "ordinary", SKIP, "greeting", SKIP, "ordinary"], clock=clock
    ) as (host, _, _):
        submit(host, "disabled")
        enable(host)
        submit(host, "你好")
        api = DashboardApi(facade=host)
        assert (
            api.mutate_behaviour(
                dict(action="save", expected_revision=1, enabled=False)
            )[0]
            == 200
        )
        submit(host, "disabled again")
        data = read(host, clock)
        assert (data["sample"]["enabled"], data["sample"]["disabled"]) == (1, 2)
        assert data["groups"][0]["decisions"]["counts"]["quick"] == 1


def test_ce02_invalid_settings_unknown_and_actual_bypass(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "behaviour.json").write_text("invalid")
    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "ordinary"], clock=clock) as (host, _, _):
        submit(host, "task")
        sample = read(host, clock)["sample"]
        assert (
            sample["enabled"],
            sample["disabled"],
            sample["unknown"],
            sample["unknown_bypass"],
        ) == (0, 0, 1, 1)


def test_ce02_enabled_preparation_failure_is_explicit_no_decision(tmp_path):
    clock = FakeClock()
    with runtime(tmp_path, [], clock=clock) as (host, model, _):
        enable(host)
        _, result = submit(host, "/skills Missing\ntask")
        assert result.outcome == "failed"
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["none"] == 1
        assert group["fallback"]["no"] == 1
        assert not model.requests


def test_ce08_no_action_and_committed_recovery_are_independent(tmp_path):
    from agent_alfred.runtime.routing import build_routing_graph, project_context

    def build(tools):
        return build_routing_graph(tools, projection=lambda s: project_context({}))

    clock = FakeClock()
    with runtime(
        tmp_path, [SKIP, "no_reply"], clock=clock, routing_graph_builder=build
    ) as (host, _, _):
        enable(host)
        accepted, result = submit(host, "不用回复")
        assert result.reply is None
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["counts"]["no_action"] == 1
        assert group["recovered"]["yes"] == 1
        import sqlite3

        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM agent_log "
                    "WHERE run_id=? AND role='assistant'",
                    (accepted.run_id,),
                ).fetchone()[0]
                == 0
            )


def test_ce04_cancel_after_committed_full_keeps_decision(tmp_path):
    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "full", KeyboardInterrupt()], clock=clock) as (
        host,
        _,
        _,
    ):
        enable(host)
        _, result = submit(host, "task")
        assert result.outcome == "interrupted"
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["counts"]["full"] == 1
        assert group["fallback"]["no"] == 1
        assert group["blocked"]["reasons"] == {"cancelled": 1}


def test_ce07_failed_final_transaction_stays_pending_then_unknown_after_restart(
    tmp_path,
):
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "no_reply"], clock=clock) as (host, _, _):
        enable(host)
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute(
                "CREATE TRIGGER fail_recording BEFORE UPDATE OF phase ON runs "
                "WHEN NEW.phase='finished' "
                "BEGIN SELECT RAISE(ABORT, 'controlled IO fault'); END"
            )
        submit(host, "不用回复")
        data = read(host, clock)
        assert (data["sample"]["pending"], data["sample"]["finished"]) == (1, 0)
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute("DROP TRIGGER fail_recording")
    with runtime(tmp_path, [], clock=clock) as (host, model, _):
        data = read(host, clock)
        assert (data["sample"]["pending"], data["sample"]["finished"]) == (0, 1)
        group = data["groups"][0]
        assert group["decisions"]["unknown"] == 1
        assert group["fallback"]["unknown"] == 1
        assert group["missing_reasons"] == {"summary_missing": 1}
        assert not model.requests


def test_ce11_utc_half_open_window_rolls_without_activity(tmp_path):
    from datetime import datetime, timezone

    boundary = datetime(2026, 9, 20, tzinfo=timezone.utc)
    clock = FakeClock(wall=boundary)
    with runtime(tmp_path, [SKIP, "greeting"] * 5, clock=clock) as (host, _, _):
        enable(host)
        for timestamp in [
            "2026-09-13T00:00:00Z",
            "2026-09-12T23:59:59Z",
            "2026-09-20T00:00:00Z",
            "2026-09-20T07:59:59+08:00",
            "2026-09-19T23:59:59+00:00",
        ]:
            clock.wall = datetime.fromisoformat(timestamp)
            submit(host, "你好")
        clock.wall = boundary
        api = DashboardApi(facade=host)
        assert api.routing_statistics("7d")[1]["sample"]["admitted"] == 3
        clock.wall += timedelta(days=7)
        assert api.routing_statistics("7d")[1]["sample"]["admitted"] == 1
        assert api.routing_statistics("all")[1]["sample"]["admitted"] == 5


def test_ce06_all_reads_more_than_one_hundred_real_runs(tmp_path):
    clock = FakeClock()
    with runtime(
        tmp_path, [SKIP, "greeting"] * 100 + [SKIP, "no_reply"], clock=clock
    ) as (host, _, _):
        enable(host)
        for _ in range(100):
            submit(host, "你好")
        submit(host, "不用回复")
        data = read(host, clock, "all")
        assert data["sample"]["admitted"] == 101
        assert data["groups"][0]["decisions"]["counts"]["no_action"] == 1
        assert data["groups"][0]["decisions"]["counts"]["quick"] == 100


def test_ce13_bad_metric_preserves_others_but_bad_window_fails(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"], clock=clock) as (host, _, _):
        enable(host)
        accepted, _ = submit(host, "你好")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            raw = conn.execute("SELECT telemetry FROM runs").fetchone()[0]
            body = json.loads(raw)
            body["memory"]["routing_statistics"]["route"] = "<img onerror=alert(1)>"
            conn.execute("UPDATE runs SET telemetry=?", (json.dumps(body),))
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["unknown"] == 1
        assert group["fallback"]["no"] == 1
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute(
                "UPDATE runs SET accepted_at='invalid' WHERE run_id=?",
                (accepted.run_id,),
            )
        assert DashboardApi(facade=host).routing_statistics() == (
            503,
            {"code": "statistics_invalid_data"},
        )


def test_ce06_worker_budget_stops_sql_parse_and_result_construction(tmp_path):
    import pytest

    from agent_alfred.routing_statistics.service import StatisticsError, read_statistics

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"] * 10, clock=clock) as (host, _, _):
        enable(host)
        for _ in range(10):
            submit(host, "你好")
        clock.wall += timedelta(seconds=1)
        for phase in ("open", "sql", "parse", "result"):
            work = FakeClock()

            def checkpoint(stage):
                if stage == phase:
                    work.monotonic_value = 3

            with pytest.raises(StatisticsError, match="query_timeout"):
                read_statistics(
                    tmp_path / "runs.sqlite3",
                    window="all",
                    as_of=clock.wall,
                    process_instance_id="test",
                    work_clock=work.monotonic,
                    checkpoint=checkpoint,
                )
            assert read(host, clock)["sample"]["admitted"] == 10


def test_ce06_real_sql_lock_timeout_then_next_business_run(tmp_path):
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"] * 2, clock=clock) as (host, _, _):
        enable(host)
        submit(host, "你好")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("BEGIN EXCLUSIVE")
            assert DashboardApi(facade=host).routing_statistics() == (
                504,
                {"code": "query_timeout"},
            )
            conn.rollback()
        submit(host, "你好")
        assert read(host, clock)["sample"]["admitted"] == 2


def test_ce14_same_snapshot_during_concurrent_finalization(tmp_path):
    import sqlite3

    from agent_alfred.routing_statistics.service import read_statistics

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"] * 2, clock=clock) as (host, _, _):
        enable(host)
        submit(host, "你好")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        clock.wall += timedelta(seconds=1)

        def checkpoint(stage):
            if stage == "snapshot":
                submit(host, "你好")

        data = read_statistics(
            tmp_path / "runs.sqlite3",
            window="all",
            as_of=clock.wall + timedelta(seconds=1),
            process_instance_id="test",
            checkpoint=checkpoint,
        )
        assert data["sample"]["admitted"] == 1
        assert data["groups"][0]["decisions"]["counts"]["quick"] == 1
        assert read(host, clock)["sample"]["admitted"] == 2


def test_ce09_pending_rejected_and_atomic_admission_failure(tmp_path):
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_alfred.runtime.host import SubmitRequest

    at_handoff, release = threading.Event(), threading.Event()

    def handoff(item):
        at_handoff.set()
        assert release.wait(5)
        raise OSError("controlled handoff failure")

    clock = FakeClock()
    with runtime(tmp_path, [], clock=clock, publish_work=handoff) as (host, model, _):
        enable(host)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(host.submit, SubmitRequest("task"))
            try:
                assert at_handoff.wait(5)
                assert read(host, clock)["sample"]["admitted"] == 0
            finally:
                release.set()
            assert future.result().kind == "handoff_failed"
        assert read(host, clock)["sample"]["admitted"] == 0
        assert not model.requests
    with runtime(tmp_path, [], clock=clock) as (host, model, _):
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            conn.execute(
                "CREATE TRIGGER admission_fault "
                "BEFORE UPDATE OF routing_admission ON runs "
                "BEGIN SELECT RAISE(ABORT, 'admission IO fault'); END"
            )
        assert host.submit(SubmitRequest("task")).kind == "admission_failed"
        assert read(host, clock)["sample"]["admitted"] == 0
        assert not model.requests


def test_ce12_future_semantics_and_unknown_format_never_gain_current_percentages(
    tmp_path,
):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"] * 2, clock=clock) as (host, _, _):
        enable(host)
        ids = [submit(host, "你好")[0].run_id for _ in range(2)]
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            for index, run_id in enumerate(ids):
                raw = conn.execute(
                    "SELECT routing_admission FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                evidence = json.loads(raw)
                evidence["policy_version"] = "future-policy"
                if index:
                    evidence["schema_version"] = 999
                conn.execute(
                    "UPDATE runs SET routing_admission=? WHERE run_id=?",
                    (json.dumps(evidence), run_id),
                )
        data = read(host, clock)
        assert data["groups"][0]["current"] is True
        assert data["groups"][0]["total"] == 0
        assert data["groups"][1]["current"] is False
        assert data["groups"][1]["decisions"]["unknown"] == 1
        assert data["groups"][1]["fallback"]["rate"]["value"] is None


def test_ce01_cli_entry_produces_durable_statistics(tmp_path):
    from io import StringIO

    from agent_alfred.gateway.cli import _send

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"], clock=clock) as (host, _, _):
        enable(host)
        assert _send(host, "你好", host.create_session(), StringIO()) == 0
        assert read(host, clock)["groups"][0]["decisions"]["counts"]["quick"] == 1


def test_ce08_recovery_must_commit_before_counting(tmp_path):
    from agent_alfred.events import CapturingSink
    from agent_alfred.graph import NodeOutcome
    from agent_alfred.runtime.routing import build_routing_graph, project_context
    from agent_alfred.settings import Settings

    for stage, expected in [("before", 0), ("during", 0), ("after", 1)]:
        clock = FakeClock()

        class StopAtBoundary(CapturingSink):
            def commit(self, prepared, event):
                super().commit(prepared, event)
                if (
                    event.payload.name == "node.finished"
                    and event.envelope.node_id
                    == ("project_context" if stage == "before" else "recover_context")
                    and stage != "during"
                ):
                    clock.monotonic_value = 60

        def recovery(state, context):
            if stage == "during":
                clock.monotonic_value = 60
            return NodeOutcome({"context_recovery": {"unavailable": True}})

        def build(tools):
            return build_routing_graph(
                tools, projection=lambda s: project_context({}), recovery=recovery
            )

        with runtime(
            tmp_path / stage,
            [SKIP, "no_reply"],
            clock=clock,
            settings=Settings(overall_deadline_s=30),
            extra_sinks=(StopAtBoundary(),),
            routing_graph_builder=build,
        ) as (host, _, _):
            enable(host)
            _, result = submit(host, "不用回复")
            assert result.outcome == "failed"
            group = read(host, clock)["groups"][0]
            assert group["recovered"]["yes"] == expected
            assert group["recovered"]["no"] == 1 - expected
            assert group["decisions"]["none"] == 1


def test_ce04_stop_before_or_after_actual_fallback_entry(tmp_path):
    for stage, expected in [("before", 0), ("entered", 1)]:
        clock = FakeClock()

        def checkpoint(actual):
            if actual == stage:
                raise KeyboardInterrupt()

        with runtime(
            tmp_path / stage,
            [SKIP, RuntimeError("classifier")],
            clock=clock,
            routing_fallback_checkpoint=checkpoint,
        ) as (host, model, _):
            enable(host)
            _, result = submit(host, "task")
            assert result.outcome == "interrupted"
            group = read(host, clock)["groups"][0]
            assert group["fallback"]["yes"] == expected
            assert group["fallback"]["no"] == 1 - expected
            assert len(model.requests) == 2


def test_ce05_all_legacy_evidence_forms_are_conservative(tmp_path):
    import json
    import sqlite3

    cases = [
        ({"graph_result": "Failed", "recoveries": []}, None, None, None),
        ({"graph_result": "BudgetExhausted", "recoveries": []}, None, None, None),
        ({"graph_result": "Completed"}, None, None, None),
        ({"graph_result": "Completed", "recoveries": []}, False, None, None),
        ({"graph_result": "NoAction", "recoveries": []}, False, None, None),
        ({"graph_result": "CompletedWithRecovery", "recoveries": []}, None, None, None),
        (
            {
                "graph_result": "Failed",
                "recoveries": [
                    dict(
                        node_id="project_context",
                        code="node_failed",
                        message="ContextInvalid",
                        side_effect_state="none",
                    )
                ],
            },
            True,
            None,
            None,
        ),
        (
            {
                "graph_result": "Completed",
                "recoveries": [
                    dict(
                        node_id="other",
                        code="node_failed",
                        message="x",
                        side_effect_state="none",
                    )
                ],
            },
            None,
            None,
            None,
        ),
        (
            {
                "graph_result": "Failed",
                "fallback": dict(decision="not_needed", model_requests=0),
            },
            None,
            None,
            None,
        ),
        (
            {
                "graph_result": "Completed",
                "fallback": dict(
                    decision="not_needed", entered=False, model_requests=0
                ),
            },
            None,
            False,
            False,
        ),
        (
            {
                "graph_result": "Failed",
                "fallback": dict(
                    decision="allowed",
                    reason="graph_failed",
                    entered=True,
                    model_requests=0,
                ),
            },
            None,
            True,
            False,
        ),
        (
            {
                "graph_id": None,
                "fallback": dict(
                    decision="allowed",
                    reason="routing_unavailable",
                    entered=True,
                    model_requests=0,
                ),
            },
            None,
            False,
            True,
        ),
        (
            {
                "graph_result": "Failed",
                "fallback": dict(
                    decision="allowed", reason="unknown", entered=True, model_requests=0
                ),
            },
            None,
            None,
            None,
        ),
        (
            {
                "graph_result": "Failed",
                "fallback": dict(
                    decision="blocked",
                    reason="side_effect_occurred",
                    entered=False,
                    model_requests=0,
                ),
            },
            None,
            False,
            False,
        ),
        (
            {"schema_version": 999, "graph_result": "Completed", "recoveries": []},
            None,
            None,
            None,
        ),
    ]
    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"], clock=clock) as (host, _, _):
        enable(host)
        submit(host, "你好")
        clock.wall += timedelta(seconds=1)
        for changes, r, f, b in cases:
            routing = dict(
                schema_version=1,
                graph_id="message_routing",
                settings=dict(status="ok", enabled=True),
                classification=dict(status="not_started"),
            )
            routing.update(changes)
            with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
                conn.execute(
                    "UPDATE runs SET routing_admission=NULL, telemetry=?",
                    (json.dumps(dict(memory=dict(routing=routing))),),
                )
            data = DashboardApi(facade=host).routing_statistics()[1]
            if routing["schema_version"] == 999:
                assert data["sample"]["unknown"] == 1
                continue
            group = data["groups"][1]
            for metric, expected in [("recovered", r), ("fallback", f), ("bypass", b)]:
                key = (
                    "yes"
                    if expected is True
                    else "no"
                    if expected is False
                    else "unknown"
                )
                assert group[metric][key] == 1, (changes, metric, group)
            assert group["decisions"]["unknown"] == 1
            assert group["fallback"]["rate"]["value"] is None


def test_ce13_contradictory_current_evidence_does_not_choose_convenient_truth(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"], clock=clock) as (host, _, _):
        enable(host)
        submit(host, "你好")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            raw = json.loads(conn.execute("SELECT telemetry FROM runs").fetchone()[0])
            stats = raw["memory"]["routing_statistics"]
            stats.update(fallback=True, bypass=True)
            stats.pop("recovered")
            conn.execute("UPDATE runs SET telemetry=?", (json.dumps(raw),))
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["counts"]["quick"] == 1
        assert group["fallback"]["unknown"] == group["bypass"]["unknown"] == 1
        assert group["recovered"]["unknown"] == 1


def test_ce13_missing_route_is_not_an_explicit_no_decision(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"], clock=clock) as (host, _, _):
        enable(host)
        submit(host, "你好")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            body = json.loads(conn.execute("SELECT telemetry FROM runs").fetchone()[0])
            del body["memory"]["routing_statistics"]["route"]
            conn.execute("UPDATE runs SET telemetry=?", (json.dumps(body),))
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["unknown"] == 1
        assert group["decisions"]["none"] == 0


def test_ce08_cancellation_after_committed_recovery_keeps_yes(tmp_path):
    from agent_alfred.events import CapturingSink
    from agent_alfred.runtime.routing import build_routing_graph, project_context

    class Cancel(CapturingSink):
        def commit(self, prepared, event):
            super().commit(prepared, event)
            if (
                event.payload.name == "node.finished"
                and event.envelope.node_id == "recover_context"
            ):
                raise KeyboardInterrupt()

    def build(tools):
        return build_routing_graph(tools, projection=lambda s: project_context({}))

    clock = FakeClock()
    with runtime(
        tmp_path,
        [SKIP, "no_reply"],
        clock=clock,
        extra_sinks=(Cancel(),),
        routing_graph_builder=build,
    ) as (host, _, _):
        enable(host)
        _, result = submit(host, "不用回复")
        assert result.outcome == "interrupted"
        assert read(host, clock)["groups"][0]["recovered"]["yes"] == 1


def test_ce12_model_assignment_change_does_not_split_semantics(tmp_path):
    from agent_alfred.runtime.config import MutableAssignmentProvider
    from agent_alfred.settings import Settings

    clock = FakeClock()
    for model_id in ("first-model", "other-model"):
        provider = MutableAssignmentProvider(
            endpoint_id="test",
            model_id=model_id,
            wire_style="openai",
            api_key="test-key",
            settings=Settings(),
        )
        with runtime(
            tmp_path, [SKIP, "greeting"], clock=clock, snapshot_provider=provider
        ) as (host, model, _):
            if not host.behaviour()["enabled"]:
                enable(host)
            submit(host, "你好")
            assert model.requests[0].model.model_id == model_id
            data = read(host, clock)
    assert len(data["groups"]) == 1
    assert data["groups"][0]["total"] == 2


def test_review_std01_worker_is_owned_during_constructor_interrupt(
    tmp_path, monkeypatch
):
    import subprocess

    import pytest

    from agent_alfred.routing_statistics.service import query_statistics

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "greeting"] * 2, clock=clock) as (host, _, _):
        enable(host)
        submit(host, "你好")
        children = []
        original = subprocess.Popen.__init__

        def interrupt(child, *args, **kwargs):
            original(child, *args, **kwargs)
            children.append(child)
            raise KeyboardInterrupt("real child created before constructor return")

        try:
            with monkeypatch.context() as patch:
                patch.setattr(subprocess.Popen, "__init__", interrupt)
                with pytest.raises(KeyboardInterrupt):
                    query_statistics(
                        str(tmp_path / "runs.sqlite3"),
                        window="all",
                        as_of=clock.wall,
                        process_instance_id="test",
                    )
            assert len(children) == 1
            child = children[0]
            assert child.poll() is not None
            assert all(
                getattr(child, name).closed for name in ("stdin", "stdout", "stderr")
            )
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()
                for name in ("stdin", "stdout", "stderr"):
                    getattr(child, name).close()
        submit(host, "你好")
        assert read(host, clock)["sample"]["admitted"] == 2


def test_review_spec01_conflicts_invalidate_dependent_axes_only(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "full", "answer"], clock=clock) as (host, _, _):
        enable(host)
        submit(host, "task")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            original = json.loads(
                conn.execute("SELECT telemetry FROM runs").fetchone()[0]
            )
        cases = [
            ({"graph_entered": False, "recovered": True}, ("decisions", "recovered")),
            ({"fallback": True, "blocked": "side_effect_occurred"}, ("fallback",)),
            (
                {"bypass": True, "graph_entered": False, "blocked": "cancelled"},
                ("decisions", "bypass"),
            ),
        ]
        for changes, unknown in cases:
            body = json.loads(json.dumps(original))
            body["memory"]["routing_statistics"].update(changes)
            with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
                conn.execute("UPDATE runs SET telemetry=?", (json.dumps(body),))
            data = read(host, clock)
            assert data["sample"]["finished"] == 1
            group = data["groups"][0]
            for metric in unknown:
                assert group[metric]["unknown"] == 1, (changes, group)
            if "decisions" not in unknown:
                assert group["decisions"]["counts"]["full"] == 1
            if "recovered" not in unknown:
                assert group["recovered"]["no"] == 1
            assert group["blocked"]["total"] == 0


def test_ce06_http_disconnect_reaps_worker_then_business_can_commit(
    tmp_path, monkeypatch
):
    import socket
    import sqlite3
    import subprocess
    import threading

    from agent_alfred.evals.deterministic._web_lifecycle_test_helpers import (
        free_loopback_port,
    )
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.wiring import build_dashboard

    children, spawned, reaped = [], threading.Event(), threading.Event()
    original_init, original_wait = subprocess.Popen.__init__, subprocess.Popen.wait

    def observe(child, *args, **kwargs):
        original_init(child, *args, **kwargs)
        children.append(child)
        spawned.set()

    def wait(child, *args, **kwargs):
        result = original_wait(child, *args, **kwargs)
        if child in children:
            reaped.set()
        return result

    dashboard = build_dashboard(
        state_dir=tmp_path,
        port=free_loopback_port(),
        factory=ScriptedModelFactory(ScriptedModel([SKIP, "ordinary"])),
    )
    dashboard.start()
    try:
        with (
            monkeypatch.context() as patch,
            sqlite3.connect(tmp_path / "db.sqlite3") as conn,
        ):
            patch.setattr(subprocess.Popen, "__init__", observe)
            patch.setattr(subprocess.Popen, "wait", wait)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("BEGIN EXCLUSIVE")
            sock = socket.create_connection(("127.0.0.1", dashboard.port))
            sock.sendall(
                (
                    "GET /api/behaviour/routing-statistics?window=all HTTP/1.1\r\n"
                    f"Host: localhost:{dashboard.port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            )
            assert spawned.wait(3)
            sock.close()
            assert reaped.wait(3)
            conn.rollback()
        assert children and all(child.poll() is not None for child in children)
        accepted = dashboard.host.submit(SubmitRequest("next"))
        assert accepted.kind == "accepted"
        assert dashboard.host.wait(accepted.run_id).outcome == "completed"
        assert DashboardApi(facade=dashboard.host).routing_statistics("all")[0] == 200
    finally:
        assert dashboard.close()
    assert all(
        getattr(child, name).closed
        for child in children
        for name in ("stdin", "stdout", "stderr")
    )


def test_ac05_positive_context_failure_decision(tmp_path):
    from agent_alfred.runtime.routing import build_routing_graph, project_context

    clock = FakeClock()
    with runtime(
        tmp_path,
        [SKIP, "full"],
        clock=clock,
        routing_graph_builder=lambda tools: build_routing_graph(
            tools, projection=lambda s: project_context({})
        ),
    ) as (host, _, _):
        enable(host)
        submit(host, "task")
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["counts"]["context_failure"] == 1
        assert group["recovered"]["yes"] == 1
        assert group["fallback"]["no"] == 1


def test_review_std01_interrupt_actual_constructor_return_instruction(monkeypatch):
    import dis
    import subprocess
    from datetime import datetime, timezone

    import pytest

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.routing_statistics.service import query_statistics

    children = []
    original = subprocess.Popen.__init__

    def observe(child, *args, **kwargs):
        original(child, *args, **kwargs)
        children.append(child)

    instructions = list(dis.get_instructions(query_statistics))
    incoming = next(
        i
        for i, ins in enumerate(instructions)
        if ins.opname == "STORE_FAST" and ins.argval == "incoming"
    )
    edge = instructions[incoming - 2]
    assert edge.opname == "POP_TOP"
    monkeypatch.setattr(subprocess.Popen, "__init__", observe)
    try:
        with interrupt_instruction_once(
            query_statistics.__code__,
            edge.offset,
            KeyboardInterrupt("constructor returned"),
        ) as armed:
            with pytest.raises(KeyboardInterrupt):
                query_statistics(
                    "/unused-before-input.sqlite3",
                    window="all",
                    as_of=datetime.now(timezone.utc),
                    process_instance_id="test",
                )
        assert armed == [False] and len(children) == 1
        assert children[0].poll() is not None
        assert all(
            getattr(children[0], name).closed for name in ("stdin", "stdout", "stderr")
        )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
            for name in ("stdin", "stdout", "stderr"):
                getattr(child, name).close()


def test_review_spec01_legacy_conflicting_route_is_unknown(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "no_reply"], clock=clock) as (host, _, _):
        enable(host)
        submit(host, "不用回复")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            body = json.loads(conn.execute("SELECT telemetry FROM runs").fetchone()[0])
            body["memory"]["routing"]["route"] = "full"
            conn.execute(
                "UPDATE runs SET routing_admission=NULL, telemetry=?",
                (json.dumps(body),),
            )
        group = read(host, clock)["groups"][1]
        assert group["decisions"]["unknown"] == 1
        assert group["recovered"]["no"] == 1
        assert group["fallback"]["no"] == 1


def test_review_spec01_conflicting_supported_sources_are_not_arbitrated(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    with runtime(tmp_path, [SKIP, "full", "answer"], clock=clock) as (host, _, _):
        enable(host)
        submit(host, "task")
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            body = json.loads(conn.execute("SELECT telemetry FROM runs").fetchone()[0])
            body["memory"]["routing"]["route"] = "quick"
            body["memory"]["routing_statistics"]["recovered"] = True
            conn.execute("UPDATE runs SET telemetry=?", (json.dumps(body),))
        group = read(host, clock)["groups"][0]
        assert group["decisions"]["unknown"] == 1
        assert group["recovered"]["unknown"] == 1
        assert group["fallback"]["no"] == 1


def test_review_std01_cleanup_instruction_interruptions_release_real_worker(
    monkeypatch,
):
    import dis
    import subprocess
    from datetime import datetime, timezone

    import pytest

    from agent_alfred.evals.deterministic._monitoring_test_helpers import (
        interrupt_instruction_once,
    )
    from agent_alfred.routing_statistics import service

    children = []
    original = subprocess.Popen.__init__

    def observe(child, *args, **kwargs):
        original(child, *args, **kwargs)
        children.append(child)

    monkeypatch.setattr(subprocess.Popen, "__init__", observe)
    for operation in ("kill", "wait", "close"):
        code = getattr(service, "_close_worker", service.query_statistics).__code__
        edge = [
            i
            for i in dis.get_instructions(code)
            if i.opname == "LOAD_ATTR" and i.argval == operation
        ][-1]
        try:
            with interrupt_instruction_once(
                code, edge.offset, KeyboardInterrupt(operation)
            ) as armed:
                with pytest.raises(KeyboardInterrupt, match=operation):
                    service.query_statistics(
                        "/unused.sqlite3",
                        window="all",
                        as_of=datetime.now(timezone.utc),
                        process_instance_id="test",
                        cancelled=lambda: True,
                    )
            assert armed == [False]
            assert children[-1].poll() is not None
            assert all(
                getattr(children[-1], name).closed
                for name in ("stdin", "stdout", "stderr")
            )
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()
                for name in ("stdin", "stdout", "stderr"):
                    getattr(child, name).close()


def test_review_spec01_explicit_no_decision_and_blocked_reasons_reconcile(tmp_path):
    import json
    import sqlite3

    clock = FakeClock()
    for case in ("none", "blocked", "absent_blocked"):
        path = tmp_path / case
        with runtime(
            path,
            [SKIP, "full", "answer" if case == "none" else KeyboardInterrupt("cancel")],
            clock=clock,
        ) as (host, _, _):
            enable(host)
            submit(host, "task")
            with sqlite3.connect(path / "runs.sqlite3") as conn:
                body = json.loads(
                    conn.execute("SELECT telemetry FROM runs").fetchone()[0]
                )
                if case == "none":
                    body["memory"]["routing_statistics"]["route"] = None
                else:
                    body["memory"]["routing_statistics"]["blocked"] = (
                        "side_effect_occurred" if case == "blocked" else None
                    )
                conn.execute("UPDATE runs SET telemetry=?", (json.dumps(body),))
            group = read(host, clock)["groups"][0]
            if case == "none":
                assert group["decisions"]["unknown"] == 1
                assert group["decisions"]["none"] == 0
                assert group["blocked"]["total"] == 0
            else:
                assert group["decisions"]["counts"]["full"] == 1
                assert group["blocked"]["total"] == 0
                assert group["blocked"]["reasons"] == {}
                assert group["missing_reasons"]["invalid_or_missing_metric"] == 1
            assert (
                group["fallback"]["no"]
                == group["recovered"]["no"]
                == group["bypass"]["no"]
                == 1
            )


def test_review_std01_persistent_cleanup_retains_owner_for_read_or_host_close(
    tmp_path, monkeypatch
):
    import subprocess

    import pytest

    from agent_alfred.resource_rollback import RollbackSlot
    from agent_alfred.routing_statistics.service import StatisticsError

    original_init, original_kill = subprocess.Popen.__init__, subprocess.Popen.kill
    for continuation in ("read", "close"):
        clock = FakeClock()
        children = []

        def observe(child, *args, **kwargs):
            original_init(child, *args, **kwargs)
            children.append(child)

        def refuse(child):
            raise OSError("controlled kill failure")

        with runtime(tmp_path / continuation, [SKIP, "greeting"] * 2, clock=clock) as (
            host,
            _,
            _,
        ):
            enable(host)
            submit(host, "你好")
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(subprocess.Popen, "__init__", observe)
                    patch.setattr(subprocess.Popen, "kill", refuse)
                    with pytest.raises(StatisticsError) as raised:
                        DashboardApi(facade=host).routing_statistics(
                            cancelled=lambda: True
                        )
                reachable = RollbackSlot()
                reachable.capture_failure(raised.value)
                assert not reachable.settled
                del reachable
                assert children[-1].poll() is None
                raised.value.__traceback__ = None
                raised.value.__context__ = None
                del raised
                if continuation == "read":
                    assert read(host, clock)["sample"]["admitted"] == 1
                    submit(host, "你好")
                    assert read(host, clock)["sample"]["admitted"] == 2
                else:
                    assert host.close()
                assert all(child.poll() is not None for child in children)
                assert all(
                    getattr(child, name).closed
                    for child in children
                    for name in ("stdin", "stdout", "stderr")
                )
            finally:
                for child in children:
                    if child.poll() is None:
                        original_kill(child)
                    child.wait()
                    for name in ("stdin", "stdout", "stderr"):
                        getattr(child, name).close()
