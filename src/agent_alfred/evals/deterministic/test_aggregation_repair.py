"""Reviewer counterexamples through approved public aggregation boundaries."""

import pytest

from agent_alfred.evals.deterministic.test_forgetting import CONTEXT
from agent_alfred.evals.deterministic.test_runtime_skills import SKIP, runtime, submit


def episode(host):
    value = host.memory_service.execute(
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
    assert value["status"] == "saved"
    return value["memory_id"]


@pytest.mark.parametrize(
    "end", [{"invalid": 1}, 1, [], True, None, "2026-09-14T11:00:00+08:00"]
)
def test_spec02_episode_end_type_is_validated_before_source_commit(tmp_path, end):
    class Source:
        def __init__(self, real):
            self.real = real

        def read(self, kind, request, context):
            value = self.real.read(kind, request, context)
            value["items"][0]["content"]["ended_at"] = end
            return value

    valid = end is None or isinstance(end, str)
    with runtime(tmp_path, ["draft [[E1]]"], aggregation_sources=Source) as (
        host,
        model,
        _,
    ):
        episode(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("episodic",),
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        if valid:
            assert facts["graph_result"] == "Completed"
            assert len(model.requests) == 1
        else:
            assert facts["reason_code"] == "sources_unavailable"
            assert facts["sources"]["episodic"]["code"] == "invalid_source_result"
            assert not model.requests and result.step_count == 0


@pytest.mark.parametrize("body", [{"bad": 1}, 1, [], True, None])
def test_spec02_history_nested_text_is_validated_without_coercion(tmp_path, body):
    class Source:
        def __init__(self, real):
            self.real = real

        def read(self, kind, request, context):
            value = self.real.read(kind, request, context)
            value["items"][0]["content"]["assistant"][0]["text"] = body
            return value

    with runtime(
        tmp_path, [SKIP, "history", "must not send"], aggregation_sources=Source
    ) as (host, model, _):
        prior, _ = submit(host, "coffee")
        accepted = host.aggregate(
            session_id=prior.session_id, goal="goal", keywords="", sources=("history",)
        )
        result = host.wait(accepted.run_id)
        assert (
            result.memory_telemetry["aggregation"]["reason_code"]
            == "sources_unavailable"
        )
        assert len(model.requests) == 2 and result.step_count == 0


def test_spec03_ce15_history_trim_keeps_memory_and_synthesizes(tmp_path):
    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    with runtime(tmp_path, [SKIP, "history " * 600]) as (host, _, _):
        prior, _ = submit(host, "hello")
        save_fact(host)
    with runtime(
        tmp_path, ["draft [[S1]]"], settings=Settings(input_character_limit=1800)
    ) as (host, model, _):
        accepted = host.aggregate(
            session_id=prior.session_id,
            goal="goal",
            keywords="coffee",
            sources=("semantic", "history"),
        )
        result = host.wait(accepted.run_id)
        assert result.outcome == "completed" and result.step_count == 1
        facts = result.memory_telemetry["aggregation"]
        assert facts["sources"]["history"]["request_excluded"] == 1
        assert [p["kind"] for p in facts["provided"]] == ["semantic"]
        text = message_plain_text(model.requests[0].messages[0])
        assert "coffee preference" in text and "history history" not in text


def test_spec03_ce16_local_timeout_keeps_other_two_sources(tmp_path):
    calls = []

    class Source:
        def __init__(self, real):
            self.real = real

        def read(self, kind, request, context):
            calls.append(kind)
            if kind == "semantic":
                raise TimeoutError()
            return self.real.read(kind, request, context)

    with runtime(
        tmp_path, [SKIP, "history coffee", "draft"], aggregation_sources=Source
    ) as (host, model, _):
        prior, _ = submit(host, "coffee")
        episode(host)
        accepted = host.aggregate(
            session_id=prior.session_id,
            goal="goal",
            keywords="coffee",
            sources=("semantic", "episodic", "history"),
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        assert result.outcome == "completed" and result.step_count == 1
        assert facts["graph_result"] == "CompletedWithRecovery"
        assert facts["sources"]["semantic"]["code"] == "local_timeout"
        assert {p["kind"] for p in facts["provided"]} == {"episodic", "history"}
        assert (
            sorted(calls) == ["episodic", "history", "semantic"]
            and len(model.requests) == 3
        )


def test_spec03_ce07_terminal_http_failure_preserves_attempts_without_fallback(
    tmp_path,
):
    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.evals.deterministic.test_aggregation_safety import wire_runtime
    from agent_alfred.settings import Settings

    with wire_runtime(tmp_path, fail_always=True, settings=Settings(max_steps=1)) as (
        host,
        sent,
        _,
        _,
    ):
        save_fact(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("semantic",),
        )
        result = host.wait(accepted.run_id)
        assert (
            result.outcome == "failed"
            and result.reply is None
            and result.step_count == 1
        )
        assert (
            len(sent) == 2 and sum(len(r.attempts) for r in result.model_results) == 2
        )
        assert all(
            a.outcome == "aborted" for r in result.model_results for a in r.attempts
        )
        assert (
            result.memory_telemetry["aggregation"]["fallback"]["decision"] == "blocked"
        )
        assert (
            len(host.mainbar_pairs(session_id=accepted.session_id, limit=10).items) == 1
        )


@pytest.mark.parametrize(
    "change", ["delete_memory", "isolate_history", "add_unselected"]
)
@pytest.mark.parametrize("at", [1, 2])
def test_spec03_ce10_identity_permission_and_unselected_changes(tmp_path, change, at):
    import sqlite3

    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.evals.deterministic.test_aggregation_safety import wire_runtime
    from agent_alfred.evals.deterministic.test_memory_commands import service

    gate = '{"retrieve":true,"query":"coffee","reason_code":"personal_information"}'
    with runtime(tmp_path, [gate, "history coffee"]) as (host, _, _):
        memory_id = save_fact(host)
        prior, result = submit(host, "coffee preference")
        assert result.outcome == "completed"
        assert result.memory_telemetry["input_attempts"][-1]["references"]
    seen = []

    def barrier(run, attempt):
        seen.append(attempt)
        if len(seen) != at:
            return
        # Independent public Store/Forgetting writer at the approved P3 fault
        # seam. The real HTTP MutationGate 409 is covered in the browser test.
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            writer = service(conn)
            if change == "add_unselected":
                value = writer.execute(
                    dict(
                        operation_id="added",
                        kind="episodic",
                        action="save",
                        payload=dict(
                            summary="NEW_UNSELECTED",
                            occurred_at="2026-09-14T10:00:00+08:00",
                            occurred_until=None,
                        ),
                    ),
                    CONTEXT,
                )
                assert value["status"] == "saved"
            else:
                value = writer.execute(
                    dict(
                        operation_id="removed",
                        kind="semantic",
                        action="delete",
                        expected_version=1,
                        payload=dict(id=memory_id),
                    ),
                    CONTEXT,
                )
                assert value["status"] == "deleted", value
                if change == "isolate_history":
                    assert writer.forgetting.evaluate_history(
                        (prior.run_id,), purpose="working_window"
                    )["denied"] == [prior.run_id]

    with wire_runtime(tmp_path, before_send=barrier, fail_first=True) as (
        host,
        sent,
        _,
        _,
    ):
        accepted = host.aggregate(
            session_id=prior.session_id,
            goal="goal",
            keywords="coffee",
            sources=("history",) if change == "isolate_history" else ("semantic",),
        )
        result = host.wait(accepted.run_id)
        expected = 2 if change == "add_unselected" else at - 1
        assert len(sent) == expected
        assert sum(len(r.attempts) for r in result.model_results) == expected
        assert result.outcome == (
            "completed" if change == "add_unselected" else "failed"
        )
        assert len(result.memory_telemetry["input_attempts"]) == expected
        if change == "add_unselected":
            assert sent[0] == sent[1] and "NEW_UNSELECTED" not in str(sent)
        with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM tool_metering WHERE run_id=?", (accepted.run_id,)
            ).fetchone() == (1,)


def test_spec03_ce04_actual_preparation_excludes_drafts_but_explicit_chat_qualifies(
    tmp_path, monkeypatch
):
    import sqlite3

    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.evals.deterministic.test_consolidation_scheduling import (
        _chat,
        _observe_settlement,
        _system,
    )
    from agent_alfred.evals.deterministic.test_memory_consolidation_service import PLAN
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.settings import Settings
    from agent_alfred.trace import RunBundleTraceSink

    clock = FakeClock()
    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=clock,
        process_instance_id="skills-test",
    )
    with runtime(
        tmp_path,
        ["原始草稿 [[S1]]", SKIP, "对显式讨论的回答", PLAN],
        clock=clock,
        settings=Settings(consolidation_source_threshold=1),
        extra_sinks=(trace,),
    ) as (host, model, _):
        with sqlite3.connect(
            tmp_path / "runs.sqlite3", check_same_thread=False
        ) as conn:
            settled = _observe_settlement(host, monkeypatch, conn)
            save_fact(host)
            session = host.create_session()
            draft = host.aggregate(
                session_id=session,
                goal="AGGREGATE_ONLY_GOAL",
                keywords="coffee",
                sources=("semantic",),
            )
            assert host.wait(draft.run_id).outcome == "completed"
            assert len(model.requests) == 1
            assert conn.execute(
                "SELECT count(*) FROM runs WHERE purpose='consolidation'"
            ).fetchone() == (0,)
            preparation = host.generate_consolidation(session)
            assert preparation.kind == "accepted"
            assert host.wait(preparation.run_id).step_count == 0
            _system(conn, settled)
            assert len(model.requests) == 1
            assert (
                host.memory_service.consolidation.session_status(session)[
                    "unprocessed_count"
                ]
                == 0
            )
            chat = _chat(host, settled, session, "请讨论这个草稿：原始草稿 [[S1]]")
            _system(conn, settled)
            assert len(model.requests) == 4
            assert conn.execute(
                "SELECT run_id FROM memory_consolidation_source_results"
            ).fetchall() == [(chat.run_id,)]
            prompt = str(model.requests[-1])
            assert "请讨论这个草稿" in prompt
            assert draft.run_id not in prompt and "AGGREGATE_ONLY_GOAL" not in prompt


def _sized_store(tmp_path, kind, count):
    import json

    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    with runtime(
        tmp_path,
        ["draft"],
        settings=Settings(per_store_limit=count, per_store_character_budget=50000),
    ) as (host, model, _):
        for n in range(count):
            if kind == "semantic":
                save_fact(host, "coffee Unicode中文 🐈 " + str(n), "fact-" + str(n))
            else:
                value = host.memory_service.execute(
                    dict(
                        operation_id="episode-" + str(n),
                        kind="episodic",
                        action="save",
                        payload=dict(
                            summary="coffee Unicode中文 🐈 " + str(n),
                            occurred_at="2026-09-14T10:00:00+08:00",
                            occurred_until=None,
                        ),
                    ),
                    CONTEXT,
                )
                assert value["status"] == "saved"
        session = host.create_session()
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        assert host.wait(accepted.run_id).outcome == "completed"
        supplied = json.loads(message_plain_text(model.requests[0].messages[0]))[
            "provided_sources"
        ]
        assert len(supplied) == count
        return session, supplied


@pytest.mark.parametrize("kind", ["semantic", "episodic"])
@pytest.mark.parametrize("count", [1, 2, 11])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_spec04_ce06_full_numbered_store_encoding_boundary(
    tmp_path, kind, count, delta
):
    import json

    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    session, supplied = _sized_store(tmp_path, kind, count)
    # Measure the real delivered per-store array, including Unicode, labels,
    # brackets and commas, independently of production's sizing function.
    budget = (
        len(
            json.dumps(
                supplied, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        + delta
    )
    expected = count - 1 if delta < 0 else count
    with runtime(
        tmp_path,
        ["draft"],
        settings=Settings(per_store_limit=count, per_store_character_budget=budget),
    ) as (host, model, _):
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        assert result.outcome == "completed"
        assert facts["sources"][kind]["capacity_excluded"] == count - expected
        assert facts["sources"][kind]["actual_input_count"] == expected
        assert result.step_count == int(expected > 0)
        if expected:
            actual = json.loads(message_plain_text(model.requests[0].messages[0]))[
                "provided_sources"
            ]
            assert actual == supplied[:expected]
            assert (
                len(
                    json.dumps(
                        actual,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                <= budget
            )
            assert len(model.requests) == 1
        else:
            assert facts["reason_code"] == "capacity_excluded_all"
            assert not model.requests and result.reply is None


@pytest.mark.parametrize("kind", ["semantic", "episodic"])
def test_spec04_ce09_source_cannot_bypass_numbered_capacity(tmp_path, kind):
    import json

    from agent_alfred.settings import Settings

    session, supplied = _sized_store(tmp_path, kind, 1)
    budget = (
        len(
            json.dumps(
                supplied, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        - 1
    )

    class OversizedSource:
        def __init__(self, real):
            self.real = real

        def read(self, source, request, context):
            # Approved source fault seam: return the real Store record with a
            # larger read allowance. The actual node must enforce its own limit.
            larger = dict(
                request,
                limits=dict(request["limits"], per_store_character_budget=50000),
            )
            return self.real.read(source, larger, context)

    with runtime(
        tmp_path,
        ["must not send"],
        settings=Settings(per_store_character_budget=budget),
        aggregation_sources=OversizedSource,
    ) as (host, model, _):
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        assert facts["sources"][kind]["code"] == "invalid_source_result"
        assert facts["reason_code"] == "sources_unavailable"
        assert not model.requests and result.step_count == 0


@pytest.mark.parametrize("kind", ["semantic", "episodic"])
def test_spec04_ce06_skipped_long_first_keeps_contiguous_labels_without_more_reads(
    tmp_path, kind
):
    import json

    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    with runtime(
        tmp_path,
        ["draft"],
        settings=Settings(per_store_limit=2, per_store_character_budget=50000),
    ) as (host, model, _):
        ids = []
        for n, text in enumerate(
            ["coffee outside", "coffee short", "coffee " + "x" * 2000]
        ):
            payload = (
                dict(subject="me", fact=text)
                if kind == "semantic"
                else dict(
                    summary=text,
                    occurred_at="2026-09-14T10:00:00+08:00",
                    occurred_until=None,
                )
            )
            saved = host.memory_service.execute(
                dict(
                    operation_id="seed-" + str(n),
                    kind=kind,
                    action="save",
                    payload=payload,
                ),
                CONTEXT,
            )
            assert saved["status"] == "saved"
            ids.append(saved["memory_id"])
        session = host.create_session()
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        assert host.wait(accepted.run_id).outcome == "completed"
        supplied = json.loads(message_plain_text(model.requests[0].messages[0]))[
            "provided_sources"
        ]
        assert [s["memory_id"] for s in supplied] == [ids[2], ids[1]]
    short = dict(supplied[1], label="S1" if kind == "semantic" else "E1")
    budget = len(
        json.dumps([short], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    with runtime(
        tmp_path,
        ["draft"],
        settings=Settings(per_store_limit=2, per_store_character_budget=budget),
    ) as (host, model, _):
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]["sources"][kind]
        assert facts["candidate_count"] == 2 and facts["capacity_excluded"] == 1
        assert facts["actual_input_count"] == 1 and facts["remaining"] == "unknown"
        assert result.step_count == 1 and len(model.requests) == 1
        actual = json.loads(message_plain_text(model.requests[0].messages[0]))[
            "provided_sources"
        ]
        assert actual == [short]


def _credential_provider(settings, credential):
    from agent_alfred.runtime.config import MutableAssignmentProvider

    return MutableAssignmentProvider(
        endpoint_id="test",
        model_id="m",
        wire_style="openai",
        api_key=credential,
        settings=settings,
    )


@pytest.mark.parametrize("kind", ["semantic", "episodic"])
@pytest.mark.parametrize("credential", ["XY", "LONG_CREDENTIAL_VALUE"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_spec04_actual_redacted_capacity_expands_or_shrinks_at_boundary(
    tmp_path, kind, credential, delta
):
    import json

    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    text = "coffee " + (credential + " ") * 20
    settings = Settings(per_store_character_budget=50000)
    with runtime(
        tmp_path,
        ["draft"],
        settings=settings,
        snapshot_provider=_credential_provider(settings, credential),
    ) as (host, model, _):
        payload = (
            dict(subject="me", fact=text)
            if kind == "semantic"
            else dict(
                summary=text,
                occurred_at="2026-09-14T10:00:00+08:00",
                occurred_until=None,
            )
        )
        saved = host.memory_service.execute(
            dict(operation_id="seed", kind=kind, action="save", payload=payload),
            CONTEXT,
        )
        assert saved["status"] == "saved"
        session = host.create_session()
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        assert host.wait(accepted.run_id).outcome == "completed"
        actual = json.loads(message_plain_text(model.requests[0].messages[0]))[
            "provided_sources"
        ]
        field = "fact" if kind == "semantic" else "summary"
        assert actual[0]["content"][field] == "coffee " + "*** " * 20
    budget = (
        len(
            json.dumps(
                actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        + delta
    )
    settings = Settings(per_store_character_budget=budget)
    with runtime(
        tmp_path,
        ["draft"],
        settings=settings,
        snapshot_provider=_credential_provider(settings, credential),
    ) as (host, model, _):
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        assert result.outcome == "completed"
        if delta < 0:
            assert facts["sources"][kind]["capacity_excluded"] == 1
            assert facts["reason_code"] == "capacity_excluded_all"
            assert (
                not model.requests and result.step_count == 0 and result.reply is None
            )
        else:
            assert facts["sources"][kind]["capacity_excluded"] == 0
            assert result.step_count == 1 and len(model.requests) == 1
            supplied = json.loads(message_plain_text(model.requests[0].messages[0]))[
                "provided_sources"
            ]
            assert supplied == actual
            assert (
                len(
                    json.dumps(
                        supplied,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                <= budget
            )


@pytest.mark.parametrize("kind", ["semantic", "episodic"])
def test_spec04_redacted_long_first_uses_later_short_item_with_contiguous_label(
    tmp_path, kind
):
    import json

    from agent_alfred.messages import message_plain_text
    from agent_alfred.settings import Settings

    settings = Settings(per_store_limit=2, per_store_character_budget=50000)
    with runtime(
        tmp_path,
        ["draft"],
        settings=settings,
        snapshot_provider=_credential_provider(settings, "XY"),
    ) as (host, model, _):
        ids = []
        for n, text in enumerate(
            ["coffee outside", "coffee short", "coffee " + "XY" * 20]
        ):
            payload = (
                dict(subject="me", fact=text)
                if kind == "semantic"
                else dict(
                    summary=text,
                    occurred_at="2026-09-14T10:00:00+08:00",
                    occurred_until=None,
                )
            )
            saved = host.memory_service.execute(
                dict(
                    operation_id="seed-" + str(n),
                    kind=kind,
                    action="save",
                    payload=payload,
                ),
                CONTEXT,
            )
            assert saved["status"] == "saved"
            ids.append(saved["memory_id"])
        session = host.create_session()
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        assert host.wait(accepted.run_id).outcome == "completed"
        supplied = json.loads(message_plain_text(model.requests[0].messages[0]))[
            "provided_sources"
        ]
        assert [i["memory_id"] for i in supplied] == [ids[2], ids[1]]
    budget = (
        len(
            json.dumps(
                [supplied[0]], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        - 1
    )
    short = dict(supplied[1], label="S1" if kind == "semantic" else "E1")
    settings = Settings(per_store_limit=2, per_store_character_budget=budget)
    with runtime(
        tmp_path,
        ["draft"],
        settings=settings,
        snapshot_provider=_credential_provider(settings, "XY"),
    ) as (host, model, _):
        accepted = host.aggregate(
            session_id=session, goal="goal", keywords="coffee", sources=(kind,)
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]["sources"][kind]
        assert facts["candidate_count"] == 2 and facts["capacity_excluded"] == 1
        assert facts["remaining"] == "unknown" and facts["actual_input_count"] == 1
        assert result.step_count == 1 and len(model.requests) == 1
        assert json.loads(message_plain_text(model.requests[0].messages[0]))[
            "provided_sources"
        ] == [short]


def test_source_structure_is_revalidated_after_registry_redaction(tmp_path):
    from agent_alfred.settings import Settings

    settings = Settings()
    # A real configured credential can overlap a source date. The central
    # projection must not commit the now-invalid timestamp to the source key.
    with runtime(
        tmp_path,
        ["must not send"],
        settings=settings,
        snapshot_provider=_credential_provider(settings, "2026"),
    ) as (host, model, sink):
        episode(host)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=("episodic",),
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        assert facts["sources"]["episodic"]["code"] == "invalid_source_result"
        assert facts["reason_code"] == "sources_unavailable"
        assert not model.requests and result.step_count == 0


def _aggregation_cli(host, session, sources):
    import io

    from agent_alfred.gateway.cli import build_parser, run_aggregation_command

    args = build_parser().parse_args(
        [
            "--aggregate",
            "goal",
            "--session",
            session,
            "--keywords",
            "coffee",
            "--sources",
            ",".join(sources),
        ]
    )
    out = io.StringIO()
    status = run_aggregation_command(host, args, out)
    draft = next(
        p
        for p in host.mainbar_pairs(session_id=session, limit=20).items
        if p.aggregation
    )
    return status, out.getvalue(), draft.aggregation


@pytest.mark.parametrize("kind", ["semantic", "episodic"])
def test_spec05_cli_shows_capacity_and_unfinished_search_for_both_stores(
    tmp_path, kind
):
    from agent_alfred.settings import Settings

    with runtime(
        tmp_path,
        ["CLI draft"],
        settings=Settings(per_store_limit=2, per_store_character_budget=500),
    ) as (host, model, _):
        for n, text in enumerate(
            ["coffee outside", "coffee short", "coffee " + "x" * 2000]
        ):
            payload = (
                dict(subject="me", fact=text)
                if kind == "semantic"
                else dict(
                    summary=text,
                    occurred_at="2026-09-14T10:00:00+08:00",
                    occurred_until=None,
                )
            )
            assert (
                host.memory_service.execute(
                    dict(
                        operation_id="seed-" + str(n),
                        kind=kind,
                        action="save",
                        payload=payload,
                    ),
                    CONTEXT,
                )["status"]
                == "saved"
            )
        status, output, facts = _aggregation_cli(host, host.create_session(), (kind,))
        source = facts["sources"][kind]
        assert status == 0 and len(model.requests) == 1
        assert source["candidate_count"] == 2 and source["capacity_excluded"] == 1
        assert source["actual_input_count"] == 1 and source["remaining"] == "unknown"
        line = next(x for x in output.splitlines() if x.startswith(kind + ":"))
        for text in [
            "实际提供 1",
            "来源容量排除 1",
            "请求裁剪 0",
            "许可排除 0",
            "未取完状态 unknown",
        ]:
            assert text in line
        assert "不代表逐句证实" in output


def test_spec05_cli_shows_real_permission_exclusion(tmp_path):
    from agent_alfred.clock import FakeClock
    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.trace import RunBundleTraceSink

    trace = RunBundleTraceSink(
        root=ManagedStateDirectory.acquire_trace_root(tmp_path / "traces"),
        clock=FakeClock(),
        process_instance_id="skills-test",
    )
    gate = '{"retrieve":true,"query":"coffee","reason_code":"personal_information"}'
    with runtime(
        tmp_path, [gate, "old history", "CLI draft"], extra_sinks=(trace,)
    ) as (host, model, _):
        memory_id = save_fact(host)
        prior, result = submit(host, "coffee preference")
        assert result.memory_telemetry["input_attempts"][-1]["references"]
        deleted = host.memory_service.execute(
            dict(
                operation_id="delete-source",
                kind="semantic",
                action="delete",
                expected_version=1,
                payload=dict(id=memory_id),
            ),
            CONTEXT,
        )
        assert deleted.get("status") == "deleted", deleted
        save_fact(host, "coffee new", "new-fact")
        status, output, facts = _aggregation_cli(
            host, prior.session_id, ("semantic", "history")
        )
        assert status == 0 and len(model.requests) == 3
        assert facts["sources"]["history"]["permission_excluded"] == 1
        line = next(x for x in output.splitlines() if x.startswith("history:"))
        assert "许可排除 1" in line and "实际提供 0" in line
        assert "未取完状态 none" in line


def test_spec05_cli_shows_request_window_trim(tmp_path):
    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.settings import Settings

    with runtime(tmp_path, [SKIP, "history " * 600]) as (host, _, _):
        prior, _ = submit(host, "coffee")
        save_fact(host)
    with runtime(
        tmp_path, ["CLI draft"], settings=Settings(input_character_limit=1800)
    ) as (host, model, _):
        status, output, facts = _aggregation_cli(
            host, prior.session_id, ("semantic", "history")
        )
        assert status == 0 and len(model.requests) == 1
        assert facts["sources"]["history"]["request_excluded"] == 1
        line = next(x for x in output.splitlines() if x.startswith("history:"))
        assert "请求裁剪 1" in line and "来源容量排除 0" in line
        assert "实际提供 0" in line


def test_spec05_cli_keeps_failed_or_skipped_counters_unknown(tmp_path):
    class Source:
        def __init__(self, real):
            self.real = real

        def read(self, kind, request, context):
            if kind == "semantic":
                raise TimeoutError("local fault")
            return self.real.read(kind, request, context)

    with runtime(tmp_path, ["CLI draft"], aggregation_sources=Source) as (
        host,
        model,
        _,
    ):
        episode(host)
        status, output, facts = _aggregation_cli(
            host, host.create_session(), ("semantic", "episodic")
        )
        assert status == 0 and len(model.requests) == 1
        assert facts["sources"]["semantic"]["capacity_excluded"] is None
        assert "已降级" in output
        for kind, state in [("semantic", "failed"), ("history", "skipped")]:
            line = next(x for x in output.splitlines() if x.startswith(kind + ":"))
            assert state in line
            assert "来源容量排除 unknown" in line and "许可排除 unknown" in line
            assert "未取完状态 unknown" in line and "None" not in line
        assert "local_timeout" in output


def test_spec05_cli_no_sources_does_not_invent_exclusion_counts(tmp_path):
    with runtime(tmp_path, []) as (host, model, _):
        status, output, facts = _aggregation_cli(host, host.create_session(), ())
        assert status == 0 and not model.requests
        assert facts["reason_code"] == "sources_not_selected"
        assert "聚合草稿" not in output
        for kind in ["semantic", "episodic", "history"]:
            line = next(x for x in output.splitlines() if x.startswith(kind + ":"))
            assert "skipped" in line and "来源容量排除 unknown" in line
            assert "许可排除 unknown" in line and "未取完状态 unknown" in line


@pytest.mark.parametrize("limit", [100, 64000])
@pytest.mark.parametrize("with_semantic", [False, True])
def test_spec06_postvalidation_recovery_survives_join_failure(
    tmp_path, limit, with_semantic
):
    from agent_alfred.evals.deterministic.test_aggregation import save_fact
    from agent_alfred.settings import Settings

    settings = Settings(input_character_limit=limit)
    with runtime(
        tmp_path,
        ["draft"],
        settings=settings,
        snapshot_provider=_credential_provider(settings, "2026"),
    ) as (host, model, _):
        episode(host)
        if with_semantic:
            save_fact(host)
        sources = ("semantic", "episodic") if with_semantic else ("episodic",)
        accepted = host.aggregate(
            session_id=host.create_session(),
            goal="goal",
            keywords="coffee",
            sources=sources,
        )
        result = host.wait(accepted.run_id)
        facts = result.memory_telemetry["aggregation"]
        persisted = (
            host.mainbar_pairs(session_id=accepted.session_id, limit=10)
            .items[0]
            .aggregation
        )
        assert persisted["sources"] == facts["sources"]
        assert persisted["sources"]["episodic"]["read_outcome"] == "failed"
        assert persisted["sources"]["episodic"]["code"] == "invalid_source_result"
        assert persisted["sources"]["history"]["read_outcome"] == "skipped"
        assert persisted["sources"]["semantic"]["read_outcome"] == (
            "succeeded" if with_semantic else "skipped"
        )
        if limit == 100:
            assert result.outcome == "failed" and result.error == "input_limit_exceeded"
            assert persisted["error"] == "input_limit_exceeded"
            assert not model.requests and result.step_count == 0
        elif with_semantic:
            assert result.outcome == "completed" and len(model.requests) == 1
            assert persisted["sources"]["semantic"]["actual_input_count"] == 1
        else:
            assert persisted["reason_code"] == "sources_unavailable"
            assert not model.requests and result.step_count == 0


def _spec07_seed_history(path, answers):
    script = [value for answer in answers for value in (SKIP, answer)]
    with runtime(path, script) as (host, _, _):
        session = host.create_session()
        runs = []
        for n in range(len(answers)):
            accepted, result = submit(host, f"history question {n}", session)
            assert result.outcome == "completed"
            runs.append(accepted.run_id)
    return session, runs


def _spec07_assert_cli_history(
    host, session, output, facts, *, rounds, trimmed, provided, reason=None
):
    history = facts["sources"]["history"]
    assert history["round_excluded"] == rounds
    assert history["request_excluded"] == trimmed
    assert history["capacity_excluded"] == 0
    assert history["actual_input_count"] == provided
    line = next(line for line in output.splitlines() if line.startswith("history:"))
    for expected in (
        f"轮数排除 {rounds}",
        f"请求裁剪 {trimmed}",
        f"实际提供 {provided}",
        "来源容量排除 0",
    ):
        assert expected in line
    pair = next(
        p
        for p in host.mainbar_pairs(session_id=session, limit=20).items
        if p.aggregation
    )
    assert pair.aggregation == facts
    if reason:
        assert facts["reason_code"] == reason
        assert reason in output
        assert facts["steps"] == [] and facts.get("provided", []) == []
        assert pair.assistant_message is None
    else:
        assert len(facts["steps"]) == 1 and pair.assistant_message is not None


@pytest.mark.parametrize("rounds", [0, 1, 3])
def test_spec07_cli_round_exclusion_and_zero_round_reason(tmp_path, rounds):
    from agent_alfred.settings import load_settings

    session, history = _spec07_seed_history(tmp_path, ["old answer", "new answer"])
    with runtime(
        tmp_path,
        ["draft"],
        settings=load_settings(environ={}, working_memory_rounds=rounds),
    ) as (host, model, _):
        status, output, facts = _aggregation_cli(host, session, ("history",))
        count = min(rounds, 2)
        assert status == 0 and len(model.requests) == bool(count)
        _spec07_assert_cli_history(
            host,
            session,
            output,
            facts,
            rounds=2 - count,
            trimmed=0,
            provided=count,
            reason=None if count else "capacity_excluded_all",
        )
        assert facts["sources"]["history"]["candidate_count"] == 2
        assert [item["run_id"] for item in facts.get("provided", [])] == (
            history[-count:] if count else []
        )
        if not count:
            pair = next(
                p
                for p in host.mainbar_pairs(session_id=session, limit=20).items
                if p.aggregation
            )
            evidence = host.read_run_evidence(
                pair.run_id, trace_root=tmp_path / "traces"
            )
            assert evidence["attempts"] == []
            assert evidence["memory"]["input_attempts"] == []


@pytest.mark.parametrize("last_long", [False, True])
def test_spec07_round_and_request_exclusion_are_distinct(tmp_path, last_long):
    from agent_alfred.settings import Settings

    session, history = _spec07_seed_history(
        tmp_path,
        ["old answer", "long " * 600, "long " * 600 if last_long else "short answer"],
    )
    with runtime(
        tmp_path,
        ["draft"],
        settings=Settings(working_memory_rounds=2, input_character_limit=1800),
    ) as (host, model, _):
        status, output, facts = _aggregation_cli(host, session, ("history",))
        provided = 0 if last_long else 1
        assert status == 0 and len(model.requests) == provided
        _spec07_assert_cli_history(
            host,
            session,
            output,
            facts,
            rounds=1,
            trimmed=2 - provided,
            provided=provided,
            reason="capacity_excluded_all" if last_long else None,
        )
        source = facts["sources"]["history"]
        assert (
            source["approved_count"]
            == source["round_excluded"]
            + source["request_excluded"]
            + source["actual_input_count"]
            == 3
        )
        assert [item["run_id"] for item in facts.get("provided", [])] == (
            [] if last_long else history[-1:]
        )


@pytest.mark.parametrize("rounds", [0, 1])
def test_spec07_source_failure_priority_with_round_exclusion(tmp_path, rounds):
    from agent_alfred.settings import Settings

    class Source:
        def __init__(self, real):
            self.real = real

        def read(self, kind, request, context):
            if kind == "semantic":
                raise TimeoutError("source fault")
            return self.real.read(kind, request, context)

    session, _ = _spec07_seed_history(tmp_path, ["old answer", "new answer"])
    with runtime(
        tmp_path,
        ["draft"],
        settings=Settings(working_memory_rounds=rounds),
        aggregation_sources=Source,
    ) as (host, model, _):
        status, output, facts = _aggregation_cli(host, session, ("semantic", "history"))
        assert status == 0 and len(model.requests) == rounds
        _spec07_assert_cli_history(
            host,
            session,
            output,
            facts,
            rounds=2 - rounds,
            trimmed=0,
            provided=rounds,
            reason="sources_unavailable" if rounds == 0 else None,
        )
        assert facts["sources"]["semantic"]["read_outcome"] == "failed"
        assert facts["sources"]["semantic"]["code"] == "local_timeout"
        assert "local_timeout" in output and "已降级" in output
