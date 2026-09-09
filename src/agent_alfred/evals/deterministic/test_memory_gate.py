"""Public retrieval decisions and reference evidence contracts."""

import pytest

from agent_alfred.memory.retrieval_gate import fallback_decision


@pytest.mark.parametrize(
    "text,reason",
    [
        (" ＨＥＬＬＯ！！ ", "greeting"),
        ("thank   you...", "greeting"),
        ("（1 + -2） × .5 =", "arithmetic"),
        ("1 / 0?", "arithmetic"),
    ],
)
def test_fallback_skips_only_whole_greetings_and_valid_arithmetic(text, reason):
    decision = fallback_decision(text)
    assert (decision.retrieve, decision.query, decision.reason_code) == (
        False,
        None,
        reason,
    )


@pytest.mark.parametrize(
    "text",
    [
        "你好，我不吃香菜",
        "地球为什么是圆的？",
        "1+2元",
        "1",
        "(1+2",
        "1 2+3",
        '__import__("os")',
        "2**3",
        "1+=",
        "hello world",
    ],
)
def test_fallback_conservatively_retrieves_original_mixed_or_invalid_text(text):
    decision = fallback_decision(text)
    assert (decision.retrieve, decision.query, decision.reason_code) == (
        True,
        text,
        "conservative_retrieve",
    )


def test_model_decision_accepts_only_closed_coherent_json():
    from agent_alfred.memory.retrieval_gate import parse_model_decision

    decision = parse_model_decision(
        '{"retrieve":true,"query":"改写词","reason_code":"history_recall"}'
    )
    assert (decision.retrieve, decision.query, decision.reason_code) == (
        True,
        "改写词",
        "history_recall",
    )
    for raw in [
        '{"retrieve":1,"query":"x","reason_code":"history_recall"}',
        '{"retrieve":true,"query":" ","reason_code":"history_recall"}',
        '{"retrieve":false,"query":"x","reason_code":"greeting"}',
        '{"retrieve":false,"query":null,"reason_code":"personal_information"}',
        '{"retrieve":true,"query":"x","reason_code":"greeting"}',
        '{"retrieve":true,"query":"x","reason_code":"history_recall","reason":"secret"}',
        '{"retrieve":true,"retrieve":false,"query":null,"reason_code":"greeting"}',
        "```json\n{}\n```",
        "[]",
        "null",
    ]:
        with pytest.raises(ValueError):
            parse_model_decision(raw)


class SearchBackend:
    """Injected Store boundary with controlled order and failure."""

    def __init__(self, hits=(), error=None):
        self.hits = list(hits)
        self.error = error
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.hits[: query.limit]


def fact_hit(identifier, fact):
    from datetime import UTC, datetime

    from agent_alfred.memory.types import FactHit, FactRecord, ManualOrigin, MemoryId

    now = datetime(2026, 1, 1, tzinfo=UTC)
    origin = ManualOrigin("web")
    return FactHit(
        FactRecord(
            MemoryId(identifier), "主题", fact, origin, now, 1, now, origin, True
        ),
        "SECRET_SCORE",
    )


def test_retrieval_keeps_complete_prefix_and_never_exposes_content_in_evidence():
    import json

    from agent_alfred.memory.retrieval_gate import retrieve

    semantic = SearchBackend(
        [
            fact_hit("first", "首文"),
            fact_hit("long", "秘" * 1000),
            fact_hit("last", "后文"),
        ]
    )
    episodic = SearchBackend()
    result = retrieve(
        fallback_decision("原始秘密查询"),
        semantic,
        episodic,
        fallback_reason="model_unavailable",
        per_store_character_budget=200,
    )
    assert (
        result.reference_text == "检索参考资料\n本次仅提供部分召回\n"
        'semantic=[{"id":"first","version":1,"subject":"主题","fact":"首文",'
        '"origin":{"type":"manual","source":"web"}}]\nepisodic=[]'
    )
    assert result.evidence["input_disposition"] == "partial"
    assert result.evidence["hit_count"] == 3
    assert result.evidence["selected_count"] == 1
    assert [ref["omission_reason"] for ref in result.evidence["references"]] == [
        None,
        "character_budget",
        "after_prefix_stop",
    ]
    assert result.selected_references == (
        {"kind": "semantic", "memory_id": "first", "record_version": 1},
    )
    encoded = json.dumps(result.evidence, ensure_ascii=False)
    assert all(
        secret not in encoded
        for secret in ("主题", "首文", "后文", "原始秘密查询", "SECRET_SCORE")
    )
    assert semantic.queries[0].text == episodic.queries[0].text == "原始秘密查询"
    assert semantic.queries[0].subject is None
    assert episodic.queries[0].since is episodic.queries[0].until is None


@pytest.mark.parametrize("which", ["semantic", "episodic"])
def test_store_failure_discards_half_references_and_preserves_unknown_counts(which):
    import json

    from agent_alfred.memory.retrieval_gate import retrieve

    first = SearchBackend(
        [fact_hit("first", "secret_body")],
        RuntimeError("secret_exception") if which == "semantic" else None,
    )
    second = SearchBackend(
        error=OSError("secret_exception") if which == "episodic" else None
    )
    result = retrieve(
        fallback_decision("secret_query"),
        first,
        second,
        fallback_reason="model_call_failed",
    )
    assert result.failure_code == "memory_storage_error"
    assert result.reference_text is None
    assert result.selected_references == ()
    assert result.evidence["outcome"] == "error"
    assert result.evidence["hit_count"] is None
    assert result.evidence["selected_count"] == 0
    assert result.evidence["stores"][which]["status"] == "failed"
    if which == "semantic":
        assert result.evidence["stores"]["episodic"]["status"] == "not_queried"
    else:
        assert result.evidence["stores"]["semantic"]["hit_count"] == 1
        assert result.evidence["references"][0]["omission_reason"] == "store_error"
    assert "secret" not in json.dumps(result.evidence)


def test_no_hits_continue_but_all_excluded_prevents_normal_answer():
    from agent_alfred.memory.retrieval_gate import retrieve

    decision = fallback_decision("recall")
    miss = retrieve(
        decision, SearchBackend(), SearchBackend(), fallback_reason="invalid_output"
    )
    assert (
        miss.evidence["outcome"],
        miss.evidence["input_disposition"],
        miss.reference_text,
        miss.failure_code,
    ) == ("miss", "no_hits", None, None)
    excluded = retrieve(
        decision,
        SearchBackend([fact_hit("long", "x" * 1000)]),
        SearchBackend(),
        fallback_reason="invalid_output",
        per_store_character_budget=10,
    )
    assert (
        excluded.evidence["outcome"],
        excluded.evidence["input_disposition"],
        excluded.reference_text,
        excluded.failure_code,
    ) == ("hit", "all_excluded", None, "memory_input_unavailable")
    assert excluded.evidence["hit_count"] == 1


def test_unicode_budget_measures_serialized_codepoints_and_stops_at_exact_boundary():
    from agent_alfred.memory.retrieval_gate import retrieve

    # One supplementary character remains one codepoint; quotes need escaping.
    serialized = (
        '[{"id":"a","version":1,"subject":"主题","fact":"😀\\"",'
        '"origin":{"type":"manual","source":"web"}}]'
    )
    for budget, selected in ((len(serialized), 1), (len(serialized) - 1, 0)):
        result = retrieve(
            fallback_decision("recall"),
            SearchBackend([fact_hit("a", '😀"')]),
            SearchBackend(),
            fallback_reason="model_unavailable",
            per_store_character_budget=budget,
        )
        assert result.evidence["selected_count"] == selected
        if selected:
            assert (
                result.reference_text
                == "检索参考资料\nsemantic=" + serialized + "\nepisodic=[]"
            )


def test_each_group_has_independent_budget_and_selected_order():
    from datetime import UTC, datetime

    from agent_alfred.memory.retrieval_gate import retrieve
    from agent_alfred.memory.types import (
        EpisodeHit,
        EpisodeRecord,
        MemoryId,
        ToolOrigin,
    )

    now = datetime(2026, 1, 1, tzinfo=UTC)
    origin = ToolOrigin("call")
    episode = EpisodeHit(
        EpisodeRecord(
            MemoryId("episode"), "回忆", now, None, origin, now, 2, now, origin, False
        )
    )
    result = retrieve(
        fallback_decision("recall"),
        SearchBackend([fact_hit("long", "x" * 1000), fact_hit("short", "x")]),
        SearchBackend([episode]),
        fallback_reason="model_unavailable",
        per_store_character_budget=200,
    )
    assert result.selected_references == (
        {"kind": "episodic", "memory_id": "episode", "record_version": 2},
    )
    assert result.evidence["input_disposition"] == "partial"
    assert result.reference_text == (
        "检索参考资料\n本次仅提供部分召回\nsemantic=[]\nepisodic="
        '[{"id":"episode","version":2,"summary":"回忆",'
        '"occurred_at":"2026-01-01T00:00:00+00:00",'
        '"occurred_until":null,"origin":{"type":"tool","call_id":"call"}}]'
    )


def test_skipped_gate_has_no_searches_and_timing_uses_injected_monotonic_clock():
    from agent_alfred.memory.retrieval_gate import retrieve

    semantic, episodic = SearchBackend(), SearchBackend()
    result = retrieve(
        fallback_decision("你好"),
        semantic,
        episodic,
        fallback_reason="model_deadline",
        gate_step_index=3,
        model_ref={
            "endpoint_id": "ep",
            "model_id": "model",
            "wire_style": "responses",
            "url": "secret",
        },
        model_ms=4000.0,
        started_at=1.0,
        clock=lambda: 5.0,
    )
    assert semantic.queries == episodic.queries == []
    assert result.evidence["outcome"] == "skip"
    assert result.evidence["hit_count"] == result.evidence["selected_count"] == 0
    assert result.evidence["latency_ms"] == 4000.0
    assert result.evidence["timing"] == {
        "model_ms": 4000.0,
        "search_ms": None,
        "selection_ms": None,
    }
    assert result.evidence["model_ref"] == {
        "endpoint_id": "ep",
        "model_id": "model",
        "wire_style": "responses",
    }
    assert result.evidence["gate_step_index"] == 3


@pytest.mark.parametrize(
    "config",
    [
        {"per_store_limit": True},
        {"per_store_limit": 0},
        {"per_store_character_budget": 0},
        {"per_store_character_budget": 1.5},
    ],
)
def test_invalid_gate_search_configuration_is_rejected(config):
    from agent_alfred.memory.retrieval_gate import retrieve

    with pytest.raises(ValueError, match="invalid_memory_gate_config"):
        retrieve(fallback_decision("你好"), SearchBackend(), SearchBackend(), **config)


def test_saved_fact_is_retrieved_with_origin_then_delete_removes_next_gate_hit():
    import hashlib
    import hmac
    import sqlite3

    from agent_alfred.memory.episodic import SQLiteEpisodicStore
    from agent_alfred.memory.retrieval_gate import retrieve
    from agent_alfred.memory.semantic import SQLiteSemanticStore
    from agent_alfred.memory.types import ManualOrigin
    from agent_alfred.schema import migrate

    def fingerprint(value):
        return hmac.new(b"gate-test", value, hashlib.sha256).hexdigest(), "key1"

    conn = sqlite3.connect(":memory:")
    try:
        migrate(conn)
        semantic = SQLiteSemanticStore(conn, fingerprint=fingerprint)
        episodic = SQLiteEpisodicStore(conn, fingerprint=fingerprint)
        with conn:
            conn.execute("BEGIN")
            saved = semantic.save("Alice", "coriander preference", ManualOrigin("web"))
            unrelated = semantic.save("Alice", "travel plans", ManualOrigin("web"))
        result = retrieve(
            fallback_decision("coriander"),
            semantic,
            episodic,
            fallback_reason="model_unavailable",
        )
        assert result.evidence["hit_count"] == result.evidence["selected_count"] == 1
        assert result.evidence["references"] == [
            {
                "kind": "semantic",
                "memory_id": saved,
                "record_version": 1,
                "origin": {"type": "manual", "source": "web"},
                "rank": 1,
                "selected": True,
                "omission_reason": None,
            }
        ]
        with conn:
            conn.execute("BEGIN")
            semantic.delete(saved, expected_version=1)
        next_result = retrieve(
            fallback_decision("coriander"),
            semantic,
            episodic,
            fallback_reason="model_unavailable",
        )
        assert next_result.evidence["outcome"] == "miss"
        assert semantic.get(unrelated) is not None
    finally:
        conn.close()


def test_edit_invalidates_old_reference_without_searching_or_promoting_omitted_hits():
    from agent_alfred.memory.retrieval_gate import retrieve

    semantic = SearchBackend(
        [
            fact_hit("first", "old secret"),
            fact_hit("long", "x" * 1000),
            fact_hit("later", "later secret"),
        ]
    )
    episodic = SearchBackend()
    result = retrieve(
        fallback_decision("recall"),
        semantic,
        episodic,
        fallback_reason="model_unavailable",
        per_store_character_budget=200,
    )
    # Store is now unavailable: invalidation cannot silently fetch replacement data.
    semantic.error = RuntimeError("must not query")
    before = result.reference_set
    assert before.selected_references == (
        {"kind": "semantic", "memory_id": "first", "record_version": 1},
    )
    after = before.invalidate("semantic", "first")
    assert after.reference_text is None
    assert after.selected_references == ()
    assert after.invalidate("semantic", "first") == after
    assert (
        result.evidence["selected_count"] == 1
    )  # The completed gate stays historical.
    assert before.invalidate("episodic", "first") == before


def test_edit_keeps_unrelated_selected_version_and_marks_reduced_input_partial():
    from agent_alfred.memory.retrieval_gate import retrieve

    result = retrieve(
        fallback_decision("recall"),
        SearchBackend(
            [fact_hit("first", "old secret"), fact_hit("second", "retained")]
        ),
        SearchBackend(),
        fallback_reason="model_unavailable",
    )
    after = result.reference_set.invalidate("semantic", "first")
    assert after.selected_references == (
        {"kind": "semantic", "memory_id": "second", "record_version": 1},
    )
    assert after.reference_text == (
        "检索参考资料\n本次仅提供部分召回\n"
        'semantic=[{"id":"second","version":1,"subject":"主题","fact":"retained",'
        '"origin":{"type":"manual","source":"web"}}]\nepisodic=[]'
    )
    assert "old secret" not in after.reference_text
    assert result.evidence["input_disposition"] == "ready"


def test_deep_expression_and_json_inputs_never_raise_recursion_error():
    from agent_alfred.memory.retrieval_gate import parse_model_decision

    expression = "(" * 10_000 + "1+2" + ")" * 10_000
    assert fallback_decision(expression).reason_code == "arithmetic"
    invalid = expression[:-1]
    assert fallback_decision(invalid).query == invalid
    with pytest.raises(ValueError, match="invalid_gate_output"):
        parse_model_decision("[" * 10_000 + "null" + "]" * 10_000)
