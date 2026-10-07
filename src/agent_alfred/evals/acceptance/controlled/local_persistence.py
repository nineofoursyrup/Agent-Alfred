"""Owner protected SQLite ledger and an append/fsync local witness.

This is a distinct installed adapter. SQLite fixture classes retain their
synthetic-only guard. The witness is independent of the ledger file, not of the
trusted macOS owner: a coordinated administrator rollback is not detectable.
"""

import fcntl
import hashlib
import os
from bisect import bisect_left, insort
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
from threading import RLock

from agent_alfred.resource_rollback import (
    ConstructionOwner,
    OwnedDescriptor,
    ResumableRollback,
    capture_call_result,
)

from ..materials import strict_json
from ..schema import digest, encode
from .contract import exact
from .local_files import OwnerDirectory
from .persistence import (
    MAX_EVENT_BYTES,
    DurableExecutionAnchor,
    DurableExecutionStore,
    SQLiteDocumentStorage,
)
from .store import ZERO

_LOCAL_STORAGE = object()


class OwnerSQLiteDocuments(SQLiteDocumentStorage):
    def __init__(self, directory, filename, *, token=None, _rollback=None):
        if token is not _LOCAL_STORAGE or type(directory) is not OwnerDirectory:
            raise ValueError("trusted_local_installation_required")
        self.directory = directory
        super().__init__(directory.file(filename, create=True), _rollback=_rollback)

    def _present(self):
        self.directory.file(self.path)
        super()._present()
        for suffix in ("-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists() or path.is_symlink():
                self.directory.file(path)


class AppendWitnessDocuments:
    """Atomic CAS groups in one append-only, checksummed, fsynced journal.

    All processes serialize through flock. A torn append, deletion, replacement or
    observed truncation blocks the caller; no automatic repair grants permission.
    """

    def __init__(self, directory, filename, *, token=None, _rollback=None):
        if token is not _LOCAL_STORAGE or type(directory) is not OwnerDirectory:
            raise ValueError("trusted_local_installation_required")
        self.directory = directory
        self.path = directory.file(filename, create=True)
        self.lock = RLock()
        self._descriptor = OwnedDescriptor()
        self._resources = ResumableRollback()
        self._resources.own(self._descriptor)
        self.documents, self.offset, self.sequence, self.head = {}, 0, 0, ZERO
        self.keys = {}
        self._stamp = None
        self._prefix_hash = hashlib.sha256()
        owner = ConstructionOwner(_rollback)
        owner.rollback.own(self._resources)
        try:
            capture_call_result(
                self._descriptor,
                "fd",
                partial(os.open, self.path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW),
            )
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino)
            with self._locked():
                pass
            owner.publish(self, parts=(self._resources,))
        except BaseException as error:
            owner.fail(error)

    @property
    def fd(self):
        value = self._descriptor.fd
        return value if value >= 0 else None

    def _changes(self, writes):
        changed = {}
        for part, key, value, old in writes:
            identity = (part, key)
            if type(part) is not str or type(key) is not str or identity in changed:
                raise ValueError("local_witness_invalid")
            before = self.documents.get(identity)
            if (digest(before) if before is not None else None) != old:
                raise ValueError("ledger_conflict")
            changed[identity] = deepcopy(value)
        return changed

    def _apply(self, writes):
        changed = self._changes(writes)
        for part, key in changed:
            if (part, key) not in self.documents:
                insort(self.keys.setdefault(part, []), key)
        self.documents.update(changed)

    @staticmethod
    def _file_stamp(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def _verify_prefix(self):
        observed = hashlib.sha256()
        position = 0
        while position < self.offset:
            raw = os.pread(self.fd, min(512 * 1024, self.offset - position), position)
            if not raw:
                raise ValueError("local_witness_rollback")
            observed.update(raw)
            position += len(raw)
        if observed.digest() != self._prefix_hash.digest():
            raise ValueError("local_witness_prefix_changed")

    def _refresh(self, *, own_append=None):
        info = os.fstat(self.fd)
        if info.st_size < self.offset:
            raise ValueError("local_witness_rollback")
        stamp = self._file_stamp(info)
        if own_append is not None:
            if info.st_size != self.offset + len(own_append):
                raise ValueError("local_witness_changed_during_append")
        elif self._stamp is not None and stamp != self._stamp:
            # Another writer or a same-size rewrite must prove the old prefix
            # still matches the exact bytes already observed by this process.
            self._verify_prefix()
        os.lseek(self.fd, self.offset, os.SEEK_SET)
        with os.fdopen(os.dup(self.fd), "rb") as stream:
            while raw := stream.readline(2 * MAX_EVENT_BYTES + 1):
                if not raw.endswith(b"\n") or len(raw) > 2 * MAX_EVENT_BYTES:
                    raise ValueError("local_witness_incomplete")
                if own_append is not None and raw != own_append:
                    raise ValueError("local_witness_changed_during_append")
                row = strict_json(raw, limit=2 * MAX_EVENT_BYTES)
                exact(row, "contract version sequence previous writes sha256")
                body = {k: v for k, v in row.items() if k != "sha256"}
                if (
                    row["contract"] != "V1-LOCAL-APPEND-WITNESS"
                    or type(row["version"]) is not int
                    or row["version"] != 1
                    or row["sequence"] != self.sequence + 1
                    or row["previous"] != self.head
                    or digest(body) != row["sha256"]
                ):
                    raise ValueError("local_witness_unverifiable")
                self._apply(row["writes"])
                self._prefix_hash.update(raw)
                self.sequence, self.head = row["sequence"], row["sha256"]
                self.offset += len(raw)
        if self._file_stamp(os.fstat(self.fd)) != stamp:
            raise ValueError("local_witness_changed_during_read")
        self._stamp = stamp

    @contextmanager
    def _locked(self):
        with self.lock:
            if self.fd is None:
                raise ValueError("local_witness_closed")
            self.directory.file(self.path)
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) != self.identity:
                raise ValueError("local_witness_replaced")
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            try:
                self._refresh()
                yield
            finally:
                fcntl.flock(self.fd, fcntl.LOCK_UN)

    def generation(self):
        with self._locked():
            return self.sequence

    def read(self, part, key):
        with self._locked():
            return deepcopy(self.documents.get((part, key)))

    def scan(self, part, prefix):
        with self._locked():
            keys = self.keys.get(part, [])
            start = bisect_left(keys, prefix)
            selected = []
            for index in range(start, len(keys)):
                key = keys[index]
                if not key.startswith(prefix):
                    break
                selected.append(key)
            return [(key, deepcopy(self.documents[(part, key)])) for key in selected]

    def transaction(self, writes):
        with self._locked():
            # Validate the entire group before any persistent bytes change.
            self._changes(writes)
            body = {
                "contract": "V1-LOCAL-APPEND-WITNESS",
                "version": 1,
                "sequence": self.sequence + 1,
                "previous": self.head,
                "writes": writes,
            }
            raw = encode({**body, "sha256": digest(body)}) + b"\n"
            if len(raw) > 2 * MAX_EVENT_BYTES:
                raise ValueError("local_witness_capacity_exceeded")
            # On interruption the durable tail may be partial. Refresh never
            # silently truncates it, and Authority retains the original fault.
            sent = 0
            while sent < len(raw):
                count = os.write(self.fd, raw[sent:])
                if count <= 0:
                    raise OSError("local_witness_write_failed")
                sent += count
            os.fsync(self.fd)
            # Our own locked append has a known exact tail. Verify that tail
            # without re-hashing the entire prefix for every ledger event.
            self._refresh(own_append=raw)

    def save_anchor(self, event):
        self.verify_anchor(event)

    def verify_anchor(self, event):
        found = self.read("JOB#" + event["job_id"], f"ANCHOR#{event['revision']:06d}")
        if found != event:
            raise ValueError("local_witness_anchor_mismatch")

    def verify_anchor_inventory(self, job_id, events):
        if self.scan("JOB#" + job_id, "ANCHOR#") != [
            (f"ANCHOR#{event['revision']:06d}", event) for event in events
        ]:
            raise ValueError("local_witness_inventory_mismatch")

    def close(self):
        with self.lock:
            self._resources.close()


class LocalExecutionStore(DurableExecutionStore):
    trust_profile = "owner_trusted_local"

    def __init__(self, directory, filename, *, token=None, _rollback=None):
        owner = ConstructionOwner(_rollback)
        try:
            backend = OwnerSQLiteDocuments(
                directory, filename, token=token, _rollback=owner.rollback
            )
            super().__init__(backend)
            owner.publish(self, parts=(backend,))
        except BaseException as failure:
            owner.fail(failure)

    def close(self):
        self.backend.close()


class LocalExecutionWitness(DurableExecutionAnchor):
    trust_profile = "owner_trusted_local"
    independent_administrator = False

    def __init__(self, directory, filename, *, token=None, _rollback=None):
        owner = ConstructionOwner(_rollback)
        try:
            backend = AppendWitnessDocuments(
                directory, filename, token=token, _rollback=owner.rollback
            )
            super().__init__(backend)
            owner.publish(self, parts=(backend,))
        except BaseException as failure:
            owner.fail(failure)

    def close(self):
        self.backend.close()
