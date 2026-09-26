"""Bounded supplemental retrieval through real Store and Host boundaries."""

import sqlite3

from agent_alfred.evals.deterministic.test_memory_stores import NOW, fingerprint
from agent_alfred.memory.semantic import SQLiteSemanticStore
from agent_alfred.memory.types import FactQuery, ManualOrigin
from agent_alfred.schema import migrate


def test_original_chinese_query_recalls_saved_preference_with_provenance():
    with sqlite3.connect(':memory:') as conn:
        migrate(conn)
        store = SQLiteSemanticStore(conn, fingerprint=fingerprint)
        conn.execute('BEGIN')
        saved = store.save('fixture',
                           '测试用户偏好低因咖啡（decaf coffee），下午不喝普通咖啡。',
                           ManualOrigin('web'))
        conn.commit()
        hits = store.search(FactQuery(text='咖啡偏好'))
        assert [hit.record.id for hit in hits] == [saved]
        assert hits[0].relevance == 'han-bigram-fallback-v1'


def test_episode_fallback_preserves_time_scope_and_is_not_used_after_fts_hits():
    from datetime import timedelta

    from agent_alfred.memory.episodic import SQLiteEpisodicStore
    from agent_alfred.memory.types import EpisodeQuery

    with sqlite3.connect(':memory:') as conn:
        migrate(conn)
        store = SQLiteEpisodicStore(conn, fingerprint=fingerprint)
        conn.execute('BEGIN')
        old = store.save('昨天骑车去湖边', NOW - timedelta(days=1), None,
                         ManualOrigin('web'))
        current = store.save('今天骑车去湖边', NOW, None, ManualOrigin('web'))
        conn.commit()
        hits = store.search(EpisodeQuery(text='骑车路线', since=NOW))
        assert [h.record.id for h in hits] == [current]
        assert hits[0].relevance == 'han-bigram-fallback-v1'
        # A real FTS hit outside the requested time window must not trigger a
        # second search after filtering it away.
        assert store.search(EpisodeQuery(text='昨天骑车去湖边', since=NOW)) == ()
        assert store.get(old) is not None


def test_host_records_supplemental_recall_and_sends_saved_body(tmp_path):
    from agent_alfred.connections import CredentialOverlay
    from agent_alfred.memory.commands import CommandContext
    from agent_alfred.messages import message_plain_text
    from agent_alfred.model import ScriptedModel, ScriptedModelFactory
    from agent_alfred.runtime.host import SubmitRequest
    from agent_alfred.settings import Settings
    from agent_alfred.wiring import build_default_host

    fact = '测试用户偏好低因咖啡（decaf coffee），下午不喝普通咖啡。'
    model = ScriptedModel([
        '{"retrieve":true,"query":"咖啡偏好","reason_code":"personal_information"}',
        'Offline boundary response; not a real quality result.',
    ])
    host = build_default_host(state_dir=tmp_path, settings=Settings(),
                              credentials=CredentialOverlay({}, None),
                              factory=ScriptedModelFactory(model))
    host.start()
    try:
        host.memory_service.execute({
            'operation_id': 'test-save', 'action': 'save', 'kind': 'semantic',
            'payload': {'subject': 'fixture', 'fact': fact},
        }, CommandContext(ManualOrigin('web'), 'web'))
        session = host.create_session()
        run = host.submit(SubmitRequest(
            '根据我保存的偏好，下午应该给我准备哪种咖啡？不要补充未记录的健康原因。',
            session_id=session))
        assert host.wait(run.run_id).outcome == 'completed'
        evidence = host.read_run_evidence(run.run_id, trace_root=tmp_path / 'traces')
        gate = evidence['memory']['gate']
        assert (gate['stores']['semantic']['retrieval_method']
                == 'han-bigram-fallback-v1')
        assert gate['selected_count'] == 1
        answer = model.requests[-1]
        text = '\n'.join(message_plain_text(m) for m in answer.messages)
        text += '\n'.join(b.text for b in answer.system or ())
        assert fact in text
        assert len(model.requests) == 2
    finally:
        host.close()


def test_fallback_never_augments_fts_and_preserves_scope_limit_and_deletion():
    with sqlite3.connect(':memory:') as conn:
        migrate(conn)
        store = SQLiteSemanticStore(conn, fingerprint=fingerprint)
        conn.execute('BEGIN')
        fallback = store.save('alice', '用户偏好低因咖啡', ManualOrigin('web'))
        exact = store.save('alice', '咖啡偏好', ManualOrigin('web'))
        store.save('bob', '用户偏好茶饮', ManualOrigin('web'))
        conn.commit()
        hits = store.search(FactQuery(text='咖啡偏好'))
        assert [h.record.id for h in hits] == [exact]
        assert hits[0].relevance is None
        conn.execute('BEGIN')
        store.delete(exact, expected_version=1)
        conn.commit()
        hits = store.search(FactQuery(text='咖啡偏好', subject='alice', limit=1))
        assert [h.record.id for h in hits] == [fallback]
        assert store.search(FactQuery(text='咖啡', subject='bob')) == ()
        assert store.search(FactQuery(text='咖')) == ()
        assert store.search(FactQuery(text='茶' * 129)) == ()
        conn.execute('BEGIN')
        store.delete(fallback, expected_version=1)
        conn.commit()
        assert store.search(FactQuery(text='咖啡', subject='alice')) == ()


def test_fallback_is_bounded_and_orders_by_match_count_then_recency():
    with sqlite3.connect(':memory:') as conn:
        migrate(conn)
        store = SQLiteSemanticStore(conn, fingerprint=fingerprint)
        conn.execute('BEGIN')
        old = store.save('old', '偏好低因咖啡', ManualOrigin('web'))
        for i in range(1000):
            store.save(str(i), '无关记录', ManualOrigin('web'))
        conn.commit()
        assert store.search(FactQuery(text='咖啡偏好')) == ()
        assert store.get(old) is not None
        conn.execute('BEGIN')
        best = store.save('new', '偏好低因咖啡', ManualOrigin('web'))
        recent = store.save('recent', '正在准备咖啡', ManualOrigin('web'))
        conn.commit()
        hits = store.search(FactQuery(text='咖啡偏好', limit=2))
        assert [h.record.id for h in hits] == [best, recent]
        page = store.search_page(FactQuery(text='咖啡偏好', limit=1))
        assert [r.id for r in page.records] == [best]
        second = store.search_page(FactQuery(text='咖啡偏好', limit=1),
                                    cursor=page.next_cursor)
        assert [r.id for r in second.records] == [recent]
