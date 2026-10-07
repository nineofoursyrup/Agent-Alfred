"""Bounded durable ledger, immutable objects and a separate append-only anchor.

SQLite is a local fixture adapter. The document protocol is also implemented by
the AWS adapter: each event is a separate item, never an accumulating state blob.
Only the trusted Authority owns these objects; workers receive no storage handle.
"""

import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
from pathlib import Path
from threading import RLock

from agent_alfred.resource_rollback import ConstructionOwner, capture_call_result

from ..materials import strict_json
from ..schema import digest, encode
from .contract import exact, integer, text
from .store import ZERO

# 576 * 14 = 8064 successful Attempt events; up to 216 finishes * 2,
# 4 phase transitions * 2, activation, and 7879 slots for control/audit/faults.
# Exhaustion stops; it never truncates, rolls over, or opens another job.
MAX_EVENTS = 16384
MAX_STATE_BYTES = 256 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_OBJECTS = 8192
MAX_CONTROL_REQUESTS = 4096
MAX_FAULTS = 256


def bounded(value, limit):
    raw = encode(value)
    if len(raw) > limit:
        raise ValueError("execution_document_capacity_exceeded")
    return raw


def check_event(state, event, old_revision, old_digest, attempt):
    integer(state["revision"], minimum=1)
    if state["revision"] > MAX_EVENTS:
        raise ValueError("execution_event_capacity_exceeded")
    if (
        state["revision"] != old_revision + 1
        or event["revision"] != state["revision"]
        or event["job_id"] != state["job_id"]
        or event["previous_digest"] != old_digest
        or event["state_digest"]
        != digest({k: v for k, v in state.items() if k != "event_digest"})
        or digest(event) != state["event_digest"]
        or event.get("attempt_id") != (attempt["attempt_id"] if attempt else None)
        or event.get("attempt_sha256") != (digest(attempt) if attempt else None)
    ):
        raise ValueError("ledger_event_invalid")
    bounded(state, MAX_STATE_BYTES)
    bounded(event, MAX_EVENT_BYTES)
    if attempt is not None:
        bounded(attempt, MAX_EVENT_BYTES)


class DocumentLedger:
    """Shared verified protocol over an atomic document backend.

    Backend transaction rows are (partition, key, document, expected_digest).
    None means insert-only. A backend must atomically reject the whole group if
    any old digest differs, and must read strongly consistently with pagination.
    """

    def __init__(self, backend):
        self.backend = backend
        self._validated = {}

    def _partition(self, job_id):
        return "JOB#" + text(job_id)

    def faults(self, job_id):
        rows = self.backend.scan(self._partition(job_id), "FAULT#")
        previous = ZERO
        for number, (_, row) in enumerate(rows, 1):
            if row["sequence"] != number or row["previous"] != previous:
                raise ValueError("execution_fault_history_unverifiable")
            previous = digest(row)
        return [row for _, row in rows]

    def record_fault(self, job_id, reason, detail):
        """An irreversible local barrier, mirrored by the service in both domains."""
        partition = self._partition(job_id)
        observation = {"reason": text(reason), "detail": deepcopy(detail)}
        bounded(observation, MAX_EVENT_BYTES)
        for _ in range(4):
            rows = self.faults(job_id)
            if any(r["observation"] == observation for r in rows):
                return
            if len(rows) >= MAX_FAULTS:
                raise ValueError("execution_fault_capacity_exceeded")
            row = {
                "sequence": len(rows) + 1,
                "previous": digest(rows[-1]) if rows else ZERO,
                "observation": observation,
            }
            try:
                self.backend.transaction(
                    [(partition, f"FAULT#{row['sequence']:06d}", row, None)]
                )
                return
            except ValueError as error:
                if str(error) != "ledger_conflict":
                    raise
        raise ValueError("ledger_conflict")


class DurableExecutionStore(DocumentLedger):
    def get(self, job_id):
        part = self._partition(job_id)
        state = self.backend.read(part, "STATE")
        if state is None:
            if self.backend.scan(part, "EVENT#"):
                raise ValueError("authority_state_missing")
            return None
        event = self.backend.read(part, f"EVENT#{state['revision']:06d}")
        if (
            state["job_id"] != job_id
            or event is None
            or digest(event) != state["event_digest"]
            or event["state_digest"]
            != digest({k: v for k, v in state.items() if k != "event_digest"})
        ):
            raise ValueError("authority_state_unverifiable")
        cached = self._validated.get(job_id)
        head = (self.backend.generation(), state["revision"], state["event_digest"])
        if cached is None or cached[:3] != head:
            events = self._read_events(state)
            latest = {
                event["attempt_id"]: (event["revision"], event["attempt_sha256"])
                for event in events
                if event["attempt_id"] is not None
            }
            self._validated[job_id] = (*head, latest)
        return state

    def put(self, state, event, *, old_revision, old_digest, attempt=None):
        check_event(state, event, old_revision, old_digest, attempt)
        part = self._partition(state["job_id"])
        old = self.get(state["job_id"])
        if (old is None and (old_revision != 0 or old_digest != ZERO)) or (
            old is not None
            and (old["revision"] != old_revision or old["event_digest"] != old_digest)
        ):
            raise ValueError("ledger_conflict")
        writes = [
            (part, "STATE", state, digest(old) if old else None),
            (part, f"EVENT#{state['revision']:06d}", event, None),
        ]
        if attempt is not None:
            key = "ATTEMPT#" + text(attempt["attempt_id"])
            before = self.backend.read(part, key)
            if before is None:
                writes.append(
                    (
                        part,
                        "CONSUMED#" + attempt["prepared_ref"],
                        {"attempt_id": attempt["attempt_id"]},
                        None,
                    )
                )
            elif before["row"]["prepared_ref"] != attempt["prepared_ref"]:
                raise ValueError("original_attempt_binding_immutable")
            writes.append(
                (
                    part,
                    key,
                    {"row": attempt, "revision": state["revision"]},
                    digest(before) if before else None,
                )
            )
        self.backend.transaction(writes)
        previous_attempts = dict(
            self._validated.get(state["job_id"], (None, None, None, {}))[3]
        )
        if attempt is not None:
            previous_attempts[attempt["attempt_id"]] = (
                state["revision"],
                digest(attempt),
            )
        self._validated[state["job_id"]] = (
            self.backend.generation(),
            state["revision"],
            state["event_digest"],
            previous_attempts,
        )

    def get_attempt(self, job_id, attempt_id):
        self.get(job_id)
        part = self._partition(job_id)
        document = self.backend.read(part, "ATTEMPT#" + text(attempt_id))
        if document is None:
            return None
        row = document["row"]
        event = self.backend.read(part, f"EVENT#{document['revision']:06d}")
        if (
            row["attempt_id"] != attempt_id
            or event is None
            or event["attempt_id"] != attempt_id
            or event["attempt_sha256"] != digest(row)
            or self._validated[job_id][3].get(attempt_id)
            != (document["revision"], digest(row))
        ):
            raise ValueError("attempt_ledger_unverifiable")
        return row

    def list_attempts(self, job_id):
        self.get(job_id)
        rows = self.backend.scan(self._partition(job_id), "ATTEMPT#")
        if len(rows) > 576:
            raise ValueError("attempt_ledger_capacity_exceeded")
        if {row["row"]["attempt_id"] for _, row in rows} != set(
            self._validated[job_id][3]
        ):
            raise ValueError("attempt_ledger_unverifiable")
        values = []
        for _, document in rows:
            row = document["row"]
            if self._validated[job_id][3].get(row["attempt_id"]) != (
                document["revision"],
                digest(row),
            ):
                raise ValueError("attempt_ledger_unverifiable")
            values.append(row)
        return values

    def prepared_consumed(self, job_id, reference):
        consumed = self.backend.read(self._partition(job_id), "CONSUMED#" + reference)
        if consumed is None:
            return False
        row = self.get_attempt(job_id, consumed["attempt_id"])
        if row is None or row["prepared_ref"] != reference:
            raise ValueError("attempt_ledger_unverifiable")
        return True

    def put_object(self, value):
        bounded(value, MAX_OBJECT_BYTES)
        return self.backend.put_object(value)

    def get_object(self, identity):
        return self.backend.get_object(identity)

    def read_events(self, job_id):
        """Complete ordered audit read, no last-N truncation or hidden pagination."""
        state = self.get(job_id)
        if state is None:
            raise ValueError("job_unknown")
        return self._read_events(state)

    def _read_events(self, state):
        job_id = state["job_id"]
        rows = self.backend.scan(self._partition(job_id), "EVENT#")
        previous = ZERO
        if state is None or len(rows) != state["revision"]:
            raise ValueError("authority_event_unverifiable")
        for number, (key, event) in enumerate(rows, 1):
            if (
                key != f"EVENT#{number:06d}"
                or event["job_id"] != job_id
                or event["revision"] != number
                or event["previous_digest"] != previous
            ):
                raise ValueError("authority_event_unverifiable")
            previous = digest(event)
        if previous != state["event_digest"]:
            raise ValueError("authority_event_unverifiable")
        return [event for _, event in rows]

    def claim_control(self, job_id, request_id, request):
        part, key = self._partition(job_id), "CONTROL#" + text(request_id)
        current = self.backend.read(part, key)
        if current is not None:
            if current["request_sha256"] != digest(request):
                raise ValueError("control_id_reused")
            return False, current
        if len(self.backend.scan(part, "CONTROL#")) >= MAX_CONTROL_REQUESTS:
            raise ValueError("control_capacity_exceeded")
        row = {
            "request_sha256": digest(request),
            "state": "PENDING",
            "result_ref": None,
        }
        try:
            self.backend.transaction([(part, key, row, None)])
        except ValueError as error:
            if str(error) != "ledger_conflict":
                raise
            current = self.backend.read(part, key)
            if current is None or current["request_sha256"] != digest(request):
                raise ValueError("control_id_reused") from error
            return False, current
        return True, row

    def complete_control(self, job_id, request_id, request, result):
        part, key = self._partition(job_id), "CONTROL#" + text(request_id)
        old = self.backend.read(part, key)
        if old is None or old["request_sha256"] != digest(request):
            raise ValueError("control_request_unverifiable")
        result_ref = self.put_object(result)
        row = {**old, "state": "COMPLETED", "result_ref": result_ref}
        if old["state"] == "COMPLETED":
            if old != row:
                raise ValueError("control_result_changed")
            return old
        self.backend.transaction([(part, key, row, digest(old))])
        return row

    def control_status(self, job_id, request_id):
        return self.backend.read(self._partition(job_id), "CONTROL#" + text(request_id))

    def capacity(self, job_id):
        rows = self.backend.scan(self._partition(job_id), "")
        sizes = {}
        for key, row in rows:
            kind = key.split("#", 1)[0].lower()
            sizes.setdefault(kind, {"count": 0, "max_bytes": 0, "total_bytes": 0})
            value = len(encode(row))
            sizes[kind]["count"] += 1
            sizes[kind]["total_bytes"] += value
            sizes[kind]["max_bytes"] = max(sizes[kind]["max_bytes"], value)
        sizes["objects"] = self.backend.object_capacity()
        return {
            "limits": {
                "events": MAX_EVENTS,
                "state_bytes": MAX_STATE_BYTES,
                "event_bytes": MAX_EVENT_BYTES,
                "object_bytes": MAX_OBJECT_BYTES,
                "objects": MAX_OBJECTS,
                "control_requests": MAX_CONTROL_REQUESTS,
            },
            "observed": sizes,
            "real_cloud_capacity": "NOT_VERIFIED",
        }


class DurableExecutionAnchor(DocumentLedger):
    def read(self, job_id):
        part = self._partition(job_id)
        high = self.backend.read(part, "HIGH")
        if high is None:
            if self.backend.scan(part, "ANCHOR#"):
                raise ValueError("anchor_high_water_missing")
            return None
        event = self.backend.read(part, f"ANCHOR#{high['revision']:06d}")
        if event is None or event["event_digest"] != high["digest"]:
            raise ValueError("anchor_high_water_unverifiable")
        self.backend.verify_anchor(event)
        head = (self.backend.generation(), high["revision"], high["digest"])
        if self._validated.get(job_id) != head:
            self._read_events(high)
            self._validated[job_id] = head
        return high

    def commit(self, event):
        exact(event, "job_id revision previous_digest event_digest")
        integer(event["revision"], minimum=1)
        if event["revision"] > MAX_EVENTS:
            raise ValueError("execution_event_capacity_exceeded")
        job_id, part = text(event["job_id"]), self._partition(event["job_id"])
        old = self.read(job_id)
        if (
            old is None and (event["revision"] != 1 or event["previous_digest"] != ZERO)
        ) or (
            old is not None
            and (
                event["revision"] != old["revision"] + 1
                or event["previous_digest"] != old["digest"]
            )
        ):
            raise ValueError("anchor_conflict")
        high = {
            "job_id": job_id,
            "revision": event["revision"],
            "digest": event["event_digest"],
        }
        self.backend.transaction(
            [
                (part, "HIGH", high, digest(old) if old else None),
                (part, f"ANCHOR#{event['revision']:06d}", event, None),
            ]
        )
        # Like the established P2 anchor: a partial DDB/object commit stays
        # unverifiable. Authority never repairs it into permission.
        self.backend.save_anchor(event)
        self._validated[job_id] = (
            self.backend.generation(),
            high["revision"],
            high["digest"],
        )
        if self.read(job_id) != high:
            raise ValueError("anchor_commit_unverifiable")

    def read_events(self, job_id):
        high = self.read(job_id)
        if high is None:
            raise ValueError("job_unknown")
        return self._read_events(high)

    def _read_events(self, high):
        job_id = high["job_id"]
        rows = self.backend.scan(self._partition(job_id), "ANCHOR#")
        if high is None or len(rows) != high["revision"]:
            raise ValueError("anchor_chain_unverifiable")
        previous = ZERO
        for number, (key, event) in enumerate(rows, 1):
            if (
                key != f"ANCHOR#{number:06d}"
                or event["job_id"] != job_id
                or event["revision"] != number
                or event["previous_digest"] != previous
            ):
                raise ValueError("anchor_chain_unverifiable")
            self.backend.verify_anchor(event)
            previous = event["event_digest"]
        if previous != high["digest"]:
            raise ValueError("anchor_chain_unverifiable")
        self.backend.verify_anchor_inventory(job_id, [event for _, event in rows])
        return [event for _, event in rows]


class SQLiteDocumentStorage:
    """Shared SQLite document algorithm; this class confers no installation trust."""

    def __init__(self, path, *, _rollback=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = None
        owner = ConstructionOwner(_rollback)
        owner.rollback.own(self)
        try:
            capture_call_result(
                self,
                "db",
                partial(
                    sqlite3.connect,
                    self.path,
                    timeout=5,
                    check_same_thread=False,
                    isolation_level=None,
                ),
            )
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS documents (part TEXT NOT NULL, "
                "key TEXT NOT NULL, sha256 TEXT NOT NULL, raw BLOB NOT NULL, "
                "PRIMARY KEY(part,key))"
            )
            self.identity = (self.path.stat().st_dev, self.path.stat().st_ino)
            owner.publish(self)
        except BaseException as failure:
            owner.fail(failure)

    def _present(self):
        try:
            current = self.path.stat()
        except OSError as error:
            raise ValueError("execution_store_missing") from error
        if (current.st_dev, current.st_ino) != self.identity:
            raise ValueError("execution_store_replaced")

    def generation(self):
        with self._locked():
            return self.db.execute("PRAGMA data_version").fetchone()[0]

    @contextmanager
    def _locked(self):
        with self.lock:
            self._present()
            yield

    def _decode(self, row):
        if row is None:
            return None
        value = strict_json(row[1], limit=MAX_OBJECT_BYTES)
        if digest(value) != row[0]:
            raise ValueError("execution_document_unverifiable")
        return value

    def read(self, part, key):
        with self._locked():
            return self._decode(
                self.db.execute(
                    "SELECT sha256,raw FROM documents WHERE part=? AND key=?",
                    (part, key),
                ).fetchone()
            )

    def scan(self, part, prefix):
        with self._locked():
            rows = self.db.execute(
                "SELECT key,sha256,raw FROM documents WHERE part=? "
                "AND substr(key,1,?)=? ORDER BY key",
                (part, len(prefix), prefix),
            ).fetchall()
            return [(row[0], self._decode(row[1:])) for row in rows]

    def transaction(self, writes):
        with self._locked():
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for part, key, value, old in writes:
                    found = self.db.execute(
                        "SELECT sha256 FROM documents WHERE part=? AND key=?",
                        (part, key),
                    ).fetchone()
                    if (found[0] if found else None) != old:
                        raise ValueError("ledger_conflict")
                    self.db.execute(
                        "INSERT INTO documents VALUES (?,?,?,?) "
                        "ON CONFLICT(part,key) DO UPDATE SET sha256=excluded.sha256, "
                        "raw=excluded.raw",
                        (part, key, digest(value), encode(value)),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def put_object(self, value):
        identity = digest(value)
        old = self.read("OBJECT", identity)
        if old is not None:
            return identity
        with self._locked():
            count = self.db.execute(
                "SELECT count(*) FROM documents WHERE part='OBJECT'"
            ).fetchone()[0]
            if count >= MAX_OBJECTS:
                raise ValueError("execution_object_capacity_exceeded")
            try:
                self.transaction([("OBJECT", identity, value, None)])
            except ValueError as error:
                if (
                    str(error) != "ledger_conflict"
                    or self.get_object(identity) != value
                ):
                    raise
        return identity

    def get_object(self, identity):
        value = self.read("OBJECT", identity)
        if value is None or digest(value) != identity:
            raise ValueError("execution_object_mismatch")
        return value

    def object_capacity(self):
        with self._locked():
            count, maximum, total = self.db.execute(
                "SELECT count(*),coalesce(max(length(raw)),0),"
                "coalesce(sum(length(raw)),0) FROM documents WHERE part='OBJECT'"
            ).fetchone()
        return {"count": count, "max_bytes": maximum, "total_bytes": total}

    def save_anchor(self, event):
        pass  # In SQLite the atomic append is the fixture's durable anchor.

    def verify_anchor(self, event):
        pass

    def verify_anchor_inventory(self, job_id, events):
        pass

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()


class SQLiteDocuments(SQLiteDocumentStorage):
    """Unprotected local fixture, never a production installation."""

    synthetic_only = True


class SQLiteExecutionStore(DurableExecutionStore):
    synthetic_only = True

    def __init__(self, path, *, _rollback=None):
        owner = ConstructionOwner(_rollback)
        try:
            backend = SQLiteDocuments(path, _rollback=owner.rollback)
            super().__init__(backend)
            owner.publish(self, parts=(backend,))
        except BaseException as failure:
            owner.fail(failure)

    def close(self):
        self.backend.close()


class SQLiteExecutionAnchor(DurableExecutionAnchor):
    synthetic_only = True

    def __init__(self, path, *, _rollback=None):
        owner = ConstructionOwner(_rollback)
        try:
            backend = SQLiteDocuments(path, _rollback=owner.rollback)
            super().__init__(backend)
            owner.publish(self, parts=(backend,))
        except BaseException as failure:
            owner.fail(failure)

    def close(self):
        self.backend.close()
