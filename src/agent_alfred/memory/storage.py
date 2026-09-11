"""Shared SQLite mechanics, with transaction ownership left to the caller."""

import json
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from agent_alfred.memory.types import (
    NORMALIZATION_VERSION,
    AlreadyAbsent,
    ConsolidationOrigin,
    Deleted,
    DeleteOutcome,
    MemoryId,
    Origin,
    Saved,
    TransactionMode,
    VersionConflict,
    origin_json,
    parse_origin,
)

Fingerprint = Callable[[bytes], tuple[str, str]]


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("memory time must include a UTC offset")
    return value.astimezone(UTC)


def canonical(values: dict[str, str | None]) -> bytes:
    return json.dumps(
        {"v": NORMALIZATION_VERSION, **values},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def literal_match(text: str) -> str:
    # FTS syntax is never accepted from callers: whitespace-delimited literal
    # phrases match independently, and punctuation cannot become an operator.
    words = text.split()
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in words)


def validate_limit(limit: int) -> None:
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")


class SQLiteStore:
    """Private mechanics shared by the two concrete Store families."""

    table: str

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fingerprint: Fingerprint,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._conn = conn
        self._fingerprint = fingerprint
        self._clock = clock

    @property
    def transaction_mode(self) -> TransactionMode:
        return "local_atomic"

    @property
    def normalization_version(self) -> int:
        return NORMALIZATION_VERSION

    @property
    def memory_revision(self) -> int:
        return self._conn.execute(
            "SELECT revision FROM memory_revision WHERE singleton=1"
        ).fetchone()[0]

    def _writing(self) -> None:
        if not self._conn.in_transaction:
            raise RuntimeError("memory writes require a caller-owned transaction")

    def _bump(self) -> None:
        self._conn.execute(
            "UPDATE memory_revision SET revision=revision+1 WHERE singleton=1"
        )

    def _rows(self, sql: str, params=()) -> list[dict]:
        cursor = self._conn.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _row(self, id: MemoryId) -> dict | None:
        rows = self._rows(
            f"SELECT rowid AS position, * FROM {self.table} WHERE id=?", (id,)
        )
        return rows[0] if rows else None

    def _save(
        self, values: dict, key: bytes, origin: Origin, human_protected: bool
    ) -> Saved:
        self._writing()
        origin_data = origin_json(origin)
        protected = human_protected or not isinstance(origin, ConsolidationOrigin)
        # The first lookup establishes a SQLite read snapshot within the
        # caller transaction. A concurrent writer cannot be silently overwritten:
        # upgrading a stale snapshot raises SQLITE_BUSY_SNAPSHOT.
        existing = self._rows(
            f"SELECT * FROM {self.table} WHERE idempotency_key=?", (key.decode(),)
        )
        if existing:
            row = existing[0]
            if protected and not row["human_protected"]:
                self._conn.execute(
                    f"UPDATE {self.table} SET human_protected=1 WHERE id=?",
                    (row["id"],),
                )
                self._bump()
            return Saved(MemoryId(row["id"]), False, row["record_version"])
        id = MemoryId(str(uuid4()))
        now = instant(self._clock()).isoformat()
        fp, key_id = self._fingerprint(key)
        data = dict(
            id=id,
            **values,
            origin_kind=origin_data["type"],
            origin_source=origin_data.get("source"),
            origin_call_id=origin_data.get("call_id"),
            origin_batch_id=origin_data.get("batch_id"),
            created_at=now,
            idempotency_key=key.decode(),
            fingerprint=fp,
            key_id=key_id,
            normalization_version=NORMALIZATION_VERSION,
            record_version=1,
            modified_at=now,
            last_change_origin=json.dumps(origin_data),
            human_protected=int(protected),
        )
        self._conn.execute(
            f"INSERT INTO {self.table} ({','.join(data)}) "
            f"VALUES ({','.join('?' for _ in data)})",
            tuple(data.values()),
        )
        self._bump()
        return Saved(id, True, 1)

    def delete(self, id: MemoryId, *, expected_version: int) -> DeleteOutcome:
        self._writing()
        validate_limit(expected_version)
        row = self._row(id)
        if row is None:
            return AlreadyAbsent(id)
        if row["record_version"] != expected_version:
            return VersionConflict(row["record_version"])
        self._conn.execute(
            f"DELETE FROM {self.table} WHERE id=? AND record_version=?",
            (id, expected_version),
        )
        self._bump()
        return Deleted(id, row["fingerprint"], row["key_id"])

    def _metadata(self, row: dict) -> dict:
        origin = {"type": row["origin_kind"]}
        for name in ("source", "call_id", "batch_id"):
            if row["origin_" + name] is not None:
                origin[name] = row["origin_" + name]
        return dict(
            id=MemoryId(row["id"]),
            origin=parse_origin(origin),
            created_at=instant(datetime.fromisoformat(row["created_at"])),
            record_version=row["record_version"],
            modified_at=instant(datetime.fromisoformat(row["modified_at"])),
            last_change_origin=parse_origin(json.loads(row["last_change_origin"])),
            human_protected=bool(row["human_protected"]),
        )

    def _update(
        self,
        row: dict,
        *,
        values: dict,
        key: bytes,
        origin: Origin,
        human_protected: bool,
        approval_proof=None,
    ):
        from agent_alfred.memory.types import DuplicateConflict, UpdateApplied

        other = self._rows(
            f"SELECT id FROM {self.table} WHERE idempotency_key=? AND id<>?",
            (key.decode(), row["id"]),
        )
        if other:
            return DuplicateConflict(MemoryId(other[0]["id"]))
        changed = any(row[name] != value for name, value in values.items())
        if (
            changed
            and row["human_protected"]
            and isinstance(origin, ConsolidationOrigin)
        ):
            self._require_approval_proof(
                approval_proof,
                origin,
                row["id"],
                values,
                row["record_version"],
            )
        protected = (
            row["human_protected"]
            or human_protected
            or not isinstance(origin, ConsolidationOrigin)
            or approval_proof is not None
        )
        if changed or protected != row["human_protected"]:
            updates = {"human_protected": int(protected)}
            if changed:
                fp, key_id = self._fingerprint(key)
                updates.update(
                    values,
                    idempotency_key=key.decode(),
                    fingerprint=fp,
                    key_id=key_id,
                    record_version=row["record_version"] + 1,
                    modified_at=instant(self._clock()).isoformat(),
                    last_change_origin=json.dumps(origin_json(origin)),
                )
            assignments = ",".join(name + "=?" for name in updates)
            self._conn.execute(
                f"UPDATE {self.table} SET {assignments} "
                "WHERE id=? AND record_version=?",
                (*updates.values(), row["id"], row["record_version"]),
            )
            self._bump()
        return UpdateApplied(
            MemoryId(row["id"]), changed, row["record_version"] + int(changed)
        )

    def _require_approval_proof(
        self, proof, origin, memory_id, values, expected_version
    ):
        from agent_alfred.memory.types import (
            ConsolidationApprovalProof,
            ProtectedMemoryError,
        )

        if not isinstance(proof, ConsolidationApprovalProof):
            raise ProtectedMemoryError("human-protected memory requires confirmation")
        if (
            not isinstance(origin, ConsolidationOrigin)
            or origin.batch_id != proof.batch_id
            or type(proof.revision) is not int
            or proof.revision < 1
        ):
            raise ProtectedMemoryError("human-protected memory requires confirmation")
        batch = self._conn.execute(
            "SELECT revision, status FROM memory_consolidation_batches "
            "WHERE batch_id=?",
            (proof.batch_id,),
        ).fetchone()
        if (
            batch is None
            or batch[0] != proof.revision
            or batch[1] not in ("awaiting_approval", "failed")
        ):
            raise ProtectedMemoryError("human-protected memory requires confirmation")
        approved = self._conn.execute(
            "SELECT 1 FROM memory_consolidation_approvals "
            "WHERE batch_id=? AND revision=?",
            (proof.batch_id, proof.revision),
        ).fetchone()
        if approved is None:
            raise ProtectedMemoryError("human-protected memory requires confirmation")
        if self.table != "facts":
            return
        plan_row = self._conn.execute(
            "SELECT plan_json FROM memory_consolidation_plans "
            "WHERE batch_id=? AND revision=?",
            (proof.batch_id, proof.revision),
        ).fetchone()
        if plan_row is None or not plan_row[0]:
            raise ProtectedMemoryError("human-protected memory requires confirmation")
        try:
            payload = json.loads(plan_row[0])
        except ValueError:
            raise ProtectedMemoryError(
                "human-protected memory requires confirmation"
            ) from None
        items = payload.get("semantic") if isinstance(payload, dict) else None
        if isinstance(items, list):
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("action") == "update"
                    and item.get("id") == memory_id
                    and item.get("subject") == values.get("subject")
                    and item.get("fact") == values.get("fact")
                    and item.get("expected_version") == expected_version
                ):
                    return
        raise ProtectedMemoryError("human-protected memory requires confirmation")

    def _recent(self, limit: int, cursor: str | None, record):
        import base64

        from agent_alfred.memory.types import CursorStaleError, RecordPage

        validate_limit(limit)
        rows = self._rows(
            f"SELECT {self.table}.rowid AS position, {self.table}.*, "
            "memory_revision.revision AS read_revision FROM memory_revision "
            f"LEFT JOIN {self.table} ON 1=1 WHERE singleton=1"
        )
        revision = rows[0]["read_revision"]
        rows = [row for row in rows if row["id"] is not None]
        rows.sort(
            key=lambda row: (
                instant(datetime.fromisoformat(row["created_at"])),
                row["position"],
            ),
            reverse=True,
        )
        offset = 0
        if cursor is not None:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor))
                if (
                    value["table"] != self.table
                    or value["revision"] != revision
                    or value["limit"] != limit
                ):
                    raise CursorStaleError("memory list cursor is stale")
                position = value["position"]
                offset = next(
                    index + 1
                    for index, row in enumerate(rows)
                    if row["position"] == position
                )
            except (ValueError, KeyError, TypeError, StopIteration) as exc:
                raise CursorStaleError("memory list cursor is stale") from exc
        selected = rows[offset : offset + limit]
        next_cursor = None
        if offset + limit < len(rows):
            value = dict(
                table=self.table,
                revision=revision,
                limit=limit,
                position=selected[-1]["position"],
            )
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(value, separators=(",", ":")).encode()
            ).decode()
        return RecordPage(tuple(record(row) for row in selected), next_cursor, revision)

    def _search_page(self, query, cursor, search):
        import base64
        import hashlib
        import sys
        from dataclasses import asdict, replace

        from agent_alfred.memory.types import CursorStaleError, RecordPage

        validate_limit(query.limit)
        values = asdict(query)
        for name, value in values.items():
            if isinstance(value, datetime):
                values[name] = instant(value).isoformat()
        binding = hashlib.sha256(
            json.dumps(
                {"table": self.table, "query": values},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        revision = self.memory_revision
        offset = 0
        if cursor is not None:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor))
                if value["binding"] != binding or value["revision"] != revision:
                    raise CursorStaleError("memory search cursor is stale")
                offset = value["offset"]
                if type(offset) is not int or offset < 0:
                    raise CursorStaleError("memory search cursor is stale")
            except (ValueError, KeyError, TypeError) as exc:
                raise CursorStaleError("memory search cursor is stale") from exc
        hits = search(replace(query, limit=sys.maxsize))
        if self.memory_revision != revision:
            raise CursorStaleError("memory search changed while reading")
        selected = hits[offset : offset + query.limit]
        next_cursor = None
        if offset + query.limit < len(hits):
            value = dict(
                binding=binding, revision=revision, offset=offset + query.limit
            )
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(value, separators=(",", ":")).encode()
            ).decode()
        return RecordPage(tuple(hit.record for hit in selected), next_cursor, revision)
