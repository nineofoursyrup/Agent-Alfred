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
import threading
import time

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
    conn: sqlite3.Connection,
    script: list | None = None,
    *,
    before_recording_commit: threading.Event | None = None,
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
        before_recording_commit=before_recording_commit,
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


# --- an in-flight Run must not close the runs segment (the #30 lease) --------


class _SelectiveLatch:
    """``before_recording_commit`` stand-in that waits only while armed, so a
    test can pause one Run's finalize transaction without stalling others."""

    def __init__(self):
        self._gate = threading.Event()
        self._gate.set()

    def arm(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def wait(self, timeout=None):
        return self._gate.wait(timeout)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def test_inflight_run_holds_the_runs_cursor_open() -> None:
    """Run A recorded, Run B accepted-but-unrecorded: reading A and paging on
    must not permanently close the runs segment; once B settles the same
    cursor continues into B without duplication or omission."""
    conn = _database_through_version(2)
    _seed_historic(conn, "s-race", ["h1", "h2"])
    latch = _SelectiveLatch()
    host = _migrated_host(
        conn, script=["a-reply", "b-reply"], before_recording_commit=latch
    )
    host.start()
    try:
        first = host.submit(SubmitRequest(message="a-q", session_id="s-race"))
        host.wait(first.run_id)
        latch.arm()
        second = host.submit(SubmitRequest(message="b-q", session_id="s-race"))
        assert second.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )

        page1 = host.open_session("s-race", page_size=1)
        assert [
            (m.role, message_plain_text_message(m)) for m in page1.messages
        ] == [("user", "a-q"), ("assistant", "a-reply")]
        # The runs segment is not closed while B is in flight: the cursor
        # must remain a runs-segment cursor that can be continued later.
        assert page1.next_cursor is not None
        assert page1.runs_pending is True

        latch.release()
        host.wait(second.run_id)

        page2 = host.open_session(
            "s-race", page_size=1, cursor=page1.next_cursor
        )
        assert [
            (m.role, message_plain_text_message(m)) for m in page2.messages
        ] == [("user", "b-q"), ("assistant", "b-reply")]
        assert all(m.run_id == second.run_id for m in page2.messages)

        # Continuing the same walk reaches the historic segment exactly once.
        rest: list = []
        cursor = page2.next_cursor
        while cursor is not None:
            page = host.open_session("s-race", page_size=1, cursor=cursor)
            rest.extend(page.messages)
            cursor = page.next_cursor
        assert [message_plain_text_message(m) for m in rest] == ["h1", "h2"]

        everything, _ = _collect(host, "s-race", page_size=1)
        assert [message_plain_text_message(m) for m in everything] == [
            "a-q",
            "a-reply",
            "b-q",
            "b-reply",
            "h1",
            "h2",
        ]
    finally:
        latch.release()
        host.close()


def test_inflight_run_blocks_historic_until_it_settles() -> None:
    """Historic messages wait behind an in-flight Run: handing them out first
    would order the settled Run's later messages after the historic ones."""
    conn = _database_through_version(2)
    _seed_historic(conn, "s-wait", ["h1", "h2"])
    latch = _SelectiveLatch()
    host = _migrated_host(
        conn, script=["a-reply", "b-reply"], before_recording_commit=latch
    )
    host.start()
    try:
        first = host.submit(SubmitRequest(message="a-q", session_id="s-wait"))
        host.wait(first.run_id)
        latch.arm()
        second = host.submit(SubmitRequest(message="b-q", session_id="s-wait"))
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )

        page = host.open_session("s-wait", page_size=5)
        assert [
            message_plain_text_message(m) for m in page.messages
        ] == ["a-q", "a-reply"], "historic rows must not leak past an in-flight Run"
        assert page.next_cursor is not None
        assert page.runs_pending is True
        # The wait cursor is positional (after A): polling it while B is in
        # flight yields the deterministic empty wait page, not a tight-loop
        # of fresh content.
        again = host.open_session("s-wait", page_size=5, cursor=page.next_cursor)
        assert again.messages == ()
        assert again.runs_pending is True
        once_more = host.open_session(
            "s-wait", page_size=5, cursor=page.next_cursor
        )
        assert once_more == again

        latch.release()
        host.wait(second.run_id)
        resumed = host.open_session(
            "s-wait", page_size=5, cursor=page.next_cursor
        )
        assert [
            message_plain_text_message(m) for m in resumed.messages
        ] == ["b-q", "b-reply", "h1", "h2"]
        assert resumed.next_cursor is None
    finally:
        latch.release()
        host.close()


def test_first_inflight_run_yields_an_empty_wait_page() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    hold = threading.Event()
    host = _migrated_host(conn, script=["b-reply"], before_recording_commit=hold)
    host.start()
    try:
        session_id = host.create_session()
        submitted = host.submit(
            SubmitRequest(message="b-q", session_id=session_id)
        )
        assert submitted.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        page = host.open_session(session_id, page_size=3)
        assert page.messages == ()
        assert page.next_cursor is not None, (
            "a fresh read must not declare the runs segment exhausted while "
            "the Session's first Run is still in flight"
        )
        assert page.runs_pending is True

        hold.set()
        host.wait(submitted.run_id)
        resumed = host.open_session(
            session_id, page_size=3, cursor=page.next_cursor
        )
        assert [
            message_plain_text_message(m) for m in resumed.messages
        ] == ["b-q", "b-reply"]
        assert resumed.next_cursor is None
    finally:
        hold.set()
        host.close()


class _FailFinalizeWhen:
    """Connection wrapper that fails the finalize UPDATE while armed."""

    def __init__(self, inner: sqlite3.Connection, flag: dict):
        self._inner = inner
        self._flag = flag

    def execute(self, sql, parameters=()):
        text = sql.lstrip().upper()
        if self._flag["armed"] and text.startswith("UPDATE") and "finished_at" in sql:
            raise sqlite3.OperationalError("injected finalize failure")
        return self._inner.execute(sql, parameters)

    def commit(self):
        return self._inner.commit()

    def rollback(self):
        return self._inner.rollback()

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_recording_failed_run_releases_the_runs_segment() -> None:
    """A Run whose recording failed never produces messages; the authoritative
    failed projection must close the runs segment so the Session can reach its
    historic rows instead of waiting on a Run that will never appear."""
    flag = {"armed": False}
    conn = _database_through_version(2)
    _seed_historic(conn, "s-fail", ["h1"])
    wrapped = _FailFinalizeWhen(conn, flag)
    latch = _SelectiveLatch()
    host = _migrated_host(
        wrapped, script=["a-reply", "unused"], before_recording_commit=latch
    )
    host.start()
    try:
        first = host.submit(SubmitRequest(message="a-q", session_id="s-fail"))
        host.wait(first.run_id)

        latch.arm()
        second = host.submit(SubmitRequest(message="b-q", session_id="s-fail"))
        assert second.kind == "accepted"
        _wait_until(
            lambda: host.snapshot().coordinator_state == "recording_pending"
        )
        flag["armed"] = True
        latch.release()
        host.wait(second.run_id)
        snap = host.snapshot()
        assert snap.coordinator_state == "recording_failed"
        assert snap.unrecorded_terminal_projection is not None
        assert snap.unrecorded_terminal_projection.run_id == second.run_id
        assert snap.unrecorded_terminal_projection.recording_state == "failed"

        messages, pages = _collect(host, "s-fail", page_size=5)
        texts = [message_plain_text_message(m) for m in messages]
        assert texts == ["a-q", "a-reply", "h1"], (
            "the failed Run is never fabricated into the chat record"
        )
        assert all(m.run_id != second.run_id for m in messages)
        assert pages[-1].next_cursor is None
        assert all(page.runs_pending is False for page in pages)
    finally:
        latch.release()
        host.close()


def test_open_session_cursor_is_bound_to_its_session() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-left", ["q1", "a1"])
    _seed_historic(conn, "s-right", ["q2"])
    host = _migrated_host(conn)
    host.start()
    try:
        left = host.open_session("s-left", page_size=1)
        assert left.next_cursor is not None
        # Re-playing another Session's cursor would silently skip rows; it
        # must fail closed instead.
        with pytest.raises(MalformedCursor):
            host.open_session(
                "s-right", page_size=1, cursor=left.next_cursor
            )
        # The session's own cursor keeps working.
        right_first = host.open_session("s-right", page_size=1)
        again = host.open_session(
            "s-right", page_size=1, cursor=right_first.next_cursor
        )
        assert [message_plain_text_message(m) for m in again.messages] == ["q2"]
    finally:
        host.close()


# --- #30: the Session title's source of truth --------------------------------


def _insert_chat_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    session_id: str,
    prompt_preview: str | None,
) -> None:
    schema.insert_accepted_run(
        conn,
        run_id=run_id,
        purpose="chat",
        session_id=session_id,
        gateway="cli",
        accepted_at=f"2026-08-28T00:00:{len(run_id):02d}Z",
        prompt_preview=prompt_preview,
    )
    conn.commit()


def test_title_falls_back_to_created_at_when_the_earliest_preview_is_blank() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-blank", ["历史上的用户消息", "回答"])
    host = _migrated_host(conn)
    host.start()
    try:
        # The migrated Session already exists (backfilled from agent_log);
        # its created_at is the legacy time text copied verbatim.
        _insert_chat_run(
            conn, run_id="blank-1", session_id="s-blank", prompt_preview=""
        )
        conn.commit()
        page = host.list_sessions(limit=10)
        summary = next(
            s for s in page.sessions if s.session_id == "s-blank"
        )
        # A Run exists, so the historic user message is never the title; the
        # blank preview falls back to the created-at fallback.
        assert summary.title.startswith("新会话 · ")
        assert "历史上的用户消息" not in summary.title
        assert summary.title == f"新会话 · {summary.created_at}"

        page_open = host.open_session("s-blank", page_size=5)
        assert page_open.title == summary.title
    finally:
        host.close()


def test_title_ignores_a_whitespace_only_preview() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-space", ["应被忽略的历史用户消息"])
    host = _migrated_host(conn)
    host.start()
    try:
        _insert_chat_run(
            conn, run_id="space-1", session_id="s-space", prompt_preview="   "
        )
        conn.commit()
        page = host.list_sessions(limit=10)
        title = next(
            s.title for s in page.sessions if s.session_id == "s-space"
        )
        assert title.startswith("新会话 · ")
        assert "应被忽略的历史用户消息" not in title
    finally:
        host.close()


def test_title_treats_a_null_preview_like_a_blank_one() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-null", ["null 预览时不得使用的历史用户消息"])
    host = _migrated_host(conn)
    host.start()
    try:
        _insert_chat_run(
            conn, run_id="null-1", session_id="s-null", prompt_preview=None
        )
        conn.commit()
        page = host.list_sessions(limit=10)
        title = next(
            s.title for s in page.sessions if s.session_id == "s-null"
        )
        assert title.startswith("新会话 · ")
        assert "null 预览时不得使用的历史用户消息" not in title
    finally:
        host.close()


def test_title_uses_the_earliest_approved_chat_run_preview() -> None:
    conn = _database_through_version(2)
    host = _migrated_host(conn)
    host.start()
    try:
        schema.insert_session(conn, session_id="s-multi", created_at=_TS)
        _insert_chat_run(
            conn,
            run_id="multi-1",
            session_id="s-multi",
            prompt_preview="最早的预览",
        )
        _insert_chat_run(
            conn,
            run_id="multi-2",
            session_id="s-multi",
            prompt_preview="更晚的预览",
        )
        conn.commit()
        page = host.list_sessions(limit=10)
        title = next(
            s.title for s in page.sessions if s.session_id == "s-multi"
        )
        assert title == "最早的预览"
    finally:
        host.close()


def test_title_with_a_later_good_preview_but_blank_earliest_still_falls_back() -> None:
    """The title source is the earliest approved Run; a blank preview there
    is the created-at fallback, not the next Run's preview."""
    conn = _database_through_version(2)
    host = _migrated_host(conn)
    host.start()
    try:
        schema.insert_session(conn, session_id="s-order", created_at=_TS)
        _insert_chat_run(
            conn, run_id="order-1", session_id="s-order", prompt_preview=""
        )
        _insert_chat_run(
            conn,
            run_id="order-2",
            session_id="s-order",
            prompt_preview="后来的预览",
        )
        conn.commit()
        page = host.list_sessions(limit=10)
        title = next(
            s.title for s in page.sessions if s.session_id == "s-order"
        )
        assert title.startswith("新会话 · ")
        assert "后来的预览" not in title
    finally:
        host.close()


def test_title_without_any_run_still_uses_the_first_historic_user_message() -> None:
    conn = _database_through_version(2)
    _seed_historic(conn, "s-legacy", ["首条历史用户消息", "回答"])
    host = _migrated_host(conn)
    host.start()
    try:
        page = host.list_sessions(limit=10)
        title = next(
            s.title for s in page.sessions if s.session_id == "s-legacy"
        )
        assert title == "首条历史用户消息"
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
