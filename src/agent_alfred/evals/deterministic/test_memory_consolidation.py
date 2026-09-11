"""Public consolidation preparation and strict plan-validation contracts."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_alfred.memory.consolidation import (
    CONSOLIDATION_SYSTEM,
    ConsolidationBelowThreshold,
    ConsolidationCandidateReadError,
    ConsolidationCandidateReadFailed,
    ConsolidationCandidateReads,
    ConsolidationLimits,
    ConsolidationPlanError,
    ConsolidationSource,
    ConsolidationSourceEvidenceError,
    ConsolidationSourceTooLarge,
    CreateSemanticIntention,
    KeepSemanticIntention,
    PreparedConsolidation,
    SelectedConsolidationSources,
    UpdateSemanticIntention,
    parse_consolidation_plan,
    prepare_consolidation_input,
    prepare_consolidation_request,
    select_consolidation_sources,
)
from agent_alfred.memory.types import FactRecord, ManualOrigin, MemoryId
from agent_alfred.messages import (
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    message_plain_text,
)
from agent_alfred.model import ModelRef, ModelResponse
from agent_alfred.runtime.input_budget import (
    INPUT_VERSION,
    input_characters,
    serialize_input,
)

MODEL = ModelRef("test-endpoint", "test-model")
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CST = timezone(timedelta(hours=8))


def _source(
    run_id,
    user="用户陈述",
    assistant="助手回复",
    *,
    session="session-a",
    accepted=None,
    finished=None,
):
    return ConsolidationSource(
        session_id=session,
        run_id=run_id,
        user_text=user,
        assistant_text=assistant,
        accepted_at=NOW if accepted is None else accepted,
        finished_at=NOW + timedelta(minutes=1) if finished is None else finished,
    )


def _fact(
    identifier,
    subject="主题",
    body="事实",
    *,
    version=1,
    protected=False,
    origin=None,
):
    origin = ManualOrigin("web") if origin is None else origin
    return FactRecord(
        MemoryId(identifier),
        subject,
        body,
        origin,
        NOW,
        version,
        NOW,
        origin,
        protected,
    )


def _limits(**changes):
    values = dict(
        source_threshold=2,
        candidate_count_limit=20,
        candidate_character_limit=16000,
        request_character_limit=64000,
    )
    values.update(changes)
    return ConsolidationLimits(**values)


def _prepare(sources, candidates=None, *, session="session-a", limits=None):
    if candidates is None:
        candidates = ConsolidationCandidateReads()
    return prepare_consolidation_input(
        session,
        sources,
        candidates,
        model=MODEL,
        limits=_limits() if limits is None else limits,
    )


def test_default_limits_match_published_consolidation_budget():
    limits = ConsolidationLimits()
    assert limits.source_threshold == 10
    assert limits.candidate_count_limit == 20
    assert limits.candidate_character_limit == 16000
    assert limits.request_character_limit == 64000


@pytest.mark.parametrize("field", [
    "source_threshold",
    "candidate_count_limit",
    "candidate_character_limit",
    "request_character_limit",
])
def test_limits_reject_non_positive_or_bool_values(field):
    for value in (0, -1, True):
        with pytest.raises(ValueError, match="invalid_consolidation_config"):
            ConsolidationLimits(**{field: value})


def test_separate_sessions_do_not_combine_to_reach_threshold():
    limits = _limits(source_threshold=10)
    session_a = [_source(f"a{i}", session="sess-a") for i in range(6)]
    session_b = [_source(f"b{i}", session="sess-b") for i in range(6)]
    mixed = (*session_a, *session_b)
    result = _prepare(mixed, session="sess-a", limits=limits)
    assert isinstance(result, ConsolidationBelowThreshold)
    assert result.session_id == "sess-a"
    assert result.unprocessed_count == 6
    assert result.threshold == 10
    other = _prepare(mixed, session="sess-b", limits=limits)
    assert isinstance(other, ConsolidationBelowThreshold)
    assert other.unprocessed_count == 6


def test_threshold_minus_one_does_not_prepare_and_threshold_selects_oldest_prefix():
    limits = ConsolidationLimits()
    nine = [_source(f"r{i}") for i in range(9)]
    below = _prepare(nine, limits=limits)
    assert isinstance(below, ConsolidationBelowThreshold)
    assert below.unprocessed_count == 9
    ten = [*nine, _source("r9")]
    extras = [*ten, _source("r10"), _source("r11")]
    prepared = _prepare(extras, limits=limits)
    assert isinstance(prepared, PreparedConsolidation)
    assert [source.run_id for source in prepared.sources] == [
        f"r{i}" for i in range(10)
    ]
    assert prepared.candidate_disposition == "no_hits"
    assert prepared.candidates == ()
    assert prepared.search_query == " ".join(["用户陈述 助手回复"] * 10)


def test_contiguous_prefix_does_not_skip_a_source_that_cannot_fit():
    first = _source("r1", "one", "ok")
    second = _source("r2", "two", "ok")
    fitted = _prepare([first, second], limits=_limits(source_threshold=2))
    two_size = input_characters(fitted.request)
    huge = _source("r3", "H" * 4000, "ok")
    fourth = _source("r4", "four", "ok")
    result = _prepare(
        [first, second, huge, fourth],
        limits=_limits(source_threshold=4, request_character_limit=two_size),
    )
    assert isinstance(result, PreparedConsolidation)
    assert [source.run_id for source in result.sources] == ["r1", "r2"]


def test_too_large_first_source_is_explicit_and_does_not_skip_to_later_groups():
    secret = "SECRET_SOURCE_BODY"
    oversized = _source("run-big", secret * 5000, "reply")
    later = _source("run-small", "later", "ok")
    result = _prepare(
        [oversized, later],
        limits=_limits(source_threshold=1),
    )
    assert isinstance(result, ConsolidationSourceTooLarge)
    assert result.session_id == "session-a"
    assert result.run_id == "run-big"
    assert result.limit == 64000
    assert result.characters > result.limit
    assert secret not in repr(result)
    assert secret not in str(result)


def test_source_priority_can_leave_zero_candidate_room_without_looking_like_no_hits():
    first = _source("r1", "one", "ok")
    second = _source("r2", "two", "ok")
    fitted = _prepare([first, second])
    tight = _limits(
        source_threshold=2,
        request_character_limit=input_characters(fitted.request),
    )
    candidate = _fact("mem-1", "住地", "北京")
    excluded = _prepare(
        [first, second],
        ConsolidationCandidateReads(search_hits=(candidate,)),
        limits=tight,
    )
    assert isinstance(excluded, PreparedConsolidation)
    assert excluded.candidate_disposition == "all_excluded"
    assert excluded.candidates == ()
    assert excluded.candidate_text is None
    assert all(
        "长期记忆候选" not in message_plain_text(message)
        for message in excluded.request.messages
    )
    empty = _prepare([first, second], limits=tight)
    assert empty.candidate_disposition == "no_hits"


def test_candidate_read_failure_is_not_an_empty_library():
    sources = [_source("r1"), _source("r2")]
    failed = _prepare(
        sources,
        ConsolidationCandidateReadError("storage_read_failed"),
    )
    assert isinstance(failed, ConsolidationCandidateReadFailed)
    assert failed.code == "storage_read_failed"
    assert not isinstance(failed, PreparedConsolidation)
    selected = select_consolidation_sources(
        "session-a",
        sources,
        model=MODEL,
        limits=_limits(),
    )
    assert isinstance(selected, SelectedConsolidationSources)
    unavailable = prepare_consolidation_request(
        selected,
        ConsolidationCandidateReadError("storage_unavailable"),
    )
    assert isinstance(unavailable, ConsolidationCandidateReadFailed)
    assert unavailable.code == "storage_unavailable"


def test_duplicate_candidates_keep_search_first_order():
    search = (
        _fact("keep-me", "住地", "北京"),
        _fact("second", "食物", "不吃香菜"),
    )
    recent = (
        _fact("keep-me", "住地", "should-not-win"),
        _fact("third", "语言", "中文"),
        _fact("second", "食物", "dup-recent"),
    )
    result = _prepare(
        [_source("r1"), _source("r2")],
        ConsolidationCandidateReads(search_hits=search, recent=recent),
    )
    assert isinstance(result, PreparedConsolidation)
    assert [record.id for record in result.candidates] == [
        "keep-me",
        "second",
        "third",
    ]
    assert result.candidates[0].fact == "北京"
    assert result.candidate_disposition == "ready"
    expected = (
        '[{"id":"keep-me","version":1,"subject":"住地","fact":"北京",'
        '"origin":{"type":"manual","source":"web"},"human_protected":false},'
        '{"id":"second","version":1,"subject":"食物","fact":"不吃香菜",'
        '"origin":{"type":"manual","source":"web"},"human_protected":false},'
        '{"id":"third","version":1,"subject":"语言","fact":"中文",'
        '"origin":{"type":"manual","source":"web"},"human_protected":false}]'
    )
    assert result.candidate_text == expected
    assert expected in message_plain_text(result.request.messages[-1])
    assert result.request.messages[-1].role == "user"
    assert result.request.tool_choice == "none"
    assert result.request.tools == ()
    assert result.request.system == (TextBlock(CONSOLIDATION_SYSTEM),)


def test_long_candidate_is_not_skipped_in_favor_of_a_later_short_one():
    long_item = _fact("long", "主题", "秘" * 80)
    short_item = _fact("short", "主题", "短")
    result = _prepare(
        [_source("r1"), _source("r2")],
        ConsolidationCandidateReads(search_hits=(long_item, short_item)),
        limits=_limits(candidate_character_limit=40),
    )
    assert isinstance(result, PreparedConsolidation)
    assert result.candidates == ()
    assert result.candidate_disposition == "all_excluded"


def test_candidate_count_stops_at_twenty_whole_records():
    hits = tuple(_fact(f"id-{i}", "s", "f") for i in range(21))
    result = _prepare(
        [_source("r1"), _source("r2")],
        ConsolidationCandidateReads(search_hits=hits),
    )
    assert [record.id for record in result.candidates] == [f"id-{i}" for i in range(20)]
    assert result.candidate_disposition == "partial"


def test_counted_request_uses_request_input_v1_json_escaping():
    marked = _source("r1", "hello\x01", "reply")
    other = _source("r2", "next", "ok")
    result = _prepare([marked, other])
    assert isinstance(result, PreparedConsolidation)
    serialized = serialize_input(result.request)
    assert INPUT_VERSION == "request-input-v1"
    assert '"version":"request-input-v1"' in serialized
    assert "\\u0001" in serialized
    assert "\x01" not in serialized
    assert input_characters(result.request) == len(serialized)
    assert input_characters(result.request) > len("hello\x01replynextok") + len(
        CONSOLIDATION_SYSTEM
    )


def test_episode_interval_uses_utc_bounds_and_instant_when_equal():
    first = _source(
        "r1",
        accepted=datetime(2026, 9, 1, 8, 0, tzinfo=CST),
        finished=datetime(2026, 9, 1, 8, 5, tzinfo=CST),
    )
    second = _source(
        "r2",
        accepted=datetime(2026, 9, 2, 0, 30, tzinfo=UTC),
        finished=datetime(2026, 9, 2, 1, 0, tzinfo=UTC),
    )
    prepared = _prepare([first, second])
    assert prepared.occurred_at == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    assert prepared.occurred_until == datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
    same = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    instant = _prepare(
        [
            _source("r1", accepted=same, finished=same),
            _source("r2", accepted=same, finished=same),
        ]
    )
    assert instant.occurred_at == same
    assert instant.occurred_until is None


def test_missing_naive_or_inverted_timestamps_are_evidence_errors():
    other = _source("r2")
    missing = _prepare(
        [
            ConsolidationSource(
                "session-a",
                "r1",
                "用户陈述",
                "助手回复",
                None,
                NOW,
            ),
            other,
        ]
    )
    assert isinstance(missing, ConsolidationSourceEvidenceError)
    assert missing.run_id == "r1"
    assert missing.code == "invalid_timestamp"
    naive = _prepare(
        [
            _source("r1", accepted=datetime(2026, 9, 1, 12, 0), finished=NOW),
            other,
        ]
    )
    assert isinstance(naive, ConsolidationSourceEvidenceError)
    inverted = _prepare(
        [
            _source(
                "r1",
                accepted=datetime(2026, 9, 2, tzinfo=UTC),
                finished=datetime(2026, 9, 1, tzinfo=UTC),
            ),
            other,
        ]
    )
    assert isinstance(inverted, ConsolidationSourceEvidenceError)
    assert "UTC offset" not in str(missing)
    assert "UTC offset" not in str(naive)


def test_distinct_source_dates_yield_distinct_model_date_context():
    spoken = "我昨天完成了模型飞机，下周想再做一个。"
    reply = "收到。"

    def prepared_on(day):
        start = datetime(2026, 9, day, 12, tzinfo=UTC)
        return _prepare(
            [
                _source(
                    "run",
                    spoken,
                    reply,
                    accepted=start,
                    finished=start + timedelta(minutes=1),
                )
            ],
            limits=_limits(source_threshold=1),
        )

    first = prepared_on(1)
    later = prepared_on(8)
    assert isinstance(first, PreparedConsolidation)
    assert isinstance(later, PreparedConsolidation)
    assert first.occurred_at != later.occurred_at
    first_input = serialize_input(first.request)
    later_input = serialize_input(later.request)
    assert first_input != later_input
    assert "2026-09-01T12:00:00+00:00" in first_input
    assert "2026-09-08T12:00:00+00:00" in later_input
    assert spoken in first_input
    assert spoken in later_input


def test_multi_day_sources_keep_envelopes_outside_quoted_turns():
    first = _source(
        "run-day-1",
        "我昨天完成了模型飞机。",
        "记下了。",
        accepted=datetime(2026, 9, 1, 8, 0, tzinfo=CST),
        finished=datetime(2026, 9, 1, 8, 5, tzinfo=CST),
    )
    second = _source(
        "run-day-8",
        "下周想再做一个。",
        "好。",
        accepted=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        finished=datetime(2026, 9, 8, 12, 1, tzinfo=UTC),
    )
    prepared = _prepare([first, second])
    messages = prepared.request.messages
    assert [message.role for message in messages] == [
        "user",
        "user",
        "assistant",
        "user",
        "user",
        "assistant",
    ]
    assert message_plain_text(messages[0]) == (
        '来源语境={"run_id":"run-day-1","accepted_at":"2026-09-01T08:00:00+08:00",'
        '"finished_at":"2026-09-01T08:05:00+08:00"}'
    )
    assert message_plain_text(messages[1]) == first.user_text
    assert message_plain_text(messages[2]) == first.assistant_text
    assert message_plain_text(messages[3]) == (
        '来源语境={"run_id":"run-day-8","accepted_at":"2026-09-08T12:00:00+00:00",'
        '"finished_at":"2026-09-08T12:01:00+00:00"}'
    )
    assert message_plain_text(messages[4]) == second.user_text
    assert message_plain_text(messages[5]) == second.assistant_text
    assert "yesterday" in CONSOLIDATION_SYSTEM
    assert "reading date" in CONSOLIDATION_SYSTEM


def test_source_date_context_is_required_capacity_before_candidates():
    first = _source("r1", "one", "ok")
    second = _source("r2", "two", "ok")
    one = _prepare([first], limits=_limits(source_threshold=1))
    one_size = input_characters(one.request)
    serialized = serialize_input(one.request)
    assert "来源语境" in serialized
    assert '"run_id":"r1"' in message_plain_text(one.request.messages[0])
    packed = _prepare(
        [first, second],
        ConsolidationCandidateReads(search_hits=(_fact("mem-1", "住地", "北京"),)),
        limits=_limits(source_threshold=2, request_character_limit=one_size),
    )
    assert isinstance(packed, PreparedConsolidation)
    assert [source.run_id for source in packed.sources] == ["r1"]
    assert packed.candidate_disposition == "all_excluded"
    assert input_characters(packed.request) == one_size
    assert "长期记忆候选" not in serialize_input(packed.request)


def test_search_query_is_available_before_candidate_binding():
    sources = [
        _source("r1", "我住北京", "记下了"),
        _source("r2", "下个月可能搬上海", "那还是计划"),
    ]
    selected = select_consolidation_sources(
        "session-a",
        sources,
        model=MODEL,
        limits=_limits(),
    )
    assert selected.search_query == "我住北京 记下了 下个月可能搬上海 那还是计划"
    prepared = prepare_consolidation_request(selected, ConsolidationCandidateReads())
    assert prepared.search_query == selected.search_query
    assert prepared.occurred_at == selected.occurred_at


def _prepared_with_candidate():
    candidate = _fact("mem-1", "住地", "北京", version=4, protected=True)
    result = _prepare(
        [_source("r1"), _source("r2")],
        ConsolidationCandidateReads(search_hits=(candidate,)),
    )
    assert isinstance(result, PreparedConsolidation)
    return result


def test_below_threshold_does_not_surface_a_candidate_read_failure():
    result = _prepare(
        [_source("r1")],
        ConsolidationCandidateReadError("storage_read_failed"),
        limits=_limits(source_threshold=2),
    )
    assert isinstance(result, ConsolidationBelowThreshold)
    assert result.unprocessed_count == 1


def test_plan_accepts_create_update_keep_and_nonempty_summary():
    first = _fact("mem-1", "住地", "北京", version=4, protected=True)
    second = _fact("mem-2", "语言", "中文")
    prepared = _prepare(
        [_source("r1"), _source("r2")],
        ConsolidationCandidateReads(search_hits=(first, second)),
    )
    plan = parse_consolidation_plan(
        '{"semantic":['
        '{"action":"update","id":"mem-1","subject":"住地","fact":"已经搬到上海"},'
        '{"action":"keep","id":"mem-2"},'
        '{"action":"create","subject":"食物","fact":"不吃香菜"}'
        '],"episode_summary":"用户谈到居住地与饮食。"}',
        prepared,
    )
    assert plan.episode_summary == "用户谈到居住地与饮食。"
    assert plan.occurred_at == prepared.occurred_at
    assert plan.occurred_until == prepared.occurred_until
    assert plan.semantic == (
        UpdateSemanticIntention(
            MemoryId("mem-1"),
            "住地",
            "已经搬到上海",
            4,
        ),
        KeepSemanticIntention(MemoryId("mem-2"), 1),
        CreateSemanticIntention("食物", "不吃香菜"),
    )


@pytest.mark.parametrize(
    "raw,code",
    [
        (
            '{"semantic":[],"semantic":[],"episode_summary":"摘要"}',
            "invalid_plan_output",
        ),
        (
            '{"semantic":[{"action":"keep","id":"mem-1","id":"x"}],'
            '"episode_summary":"摘要"}',
            "invalid_plan_output",
        ),
        (
            '{"semantic":[{"action":"delete","id":"mem-1"}],"episode_summary":"摘要"}',
            "invalid_plan_output",
        ),
        (
            '{"semantic":[{"action":"update","id":"mem-1","subject":"住地",'
            '"fact":"上海","expected_version":1}],"episode_summary":"摘要"}',
            "invalid_plan_output",
        ),
        (
            '{"semantic":[],"episode_summary":"摘要","occurred_at":'
            '"2020-01-01T00:00:00Z"}',
            "invalid_plan_output",
        ),
        (
            '{"semantic":[{"action":"create","subject":1,"fact":"x"}],'
            '"episode_summary":"摘要"}',
            "invalid_plan_output",
        ),
        (
            '{"semantic":[{"action":true,"subject":"s","fact":"f"}],'
            '"episode_summary":"摘要"}',
            "invalid_plan_output",
        ),
        ("[]", "invalid_plan_output"),
        ("```json\n{}\n```", "invalid_plan_output"),
        ('{"semantic":[],"episode_summary":""}', "empty_summary"),
        ('{"semantic":[],"episode_summary":"  \\n"}', "empty_summary"),
        (
            '{"semantic":[{"action":"update","id":"ghost","subject":"住地",'
            '"fact":"上海"}],"episode_summary":"摘要"}',
            "unknown_target",
        ),
        (
            '{"semantic":[{"action":"keep","id":"mem-1"},'
            '{"action":"update","id":"mem-1","subject":"住地","fact":"上海"}],'
            '"episode_summary":"摘要"}',
            "conflicting_target",
        ),
    ],
)
def test_plan_rejects_malformed_duplicate_unknown_and_conflicting_output(raw, code):
    prepared = _prepared_with_candidate()
    with pytest.raises(ConsolidationPlanError) as caught:
        parse_consolidation_plan(raw, prepared)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert "ghost" not in str(caught.value)
    assert "上海" not in str(caught.value)


def test_plan_rejects_partial_and_non_text_model_output():
    prepared = _prepared_with_candidate()
    payload = '{"semantic":[],"episode_summary":"看起来完整"}'
    partial = ModelResponse(
        (TextBlock(payload),),
        "max_tokens",
        MODEL,
    )
    with pytest.raises(ConsolidationPlanError) as caught:
        parse_consolidation_plan(partial, prepared)
    assert caught.value.code == "incomplete_output"
    assert "看起来完整" not in str(caught.value)
    thinking = ModelResponse(
        (ThinkingBlock("secret-thought"), TextBlock(payload)),
        "end_turn",
        MODEL,
    )
    assert parse_consolidation_plan(thinking, prepared) == parse_consolidation_plan(
        payload, prepared
    )
    tool = ModelResponse(
        (ToolCallBlock("c1", "save_fact", {"subject": "x", "fact": "y"}),),
        "tool_use",
        MODEL,
    )
    with pytest.raises(ConsolidationPlanError) as caught:
        parse_consolidation_plan(tool, prepared)
    assert caught.value.code == "non_text_output"


def test_excluded_candidate_is_not_an_in_input_target():
    long_item = _fact("long", "主题", "秘" * 80)
    short_item = _fact("short", "主题", "短")
    prepared = _prepare(
        [_source("r1"), _source("r2")],
        ConsolidationCandidateReads(search_hits=(long_item, short_item)),
        limits=_limits(candidate_character_limit=40),
    )
    assert prepared.candidates == ()
    with pytest.raises(ConsolidationPlanError) as caught:
        parse_consolidation_plan(
            '{"semantic":[{"action":"keep","id":"short"}],"episode_summary":"摘要"}',
            prepared,
        )
    assert caught.value.code == "unknown_target"


@pytest.mark.parametrize(
    "blocks,stop,code",
    [
        ((ThinkingBlock('{"semantic":[],"episode_summary":"hidden"}'),),
         "end_turn", "incomplete_output"),
        ((ThinkingBlock("hidden"), TextBlock("")),
         "end_turn", "invalid_plan_output"),
        ((ThinkingBlock("hidden"), TextBlock("{}")),
         "end_turn", "invalid_plan_output"),
        ((ThinkingBlock("hidden"), TextBlock('{"semantic":[],'),
          TextBlock('"episode_summary":"split"}')),
         "end_turn", "incomplete_output"),
        ((ThinkingBlock("hidden"), ToolCallBlock("c", "save_fact", {})),
         "end_turn", "non_text_output"),
        ((ThinkingBlock("hidden"), TextBlock("{}")),
         "max_tokens", "incomplete_output"),
        ((ThinkingBlock("hidden"), object()),
         "end_turn", "non_text_output"),
    ],
)
def test_plan_thinking_cannot_supply_or_relax_final_text(blocks, stop, code):
    with pytest.raises(ConsolidationPlanError) as caught:
        parse_consolidation_plan(ModelResponse(blocks, stop, MODEL),
                                 _prepared_with_candidate())
    assert caught.value.code == code


def test_plan_ignores_thinking_json_and_preserves_only_final_intentions():
    final = '{"semantic":[],"episode_summary":"final-only"}'
    hidden = ('{"semantic":[{"action":"create","subject":"secret",'
              '"fact":"must-not-persist"}],"episode_summary":"secret"}')
    response = ModelResponse((ThinkingBlock(hidden), TextBlock(final)),
                             "end_turn", MODEL)
    prepared = _prepared_with_candidate()
    assert parse_consolidation_plan(response, prepared) == parse_consolidation_plan(
        final, prepared
    )


def test_raw_json_constraint_is_in_actual_system_and_input_measurement():
    from dataclasses import replace

    constraint = (
        "Return only the raw JSON object. Do not wrap it in Markdown code fences "
        "or include any text before or after it."
    )
    prepared = _prepared_with_candidate()
    request = prepared.request
    assert constraint in request.system[0].text
    assert constraint in serialize_input(request)
    without = replace(
        request, system=(TextBlock(request.system[0].text.replace(constraint, "")),)
    )
    assert input_characters(request) - input_characters(without) == len(constraint)


@pytest.mark.parametrize("prefix,suffix", [("", ""), ("```json\n", "\n```"),
                                           ("Here is the JSON: ", ""),
                                           ("", " Extra explanation.")])
def test_plan_requires_raw_json_without_fences_or_surrounding_prose(prefix, suffix):
    payload = '{"semantic":[],"episode_summary":"safe"}'
    prepared = _prepared_with_candidate()
    if not prefix and not suffix:
        assert parse_consolidation_plan(payload, prepared)
    else:
        with pytest.raises(ConsolidationPlanError) as caught:
            parse_consolidation_plan(prefix + payload + suffix, prepared)
        assert caught.value.code == "invalid_plan_output"
