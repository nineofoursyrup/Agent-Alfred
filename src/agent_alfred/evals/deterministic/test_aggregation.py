"""AGGREGATION-SPEC-r1: public Host through the real DAG and SQLite."""

import pytest

from agent_alfred.evals.deterministic.test_runtime_skills import runtime


def test_ce02_no_sources_is_zero_step_no_action(tmp_path):
    with runtime(tmp_path, []) as (host, model, sink):
        session = host.create_session()
        accepted = host.aggregate(
            session_id=session,
            goal="整理资料",
            keywords="",
            sources=(),
        )
        assert accepted.kind == "accepted"
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed"
        assert result.reply is None
        assert result.step_count == 0
        assert model.requests == []
        facts = result.memory_telemetry["aggregation"]
        assert facts["graph_result"] == "NoAction"
        assert facts["reason_code"] == "sources_not_selected"
        assert facts["reply_disposition"] == "no_reply"
        skipped = [
            e.envelope.node_id for e in sink.events if e.payload.name == "node.skipped"
        ]
        assert set(skipped) >= {
            "semantic_source",
            "episodic_source",
            "history_source",
            "synthesis",
        }


def test_ce13_no_sources_does_not_create_model_client(tmp_path):
    class NoClient:
        def create(self, snapshot):
            raise AssertionError("client must be delayed until synthesis")

    with runtime(tmp_path, [], factory=NoClient()) as (host, model, _):
        accepted = host.aggregate(
            session_id=host.create_session(), goal="目标", keywords="", sources=()
        )
        assert accepted.kind == "accepted"
        assert host.wait(accepted.run_id).outcome == "completed"


def test_ce13_unavailable_capture_still_allows_no_sources(tmp_path):
    class NoConfig:
        def capture(self, **options):
            raise ValueError("unconfigured")

    with runtime(tmp_path, [], snapshot_provider=NoConfig()) as (host, _, _):
        accepted = host.aggregate(
            session_id=host.create_session(), goal="目标", keywords="", sources=()
        )
        assert accepted.kind == "accepted"
        assert (
            host.wait(accepted.run_id).memory_telemetry["aggregation"]["reason_code"]
            == "sources_not_selected"
        )


def save_fact(host, text="coffee preference", operation="save-fact"):
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.memory.types import ManualOrigin

    result = host.memory_service.execute(
        dict(
            operation_id=operation,
            action="save",
            kind="semantic",
            payload=dict(subject="me", fact=text),
        ),
        CommandContext(ManualOrigin("web"), "web"),
    )
    assert result["status"] == "saved", result
    return result["memory_id"]


def test_ce05_14_selected_memory_is_only_input_and_single_step(tmp_path):
    from agent_alfred.messages import message_plain_text

    with runtime(tmp_path, ["草稿 [[S1]]"]) as (host, model, _):
        memory_id = save_fact(host)
        session = host.create_session()
        accepted = host.aggregate(
            session_id=session,
            goal="整理coffee",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed", result
        assert message_plain_text(result.reply) == "草稿 [[S1]]"
        assert result.step_count == 1
        assert len(model.requests) == 1
        assert model.requests[0].tools == ()
        assert "coffee preference" in message_plain_text(model.requests[0].messages[0])
        facts = result.memory_telemetry["aggregation"]
        assert facts["provided"][0]["memory_id"] == memory_id
        assert facts["sources"]["semantic"]["actual_input_count"] == 1
        assert facts["sources"]["semantic"]["dispatch_state"] == "sent"
        assert facts["sources"]["episodic"]["read_outcome"] == "skipped"


def test_ce03_selected_empty_is_no_matching_sources(tmp_path):
    with runtime(tmp_path, []) as (host, model, _):
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="目标",
            keywords="absent",
            sources=("semantic", "episodic", "history"),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed", result
        assert (
            result.memory_telemetry["aggregation"]["reason_code"]
            == "no_matching_sources"
        )
        assert model.requests == []


def test_ce01_09_real_sqlite_failure_retains_successful_history(tmp_path):
    import sqlite3

    from agent_alfred.evals.deterministic.test_aggregation_repair import episode
    from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, submit

    with runtime(tmp_path, [SKIP, "历史coffee回答", "降级草稿 [[H1]]"]) as (
        host,
        model,
        _,
    ):
        prior, _ = submit(host, "coffee")
        episode(host)
        # Use the public Store scope's actual SQLite connection fault boundary.
        with host.memory_service.reading_stores() as stores:
            conn = stores[0]._conn
            conn.set_authorizer(
                lambda action, arg1, *rest: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and arg1 == "facts"
                    else sqlite3.SQLITE_OK
                )
            )
        try:
            accepted = host.aggregate(
                session_id=prior.session_id,
                goal="整理",
                keywords="coffee",
                sources=("semantic", "episodic", "history"),
            )
            result = host.wait(accepted.run_id)
        finally:
            conn.set_authorizer(None)
        assert result.outcome == "completed", result
        facts = result.memory_telemetry["aggregation"]
        assert facts["graph_result"] == "CompletedWithRecovery"
        assert facts["sources"]["semantic"]["code"] == "read_failed"
        assert facts["sources"]["history"]["actual_input_count"] == 1
        assert facts["sources"]["episodic"]["actual_input_count"] == 1
        assert {p["kind"] for p in facts["provided"]} == {"episodic", "history"}
        with sqlite3.connect(tmp_path / "runs.sqlite3") as check:
            names = check.execute(
                "SELECT tool_name FROM tool_metering WHERE run_id=?", (accepted.run_id,)
            ).fetchall()
            assert sorted(n[0] for n in names) == [
                "aggregation_episodic",
                "aggregation_history",
                "aggregation_semantic",
            ]
        assert len(model.requests) == 3


def test_ce14_http_and_persisted_reply_recovery(tmp_path):
    import time

    from agent_alfred.gateway.web.api import DashboardApi

    with runtime(tmp_path, ["聚合结果 [[S1]]"]) as (host, _, _):
        save_fact(host)
        session = host.create_session()
        api = DashboardApi(facade=host)
        accepted = api.submit(
            dict(
                purpose="aggregation",
                session_id=session,
                message="目标",
                keywords="coffee",
                sources=["semantic"],
            )
        )
        assert accepted.status == 202, accepted
        until = time.monotonic() + 5
        while host.snapshot().active_run is not None and time.monotonic() < until:
            time.sleep(0.005)
        status, reply = api.recover_reply(
            dict(
                process_instance_id="skills-test",
                session_id=session,
                run_id=accepted.run_id,
            )
        )
        assert status == 200, reply
        assert reply["reply_text"] == "聚合结果 [[S1]]"
        pairs = host.mainbar_pairs(session_id=session, limit=20)
        assert len(pairs.items) == 1
        assert pairs.items[0].aggregation["graph_result"] == "Completed"


@pytest.mark.parametrize(
    "text", ["", "  ", "坏 [[S2]]", "坏 [[S01]]", "坏 [[X1]]", "坏 [[S1]", "坏 ]]"]
)
def test_ce07_invalid_draft_is_not_delivered_or_repaired(tmp_path, text):
    with runtime(tmp_path, [text]) as (host, model, _):
        save_fact(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "failed"
        assert result.reply is None
        assert len(model.requests) == 1
        assert len(result.model_results[0].attempts) == 1
        assert (
            result.memory_telemetry["aggregation"]["fallback"]["decision"] == "blocked"
        )


@pytest.mark.parametrize("limit,reason", [(100, "capacity_excluded_all"), (1000, None)])
def test_ce06_complete_memory_capacity(tmp_path, limit, reason):
    from agent_alfred.settings import Settings

    with runtime(
        tmp_path, ["合法 [[S1]]"], settings=Settings(per_store_character_budget=limit)
    ) as (host, model, _):
        save_fact(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        assert result.outcome == "completed", result
        assert facts.get("reason_code") == reason
        assert len(model.requests) == (0 if reason else 1)


def test_ce06_15_last_history_pair_removed_before_step(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, submit
    from agent_alfred.settings import Settings

    with runtime(tmp_path, [SKIP, "history " * 600]) as (host, _, _):
        prior, previous = submit(host, "hello")
        assert previous.outcome == "completed"
    with runtime(tmp_path, [], settings=Settings(input_character_limit=1800)) as (
        host,
        model,
        _,
    ):
        accepted = host.aggregate(
            session_id=prior.session_id, goal="goal", keywords="", sources=("history",)
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed", result
        assert result.step_count == 0
        assert (
            result.memory_telemetry["aggregation"]["reason_code"]
            == "capacity_excluded_all"
        )
        assert (
            result.memory_telemetry["aggregation"]["sources"]["history"][
                "request_excluded"
            ]
            == 1
        )
        assert model.requests == []


def test_ce06_base_input_limit_fails_even_without_sources(tmp_path):
    from agent_alfred.settings import Settings

    with runtime(tmp_path, [], settings=Settings(input_character_limit=100)) as (
        host,
        model,
        _,
    ):
        accepted = host.aggregate(
            session_id=host.create_session(), goal="goal", keywords="", sources=()
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "failed"
        assert result.error == "input_limit_exceeded"
        assert result.step_count == 0
        assert model.requests == []


@pytest.mark.parametrize("selected", [(), ("semantic",)])
def test_ce16_zero_step_budget(tmp_path, selected):
    from agent_alfred.settings import Settings

    with runtime(tmp_path, [], settings=Settings(max_steps=0)) as (host, model, _):
        save_fact(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=selected,
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == ("max_steps" if selected else "completed")
        assert result.step_count == 0
        assert model.requests == []


def test_ce09_structured_payload_survives_summary_boundary(tmp_path):
    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    text = "coffee " + "资料" * 5000
    with runtime(
        tmp_path, ["完整 [[S1]]"], settings=Settings(per_store_character_budget=14000)
    ) as (host, model, _):
        save_fact(host, text)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed", result
        assert text in message_plain_text(model.requests[0].messages[0])


def test_ce04_aggregation_never_reenters_window_or_consolidation(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, submit
    from agent_alfred.messages import message_plain_text

    with runtime(tmp_path, ["草稿标记 [[S1]]", SKIP, "普通答案"]) as (host, model, _):
        save_fact(host)
        session = host.create_session()
        first = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=("semantic",)
        )
        host.wait(first.run_id)
        second = host.aggregate(
            session_id=session, goal="goal", keywords="", sources=("history",)
        )
        assert (
            host.wait(second.run_id).memory_telemetry["aggregation"]["reason_code"]
            == "no_matching_sources"
        )
        assert (
            host.memory_service.consolidation.session_status(session)[
                "unprocessed_count"
            ]
            == 0
        )
        submit(host, "hello", session)
        assert [message_plain_text(m) for m in model.requests[-1].messages] == ["hello"]


@pytest.mark.parametrize(
    "sources",
    [
        ("semantic",),
        ("episodic",),
        ("history",),
        ("semantic", "episodic"),
        ("semantic", "history"),
        ("episodic", "history"),
        ("semantic", "episodic", "history"),
    ],
)
def test_ce14_cli_parsed_command(tmp_path, sources):
    import io

    from agent_alfred.evals.deterministic.test_forgetting import CONTEXT
    from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, submit
    from agent_alfred.gateway.cli import build_parser, run_aggregation_command

    with runtime(tmp_path, [SKIP, "history coffee", "CLI草稿"]) as (host, model, _):
        prior, _ = submit(host, "coffee")
        save_fact(host)
        saved = host.memory_service.execute(
            dict(
                operation_id="episode",
                kind="episodic",
                action="save",
                payload=dict(
                    summary="coffee episode",
                    occurred_at="2026-09-14T10:00:00+08:00",
                    occurred_until=None,
                ),
            ),
            CONTEXT,
        )
        assert saved["status"] == "saved"
        args = build_parser().parse_args(
            [
                "--aggregate",
                "goal",
                "--session",
                prior.session_id,
                "--keywords",
                "coffee",
                "--sources",
                ",".join(sources),
            ]
        )
        out = io.StringIO()
        assert run_aggregation_command(host, args, out) == 0
        assert "聚合草稿" in out.getvalue() and "CLI草稿" in out.getvalue()
        assert len(model.requests) == 3
        draft = next(
            p
            for p in host.mainbar_pairs(session_id=prior.session_id, limit=10).items
            if p.aggregation
        )
        assert len(draft.aggregation["provided"]) == len(sources)


def test_ce12_forgetting_invalidates_provided_identity_without_erasing_original_draft(
    tmp_path,
):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_forgetting import delete
    from agent_alfred.evals.deterministic.test_runtime_skills import submit
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.messages import message_plain_text
    from agent_alfred.trace import RunBundleTraceSink

    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="skills-test",
    )
    with runtime(
        tmp_path,
        [
            '{"retrieve":true,"query":"coffee","reason_code":"personal_information"}',
            "历史回答 coffee",
            "原始草稿 [[S1]]",
        ],
        extra_sinks=(trace,),
    ) as (
        host,
        model,
        _,
    ):
        memory_id = save_fact(host, "coffee private-source-body")
        prior, prior_result = submit(host, "coffee preference")
        assert prior_result.outcome == "completed"
        session = prior.session_id
        accepted = host.aggregate(
            session_id=session,
            goal="goal",
            keywords="coffee",
            sources=("semantic", "history"),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed"
        assert len(result.memory_telemetry["aggregation"]["provided"]) == 2
        assert result.memory_telemetry["input_attempts"][0][
            "working_history_groups"
        ] == [prior.run_id]
        gone = delete(host.memory_service, dict(memory_id=memory_id, record_version=1))
        assert gone.get("status") == "deleted", gone
        state = host.memory_service.forgetting.get_forgetting("delete")
        assert state["state"] == "complete", state
        allowed = host.memory_service.forgetting.evaluate_history(
            (accepted.run_id,), purpose="working_window"
        )
        assert allowed["denied"] == [accepted.run_id]
        page = host.mainbar_pairs(session_id=session, limit=10)
        assert (
            message_plain_text(
                next(
                    p for p in page.items if p.run_id == accepted.run_id
                ).assistant_message
            )
            == "原始草稿 [[S1]]"
        )
        facts = next(p for p in page.items if p.run_id == accepted.run_id).aggregation
        assert facts["provided"][0]["available"] is False
        assert "private-source-body" not in str(facts)
        assert "subject" not in str(facts)
        again = host.aggregate(
            session_id=session,
            goal="goal",
            keywords="coffee",
            sources=("semantic", "history"),
        )
        assert host.wait(again.run_id).reply is None
        assert len(model.requests) == 3


@pytest.mark.parametrize(
    "sources",
    [
        ("semantic",),
        ("episodic",),
        ("history",),
        ("semantic", "episodic"),
        ("semantic", "history"),
        ("episodic", "history"),
        ("semantic", "episodic", "history"),
    ],
)
@pytest.mark.parametrize("routing", [False, True])
def test_ce14_all_selected_combinations_have_one_draft_and_fixed_graph(
    tmp_path, sources, routing
):
    from agent_alfred.evals.deterministic.test_forgetting import CONTEXT
    from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, submit
    from agent_alfred.messages import message_plain_text

    with runtime(tmp_path, [SKIP, "history coffee", "draft"]) as (host, model, _):
        prior, _ = submit(host, "coffee")
        save_fact(host)
        saved = host.memory_service.execute(
            dict(
                operation_id="episode",
                kind="episodic",
                action="save",
                payload=dict(
                    summary="episode coffee",
                    occurred_at="2026-09-14T10:00:00+08:00",
                    occurred_until=None,
                ),
            ),
            CONTEXT,
        )
        assert saved.get("status") == "saved", saved
        host.apply_behaviour(dict(action="save", expected_revision=0, enabled=routing))
        accepted = host.aggregate(
            session_id=prior.session_id, goal="goal", keywords="coffee", sources=sources
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed", result
        facts = result.memory_telemetry["aggregation"]
        assert len(facts["provided"]) == len(sources)
        assert result.step_count == 1
        assert len(model.requests) == 3
        assert all(facts["sources"][k]["actual_input_count"] == 1 for k in sources)
        body = message_plain_text(model.requests[-1].messages[0])
        for kind, marker in (
            ("semantic", "coffee preference"),
            ("episodic", "episode coffee"),
            ("history", "history coffee"),
        ):
            assert (marker in body) == (kind in sources)
        second = host.aggregate(
            session_id=prior.session_id, goal="goal", keywords="", sources=()
        )
        empty = host.wait(second.run_id)
        assert (
            facts["topology_hash"]
            == empty.memory_telemetry["aggregation"]["topology_hash"]
        )


@pytest.mark.parametrize(
    "selected,nonempty", [(False, False), (True, False), (True, True)]
)
def test_ce13_unavailable_model_only_fails_when_material_exists(
    tmp_path, selected, nonempty
):
    class NoConfig:
        def capture(self, **options):
            raise ValueError("unconfigured")

    with runtime(tmp_path, [], snapshot_provider=NoConfig()) as (host, model, _):
        if nonempty:
            save_fact(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",) if selected else (),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == ("failed" if nonempty else "completed")
        assert model.requests == [] and result.reply is None
        if nonempty:
            assert result.error == "model_unavailable"


def test_ce06_long_first_item_is_skipped_without_extra_page(tmp_path):

    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    with runtime(
        tmp_path,
        ["draft [[S1]]"],
        settings=Settings(per_store_limit=2, per_store_character_budget=500),
    ) as (host, model, _):
        save_fact(host, "coffee outside", "outside")
        save_fact(host, "coffee short", "short")
        long_id = save_fact(host, "coffee " + "x" * 2000, "long")
        from agent_alfred.memory.types import FactQuery

        with host.memory_service.reading_stores() as stores:
            assert (
                str(stores[0].search(FactQuery(text="coffee", limit=2))[0].record.id)
                == long_id
            )
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]["sources"]["semantic"]
        assert result.outcome == "completed", result
        assert facts["candidate_count"] == 2 and facts["capacity_excluded"] == 1
        assert facts["actual_input_count"] == 1 and facts["remaining"] == "unknown"
        prompt = message_plain_text(model.requests[0].messages[0])
        assert "coffee short" in prompt and "coffee outside" not in prompt


def test_ce05_previous_skill_and_unselected_memory_do_not_enter_synthesis(tmp_path):
    from agent_alfred.evals.deterministic.test_forgetting import CONTEXT
    from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, skill, submit
    from agent_alfred.messages import message_plain_text

    skill(tmp_path / "builtin", "private", "UNSELECTED_SKILL_BODY")
    with runtime(tmp_path, [SKIP, "UNSELECTED_HISTORY_BODY", "draft [[E1]]"]) as (
        host,
        model,
        _,
    ):
        prior, _ = submit(host, "/skills private\ncoffee")
        save_fact(host, "coffee UNSELECTED_MEMORY_BODY")
        saved = host.memory_service.execute(
            dict(
                operation_id="episode",
                kind="episodic",
                action="save",
                payload=dict(
                    summary="coffee SELECTED_EPISODE",
                    occurred_at="2026-09-14T10:00:00+08:00",
                    occurred_until=None,
                ),
            ),
            CONTEXT,
        )
        assert saved["status"] == "saved"
        run = host.aggregate(
            session_id=prior.session_id,
            goal="goal",
            keywords="coffee",
            sources=("episodic",),
        )
        result = host.wait(run.run_id)
        assert result.outcome == "completed" and len(model.requests) == 3
        request = model.requests[-1]
        text = "\n".join(b.text for b in request.system) + message_plain_text(
            request.messages[0]
        )
        assert "SELECTED_EPISODE" in text
        for marker in (
            "UNSELECTED_SKILL_BODY",
            "UNSELECTED_HISTORY_BODY",
            "UNSELECTED_MEMORY_BODY",
        ):
            assert marker not in text
        assert request.tools == ()
        import sqlite3

        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert conn.execute(
                "SELECT tool_name FROM tool_metering WHERE run_id=?", (run.run_id,)
            ).fetchall() == [("aggregation_episodic",)]
        empty = host.aggregate(
            session_id=prior.session_id, goal="goal", keywords="", sources=()
        )
        assert host.wait(empty.run_id).step_count == 0 and len(model.requests) == 3
