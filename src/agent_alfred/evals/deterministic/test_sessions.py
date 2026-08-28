"""The ADR-0027 segmented-cursor session read side, driven only through the
public API (RuntimeHost.list_sessions / RuntimeHost.open_session).

Seeding migrates a real v2 database -- that is setup, not assertion; every
assertion about visibility, completeness, and ordering goes through the
public read interface. Raw SQL appears only where the test checks that the
migration itself preserved bytes (test_schema_v3 owns those).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.sessions import MalformedCursor, SessionNotFound
from agent_alfred.settings import Settings

_TS = "2026-08-27T12:00:00+00:00"


def _database_through_version(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.configure_connection(conn)
    for migration in schema.MIGRATIONS:
        if migration.version > version:
            break
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (migration.version, _TS),
        )
    conn.commit()
    return conn


def _seed_historic(conn: sqlite3.Connection, session_id: str, turns: list[str]) -> None:
    for i, text in enumerate(turns):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, telemetry, created_at
               ) VALUES (?, ?, ?, 'cli', ?, ?)""",
            (
                session_id,
                "user" if i % 2 == 0 else "assistant",
                json.dumps([{"type": "text", "text": text}], ensure_ascii=False),
                (
                    json.dumps({"legacy": True, "trace_incomplete": False})
                    if i % 2 == 1
                    else None
                ),
                f"legacy-time-{i:02d}",
            ),
        )


def _migrated_host(
    conn: sqlite3.Connection, script: list | None = None
) -> RuntimeHost:
    schema.migrate(conn)
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    host = RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(script or ["pong"])),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([capture], process_instance_id="proc-sessions"),
        process_instance_id="proc-sessions",
    )
    return host


def _collect(host: RuntimeHost, session_id: str, *, page_size: int):
    """Walk every page through the public cursor; return (messages, pages)."""
    pages = []
    messages = []
    cursor = None
    while True:
        page = host.open_session(session_id, page_size=page_size, cursor=cursor)
        pages.append(page)
        messages.extend(page.messages)
        if page.next_cursor is None:
            return messages, pages
        cursor = page.next_cursor


# --- only new Runs -----------------------------------------------------------


def test_only_new_run_pairs_page_stably_by_small_page_sizes() -> None:
    host = _migrated_host(
        sqlite3.connect(":memory:", check_same_thread=False), script=["pong", "pong"]
    )
    host.start()
    try:
        session_id = host.create_session()
        first = host.submit(SubmitRequest(message="q1", session_id=session_id))
        host.wait(first.run_id)
        second = host.submit(SubmitRequest(message="q2", session_id=session_id))
        host.wait(second.run_id)

        messages, pages = _collect(host, session_id, page_size=1)
        # Page size counts Run pairs: one pair per page in segment one.
        assert [(m.role, message_plain_text_message(m)) for m in messages] == [
            ("user", "q1"),
            ("assistant", "pong"),
            ("user", "q2"),
            ("assistant", "pong"),
        ]
        assert all(m.run_id in (first.run_id, second.run_id) for m in messages)
        assert [m.run_id for m in messages] == [
            first.run_id,
            first.run_id,
            second.run_id,
            second.run_id,
        ]
        assert len(pages) == 2
        assert pages[-1].next_cursor is None
        assert all(m.telemetry is None for m in messages)
    finally:
        host.close()


def test_only_historic_messages_page_by_id_and_stay_verbatim() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "weird historic id", ["旧一", "答一", "旧二", "答二"])
    host = _migrated_host(conn)
    host.start()
    try:
        messages, pages = _collect(host, "weird historic id", page_size=1)
        assert [(m.role, message_plain_text_message(m)) for m in messages] == [
            ("user", "旧一"),
            ("assistant", "答一"),
            ("user", "旧二"),
            ("assistant", "答二"),
        ]
        assert all(m.run_id is None for m in messages), "no fabricated Runs"
        assert [m.created_at for m in messages] == [
            "legacy-time-00",
            "legacy-time-01",
            "legacy-time-02",
            "legacy-time-03",
        ]
        assert [m.telemetry for m in messages] == [
            None,
            {"legacy": True, "trace_incomplete": False},
            None,
            {"legacy": True, "trace_incomplete": False},
        ]
        assert len(pages) == 4
    finally:
        host.close()


# --- mixed segments and the cross-segment boundary ---------------------------


def test_mixed_session_orders_new_pairs_first_then_historic() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-mix", ["h1", "h2", "h3"])
    host = _migrated_host(conn, script=["new1"])
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="new-question", session_id="s-mix")
        )
        host.wait(submitted.run_id)

        messages, pages = _collect(host, "s-mix", page_size=1)
        roles_and_texts = [
            (m.role, message_plain_text_message(m)) for m in messages
        ]
        assert roles_and_texts == [
            ("user", "new-question"),
            ("assistant", "new1"),
            ("user", "h1"),
            ("assistant", "h2"),
            ("user", "h3"),
        ]
        # No duplicate, no gap, no reordering across the segment boundary.
        run_ids = [m.run_id for m in messages[:2]]
        assert run_ids == [submitted.run_id, submitted.run_id]
        assert all(m.run_id is None for m in messages[2:])
        assert pages[-1].next_cursor is None
    finally:
        host.close()


def test_boundary_page_splits_exactly_at_the_segment_edge() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-edge", ["h1", "h2"])
    host = _migrated_host(conn, script=["new1"])
    host.start()
    try:
        submitted = host.submit(SubmitRequest(message="nq", session_id="s-edge"))
        host.wait(submitted.run_id)

        # page_size=1: page 1 = the new pair (page full, segment exhausted);
        # page 2 = the first historic message; page 3 = the second.
        page1 = host.open_session("s-edge", page_size=1)
        assert page1.next_cursor is not None
        page2 = host.open_session("s-edge", page_size=1, cursor=page1.next_cursor)
        assert [message_plain_text_message(m) for m in page2.messages] == ["h1"]
        page3 = host.open_session("s-edge", page_size=1, cursor=page2.next_cursor)
        assert [message_plain_text_message(m) for m in page3.messages] == ["h2"]
        assert page3.next_cursor is None

        # Re-using an earlier cursor returns the same page again.
        again = host.open_session("s-edge", page_size=1, cursor=page1.next_cursor)
        assert again == page2
    finally:
        host.close()


# --- empty pages, last pages, cursor reuse -----------------------------------


def test_empty_session_returns_one_empty_page_and_no_cursor() -> None:
    conn = _database_through_version(2)
    host = _migrated_host(conn)
    host.start()
    try:
        schema.insert_session(conn, session_id="s-empty", created_at=_TS)
        conn.commit()
        page = host.open_session("s-empty", page_size=3)
        assert page.messages == ()
        assert page.next_cursor is None
    finally:
        host.close()


def test_full_last_page_does_not_emit_a_trailing_empty_page() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-exact", ["a", "b"])
    host = _migrated_host(conn)
    host.start()
    try:
        page = host.open_session("s-exact", page_size=2)
        assert len(page.messages) == 2
        assert page.next_cursor is None
    finally:
        host.close()


# --- inbox -------------------------------------------------------------------


def test_inbox_lists_historic_and_new_sessions_newest_first_with_titles() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "weird id", ["最旧的历史问题", "最旧的历史回答"])
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES ('会话-2', 'user', ?, 'web', NULL, 'not-iso')""",
        (
            json.dumps(
                [{"type": "text", "text": "第二个会话的问题"}], ensure_ascii=False
            ),
        ),
    )
    conn.execute(
        """INSERT INTO agent_log (
             session_id, role, content, source, telemetry, created_at
           ) VALUES ('s-silent', 'assistant', ?, 'cli', NULL, 'x')""",
        (json.dumps([{"type": "text", "text": "只有助手的历史行"}]),),
    )
    host = _migrated_host(conn, script=["ok"])
    host.start()
    try:
        fresh = host.create_session()
        submitted = host.submit(SubmitRequest(message="brand new", session_id=fresh))
        host.wait(submitted.run_id)

        page = host.list_sessions(limit=10)
        summaries = {s.session_id: s for s in page.sessions}
        assert page.next_cursor is None
        # Newest activity first; the new Run stamped the freshest revision.
        assert list(summaries) == [
            fresh,
            "s-silent",
            "会话-2",
            "weird id",
        ]
        assert summaries[fresh].title == "brand new"
        # Titles of Run-less historic sessions derive from the first historic
        # user message, redacted and limited; assistant-only falls back.
        assert summaries["会话-2"].title == "第二个会话的问题"
        assert summaries["weird id"].title == "最旧的历史问题"
        assert summaries["s-silent"].title.startswith("新会话 · ")
        # Historic created_at text is copied verbatim, not re-parsed.
        assert summaries["weird id"].created_at == "legacy-time-00"
        assert summaries["会话-2"].created_at == "not-iso"
    finally:
        host.close()


def test_inbox_pages_with_small_limits_without_skipping() -> None:
    conn = _database_through_version(2)
    for i in range(5):
        _seed_historic(conn, f"hist-{i}", [f"q{i}"])
    host = _migrated_host(conn)
    host.start()
    try:
        seen: list[str] = []
        cursor = None
        while True:
            page = host.list_sessions(limit=2, cursor=cursor)
            seen.extend(s.session_id for s in page.sessions)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert len(seen) == 5
        assert len(set(seen)) == 5
        # Every historic session received its revision in max-id order, so
        # the newest-written historic session must come first.
        assert seen[0] == "hist-4"
    finally:
        host.close()


# --- stability across migration re-runs --------------------------------------


def test_cursor_results_are_stable_across_a_migration_re_run() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-stable", ["q1", "a1", "q2"])
    host = _migrated_host(conn)
    host.start()
    try:
        before_pages = _collect(host, "s-stable", page_size=1)[1]
        before_inbox = host.list_sessions(limit=10)
        schema.migrate(conn)
        after_pages = _collect(host, "s-stable", page_size=1)[1]
        after_inbox = host.list_sessions(limit=10)
        assert before_pages == after_pages
        assert before_inbox == after_inbox
    finally:
        host.close()


# --- fail-closed cursor handling ---------------------------------------------


def test_malformed_cursor_is_rejected_not_restarted() -> None:
    conn = _database_through_version(2)
    for i in range(3):
        _seed_historic(conn, f"s-cursor-{i}", ["q"])
    host = _migrated_host(conn)
    host.start()
    try:
        with pytest.raises(MalformedCursor):
            host.open_session("s-cursor-0", page_size=2, cursor="garbage")
        inbox_cursor = host.list_sessions(limit=1).next_cursor
        assert inbox_cursor is not None
        # An inbox cursor does not fit the message read; it is refused.
        with pytest.raises(MalformedCursor):
            host.open_session("s-cursor-0", page_size=2, cursor=inbox_cursor)
        with pytest.raises(SessionNotFound):
            host.open_session("no-such-session", page_size=2)
    finally:
        host.close()


def message_plain_text_message(message) -> str:
    from agent_alfred.messages import blocks_to_jsonable

    texts = [
        block["text"]
        for block in blocks_to_jsonable(message.blocks)
        if block.get("type") == "text"
    ]
    return "".join(texts)
