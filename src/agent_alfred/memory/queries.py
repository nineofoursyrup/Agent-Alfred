"""Page-facing memory reads over the public Store contracts.

The injected context manager holds the host's existing database read lock for
one request. Storage failures propagate; HTTP adapters map them to safe codes.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from agent_alfred.memory.episodic import EpisodicStore
from agent_alfred.memory.semantic import SemanticStore
from agent_alfred.memory.storage import instant, validate_limit
from agent_alfred.memory.types import (
    CursorStaleError,
    EpisodeQuery,
    EpisodeRecord,
    FactQuery,
    FactRecord,
    RecordPage,
)


class CountsUnavailable(RuntimeError):
    """A Store cannot prove a complete inventory in this reading context."""


class MemoryQueryService:
    def __init__(
        self,
        reading_stores: Callable[
            [], AbstractContextManager[tuple[SemanticStore, EpisodicStore]]
        ],
    ):
        self._reading_stores = reading_stores

    def counts(self, *, expected_memory_revision: int) -> dict:
        if type(expected_memory_revision) is not int or expected_memory_revision < 0:
            raise ValueError("invalid_input")
        with self._reading_stores() as stores:
            if any(not callable(getattr(store, "count", None)) for store in stores):
                raise CountsUnavailable("counts_unavailable")
            revisions = [getattr(store, "memory_revision", None) for store in stores]
            if any(type(value) is not int or value < 0 for value in revisions):
                raise CountsUnavailable("counts_unavailable")
            if any(value != expected_memory_revision for value in revisions):
                raise CursorStaleError("memory_changed")
            values = [store.count() for store in stores]
            if any(type(value) is not int or value < 0 for value in values):
                raise CountsUnavailable("counts_unavailable")
            if any(
                store.memory_revision != expected_memory_revision for store in stores
            ):
                raise CursorStaleError("memory_changed")
            return {
                "memory_revision": expected_memory_revision,
                "counts": {
                    "semantic": values[0],
                    "episodic": values[1],
                    "total": sum(values),
                },
            }

    def get_records(
        self,
        *,
        kind: str,
        mode: str = "list",
        text: str | None = None,
        subject: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 25,
    ) -> RecordPage[FactRecord] | RecordPage[EpisodeRecord]:
        validate_limit(page_size)
        if kind not in ("semantic", "episodic") or mode not in ("list", "search"):
            raise ValueError("invalid_input")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("invalid_input")
        if any(
            value is not None and not isinstance(value, str)
            for value in (text, subject)
        ):
            raise ValueError("invalid_input")
        if mode == "list" and any(
            value is not None for value in (text, subject, since, until)
        ):
            raise ValueError("invalid_input")
        if kind == "semantic" and (since is not None or until is not None):
            raise ValueError("invalid_input")
        if kind == "episodic" and subject is not None:
            raise ValueError("invalid_input")
        if since is not None:
            since = instant(since)
        if until is not None:
            until = instant(until)
        if since is not None and until is not None and until < since:
            raise ValueError("invalid_input")
        with self._reading_stores() as (semantic, episodic):
            if kind == "semantic":
                if mode == "list":
                    return semantic.list_recent(limit=page_size, cursor=cursor)
                return semantic.search_page(
                    FactQuery(text, subject, page_size), cursor=cursor
                )
            if mode == "list":
                return episodic.list_recent(limit=page_size, cursor=cursor)
            return episodic.search_page(
                EpisodeQuery(text, since, until, page_size), cursor=cursor
            )
