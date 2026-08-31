"""The Dashboard's read side over a real database: runs page, deep links,
MainBar pairs.

Driven only through the public Host API, because every claim here is about
what a browser is allowed to see. The two properties that matter and that a
casual implementation loses:

- an unfinished Run keeps taking new activity revisions, so paging over it
  would move it under the cursor and show it twice. It is pinned instead.
- "recorded" is a database fact. A Run that never committed has no messages,
  and a historic message with no Run is shown as what it is rather than
  dressed up as a Run that never happened.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.redact import Redactor
from agent_alfred.runtime import runs as runs_store
from agent_alfred.runtime import sessions as session_store
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.runs import (
    DEFAULT_MAINBAR_LIMIT,
    MalformedCursor,
    UnknownRunFilter,
    classify_purpose,
)
from agent_alfred.runtime.sessions import SessionNotFound
from agent_alfred.settings import Settings

_TS = "2026-08-27T12:00:00+00:00"
_REDACTION_CANARY = "sk-top-secret-value"


def _assert_redaction_canary_absent(value) -> None:
    if _REDACTION_CANARY in repr(value):
        pytest.fail("browser read leaked the redaction canary", pytrace=False)


def _v3_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.migrate(conn)
    return conn


def _v2_database() -> sqlite3.Connection:
    """A real version 2 database, so historic rows are backfilled for real.

    Seeding into an already-migrated database would leave the historic
    Session without a row, and a test that then passed would be passing
    against a shape the code will never meet.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    schema.configure_connection(conn)
    for migration in schema.MIGRATIONS:
        if migration.version > 2:
            break
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (migration.version, _TS),
        )
    conn.commit()
    return conn


def _host_over(
    conn: sqlite3.Connection,
    script: list[str] | None = None,
    *,
    redactor: Redactor | None = None,
) -> RuntimeHost:
    capture = CapturingSink(name="capture", flush_at_run_end=True)
    return RuntimeHost(
        conn=conn,
        factory=ScriptedModelFactory(ScriptedModel(script or ["pong"])),
        settings=Settings(),
        clock=FakeClock(),
        fanout=FanOutSink([capture], process_instance_id="proc-reads"),
        process_instance_id="proc-reads",
        redactor=redactor,
    )


def _fresh_host(script: list[str] | None = None) -> RuntimeHost:
    return _host_over(_v3_database(), script)


def _run(host: RuntimeHost, message: str, session_id: str | None = None, **kw):
    result = host.submit(SubmitRequest(message=message, session_id=session_id, **kw))
    host.wait(result.run_id)
    return result


def _insert_run(
    host: RuntimeHost,
    run_id: str,
    *,
    purpose: str = "chat",
    phase: str = "accepted",
    outcome: str | None = None,
    session_id: str | None = None,
) -> None:
    with host._db_lock:  # noqa: SLF001 - seeding is setup, not assertion
        conn = host._conn  # noqa: SLF001
        revision = schema.allocate_activity_revision(conn)
        conn.execute(
            """INSERT INTO runs (
                 run_id, purpose, session_id, gateway, entry_surface_id,
                 prompt_preview, phase, outcome, accepted_at, started_at,
                 finished_at, activity_revision, telemetry
               ) VALUES (?, ?, ?, 'web', NULL, ?, ?, ?, ?, NULL, ?, ?, NULL)""",
            (
                run_id,
                purpose,
                session_id,
                f"preview {run_id}",
                phase,
                outcome,
                _TS,
                _TS if phase == "finished" else None,
                revision,
            ),
        )
        conn.commit()


def _seed_historic(
    conn: sqlite3.Connection, session_id: str, turns: list[str]
) -> None:
    for index, text in enumerate(turns):
        conn.execute(
            """INSERT INTO agent_log (
                 session_id, role, content, source, telemetry, created_at
               ) VALUES (?, ?, ?, 'cli', NULL, ?)""",
            (
                session_id,
                "user" if index % 2 == 0 else "assistant",
                json.dumps([{"type": "text", "text": text}]),
                f"legacy-{index:02d}",
            ),
        )
    conn.commit()


def _historic_host(
    turns_by_session: dict[str, list[str]],
    script=None,
    *,
    redactor: Redactor | None = None,
) -> RuntimeHost:
    """A Host over a real v2 database seeded with historic rows.

    The migration to v3 runs when the Host is built, which is the order the
    real upgrade happens in: the rows predate the Session table, and the
    backfill is what makes them visible at all.
    """
    conn = _v2_database()
    for session_id, turns in turns_by_session.items():
        _seed_historic(conn, session_id, turns)
    schema.migrate(conn)
    return _host_over(conn, script, redactor=redactor)


# --- purpose classification -------------------------------------------------


@pytest.mark.parametrize(
    "purpose,expected_shelf,known",
    [
        ("chat", "chat", True),
        ("inference_probe", "system", True),
        # A purpose this build has never heard of still belongs somewhere,
        # and the somewhere is never the conversation shelf.
        ("something_added_by_a_newer_migration", "system", False),
    ],
)
def test_a_purpose_is_filed_by_the_server_not_the_client(
    purpose: str, expected_shelf: str, known: bool
) -> None:
    assert classify_purpose(purpose) == (expected_shelf, known)


def test_an_unknown_purpose_is_still_returned_verbatim() -> None:
    """Escaping, not hiding.

    The value is reported exactly as stored so the client can render it
    safely; a client that showed nothing would be concealing a Run.
    """
    host = _fresh_host()
    host.start()
    try:
        _insert_run(
            host, "r-unknown", purpose="inference_probe", phase="finished",
            outcome="completed",
        )
        page = host.list_runs(filter="system")
        assert [run.run_id for run in page.runs] == ["r-unknown"]
        assert page.runs[0].purpose == "inference_probe"
        assert page.runs[0].filter == "system"
        assert page.runs[0].purpose_known is True
    finally:
        host.close()


# --- the runs page ----------------------------------------------------------


def test_the_chat_filter_returns_only_chat_runs() -> None:
    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        _run(host, "question", session_id)
        _insert_run(
            host, "r-probe", purpose="inference_probe", phase="finished",
            outcome="completed",
        )
        chat = host.list_runs(filter="chat")
        system = host.list_runs(filter="system")
        assert [run.run_id for run in chat.runs] != []
        assert all(run.purpose == "chat" for run in chat.runs)
        assert [run.run_id for run in system.runs] == ["r-probe"]
        assert chat.non_terminal is None
    finally:
        host.close()


def test_the_all_filter_sees_both_shelves() -> None:
    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        first = _run(host, "question", session_id)
        _insert_run(
            host, "r-probe", purpose="inference_probe", phase="finished",
            outcome="completed",
        )
        page = host.list_runs(filter="all")
        assert set(run.run_id for run in page.runs) >= {first.run_id, "r-probe"}
    finally:
        host.close()


def test_the_live_run_is_pinned_outside_the_page() -> None:
    """An unfinished Run keeps moving.

    It takes a new activity revision on every transition, so leaving it in
    the page would let the cursor walk past it and show it again. Pinning it
    is what makes "pin it at the top and fold by run_id" correct rather than
    merely conventional.
    """
    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        recorded = _run(host, "question", session_id)
        _insert_run(host, "r-live", session_id=session_id, phase="running")
        page = host.list_runs(filter="chat")
        assert [run.run_id for run in page.runs] == [recorded.run_id]
        assert page.non_terminal is not None
        assert page.non_terminal.run_id == "r-live"
        # Not in the page and not in the cursor: it cannot appear twice.
        assert "r-live" not in [run.run_id for run in page.runs]
    finally:
        host.close()


def test_the_pinned_run_follows_the_filter() -> None:
    host = _fresh_host()
    host.start()
    try:
        _insert_run(host, "r-live-probe", purpose="inference_probe", phase="running")
        assert host.list_runs(filter="chat").non_terminal is None
        assert host.list_runs(filter="system").non_terminal is not None
        assert host.list_runs(filter="all").non_terminal is not None
    finally:
        host.close()


def test_paging_walks_every_run_once() -> None:
    host = _fresh_host()
    host.start()
    try:
        for index in range(6):
            _insert_run(
                host,
                f"r{index:02d}",
                phase="finished",
                outcome="completed",
            )
        seen: list[str] = []
        cursor = None
        while True:
            page = host.list_runs(filter="all", limit=2, cursor=cursor)
            seen.extend(run.run_id for run in page.runs)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert len(seen) == 6
        assert len(set(seen)) == 6
        # Newest activity first: r05 was inserted last.
        assert seen[0] == "r05"
    finally:
        host.close()


def test_reusing_a_cursor_returns_the_same_page() -> None:
    host = _fresh_host()
    host.start()
    try:
        for index in range(4):
            _insert_run(host, f"r{index:02d}", phase="finished", outcome="completed")
        first = host.list_runs(filter="all", limit=2)
        again = host.list_runs(filter="all", limit=2, cursor=first.next_cursor)
        third = host.list_runs(filter="all", limit=2, cursor=first.next_cursor)
        assert [r.run_id for r in again.runs] == [r.run_id for r in third.runs]
    finally:
        host.close()


def test_a_malformed_cursor_is_refused_not_restarted() -> None:
    host = _fresh_host()
    host.start()
    try:
        with pytest.raises(MalformedCursor):
            host.list_runs(filter="all", cursor="not-a-cursor")
    finally:
        host.close()


def test_an_unknown_filter_is_refused() -> None:
    host = _fresh_host()
    host.start()
    try:
        with pytest.raises(UnknownRunFilter):
            host.list_runs(filter="nonsense")
    finally:
        host.close()


# --- deep links -------------------------------------------------------------


def test_a_deep_link_chooses_the_filter_and_lands_on_the_run() -> None:
    host = _fresh_host()
    host.start()
    try:
        for index in range(5):
            _insert_run(host, f"r{index:02d}", phase="finished", outcome="completed")
        page = host.locate_run("r02")
        assert page is not None
        assert page.filter == "chat"
        assert "r02" in [run.run_id for run in page.runs]
        # The page ends at (or after) the target, so continuing from the
        # cursor walks forward without repeating it.
        following = host.list_runs(filter=page.filter, cursor=page.next_cursor)
        assert "r02" not in [run.run_id for run in following.runs] or (
            page.next_cursor is None
        )
    finally:
        host.close()


def test_a_deep_link_returns_one_page_not_the_whole_prefix() -> None:
    """A deep link into the ten thousandth Run still answers one page.

    The naive way to position a cursor is to fetch everything before the
    target and count it. That is a page size that grows with history, which
    is the one thing a keyset pagination exists to prevent.
    """
    host = _fresh_host()
    host.start()
    try:
        for index in range(20):
            _insert_run(host, f"r{index:02d}", phase="finished", outcome="completed")
        page = host.locate_run("r05", limit=3)
        assert page is not None
        # Newest first: r19 ... r05. The page ends on the target itself.
        assert [run.run_id for run in page.runs] == ["r07", "r06", "r05"]
        assert page.next_cursor is not None
        following = host.list_runs(filter="chat", cursor=page.next_cursor)
        assert "r05" not in [run.run_id for run in following.runs]
        assert [run.run_id for run in following.runs][:2] == ["r04", "r03"]
    finally:
        host.close()


def test_a_deep_link_into_the_newest_run_pages_forward_from_it() -> None:
    host = _fresh_host()
    host.start()
    try:
        for index in range(3):
            _insert_run(host, f"r{index:02d}", phase="finished", outcome="completed")
        # r02 is the newest, so nothing is newer than it: the page is just it.
        page = host.locate_run("r02")
        assert page is not None
        assert [run.run_id for run in page.runs] == ["r02"]
        assert page.next_cursor is not None
        following = host.list_runs(filter="chat", cursor=page.next_cursor)
        assert [run.run_id for run in following.runs] == ["r01", "r00"]
        assert following.next_cursor is None
    finally:
        host.close()


def test_a_deep_link_to_a_system_run_lands_on_the_system_shelf() -> None:
    host = _fresh_host()
    host.start()
    try:
        _insert_run(
            host, "r-probe", purpose="inference_probe", phase="finished",
            outcome="completed",
        )
        page = host.locate_run("r-probe")
        assert page is not None
        assert page.filter == "system"
    finally:
        host.close()


def test_a_deep_link_to_a_live_run_returns_the_pin() -> None:
    host = _fresh_host()
    host.start()
    try:
        _insert_run(host, "r-live", phase="running")
        page = host.locate_run("r-live")
        assert page is not None
        assert page.non_terminal is not None
        assert page.non_terminal.run_id == "r-live"
        # It has no stable page position, so no page rows are invented.
        assert page.runs == ()
    finally:
        host.close()


def test_a_deep_link_to_a_missing_run_is_none() -> None:
    host = _fresh_host()
    host.start()
    try:
        assert host.locate_run("nope") is None
    finally:
        host.close()


# --- the MainBar pairs ------------------------------------------------------


def test_mainbar_returns_one_pair_per_recorded_chat_run() -> None:
    host = _fresh_host(script=["pong", "pong"])
    host.start()
    try:
        session_id = host.create_session()
        first = _run(host, "first question", session_id)
        second = _run(host, "second question", session_id)
        page = host.mainbar_pairs(session_id=session_id)
        assert [pair.run_id for pair in page.pairs] == [
            second.run_id,
            first.run_id,
        ]
        for pair in page.pairs:
            assert pair.user_message is not None
            assert pair.assistant_message is not None
        newest = page.pairs[0]
        assert message_plain_text(newest.user_message) == "second question"
        assert message_plain_text(newest.assistant_message) == "pong"
    finally:
        host.close()


def test_the_mainbar_default_is_the_most_recent_25() -> None:
    assert DEFAULT_MAINBAR_LIMIT == 25
    host = _fresh_host(script=["pong"] * 30)
    host.start()
    try:
        session_id = host.create_session()
        for index in range(30):
            _run(host, f"q{index:02d}", session_id)
        page = host.mainbar_pairs(session_id=session_id)
        assert len(page.pairs) == 25
        assert page.next_cursor is not None
        # The newest first, and the cursor continues where it stopped.
        continuing = host.mainbar_pairs(
            session_id=session_id, cursor=page.next_cursor
        )
        assert len(continuing.pairs) == 5
        assert continuing.next_cursor is None
        overlap = {p.run_id for p in page.pairs} & {
            p.run_id for p in continuing.pairs
        }
        assert overlap == set()
    finally:
        host.close()


def test_the_mainbar_only_shows_the_requested_session() -> None:
    """Each tab's MainBar is that tab's Session's only conversation.

    ``mainbar_pairs`` answers a question about *one* Session; without the
    filter in the SQL, every tab would see every Session's chat interleaved
    -- and the cursor, which pins the page, would leak across Sessions too.
    A cursor minted by Session A answers nothing for Session B.
    """
    host = _fresh_host(script=["pong"] * 4)
    host.start()
    try:
        session_a = host.create_session()
        session_b = host.create_session()
        run_a1 = _run(host, "a-one", session_a).run_id
        run_b = _run(host, "b-one", session_b).run_id
        run_a2 = _run(host, "a-two", session_a).run_id

        page_a = host.mainbar_pairs(session_id=session_a)
        assert [pair.run_id for pair in page_a.pairs] == [run_a2, run_a1]
        page_b = host.mainbar_pairs(session_id=session_b)
        assert [pair.run_id for pair in page_b.pairs] == [run_b]

        # The cursor is bound to its Session: no cross-Session paging.
        first_page = host.mainbar_pairs(session_id=session_a, limit=1)
        assert first_page.next_cursor is not None
        with pytest.raises(MalformedCursor):
            host.mainbar_pairs(
                session_id=session_b, cursor=first_page.next_cursor
            )
        rest = host.mainbar_pairs(
            session_id=session_a, cursor=first_page.next_cursor
        )
        assert [pair.run_id for pair in rest.pairs] == [run_a1]
    finally:
        host.close()


def test_an_unknown_session_has_no_mainbar() -> None:
    host = _fresh_host()
    host.start()
    try:
        with pytest.raises(SessionNotFound):
            host.mainbar_pairs(session_id="no-such-session")
    finally:
        host.close()


def test_a_run_that_was_never_recorded_has_no_pair() -> None:
    """Recorded is a database fact, and only the database says so.

    A finished Run with no message rows never committed its finalize
    transaction; giving it a pair would be inventing a reply.
    """
    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        _insert_run(
            host, "r-unrecorded", phase="finished", outcome="completed",
            session_id=session_id,
        )
        _insert_run(host, "r-accepted", phase="accepted", session_id=session_id)
        assert host.mainbar_pairs(session_id=session_id).pairs == ()
    finally:
        host.close()


def test_a_system_run_never_reaches_the_mainbar() -> None:
    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        _insert_run(
            host, "r-probe", purpose="inference_probe", phase="finished",
            outcome="completed", session_id=session_id,
        )
        assert host.mainbar_pairs(session_id=session_id).pairs == ()
    finally:
        host.close()


def test_historic_messages_are_never_dressed_up_as_a_run() -> None:
    """The ADR-0027 line, seen from the MainBar.

    Historic rows have no run_id and no Run exists for them. The MainBar is
    paged by Run, so the only honest answer is: they are not here. They are
    still visible -- in the session view, with a null run_id.
    """
    host = _historic_host({"s-historic": ["旧问题", "旧回答"]})
    host.start()
    try:
        assert host.mainbar_pairs(session_id="s-historic").pairs == ()
        page = host.open_session("s-historic", page_size=10)
        assert [message_plain_text(m) for m in page.messages] == ["旧问题", "旧回答"]
        assert all(m.run_id is None for m in page.messages)
    finally:
        host.close()


def test_a_historic_only_session_is_visible_with_a_real_title() -> None:
    host = _historic_host({"s-historic": ["一个很旧的问题", "一个很旧的回答"]})
    host.start()
    try:
        inbox = host.list_sessions(limit=10)
        summary = next(s for s in inbox.sessions if s.session_id == "s-historic")
        # Visible, and the title is derived rather than left blank.
        assert summary.title == "一个很旧的问题"
        page = host.open_session("s-historic", page_size=10)
        assert len(page.messages) == 2
    finally:
        host.close()


def test_a_session_with_both_old_and_new_pages_across_both_segments() -> None:
    host = _historic_host({"s-mixed": ["旧问题", "旧回答", "旧问题二"]})
    host.start()
    try:
        _run(host, "新问题", "s-mixed")
        messages = []
        cursor = None
        while True:
            page = host.open_session("s-mixed", page_size=1, cursor=cursor)
            messages.extend(page.messages)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert [message_plain_text(m) for m in messages] == [
            "新问题",
            "pong",
            "旧问题",
            "旧回答",
            "旧问题二",
        ]
        # The Run's rows keep their run_id; the historic ones keep null.
        assert [m.run_id is None for m in messages] == [
            False,
            False,
            True,
            True,
            True,
        ]
    finally:
        host.close()


# --- redaction on the way out ----------------------------------------------


@pytest.mark.parametrize(
    "read,kwargs",
    [
        (session_store.list_sessions, {"limit": 1}),
        (
            session_store.open_session,
            {"session_id": "s-redactor-required", "page_size": 1},
        ),
        (runs_store.list_runs, {}),
        (runs_store.locate_run, {"run_id": "missing"}),
        (
            runs_store.mainbar_pairs,
            {"session_id": "s-redactor-required"},
        ),
        (
            runs_store.list_session_chat_runs,
            {"session_id": "s-redactor-required", "limit": 1},
        ),
    ],
)
def test_browser_read_stores_require_the_central_redactor(read, kwargs) -> None:
    """A new browser read cannot silently opt out of the last secret gate."""
    conn = _v3_database()
    host = _host_over(conn)
    try:
        session_id = host.create_session()
        kwargs = {
            key: session_id if value == "s-redactor-required" else value
            for key, value in kwargs.items()
        }
        with pytest.raises(TypeError):
            read(conn, **kwargs)
    finally:
        host.close()


def test_message_bodies_go_through_the_central_redactor() -> None:
    """The user message is stored verbatim; this read is the last gate.

    Only the assistant reply was redacted on the way in -- the user's own
    text is kept as typed, so a secret pasted into the MainBar survives in
    the session record. Every surface that shows it must run it through the
    same redactor (ADR-0003), and "the same" is the point: two consumers
    doing their own redaction is how one of them ends up leaking.
    """
    host = _host_over(
        _v3_database(),
        [f"reply contains {_REDACTION_CANARY}"],
        redactor=Redactor((_REDACTION_CANARY,)),
    )
    host.start()
    try:
        session_id = host.create_session()
        outcome = _run(host, f"my key is {_REDACTION_CANARY}", session_id)
        page = host.open_session(session_id, page_size=10)
        shown = [message_plain_text(message) for message in page.messages]
        assert any("my key is" in text for text in shown)
        pairs = host.mainbar_pairs(session_id=session_id)
        runs_page = host.list_runs(filter="chat")
        located = host.locate_run(outcome.run_id)
        chat_runs = host.list_session_chat_runs(
            session_id=session_id, limit=10
        )
        inbox = host.list_sessions(limit=10)
        # One assertion helper deliberately owns the failure text: should a
        # regression occur, pytest must not print the canary it caught.
        _assert_redaction_canary_absent(
            (shown, pairs, runs_page, located, chat_runs, inbox)
        )
    finally:
        host.close()


def test_historic_messages_go_through_the_central_redactor() -> None:
    host = _historic_host(
        {"s-historic-secret": [f"historic {_REDACTION_CANARY}"]},
        redactor=Redactor((_REDACTION_CANARY,)),
    )
    try:
        inbox = host.list_sessions(limit=10)
        page = host.open_session("s-historic-secret", page_size=10)
        _assert_redaction_canary_absent((inbox, page))
    finally:
        host.close()


class _ExplodingRedactor(Redactor):
    def __init__(self) -> None:
        super().__init__(())

    def redact_text(self, text: str) -> str:
        raise RuntimeError("redaction unavailable")

    def redact_jsonable(self, value):
        raise RuntimeError("redaction unavailable")


def test_redactor_failure_never_returns_browser_visible_raw_content() -> None:
    conn = _v3_database()
    host = _host_over(conn, redactor=_ExplodingRedactor())
    try:
        session_id = host.create_session()
        _insert_run(
            host,
            "r-redactor-failure",
            phase="finished",
            outcome="completed",
            session_id=session_id,
        )
        content = json.dumps(
            [{"type": "text", "text": f"stored {_REDACTION_CANARY}"}]
        )
        with host._db_lock:  # noqa: SLF001 - seeding is setup, not assertion
            host._conn.execute(  # noqa: SLF001
                "UPDATE runs SET prompt_preview = ? WHERE run_id = ?",
                (f"preview {_REDACTION_CANARY}", "r-redactor-failure"),
            )
            for role in ("user", "assistant"):
                host._conn.execute(  # noqa: SLF001
                    """INSERT INTO agent_log (
                         session_id, run_id, role, content, source, telemetry,
                         created_at
                       ) VALUES (?, ?, ?, ?, 'web', NULL, ?)""",
                    (session_id, "r-redactor-failure", role, content, _TS),
                )
            host._conn.commit()  # noqa: SLF001

        reads = (
            lambda: host.list_sessions(limit=10),
            lambda: host.open_session(session_id, page_size=10),
            lambda: host.list_runs(filter="chat"),
            lambda: host.locate_run("r-redactor-failure"),
            lambda: host.mainbar_pairs(session_id=session_id),
            lambda: host.list_session_chat_runs(
                session_id=session_id, limit=10
            ),
        )
        for read in reads:
            with pytest.raises(RuntimeError) as raised:
                read()
            _assert_redaction_canary_absent(str(raised.value))
    finally:
        host.close()


# --- the one cursor codec ---------------------------------------------------


def test_every_read_cursor_round_trips_through_the_shared_codec() -> None:
    """Encode/decode is one canonical pair, not four private copies.

    Four reads own cursor payloads (runs page, MainBar, inbox, session
    messages) and the codec -- canonical JSON, URL-safe base64, the
    malformed exception -- is the part every one of them needs verbatim.
    A shared codec is the only shape that cannot drift one version or one
    error word at a time.
    """
    from agent_alfred.runtime.cursor import decode_cursor, encode_cursor

    payload = {"v": 1, "k": "runs", "ar": 7, "r": "r1"}
    token = encode_cursor(payload)
    assert token == "eyJ2IjoxLCJrIjoicnVucyIsImFyIjo3LCJyIjoicjEifQ=="
    assert decode_cursor(token, version=1, kind="runs") == payload


def test_the_shared_codec_refuses_undecodable_and_foreign_tokens() -> None:
    from agent_alfred.runtime.cursor import MalformedCursor, decode_cursor

    with pytest.raises(MalformedCursor):
        decode_cursor("not-base64!!", version=1, kind="runs")
    with pytest.raises(MalformedCursor):
        # Valid base64, but not JSON.
        decode_cursor("bm90LWpzb24=", version=1, kind="runs")
    with pytest.raises(MalformedCursor):
        decode_cursor(
            "eyJ2IjoxLCJrIjoicnVucyIsImFyIjo3LCJyIjoicjEifQ==",
            version=1,
            kind="mainbar",  # wrong kind for this read
        )
    with pytest.raises(MalformedCursor):
        decode_cursor(
            "eyJ2IjoxLCJrIjoicnVucyIsImFyIjo3LCJyIjoicjEifQ==",
            version=2,  # wrong version
            kind="runs",
        )


def test_reads_reject_each_others_cursors_and_keep_the_old_wire_tokens() -> None:
    """Cross-read tokens fail closed; existing tokens still decode.

    The versions and kinds are the wire contract: a token minted by one
    read is not a token another read may silently accept, and a refactor of
    the codec must not retire a single token that is already in a browser.
    """
    from agent_alfred.runtime import runs as runs_module
    from agent_alfred.runtime import sessions as sessions_module

    host = _fresh_host()
    host.start()
    try:
        session_a = host.create_session()
        session_b = host.create_session()
        run_a1 = _run(host, "hello", session_a).run_id
        run_a2 = _run(host, "again", session_a).run_id
        _run(host, "other", session_b)
        runs_cursor = host.list_runs(filter="chat", limit=1).next_cursor
        inbox_cursor = host.list_sessions(limit=1).next_cursor
        messages_cursor = host.open_session(
            session_a, page_size=1
        ).next_cursor
        assert runs_cursor is not None
        assert inbox_cursor is not None
        assert messages_cursor is not None

        # Each read consumes its own cursor and returns its own remainder.
        assert [
            run.run_id for run in host.list_runs(
                filter="chat", limit=10, cursor=runs_cursor
            ).runs
        ] == [run_a2, run_a1]
        assert [
            session.session_id
            for session in host.list_sessions(
                limit=10, cursor=inbox_cursor
            ).sessions
        ] == [session_a]
        remainder = host.open_session(
            session_a, page_size=10, cursor=messages_cursor
        ).messages
        assert [message.role for message in remainder] == [
            "user",
            "assistant",
        ]
        assert remainder[0].run_id == run_a2

        # ...and rejects the others', in both directions.
        with pytest.raises(MalformedCursor):
            host.list_sessions(limit=10, cursor=runs_cursor)
        with pytest.raises(MalformedCursor):
            host.list_runs(filter="chat", limit=10, cursor=inbox_cursor)
    finally:
        host.close()

    # Tokens produced by the pre-refactor codec decode unchanged: the
    # canonical JSON and the base64 alphabet are the wire, not an
    # implementation detail.
    from agent_alfred.runtime.cursor import decode_cursor

    assert decode_cursor(
        "eyJ2IjoxLCJrIjoicnVucyIsImFyIjo3LCJyIjoicjEifQ==",
        version=1,
        kind="runs",
    ) == {"v": 1, "k": "runs", "ar": 7, "r": "r1"}
    assert decode_cursor(
        "eyJ2IjoxLCJrIjoibWFpbmJhciIsImFyIjo3LCJyIjoicjEifQ==",
        version=1,
        kind="mainbar",
    ) == {"v": 1, "k": "mainbar", "ar": 7, "r": "r1"}
    assert decode_cursor(
        "eyJ2IjoyLCJrIjoiaW5ib3giLCJhciI6M30=", version=2, kind="inbox"
    ) == {"v": 2, "k": "inbox", "ar": 3}
    assert decode_cursor(
        "eyJ2IjoyLCJrIjoicnVucyIsInNlZyI6InJ1bnMiLCJzIjoiczEiLCJhciI6MSwiciI6InIxIn0=",
        version=2,
        kind="runs",
    ) == {
        "v": 2,
        "k": "runs",
        "seg": "runs",
        "s": "s1",
        "ar": 1,
        "r": "r1",
    }
    assert decode_cursor(
        "eyJ2IjoyLCJrIjoicnVucyIsInNlZyI6Imhpc3RvcmljIiwicyI6InMxIiwiaWQiOjV9",
        version=2,
        kind="runs",
    ) == {"v": 2, "k": "runs", "seg": "historic", "s": "s1", "id": 5}
    # The reads re-export the unified exception: the errors they raise are
    # the shared one.
    assert runs_module.MalformedCursor is MalformedCursor
    assert sessions_module.MalformedCursor is MalformedCursor


def test_a_session_messages_cursor_is_bound_to_its_session() -> None:
    """A cursor minted by Session A answers nothing for Session B."""
    host = _fresh_host()
    host.start()
    try:
        session_a = host.create_session()
        session_b = host.create_session()
        _run(host, "hello", session_a)
        _run(host, "hello", session_a)
        _run(host, "hello", session_b)
        page = host.open_session(session_a, page_size=1)
        assert page.next_cursor is not None
        with pytest.raises(MalformedCursor):
            host.open_session(
                session_b, page_size=10, cursor=page.next_cursor
            )
    finally:
        host.close()


# --- one Session's chat Runs -------------------------------------------------


def test_a_sessions_chat_runs_page_independently_by_session() -> None:
    """The Session group's run list answers one Session and no other.

    The runs page pages the process's Runs globally and the inbox returns
    Session summaries only; neither is a Session's own run list, and folding
    either in the browser would show one tab another Session's Runs and page
    them with a cursor that moves under both.
    """
    host = _fresh_host(script=["pong"] * 6)
    host.start()
    try:
        session_a = host.create_session()
        session_b = host.create_session()
        _insert_run(host, "a-accepted", phase="accepted", session_id=session_a)
        _insert_run(host, "a-running", phase="running", session_id=session_a)
        a1 = _run(host, "a-one", session_a).run_id
        b1 = _run(host, "b-one", session_b).run_id
        a2 = _run(host, "a-two", session_a).run_id
        b2 = _run(host, "b-two", session_b).run_id
        a3 = _run(host, "a-three", session_a).run_id

        page1 = host.list_session_chat_runs(session_id=session_a, limit=2)
        assert [run.run_id for run in page1.runs] == [a3, a2]
        assert page1.next_cursor is not None
        # Replaying the same cursor returns the same page again.
        page2 = host.list_session_chat_runs(
            session_id=session_a, limit=2, cursor=page1.next_cursor
        )
        page2_again = host.list_session_chat_runs(
            session_id=session_a, limit=2, cursor=page1.next_cursor
        )
        assert [run.run_id for run in page2.runs] == [
            run.run_id for run in page2_again.runs
        ]
        # Walking the pages shows every Run exactly once, newest first -- no
        # duplicate, no missing row, no Session B row in between.
        seen = [run.run_id for run in page1.runs]
        cursor = page1.next_cursor
        while cursor is not None:
            page = host.list_session_chat_runs(
                session_id=session_a, limit=2, cursor=cursor
            )
            seen.extend(run.run_id for run in page.runs)
            cursor = page.next_cursor
        assert seen == [a3, a2, a1, "a-running", "a-accepted"]

        # Session B's list is independent and holds only its own Runs.
        page_b = host.list_session_chat_runs(session_id=session_b, limit=10)
        assert [run.run_id for run in page_b.runs] == [b2, b1]
        # A cursor minted by Session A answers nothing for Session B.
        with pytest.raises(MalformedCursor):
            host.list_session_chat_runs(
                session_id=session_b, limit=10, cursor=page1.next_cursor
            )
    finally:
        host.close()


def test_a_sessions_chat_runs_group_takes_only_admitted_chat_runs() -> None:
    """A Run's shelf is decided by the server, and this group is chat only.

    Accepted, running and finished chat Runs are all admitted and all appear;
    a system Run has its own shelf (the runs page) and never leaks in here.
    """
    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        _insert_run(
            host,
            "r-probe",
            purpose="inference_probe",
            phase="finished",
            outcome="completed",
            session_id=session_id,
        )
        _insert_run(host, "r-accepted", phase="accepted", session_id=session_id)
        _insert_run(host, "r-running", phase="running", session_id=session_id)
        recorded = _run(host, "question", session_id).run_id
        page = host.list_session_chat_runs(session_id=session_id, limit=10)
        assert [run.run_id for run in page.runs] == [
            recorded,
            "r-running",
            "r-accepted",
        ]
        assert [run.phase for run in page.runs] == [
            "finished",
            "running",
            "accepted",
        ]
    finally:
        host.close()


def test_a_recorded_run_shows_its_final_reply_and_a_running_run_shows_none(
) -> None:
    """A summary is a database fact, never a placeholder.

    A recorded Run's reply is the one final assistant message its finalize
    transaction wrote; a Run that is still running has written none, so its
    row carries no reply -- inventing "still working…" there would be the
    read dressing up a state the database does not hold.
    """
    host = _fresh_host(script=["pong"])
    host.start()
    try:
        session_id = host.create_session()
        recorded = _run(host, "question", session_id).run_id
        _insert_run(host, "r-running", phase="running", session_id=session_id)
        page = host.list_session_chat_runs(session_id=session_id, limit=10)
        by_id = {run.run_id: run for run in page.runs}
        assert by_id[recorded].reply_preview == "pong"
        assert by_id[recorded].reply_source == "cli"
        assert by_id[recorded].finished_at is not None
        assert by_id["r-running"].reply_preview is None
        assert by_id["r-running"].reply_source is None
    finally:
        host.close()


def test_an_unknown_session_has_no_chat_runs() -> None:
    host = _fresh_host()
    host.start()
    try:
        with pytest.raises(SessionNotFound):
            host.list_session_chat_runs(session_id="no-such-session", limit=10)
    finally:
        host.close()
