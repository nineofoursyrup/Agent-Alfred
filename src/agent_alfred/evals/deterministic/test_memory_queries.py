"""The page-facing read contract uses current records and revision-bound pages."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from agent_alfred.memory.episodic import SQLiteEpisodicStore
from agent_alfred.memory.queries import MemoryQueryService
from agent_alfred.memory.semantic import SQLiteSemanticStore
from agent_alfred.memory.types import CursorStaleError, ManualOrigin
from agent_alfred.schema import migrate


@pytest.fixture
def queries():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    options = dict(
        fingerprint=lambda value: ("test-fingerprint", "test-key"),
        clock=lambda: datetime(2026, 9, 9, tzinfo=UTC),
    )
    stores = SQLiteSemanticStore(conn, **options), SQLiteEpisodicStore(conn, **options)

    @contextmanager
    def reading_stores():
        yield stores

    conn.execute("BEGIN")
    yield MemoryQueryService(reading_stores), stores, conn
    conn.close()


def test_search_pages_bind_query_and_revision(queries):
    service, (semantic, _), _conn = queries
    first = semantic.save("Alice", "mint", ManualOrigin("web"))
    second = semantic.save("Bob", "mint", ManualOrigin("web"))
    head = service.get_records(kind="semantic", mode="search", text="mint", page_size=1)
    assert [record.id for record in head.records] == [second]
    tail = service.get_records(
        kind="semantic",
        mode="search",
        text="mint",
        page_size=1,
        cursor=head.next_cursor,
    )
    assert [record.id for record in tail.records] == [first]
    with pytest.raises(CursorStaleError):
        service.get_records(
            kind="semantic",
            mode="search",
            text="basil",
            page_size=1,
            cursor=head.next_cursor,
        )
    semantic.save("Cindy", "mint", ManualOrigin("web"))
    with pytest.raises(CursorStaleError):
        service.get_records(
            kind="semantic",
            mode="search",
            text="mint",
            page_size=1,
            cursor=head.next_cursor,
        )


def test_query_service_uses_half_open_times_and_keeps_list_creation_order(queries):
    service, (_, episodic), _conn = queries
    dt = datetime.fromisoformat
    later_event = episodic.save(
        "conference", dt("2026-09-10T00:00:00+00:00"), None, ManualOrigin("web")
    )
    earlier_event = episodic.save(
        "conference", dt("2026-09-09T08:00:00+08:00"), None, ManualOrigin("web")
    )
    assert [r.id for r in service.get_records(kind="episodic").records] == [
        earlier_event,
        later_event,
    ]
    page = service.get_records(
        kind="episodic",
        mode="search",
        since=dt("2026-09-09T00:00:00+00:00"),
        until=dt("2026-09-10T00:00:00+00:00"),
    )
    assert [r.id for r in page.records] == [earlier_event]
    assert [
        r.id for r in service.get_records(kind="episodic", mode="search").records
    ] == [later_event, earlier_event]
    with pytest.raises(ValueError):
        service.get_records(kind="episodic", mode="search", since=datetime(2026, 9, 9))


@pytest.mark.parametrize("mode", ["list", "search"])
def test_read_failure_is_not_an_empty_page(queries, mode):
    service, _stores, conn = queries
    conn.close()
    with pytest.raises(sqlite3.Error):
        service.get_records(
            kind="semantic", mode=mode, **({"text": "mint"} if mode == "search" else {})
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "semantic", "mode": "other"},
        {"kind": "semantic", "mode": "list", "text": "mint"},
        {"kind": "episodic", "mode": "search", "subject": "Alice"},
        {"kind": "semantic", "page_size": True},
        {"kind": "semantic", "page_size": 0},
    ],
)
def test_invalid_query_shape_is_rejected(queries, fields):
    service, _stores, _conn = queries
    with pytest.raises(ValueError):
        service.get_records(**fields)
