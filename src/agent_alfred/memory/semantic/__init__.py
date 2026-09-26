"""Semantic Store protocol and default SQLite FTS5 implementation."""

from collections.abc import Sequence
from typing import Protocol

from agent_alfred.memory import chinese_recall
from agent_alfred.memory.storage import (
    SQLiteStore,
    canonical,
    literal_match,
    normalize,
    validate_limit,
)
from agent_alfred.memory.types import (
    DeleteOutcome,
    FactHit,
    FactQuery,
    FactRecord,
    MemoryId,
    Origin,
    RecordPage,
    Saved,
    TransactionMode,
    UpdateOutcome,
)


class SemanticStore(Protocol):
    @property
    def transaction_mode(self) -> TransactionMode: ...
    @property
    def normalization_version(self) -> int: ...
    def save(self, subject: str, fact: str, origin: Origin) -> MemoryId: ...
    def get(self, id: MemoryId) -> FactRecord | None: ...
    def search(self, query: FactQuery) -> Sequence[FactHit]: ...
    def search_page(
        self, query: FactQuery, *, cursor: str | None = None
    ) -> RecordPage[FactRecord]: ...
    @property
    def memory_revision(self) -> int: ...
    def delete(self, id: MemoryId, *, expected_version: int) -> DeleteOutcome: ...
    def list_recent(
        self, *, limit: int = 25, cursor: str | None = None
    ) -> RecordPage[FactRecord]: ...
    def update(
        self,
        id: MemoryId,
        *,
        expected_version: int,
        origin: Origin,
        subject: str | None = None,
        fact: str | None = None,
        human_protected: bool = False,
    ) -> UpdateOutcome: ...


class SQLiteSemanticStore(SQLiteStore):
    table = "facts"

    def save(
        self, subject: str, fact: str, origin: Origin, *, human_protected: bool = False
    ) -> MemoryId:
        return self.save_with_result(
            subject, fact, origin, human_protected=human_protected
        ).id

    def save_with_result(
        self, subject: str, fact: str, origin: Origin, *, human_protected: bool = False
    ) -> Saved:
        key = canonical({"subject": normalize(subject), "fact": normalize(fact)})
        return self._save(
            dict(subject=subject, fact=fact), key, origin, human_protected
        )

    def _record(self, row: dict) -> FactRecord:
        return FactRecord(
            subject=row["subject"], fact=row["fact"], **self._metadata(row)
        )

    def get(self, id: MemoryId) -> FactRecord | None:
        row = self._row(id)
        return self._record(row) if row is not None else None

    def search(self, query: FactQuery) -> tuple[FactHit, ...]:
        validate_limit(query.limit)
        params = []
        where = []
        sql = "SELECT facts.* FROM facts"
        order = "facts.rowid DESC"
        if query.text is not None:
            match = literal_match(query.text)
            if not match:
                return ()
            sql += " JOIN facts_fts ON facts.rowid=facts_fts.rowid"
            where.append("facts_fts MATCH ?")
            params.append(match)
            order = "bm25(facts_fts), facts.rowid DESC"
        if query.subject is not None:
            where.append("facts.subject=?")
            params.append(query.subject)
        sql += (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._rows(sql + f" ORDER BY {order} LIMIT ?", (*params, query.limit))
        method = None
        if not rows and query.text is not None:
            rows = chinese_recall.search_rows(self, query.text, subject=query.subject)
            method = chinese_recall.METHOD
        return tuple(FactHit(self._record(row), method) for row in rows[:query.limit])

    def update(
        self,
        id: MemoryId,
        *,
        expected_version: int,
        origin: Origin,
        subject: str | None = None,
        fact: str | None = None,
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
        values = dict(
            subject=row["subject"] if subject is None else subject,
            fact=row["fact"] if fact is None else fact,
        )
        key = canonical({name: normalize(value) for name, value in values.items()})
        return self._update(
            row,
            values=values,
            key=key,
            origin=origin,
            human_protected=human_protected,
        )

    def apply_approved_consolidation(
        self,
        id: MemoryId,
        *,
        expected_version: int,
        origin: Origin,
        proof,
        subject: str | None = None,
        fact: str | None = None,
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
        values = dict(
            subject=row["subject"] if subject is None else subject,
            fact=row["fact"] if fact is None else fact,
        )
        key = canonical({name: normalize(value) for name, value in values.items()})
        return self._update(
            row,
            values=values,
            key=key,
            origin=origin,
            human_protected=True,
            approval_proof=proof,
        )

    def list_recent(
        self, *, limit: int = 25, cursor: str | None = None
    ) -> RecordPage[FactRecord]:
        return self._recent(limit, cursor, self._record)

    def search_page(
        self, query: FactQuery, *, cursor: str | None = None
    ) -> RecordPage[FactRecord]:
        return self._search_page(query, cursor, self.search)
