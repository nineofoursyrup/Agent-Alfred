"""Backend-independent memory records, queries, and closed write outcomes."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NewType

MemoryId = NewType("MemoryId", str)
TransactionMode = Literal["local_atomic", "external"]
NORMALIZATION_VERSION = 1


@dataclass(frozen=True)
class ConsolidationOrigin:
    batch_id: str


@dataclass(frozen=True)
class ManualOrigin:
    source: Literal["cli", "web"]


@dataclass(frozen=True)
class ToolOrigin:
    call_id: str


Origin = ConsolidationOrigin | ManualOrigin | ToolOrigin


def origin_json(origin: Origin) -> dict[str, str]:
    match origin:
        case ConsolidationOrigin(batch_id) if batch_id:
            return {"type": "consolidation", "batch_id": batch_id}
        case ManualOrigin(source) if source in ("cli", "web"):
            return {"type": "manual", "source": source}
        case ToolOrigin(call_id) if call_id:
            return {"type": "tool", "call_id": call_id}
    raise ValueError("invalid memory origin")


def parse_origin(value: dict[str, str]) -> Origin:
    match value:
        case {"type": "consolidation", "batch_id": batch_id}:
            result = ConsolidationOrigin(batch_id)
        case {"type": "manual", "source": source} if source in ("cli", "web"):
            result = ManualOrigin(source)
        case {"type": "tool", "call_id": call_id}:
            result = ToolOrigin(call_id)
        case _:
            raise ValueError("invalid memory origin")
    origin_json(result)
    return result


@dataclass(frozen=True)
class FactRecord:
    id: MemoryId
    subject: str
    fact: str
    origin: Origin
    created_at: datetime
    record_version: int
    modified_at: datetime
    last_change_origin: Origin
    human_protected: bool


@dataclass(frozen=True)
class EpisodeRecord:
    id: MemoryId
    summary: str
    occurred_at: datetime
    occurred_until: datetime | None
    origin: Origin
    created_at: datetime
    record_version: int
    modified_at: datetime
    last_change_origin: Origin
    human_protected: bool


@dataclass(frozen=True)
class FactHit:
    record: FactRecord
    relevance: str | None = None


@dataclass(frozen=True)
class EpisodeHit:
    record: EpisodeRecord
    relevance: str | None = None


@dataclass(frozen=True)
class FactQuery:
    text: str | None = None
    subject: str | None = None
    limit: int = 10


@dataclass(frozen=True)
class EpisodeQuery:
    text: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 10


@dataclass(frozen=True)
class Saved:
    id: MemoryId
    created: bool
    version: int


@dataclass(frozen=True)
class UpdateApplied:
    id: MemoryId
    changed: bool
    version: int


@dataclass(frozen=True)
class VersionConflict:
    current_version: int


@dataclass(frozen=True)
class DuplicateConflict:
    existing_id: MemoryId


@dataclass(frozen=True)
class NotFound:
    id: MemoryId


@dataclass(frozen=True)
class Deleted:
    id: MemoryId
    fingerprint: str
    key_id: str


@dataclass(frozen=True)
class AlreadyAbsent:
    id: MemoryId


UpdateOutcome = UpdateApplied | VersionConflict | DuplicateConflict | NotFound
DeleteOutcome = Deleted | AlreadyAbsent | VersionConflict


class ProtectedMemoryError(ValueError):
    """An automatic consolidation cannot overwrite human-protected content."""


@dataclass(frozen=True)
class ConsolidationApprovalProof:
    """Persisted approval of one batch revision; not a caller-supplied flag."""

    batch_id: str
    revision: int


class Unchanged:
    """Omitted optional interval endpoint, distinct from an explicit null."""


UNCHANGED = Unchanged()


class CursorStaleError(ValueError):
    """The query or memory revision changed; restart the list from its head."""


@dataclass(frozen=True)
class RecordPage[T]:
    records: tuple[T, ...]
    next_cursor: str | None
    read_revision: int
