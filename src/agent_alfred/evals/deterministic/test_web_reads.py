"""The Dashboard's read side over a real database: runs page, deep links,
MainBar items.

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
import threading
from base64 import urlsafe_b64encode

import pytest

from agent_alfred import schema
from agent_alfred.clock import FakeClock
from agent_alfred.events import CapturingSink, FanOutSink
from agent_alfred.gateway.web.api import DashboardApi
from agent_alfred.messages import message_plain_text
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.redact import Redactor
from agent_alfred.runtime import runs as runs_store
from agent_alfred.runtime import sessions as session_store
from agent_alfred.runtime.host import RuntimeHost, SubmitRequest
from agent_alfred.runtime.runs import (
    DEFAULT_MAINBAR_LIMIT,
    MainBarHistoricMessage,
    MainBarRunPair,
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


def _current_database() -> sqlite3.Connection:
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
    before_recording_commit: threading.Event | None = None,
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
        before_recording_commit=before_recording_commit,
    )


def _fresh_host(script: list[str] | None = None) -> RuntimeHost:
    return _host_over(_current_database(), script)


def _run(host: RuntimeHost, message: str, session_id: str | None = None, **kw):
    result = host.submit(SubmitRequest(message=message, session_id=session_id, **kw))
    host.wait(result.run_id)
    return result


def _insert_run(
    host: RuntimeHost,
    run_id: str,
    *,
    purpose: str = "chat",
    gateway: str = "web",
    phase: str = "accepted",
    outcome: str | None = None,
    session_id: str | None = None,
    ignore_check_constraints: bool = False,
) -> None:
    with host._db_lock:  # noqa: SLF001 - seeding is setup, not assertion
        conn = host._conn  # noqa: SLF001
        if ignore_check_constraints:
            conn.execute("PRAGMA ignore_check_constraints = ON")
        try:
            revision = schema.allocate_activity_revision(conn)
            conn.execute(
                """INSERT INTO runs (
                     run_id, purpose, session_id, gateway, entry_surface_id,
                     prompt_preview, phase, outcome, accepted_at, started_at,
                     finished_at, activity_revision, telemetry, admission_state
                   ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, NULL,
                             'admitted')""",
                (
                    run_id,
                    purpose,
                    session_id,
                    gateway,
                    f"preview {run_id}",
                    phase,
                    outcome,
                    _TS,
                    _TS if phase == "finished" else None,
                    revision,
                ),
            )
            conn.commit()
        finally:
            if ignore_check_constraints:
                conn.execute("PRAGMA ignore_check_constraints = OFF")


def _seed_historic(
    conn: sqlite3.Connection, session_id: str, legacy_messages: list[str]
) -> None:
    for index, text in enumerate(legacy_messages):
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
    messages_by_session: dict[str, list[str]],
    script=None,
    *,
    redactor: Redactor | None = None,
    before_recording_commit: threading.Event | None = None,
) -> RuntimeHost:
    """A Host over a real v2 database seeded with historic Message rows.

    The migration to v3 runs when the Host is built, which is the order the
    real upgrade happens in: the rows predate the Session table, and the
    backfill is what makes them visible at all.
    """
    conn = _v2_database()
    for session_id, legacy_messages in messages_by_session.items():
        _seed_historic(conn, session_id, legacy_messages)
    schema.migrate(conn)
    return _host_over(
        conn,
        script,
        redactor=redactor,
        before_recording_commit=before_recording_commit,
    )


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


def test_the_read_model_preserves_an_unknown_purpose_for_server_rendering() -> None:
    """The transport-neutral row retains the value the HTTP edge must escape."""
    host = _fresh_host()
    host.start()
    try:
        _insert_run(
            host,
            "r-unknown",
            purpose="something_added_by_a_newer_migration",
            phase="finished",
            outcome="completed",
            ignore_check_constraints=True,
        )
        page = host.list_runs(filter="system")
        assert [run.run_id for run in page.runs] == ["r-unknown"]
        assert page.runs[0].purpose == "something_added_by_a_newer_migration"
        assert page.runs[0].filter == "system"
        assert page.runs[0].purpose_known is False
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


@pytest.mark.parametrize(
    "column,invalid_value,expected_error",
    [
        ("phase", "unknown", "invalid run phase"),
        ("outcome", "unknown", "invalid run outcome"),
        ("phase", "running", "invalid run lifecycle"),
        ("outcome", None, "invalid run lifecycle"),
    ],
)
def test_runs_page_rejects_invalid_persisted_lifecycle_values(
    column: str, invalid_value: object, expected_error: str
) -> None:
    host = _fresh_host()
    host.start()
    try:
        _insert_run(
            host,
            "r-corrupt",
            phase="finished",
            outcome="completed",
        )
        with host._db_lock:  # noqa: SLF001 - corrupt DB is test setup
            conn = host._conn  # noqa: SLF001
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                f"UPDATE runs SET {column} = ? WHERE run_id = ?",
                (invalid_value, "r-corrupt"),
            )
            conn.commit()
        with pytest.raises(ValueError, match=expected_error):
            host.list_runs(filter="chat")
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


@pytest.mark.parametrize(
    "run_id,expected_status", [("r1", 200), ("\ud800", 400), ("\udfff", 400)],
)
def test_runs_api_refuses_cursor_text_that_cannot_be_canonicalized(
    run_id, expected_status,
) -> None:
    payload = {"v": 1, "k": "runs", "ar": 7, "r": run_id}
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    cursor = urlsafe_b64encode(raw).decode("ascii")
    conn = _current_database()
    host = _host_over(conn)
    try:
        status, response = DashboardApi(facade=host).runs_page({"cursor": cursor})
        assert status == expected_status
        if expected_status == 400:
            assert response == {"code": "malformed_cursor"}
    finally:
        host.close()
        conn.close()


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


def test_mainbar_page_items_is_the_only_public_item_read_surface() -> None:
    host = _historic_host({"s-mainbar-items": ["旧消息"]}, script=["新回答"])
    host.start()
    try:
        recorded = _run(host, "新问题", "s-mainbar-items")
        page = host.mainbar_pairs(session_id="s-mainbar-items")

        removed_projection = "pairs"
        removed_alias = "MainBar" + "Pair"
        assert removed_projection not in dir(page)
        assert removed_alias not in vars(runs_store)
        assert tuple(type(item) for item in page.items) == (
            MainBarRunPair,
            MainBarHistoricMessage,
        )
        assert page.items[0].run_id == recorded.run_id
        assert message_plain_text(page.items[1].message) == "旧消息"
    finally:
        host.close()


def test_mainbar_returns_one_pair_per_recorded_chat_run() -> None:
    host = _fresh_host(script=["pong", "pong"])
    host.start()
    try:
        session_id = host.create_session()
        first = _run(host, "first question", session_id)
        second = _run(host, "second question", session_id)
        page = host.mainbar_pairs(session_id=session_id)
        assert all(isinstance(item, MainBarRunPair) for item in page.items)
        assert [item.run_id for item in page.items] == [
            second.run_id,
            first.run_id,
        ]
        for item in page.items:
            assert item.user_message is not None
            assert item.assistant_message is not None
        newest = page.items[0]
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
        assert len(page.items) == 25
        assert all(isinstance(item, MainBarRunPair) for item in page.items)
        assert page.next_cursor is not None
        # The newest first, and the cursor continues where it stopped.
        continuing = host.mainbar_pairs(
            session_id=session_id, cursor=page.next_cursor
        )
        assert len(continuing.items) == 5
        assert all(
            isinstance(item, MainBarRunPair) for item in continuing.items
        )
        assert continuing.next_cursor is None
        overlap = {item.run_id for item in page.items} & {
            item.run_id for item in continuing.items
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
        assert all(isinstance(item, MainBarRunPair) for item in page_a.items)
        assert [item.run_id for item in page_a.items] == [run_a2, run_a1]
        page_b = host.mainbar_pairs(session_id=session_b)
        assert all(isinstance(item, MainBarRunPair) for item in page_b.items)
        assert [item.run_id for item in page_b.items] == [run_b]

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
        assert all(isinstance(item, MainBarRunPair) for item in rest.items)
        assert [item.run_id for item in rest.items] == [run_a1]
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
    host = _historic_host({"s-unrecorded": ["旧消息"]})
    host.start()
    try:
        session_id = "s-unrecorded"
        _insert_run(
            host, "r-unrecorded", phase="finished", outcome="completed",
            session_id=session_id,
        )
        page = host.mainbar_pairs(session_id=session_id)
        assert tuple(type(item) for item in page.items) == (
            MainBarHistoricMessage,
        )
        assert isinstance(page.items[0], MainBarHistoricMessage)
        assert page.items[0].run_id is None
    finally:
        host.close()


class _FinalizerLatch(threading.Event):
    """Expose arrival and release as two deterministic test gates."""

    def __init__(self) -> None:
        super().__init__()
        self.reached = threading.Event()
        self.release = threading.Event()

    def wait(self, timeout=None):
        self.reached.set()
        return self.release.wait(timeout)


def test_mainbar_wait_cursor_sees_the_first_run_before_historic() -> None:
    """A pending first Run owns the Run-to-historic boundary.

    Reusing the exact wait cursor after recording must reveal that Run before
    any legacy row, even though finalize assigns it a newer activity revision.
    """
    from agent_alfred.runtime.cursor import decode_cursor

    latch = _FinalizerLatch()
    host = _historic_host(
        {
            "s-mainbar-wait": ["旧问题", "旧回答"],
            "s-mainbar-other": ["别的会话"],
        },
        script=["新回答"],
        before_recording_commit=latch,
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="新问题", session_id="s-mainbar-wait")
        )
        assert latch.reached.wait(2.0)
        assert host.snapshot().coordinator_state == "recording_pending"

        api = DashboardApi(facade=host)
        status, waiting = api.mainbar(
            {"session_id": "s-mainbar-wait", "limit": "1"}
        )
        assert status == 200
        assert waiting["items"] == []
        assert waiting["runs_pending"] is True
        assert waiting["next_cursor"] is not None
        wait_cursor = waiting["next_cursor"]
        payload = decode_cursor(wait_cursor, version=3, kind="mainbar")
        assert payload["seg"] == "runs_pending"
        assert payload["s"] == "s-mainbar-wait"
        assert api.mainbar(
            {"session_id": "s-mainbar-other", "cursor": wait_cursor}
        ) == (400, {"code": "malformed_cursor"})

        # An instantaneous reread is a stable wait-page snapshot, not IO.
        assert api.mainbar(
            {
                "session_id": "s-mainbar-wait",
                "limit": "1",
                "cursor": wait_cursor,
            }
        ) == (status, waiting)

        latch.release.set()
        host.wait(submitted.run_id)

        status, recorded = api.mainbar(
            {
                "session_id": "s-mainbar-wait",
                "limit": "1",
                "cursor": wait_cursor,
            }
        )
        assert status == 200
        assert recorded["runs_pending"] is False
        assert [item["run_id"] for item in recorded["items"]] == [
            submitted.run_id
        ]
        assert recorded["next_cursor"] is not None

        seen = list(recorded["items"])
        cursor = recorded["next_cursor"]
        while cursor is not None:
            status, page = api.mainbar(
                {
                    "session_id": "s-mainbar-wait",
                    "limit": "1",
                    "cursor": cursor,
                }
            )
            assert status == 200
            seen.extend(page["items"])
            cursor = page["next_cursor"]
        assert [item["type"] for item in seen] == [
            "run_pair",
            "historic_message",
            "historic_message",
        ]
        assert sum(
            item.get("run_id") == submitted.run_id for item in seen
        ) == 1
    finally:
        latch.release.set()
        host.close()


class _FailMainbarFinalize:
    def __init__(self, inner: sqlite3.Connection, armed: dict[str, bool]):
        self._inner = inner
        self._armed = armed

    def execute(self, sql, parameters=()):
        if (
            self._armed["value"]
            and sql.lstrip().upper().startswith("UPDATE")
            and "finished_at" in sql
        ):
            raise sqlite3.OperationalError("injected finalize failure")
        return self._inner.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_mainbar_failed_recording_releases_wait_cursor_to_historic() -> None:
    """Only the Host's authoritative failed projection releases the wait."""
    conn = _v2_database()
    _seed_historic(conn, "s-mainbar-fail", ["旧问题"])
    schema.migrate(conn)
    armed = {"value": False}
    latch = _FinalizerLatch()
    host = _host_over(
        _FailMainbarFinalize(conn, armed),
        ["未保存回答"],
        before_recording_commit=latch,
    )
    host.start()
    try:
        submitted = host.submit(
            SubmitRequest(message="新问题", session_id="s-mainbar-fail")
        )
        assert latch.reached.wait(2.0)
        assert host.snapshot().coordinator_state == "recording_pending"
        api = DashboardApi(facade=host)
        status, waiting = api.mainbar(
            {"session_id": "s-mainbar-fail", "limit": "5"}
        )
        assert status == 200
        assert waiting["items"] == []
        assert waiting["runs_pending"] is True
        wait_cursor = waiting["next_cursor"]
        assert wait_cursor is not None

        armed["value"] = True
        latch.release.set()
        host.wait(submitted.run_id)
        snapshot = host.snapshot()
        assert snapshot.coordinator_state == "recording_failed"
        assert snapshot.unrecorded_terminal_projection is not None
        assert snapshot.unrecorded_terminal_projection.run_id == submitted.run_id

        status, released = api.mainbar(
            {
                "session_id": "s-mainbar-fail",
                "limit": "5",
                "cursor": wait_cursor,
            }
        )
        assert status == 200
        assert released["runs_pending"] is False
        assert [item["type"] for item in released["items"]] == [
            "historic_message"
        ]
        assert released["items"][0]["blocks"] == [
            {"type": "text", "text": "旧问题"}
        ]
        assert all(
            item.get("run_id") != submitted.run_id for item in released["items"]
        )
        assert released["next_cursor"] is None
    finally:
        latch.release.set()
        host.close()


def test_mainbar_terminal_and_non_chat_runs_do_not_block_historic() -> None:
    host = _historic_host({"s-mainbar-nonblocking": ["旧问题"]})
    host.start()
    try:
        _insert_run(
            host,
            "r-old-terminal",
            phase="finished",
            outcome="failed",
            session_id="s-mainbar-nonblocking",
        )
        _insert_run(
            host,
            "r-system-live",
            purpose="inference_probe",
            phase="running",
            session_id="s-mainbar-nonblocking",
        )
        page = host.mainbar_pairs(session_id="s-mainbar-nonblocking")
        assert page.runs_pending is False
        assert page.next_cursor is None
        assert [message_plain_text(item.message) for item in page.items] == [
            "旧问题"
        ]
    finally:
        host.close()


def _record_seeded_mainbar_run(host: RuntimeHost, run_id: str, reply: str) -> None:
    with host._db_lock:  # noqa: SLF001 - deterministic setup transaction
        conn = host._conn  # noqa: SLF001
        running_revision = schema.allocate_activity_revision(conn)
        schema.update_run_phase(
            conn,
            run_id=run_id,
            from_phase="accepted",
            to_phase="running",
            activity_revision=running_revision,
            started_at=_TS,
            session_id="s-mainbar-many",
        )
        revision = schema.allocate_activity_revision(conn)
        schema.update_run_phase(
            conn,
            run_id=run_id,
            from_phase="running",
            to_phase="finished",
            activity_revision=revision,
            outcome="completed",
            finished_at=_TS,
            session_id="s-mainbar-many",
        )
        for role, text in (("user", f"q-{run_id}"), ("assistant", reply)):
            conn.execute(
                """INSERT INTO agent_log (
                     session_id, run_id, role, content, source, telemetry,
                     created_at
                   ) VALUES (?, ?, ?, ?, 'web', NULL, ?)""",
                (
                    "s-mainbar-many",
                    run_id,
                    role,
                    json.dumps([{"type": "text", "text": text}]),
                    _TS,
                ),
            )
        conn.commit()


def test_mainbar_paged_recorded_prefix_waits_for_a_later_recorded_run() -> None:
    """A pending Run cannot be lost behind an ordinary DESC continuation.

    The first page has more old recorded Runs available.  Its continuation
    must nevertheless preserve both the activity watermark and the consumed
    old-Run position while the real finalizer is paused.  Once recording
    settles, the new cohort is returned first, then the old suffix, then the
    historic segment, exactly once each.
    """
    from agent_alfred.runtime.cursor import decode_cursor

    latch = _FinalizerLatch()
    host = _historic_host(
        {"s-mainbar-many": ["旧消息"]},
        script=["稍后保存的回答"],
        before_recording_commit=latch,
    )
    host.start()
    try:
        for run_id in ("r-oldest", "r-middle", "r-newest"):
            _insert_run(host, run_id, session_id="s-mainbar-many")
            _record_seeded_mainbar_run(host, run_id, f"reply-{run_id}")

        submitted = host.submit(
            SubmitRequest(message="稍后保存的问题", session_id="s-mainbar-many")
        )
        assert latch.reached.wait(2.0)
        assert host.snapshot().coordinator_state == "recording_pending"

        api = DashboardApi(facade=host)
        status, first = api.mainbar(
            {"session_id": "s-mainbar-many", "limit": "1"}
        )
        assert status == 200
        assert [item["run_id"] for item in first["items"]] == ["r-newest"]
        assert first["runs_pending"] is True
        wait_cursor = first["next_cursor"]
        assert wait_cursor is not None
        payload = decode_cursor(wait_cursor, version=3, kind="mainbar")
        assert payload["seg"] == "runs_pending"
        assert payload["s"] == "s-mainbar-many"
        assert type(payload["w"]) is int
        assert payload["ra"] == first["items"][0]["activity_revision"]
        assert payload["rr"] == "r-newest"
        assert api.mainbar(
            {"session_id": "not-the-cursor-session", "cursor": wait_cursor}
        ) == (400, {"code": "malformed_cursor"})

        latch.release.set()
        host.wait(submitted.run_id)

        seen = list(first["items"])
        cursor = wait_cursor
        while cursor is not None:
            status, page = api.mainbar(
                {
                    "session_id": "s-mainbar-many",
                    "limit": "1",
                    "cursor": cursor,
                }
            )
            assert status == 200
            seen.extend(page["items"])
            cursor = page["next_cursor"]

        assert [item.get("run_id") for item in seen] == [
            "r-newest",
            submitted.run_id,
            "r-middle",
            "r-oldest",
            None,
        ]
        assert len([item.get("run_id") for item in seen if item.get("run_id")]) == len(
            {
                item.get("run_id")
                for item in seen
                if item.get("run_id") is not None
            }
        )
    finally:
        latch.release.set()
        host.close()


def test_mainbar_paged_recorded_prefix_resumes_after_recording_failure() -> None:
    """A failed pending Run releases the preserved old suffix, not a gap."""
    conn = _v2_database()
    _seed_historic(conn, "s-mainbar-many", ["旧消息"])
    schema.migrate(conn)
    armed = {"value": False}
    latch = _FinalizerLatch()
    host = _host_over(
        _FailMainbarFinalize(conn, armed),
        ["不会保存的回答"],
        before_recording_commit=latch,
    )
    host.start()
    try:
        for run_id in ("r-oldest", "r-middle", "r-newest"):
            _insert_run(host, run_id, session_id="s-mainbar-many")
            _record_seeded_mainbar_run(host, run_id, f"reply-{run_id}")

        submitted = host.submit(
            SubmitRequest(message="不会保存的问题", session_id="s-mainbar-many")
        )
        assert latch.reached.wait(2.0)
        api = DashboardApi(facade=host)
        status, first = api.mainbar(
            {"session_id": "s-mainbar-many", "limit": "1"}
        )
        assert status == 200
        assert [item["run_id"] for item in first["items"]] == ["r-newest"]
        assert first["runs_pending"] is True
        wait_cursor = first["next_cursor"]
        assert wait_cursor is not None

        armed["value"] = True
        latch.release.set()
        host.wait(submitted.run_id)
        assert host.snapshot().coordinator_state == "recording_failed"

        seen = list(first["items"])
        cursor = wait_cursor
        while cursor is not None:
            status, page = api.mainbar(
                {
                    "session_id": "s-mainbar-many",
                    "limit": "1",
                    "cursor": cursor,
                }
            )
            assert status == 200
            assert page["runs_pending"] is False
            seen.extend(page["items"])
            cursor = page["next_cursor"]

        assert [item.get("run_id") for item in seen] == [
            "r-newest",
            "r-middle",
            "r-oldest",
            None,
        ]
        assert all(item.get("run_id") != submitted.run_id for item in seen)
    finally:
        latch.release.set()
        host.close()


def test_mainbar_catches_multiple_pending_runs_after_a_recorded_page() -> None:
    """The wait checkpoint is after the old page, not after future revisions."""
    host = _historic_host(
        {"s-mainbar-many": ["旧消息"]}, script=["已有回答"]
    )
    host.start()
    try:
        existing_oldest = _run(host, "已有问题", "s-mainbar-many")
        for run_id in ("r-existing-middle", "r-existing-newest"):
            _insert_run(host, run_id, session_id="s-mainbar-many")
            _record_seeded_mainbar_run(host, run_id, f"reply-{run_id}")
        _insert_run(host, "r-pending-one", session_id="s-mainbar-many")
        _insert_run(host, "r-pending-two", session_id="s-mainbar-many")

        first = host.mainbar_pairs(session_id="s-mainbar-many", limit=1)
        assert [item.run_id for item in first.items] == ["r-existing-newest"]
        assert first.runs_pending is True
        wait_cursor = first.next_cursor
        assert wait_cursor is not None

        _record_seeded_mainbar_run(host, "r-pending-one", "reply-one")
        still_waiting = host.mainbar_pairs(
            session_id="s-mainbar-many", limit=5, cursor=wait_cursor
        )
        assert still_waiting.items == ()
        assert still_waiting.next_cursor == wait_cursor
        assert still_waiting.runs_pending is True

        _record_seeded_mainbar_run(host, "r-pending-two", "reply-two")

        run_ids = ["r-existing-newest"]
        historic = []
        cursor = wait_cursor
        while cursor is not None:
            page = host.mainbar_pairs(
                session_id="s-mainbar-many", limit=1, cursor=cursor
            )
            for item in page.items:
                if isinstance(item, MainBarHistoricMessage):
                    historic.append(message_plain_text(item.message))
                else:
                    run_ids.append(item.run_id)
            cursor = page.next_cursor

        assert run_ids == [
            "r-existing-newest",
            "r-pending-two",
            "r-pending-one",
            "r-existing-middle",
            existing_oldest.run_id,
        ]
        assert len(run_ids) == len(set(run_ids))
        assert historic == ["旧消息"]
    finally:
        host.close()


@pytest.mark.parametrize("record_before_drain_finishes", [False, True])
def test_mainbar_keeps_a_pending_run_recorded_during_catchup_paging(
    record_before_drain_finishes,
) -> None:
    """A Run observed pending cannot fall between catch-up and old pages."""
    latch = _FinalizerLatch()
    latch.release.set()
    session_id = "s-mainbar-many"
    host = _historic_host(
        {session_id: ["旧消息"]},
        script=["old", "one", "two", "three", "later"],
        before_recording_commit=latch,
    )
    host.start()
    try:
        old = _run(host, "old", session_id)
        latch.reached.clear()
        latch.release.clear()
        one = host.submit(SubmitRequest(message="one", session_id=session_id))
        assert one.kind == "accepted"
        assert latch.reached.wait(2.0), "first recording pause was not reached"
        assert host.snapshot().coordinator_state == "recording_pending"
        api = DashboardApi(facade=host)
        status, first = api.mainbar({"session_id": session_id, "limit": "1"})
        assert status == 200
        assert [item["run_id"] for item in first["items"]] == [old.run_id]
        assert first["runs_pending"] is True
        assert first["next_cursor"] is not None

        latch.release.set()
        host.wait(one.run_id)
        two = _run(host, "two", session_id)
        three = _run(host, "three", session_id)
        status, upper = api.mainbar({
            "session_id": session_id, "limit": "1",
            "cursor": first["next_cursor"],
        })
        assert status == 200
        assert [item["run_id"] for item in upper["items"]] == [three.run_id]
        assert upper["next_cursor"] is not None

        latch.reached.clear()
        latch.release.clear()
        later = host.submit(SubmitRequest(message="later", session_id=session_id))
        assert later.kind == "accepted"
        assert latch.reached.wait(2.0), "later recording pause was not reached"
        assert host.snapshot().coordinator_state == "recording_pending"
        status, middle = api.mainbar({
            "session_id": session_id, "limit": "1",
            "cursor": upper["next_cursor"],
        })
        assert status == 200
        assert [item["run_id"] for item in middle["items"]] == [two.run_id]
        assert middle["runs_pending"] is True
        assert middle["next_cursor"] is not None
        if record_before_drain_finishes:
            latch.release.set()
            host.wait(later.run_id)

        status, tail = api.mainbar({
            "session_id": session_id, "limit": "1",
            "cursor": middle["next_cursor"],
        })
        assert status == 200
        assert [item["run_id"] for item in tail["items"]] == [one.run_id]
        if not record_before_drain_finishes:
            latch.release.set()
            host.wait(later.run_id)
        status, latest = api.mainbar({"session_id": session_id, "limit": "1"})
        assert status == 200
        assert [item["run_id"] for item in latest["items"]] == [later.run_id]

        seen = [*first["items"], *upper["items"], *middle["items"], *tail["items"]]
        cursor = tail["next_cursor"]
        for _page in range(8):
            if cursor is None:
                break
            status, page = api.mainbar({
                "session_id": session_id, "limit": "1", "cursor": cursor,
            })
            assert status == 200
            seen.extend(page["items"])
            cursor = page["next_cursor"]
        assert cursor is None, "finite fixture did not finish paging"
        assert [item.get("run_id") for item in seen] == [
            old.run_id, three.run_id, two.run_id, one.run_id, later.run_id, None,
        ]
    finally:
        latch.release.set()
        host.close()


def test_mainbar_reopens_a_historic_cursor_without_repeating_historic() -> None:
    latch = _FinalizerLatch()
    host = _historic_host(
        {"s-mainbar-reopen": ["h1", "h2", "h3"]},
        script=["新回答"],
        before_recording_commit=latch,
    )
    host.start()
    try:
        first = host.mainbar_pairs(session_id="s-mainbar-reopen", limit=1)
        assert message_plain_text(first.items[0].message) == "h3"
        historic_cursor = first.next_cursor
        assert historic_cursor is not None

        submitted = host.submit(
            SubmitRequest(message="新问题", session_id="s-mainbar-reopen")
        )
        assert latch.reached.wait(2.0)
        assert host.snapshot().coordinator_state == "recording_pending"
        waiting = host.mainbar_pairs(
            session_id="s-mainbar-reopen", limit=1, cursor=historic_cursor
        )
        assert waiting.items == ()
        assert waiting.runs_pending is True
        wait_cursor = waiting.next_cursor
        assert wait_cursor is not None

        latch.release.set()
        host.wait(submitted.run_id)
        seen = ["historic:h3"]
        cursor = wait_cursor
        while cursor is not None:
            page = host.mainbar_pairs(
                session_id="s-mainbar-reopen", limit=1, cursor=cursor
            )
            for item in page.items:
                if isinstance(item, MainBarHistoricMessage):
                    seen.append(f"historic:{message_plain_text(item.message)}")
                else:
                    seen.append(f"run:{item.run_id}")
            cursor = page.next_cursor
        assert seen == [
            "historic:h3",
            f"run:{submitted.run_id}",
            "historic:h2",
            "historic:h1",
        ]
    finally:
        latch.release.set()
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
        assert host.mainbar_pairs(session_id=session_id).items == ()
    finally:
        host.close()


def test_historic_messages_are_visible_in_mainbar_without_a_fabricated_run() -> None:
    """The ADR-0027 line, seen from the MainBar.

    Historic rows have no run_id and no Run exists for them. The MainBar
    returns the single messages as their own union arm instead of pretending
    they are a user/assistant Run pair.
    """
    host = _historic_host({"s-historic": ["旧问题", "旧回答"]})
    host.start()
    try:
        page = host.mainbar_pairs(session_id="s-historic")
        assert [message_plain_text(item.message) for item in page.items] == [
            "旧回答",
            "旧问题",
        ]
        assert all(item.run_id is None for item in page.items)
    finally:
        host.close()


@pytest.mark.parametrize(
    "limit,expected_pages",
    [
        (
            1,
            [
                ["run:新问题二"],
                ["run:新问题一"],
                ["historic:旧问题二"],
                ["historic:旧回答"],
                ["historic:旧问题"],
            ],
        ),
        (
            2,
            [
                ["run:新问题二", "run:新问题一"],
                ["historic:旧问题二", "historic:旧回答"],
                ["historic:旧问题"],
            ],
        ),
        (
            3,
            [
                ["run:新问题二", "run:新问题一", "historic:旧问题二"],
                ["historic:旧回答", "historic:旧问题"],
            ],
        ),
    ],
)
def test_mainbar_pages_across_the_run_to_historic_boundary_without_gaps(
    limit: int, expected_pages: list[list[str]]
) -> None:
    host = _historic_host(
        {"s-mixed-mainbar": ["旧问题", "旧回答", "旧问题二"]},
        script=["新回答一", "新回答二"],
    )
    host.start()
    try:
        first = _run(host, "新问题一", "s-mixed-mainbar")
        second = _run(host, "新问题二", "s-mixed-mainbar")
        pages = []
        all_items = []
        cursor = None
        while True:
            page = host.mainbar_pairs(
                session_id="s-mixed-mainbar", limit=limit, cursor=cursor
            )
            assert page.items
            labels = []
            for item in page.items:
                if isinstance(item, MainBarHistoricMessage):
                    assert item.run_id is None
                    labels.append(f"historic:{message_plain_text(item.message)}")
                else:
                    assert item.run_id in {first.run_id, second.run_id}
                    labels.append(f"run:{message_plain_text(item.user_message)}")
            pages.append(labels)
            all_items.extend(page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        assert pages == expected_pages
        assert len(all_items) == 5
        assert len({repr(item) for item in all_items}) == 5
    finally:
        host.close()


def test_mainbar_cursor_is_session_bound_fail_closed_and_idempotent() -> None:
    from agent_alfred.runtime.cursor import decode_cursor, encode_cursor

    host = _historic_host(
        {"s-a": ["a-one", "a-two"], "s-b": ["b-one"]}
    )
    host.start()
    try:
        first = host.mainbar_pairs(session_id="s-a", limit=1)
        assert first.next_cursor is not None
        payload = decode_cursor(first.next_cursor, version=3, kind="mainbar")
        assert payload["seg"] == "historic"
        assert payload["s"] == "s-a"
        assert type(payload["id"]) is int
        continuation = host.mainbar_pairs(
            session_id="s-a", limit=1, cursor=first.next_cursor
        )
        assert continuation == host.mainbar_pairs(
            session_id="s-a", limit=1, cursor=first.next_cursor
        )
        with pytest.raises(MalformedCursor):
            host.mainbar_pairs(
                session_id="s-b", limit=1, cursor=first.next_cursor
            )

        malformed = [
            {"v": 3, "k": "mainbar", "seg": "unknown", "s": "s-a"},
            {
                "v": 3,
                "k": "mainbar",
                "seg": "historic",
                "s": "s-a",
                "id": True,
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "historic",
                "s": "s-a",
                "id": -1,
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs_pending",
                "s": "s-a",
                "w": True,
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs_pending",
                "s": "s-a",
                "w": 2,
                "u": 1,
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs_pending",
                "s": "s-a",
                "w": 1,
                "ca": 2,
                "cr": "r2",
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs_pending",
                "s": "s-a",
                "w": 2,
                "ra": 1,
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs_pending",
                "s": "s-a",
                "w": 2,
                "rr": "r1",
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs_pending",
                "s": "s-a",
                "w": 2,
                "ra": True,
                "rr": "r1",
            },
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs_pending",
                "s": "s-a",
                "w": 2,
                "ra": 3,
                "rr": "r1",
            },
        ]
        for payload in malformed:
            with pytest.raises(MalformedCursor):
                host.mainbar_pairs(
                    session_id="s-a", cursor=encode_cursor(payload)
                )
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
    conn = _current_database()
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
        _current_database(),
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
        mainbar = host.mainbar_pairs(session_id="s-historic-secret")
        _assert_redaction_canary_absent((inbox, page, mainbar))
    finally:
        host.close()


def test_mainbar_never_sends_historic_telemetry_to_the_browser() -> None:
    """Legacy telemetry remains storage/session history, not MainBar data."""
    conn = _v2_database()
    legacy_telemetry = {
        "authorization": f"Bearer {_REDACTION_CANARY}",
        "api_key": _REDACTION_CANARY,
        "detail": _REDACTION_CANARY,
    }
    _seed_historic(
        conn,
        "s-historic-telemetry",
        [f"historic body {_REDACTION_CANARY}"],
    )
    conn.execute(
        "UPDATE agent_log SET telemetry = ? WHERE session_id = ?",
        (
            json.dumps(legacy_telemetry),
            "s-historic-telemetry",
        ),
    )
    conn.commit()
    schema.migrate(conn)
    host = _host_over(conn, redactor=Redactor((_REDACTION_CANARY,)))
    try:
        session_page = host.open_session(
            "s-historic-telemetry", page_size=10
        )
        assert session_page.messages[0].telemetry == legacy_telemetry
        historic_item = host.mainbar_pairs(
            session_id="s-historic-telemetry"
        ).items[0]
        assert not hasattr(historic_item, "telemetry")
        status, payload = DashboardApi(facade=host).mainbar(
            {"session_id": "s-historic-telemetry"}
        )
        assert status == 200
        [historic] = payload["items"]
        assert historic == {
            "type": "historic_message",
            "run_id": None,
            "role": "user",
            "blocks": [{"type": "text", "text": "historic body ***"}],
            "source": "cli",
            "created_at": "legacy-00",
        }
        assert "telemetry" not in historic
        _assert_redaction_canary_absent(payload)
    finally:
        host.close()


def test_mainbar_does_not_replace_invalid_historic_content_with_a_message() -> None:
    from agent_alfred.messages import MessageError

    host = _historic_host({"s-invalid": ["will be corrupted"]})
    try:
        with host._db_lock:  # noqa: SLF001 - corrupt legacy fixture setup
            host._conn.execute(  # noqa: SLF001
                "UPDATE agent_log SET content = ? WHERE session_id = ?",
                (json.dumps([{"type": "made_up"}]), "s-invalid"),
            )
            host._conn.commit()  # noqa: SLF001
        with pytest.raises(MessageError):
            host.mainbar_pairs(session_id="s-invalid")
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
    conn = _current_database()
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
    from agent_alfred.runtime.cursor import (
        MalformedCursor,
        decode_cursor,
        encode_cursor,
    )

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
    for boolean_version in (True, False):
        with pytest.raises(MalformedCursor):
            decode_cursor(
                encode_cursor({"v": boolean_version, "k": "runs"}),
                version=1,
                kind="runs",
            )


@pytest.mark.parametrize("suffix", ("!", "$$$$", "\n", "="))
def test_the_shared_codec_refuses_noncanonical_trailing_bytes(suffix: str) -> None:
    from agent_alfred.runtime.cursor import (
        MalformedCursor,
        decode_cursor,
        encode_cursor,
    )

    token = encode_cursor({"v": 1, "k": "runs", "ar": 7, "r": "r1"})
    with pytest.raises(MalformedCursor):
        decode_cursor(token + suffix, version=1, kind="runs")


@pytest.mark.parametrize("activity_revision", [True, False])
@pytest.mark.parametrize("read", ["runs", "mainbar", "session_runs"])
def test_run_reads_reject_boolean_activity_revisions(
    activity_revision: bool, read: str
) -> None:
    """JSON booleans are not SQLite keyset positions for any Run read."""
    from agent_alfred.runtime.cursor import encode_cursor

    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        cursors = {
            "runs": encode_cursor(
                {"v": 1, "k": "runs", "ar": activity_revision, "r": "r1"}
            ),
            "mainbar": encode_cursor(
                {
                    "v": 3,
                    "k": "mainbar",
                    "seg": "runs",
                    "s": session_id,
                    "ar": activity_revision,
                    "r": "r1",
                }
            ),
            "session_runs": encode_cursor(
                {
                    "v": 1,
                    "k": "session_runs",
                    "s": session_id,
                    "ar": activity_revision,
                    "r": "r1",
                }
            ),
        }
        reads = {
            "runs": lambda: host.list_runs(cursor=cursors[read]),
            "mainbar": lambda: host.mainbar_pairs(
                session_id=session_id, cursor=cursors[read]
            ),
            "session_runs": lambda: host.list_session_chat_runs(
                session_id=session_id, limit=10, cursor=cursors[read]
            ),
        }

        with pytest.raises(MalformedCursor):
            reads[read]()
    finally:
        host.close()


@pytest.mark.parametrize("activity_revision", [0, 1, 2**63 - 1])
@pytest.mark.parametrize("read", ["runs", "mainbar", "session_runs"])
def test_run_reads_keep_exact_integer_activity_revisions(
    activity_revision: int, read: str
) -> None:
    """Zero, one and the largest SQLite integer remain legal positions."""
    from agent_alfred.runtime.cursor import encode_cursor

    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        cursors = {
            "runs": encode_cursor(
                {"v": 1, "k": "runs", "ar": activity_revision, "r": "r1"}
            ),
            "mainbar": encode_cursor(
                {
                    "v": 3,
                    "k": "mainbar",
                    "seg": "runs",
                    "s": session_id,
                    "ar": activity_revision,
                    "r": "r1",
                }
            ),
            "session_runs": encode_cursor(
                {
                    "v": 1,
                    "k": "session_runs",
                    "s": session_id,
                    "ar": activity_revision,
                    "r": "r1",
                }
            ),
        }
        reads = {
            "runs": lambda: host.list_runs(cursor=cursors[read]),
            "mainbar": lambda: host.mainbar_pairs(
                session_id=session_id, cursor=cursors[read]
            ),
            "session_runs": lambda: host.list_session_chat_runs(
                session_id=session_id, limit=10, cursor=cursors[read]
            ),
        }

        reads[read]()
    finally:
        host.close()


@pytest.mark.parametrize("position", [-1, 2**63])
@pytest.mark.parametrize(
    "read", ["runs", "mainbar", "mainbar_historic", "session_runs"]
)
def test_paged_web_reads_reject_out_of_sqlite_range_positions_before_sql(
    position: int, read: str
) -> None:
    """A forged keyset position never reaches SQLite or becomes an empty page."""
    from agent_alfred.runtime.cursor import encode_cursor

    class SqlMustNotRun:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("cursor validation must precede SQL")

    session_id = "s-cursor"
    cursors = {
        "runs": encode_cursor(
            {"v": 1, "k": "runs", "ar": position, "r": "r1"}
        ),
        "mainbar": encode_cursor(
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs",
                "s": session_id,
                "ar": position,
                "r": "r1",
            }
        ),
        "mainbar_historic": encode_cursor(
            {
                "v": 3,
                "k": "mainbar",
                "seg": "historic",
                "s": session_id,
                "id": position,
            }
        ),
        "session_runs": encode_cursor(
            {
                "v": 1,
                "k": "session_runs",
                "s": session_id,
                "ar": position,
                "r": "r1",
            }
        ),
    }
    conn = SqlMustNotRun()
    redactor = Redactor(())
    reads = {
        "runs": lambda: runs_store.list_runs(
            conn, redactor=redactor, cursor=cursors[read]
        ),
        "mainbar": lambda: runs_store.mainbar_pairs(
            conn,
            session_id=session_id,
            redactor=redactor,
            cursor=cursors[read],
        ),
        "mainbar_historic": lambda: runs_store.mainbar_pairs(
            conn,
            session_id=session_id,
            redactor=redactor,
            cursor=cursors[read],
        ),
        "session_runs": lambda: runs_store.list_session_chat_runs(
            conn,
            session_id=session_id,
            limit=10,
            redactor=redactor,
            cursor=cursors[read],
        ),
    }

    with pytest.raises(MalformedCursor):
        reads[read]()


@pytest.mark.parametrize("historic_id", [True, False])
def test_mainbar_historic_rejects_boolean_ids_before_sql(
    historic_id: bool,
) -> None:
    from agent_alfred.runtime.cursor import encode_cursor

    class SqlMustNotRun:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("cursor validation must precede SQL")

    cursor = encode_cursor(
        {
            "v": 3,
            "k": "mainbar",
            "seg": "historic",
            "s": "s-cursor",
            "id": historic_id,
        }
    )
    with pytest.raises(MalformedCursor):
        runs_store.mainbar_pairs(
            SqlMustNotRun(),
            session_id="s-cursor",
            redactor=Redactor(()),
            cursor=cursor,
        )


@pytest.mark.parametrize("historic_id", [0, 1, 2**63 - 1])
def test_mainbar_historic_keeps_exact_integer_ids(historic_id: int) -> None:
    """Every legal SQLite historic position remains stable on replay."""
    from agent_alfred.runtime.cursor import encode_cursor

    host = _historic_host({"s-cursor": ["旧问题"]})
    host.start()
    try:
        cursor = encode_cursor(
            {
                "v": 3,
                "k": "mainbar",
                "seg": "historic",
                "s": "s-cursor",
                "id": historic_id,
            }
        )
        first = host.mainbar_pairs(session_id="s-cursor", cursor=cursor)
        assert first == host.mainbar_pairs(session_id="s-cursor", cursor=cursor)
    finally:
        host.close()


def test_mainbar_negative_run_position_cannot_skip_its_run_pair() -> None:
    """A negative Run cursor must not fall through into historic messages."""
    from agent_alfred.runtime.cursor import encode_cursor

    host = _historic_host({"s-mixed": ["旧问题"]}, script=["新回答"])
    host.start()
    try:
        admitted = _run(host, "新问题", "s-mixed")
        cursor = encode_cursor(
            {
                "v": 3,
                "k": "mainbar",
                "seg": "runs",
                "s": "s-mixed",
                "ar": -1,
                "r": admitted.run_id,
            }
        )

        with pytest.raises(MalformedCursor):
            host.mainbar_pairs(session_id="s-mixed", cursor=cursor)
    finally:
        host.close()


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


def test_session_chat_runs_keep_each_admitted_runs_gateway_without_a_reply(
) -> None:
    """Gateway is the Run's entry fact, even before any reply exists.

    Startup recovery can finish a Run without recording an assistant row, and
    accepted/running Runs have no reply yet.  Their origins must therefore
    come from each Run row rather than the nullable reply ``source``.
    """
    host = _fresh_host()
    session_id = host.create_session()
    _insert_run(
        host,
        "web-recovered",
        gateway="web",
        phase="running",
        session_id=session_id,
    )
    host.start()
    try:
        _insert_run(
            host,
            "cli-accepted",
            gateway="cli",
            phase="accepted",
            session_id=session_id,
        )
        _insert_run(
            host,
            "cli-running",
            gateway="cli",
            phase="running",
            session_id=session_id,
        )

        page = host.list_session_chat_runs(session_id=session_id, limit=10)
        by_id = {run.run_id: run for run in page.runs}

        assert by_id["cli-accepted"].gateway == "cli"
        assert by_id["cli-accepted"].phase == "accepted"
        assert by_id["cli-running"].gateway == "cli"
        assert by_id["cli-running"].phase == "running"
        assert by_id["web-recovered"].gateway == "web"
        assert by_id["web-recovered"].phase == "finished"
        assert by_id["web-recovered"].outcome == "interrupted"
        assert all(run.reply_preview is None for run in by_id.values())
        assert all(run.reply_source is None for run in by_id.values())
    finally:
        host.close()


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

    The seeded accepted, running and finished chat Runs are admitted and appear;
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


@pytest.mark.parametrize(
    "column,invalid_value,expected_error",
    [
        ("phase", "unknown", "invalid run phase"),
        ("outcome", "unknown", "invalid run outcome"),
        ("phase", "running", "invalid run lifecycle"),
        ("outcome", None, "invalid run lifecycle"),
    ],
)
def test_session_run_list_rejects_invalid_persisted_lifecycle_values(
    column: str, invalid_value: object, expected_error: str
) -> None:
    host = _fresh_host()
    host.start()
    try:
        session_id = host.create_session()
        _insert_run(
            host,
            "r-corrupt-session",
            phase="finished",
            outcome="completed",
            session_id=session_id,
        )
        with host._db_lock:  # noqa: SLF001 - corrupt DB is test setup
            conn = host._conn  # noqa: SLF001
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                f"UPDATE runs SET {column} = ? WHERE run_id = ?",
                (invalid_value, "r-corrupt-session"),
            )
            conn.commit()
        with pytest.raises(ValueError, match=expected_error):
            host.list_session_chat_runs(session_id=session_id, limit=10)
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
