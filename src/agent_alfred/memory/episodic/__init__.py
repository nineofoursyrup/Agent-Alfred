"""Episodic Store protocol and default SQLite FTS5 implementation."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from agent_alfred.memory.storage import (
    SQLiteStore,
    canonical,
    instant,
    literal_match,
    normalize,
    validate_limit,
)
from agent_alfred.memory.types import (
    UNCHANGED,
    DeleteOutcome,
    EpisodeHit,
    EpisodeQuery,
    EpisodeRecord,
    MemoryId,
    Origin,
    RecordPage,
    Saved,
    TransactionMode,
    Unchanged,
    UpdateOutcome,
)


class EpisodicStore(Protocol):
    @property
    def transaction_mode(self) -> TransactionMode: ...
    @property
    def normalization_version(self) -> int: ...
    def save(
        self,
        summary: str,
        occurred_at: datetime,
        occurred_until: datetime | None,
        origin: Origin,
    ) -> MemoryId: ...
    def get(self, id: MemoryId) -> EpisodeRecord | None: ...
    def search(self, query: EpisodeQuery) -> Sequence[EpisodeHit]: ...
    def search_page(
        self, query: EpisodeQuery, *, cursor: str | None = None
    ) -> RecordPage[EpisodeRecord]: ...
    @property
    def memory_revision(self) -> int: ...
    def delete(self, id: MemoryId, *, expected_version: int) -> DeleteOutcome: ...
    def list_recent(
        self, *, limit: int = 25, cursor: str | None = None
    ) -> RecordPage[EpisodeRecord]: ...
    def update(
        self,
        id: MemoryId,
        *,
        expected_version: int,
        origin: Origin,
        summary: str | None = None,
        occurred_at: datetime | None = None,
        occurred_until: datetime | None | Unchanged = UNCHANGED,
        human_protected: bool = False,
    ) -> UpdateOutcome: ...


def episode_values(
    summary: str, occurred_at: datetime, occurred_until: datetime | None
) -> dict:
    start = instant(occurred_at)
    end = instant(occurred_until) if occurred_until is not None else None
    if end is not None and end < start:
        raise ValueError("episode end precedes start")
    return dict(
        summary=summary,
        occurred_at=start.isoformat(),
        occurred_until=end.isoformat() if end is not None else None,
    )


def episode_key(values: dict) -> bytes:
    return canonical(dict(values, summary=normalize(values["summary"])))


class SQLiteEpisodicStore(SQLiteStore):
    table = "episodes"

    def save(
        self,
        summary: str,
        occurred_at: datetime,
        occurred_until: datetime | None,
        origin: Origin,
        *,
        human_protected: bool = False,
    ) -> MemoryId:
        return self.save_with_result(
            summary,
            occurred_at,
            occurred_until,
            origin,
            human_protected=human_protected,
        ).id

    def save_with_result(
        self,
        summary: str,
        occurred_at: datetime,
        occurred_until: datetime | None,
        origin: Origin,
        *,
        human_protected: bool = False,
    ) -> Saved:
        values = episode_values(summary, occurred_at, occurred_until)
        return self._save(values, episode_key(values), origin, human_protected)

    def _record(self, row: dict) -> EpisodeRecord:
        return EpisodeRecord(
            summary=row["summary"],
            occurred_at=instant(datetime.fromisoformat(row["occurred_at"])),
            occurred_until=instant(datetime.fromisoformat(row["occurred_until"]))
            if row["occurred_until"] is not None
            else None,
            **self._metadata(row),
        )

    def get(self, id: MemoryId) -> EpisodeRecord | None:
        row = self._row(id)
        return self._record(row) if row is not None else None

    def search(self, query: EpisodeQuery) -> tuple[EpisodeHit, ...]:
        validate_limit(query.limit)
        since = instant(query.since) if query.since is not None else None
        until = instant(query.until) if query.until is not None else None
        if since is not None and until is not None:
            if until < since:
                raise ValueError("query end precedes start")
            if until == since:
                return ()
        sql = "SELECT episodes.rowid AS position, episodes.*"
        params = ()
        if query.text is not None:
            match = literal_match(query.text)
            if not match:
                return ()
            sql += (
                ", bm25(episodes_fts) AS rank FROM episodes "
                "JOIN episodes_fts ON episodes.rowid=episodes_fts.rowid "
                "WHERE episodes_fts MATCH ?"
            )
            params = (match,)
        else:
            sql += " FROM episodes"
        # Parse timestamps before ordering/filtering: legacy rows can use any
        # valid offset, and SQLite date conversion loses microsecond precision.
        rows = self._rows(sql, params)
        selected = []
        for row in rows:
            record = self._record(row)
            start, end = record.occurred_at, record.occurred_until
            if end is None:
                intersects = (since is None or start >= since) and (
                    until is None or start < until
                )
            else:
                intersects = (
                    start < end
                    and (since is None or end > since)
                    and (until is None or start < until)
                )
            if intersects:
                selected.append((row, record))
        selected.sort(
            key=lambda item: (item[1].occurred_at, item[0]["position"]), reverse=True
        )
        if query.text is not None:
            selected.sort(key=lambda item: item[0]["rank"])
        return tuple(EpisodeHit(record) for _, record in selected[: query.limit])

    def update(
        self,
        id: MemoryId,
        *,
        expected_version: int,
        origin: Origin,
        summary: str | None = None,
        occurred_at: datetime | None = None,
        occurred_until: datetime | None | Unchanged = UNCHANGED,
        human_protected: bool = False,
    ) -> UpdateOutcome:
        from agent_alfred.memory.types import NotFound, VersionConflict, origin_json

        self._writing()
        validate_limit(expected_version)
        origin_json(origin)
        row = self._row(id)
        if row is None:
            return NotFound(id)
        if row["record_version"] != expected_version:
            return VersionConflict(row["record_version"])
        previous = self._record(row)
        values = episode_values(
            previous.summary if summary is None else summary,
            previous.occurred_at if occurred_at is None else occurred_at,
            previous.occurred_until
            if isinstance(occurred_until, Unchanged)
            else occurred_until,
        )
        # Compare normalized instants, including historic offset spellings.
        row["occurred_at"] = previous.occurred_at.isoformat()
        row["occurred_until"] = (
            previous.occurred_until.isoformat()
            if previous.occurred_until is not None
            else None
        )
        return self._update(
            row,
            values=values,
            key=episode_key(values),
            origin=origin,
            human_protected=human_protected,
        )

    def list_recent(
        self, *, limit: int = 25, cursor: str | None = None
    ) -> RecordPage[EpisodeRecord]:
        return self._recent(limit, cursor, self._record)

    def search_page(
        self, query: EpisodeQuery, *, cursor: str | None = None
    ) -> RecordPage[EpisodeRecord]:
        return self._search_page(query, cursor, self.search)
