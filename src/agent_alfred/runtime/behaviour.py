"""Independent Behaviour configuration, with explicit fingerprint-bound recovery."""

import json
import os
import threading
import uuid
from pathlib import Path

from agent_alfred.atomic_config import DiskConflict, read_bytes, write_atomic


class BehaviourError(Exception):
    def __init__(self, code, cause=None, *, backup_path=None):
        super().__init__(code + (":" + cause if cause else ""))
        self.code = code
        self.cause = cause
        self.backup_path = backup_path


class BehaviourStore:
    """Host serializes mutations; IO callbacks expose only durability boundaries."""

    def __init__(self, path, *, reader=read_bytes, writer=write_atomic, backup=None):
        self.path = Path(path)
        self._reader = reader
        self._writer = writer
        self._backup = backup or self._backup_bytes
        self._lock = threading.RLock()
        self._state = None
        self._digest = None
        self._backup_path = None

    def snapshot(self):
        with self._lock:
            return (
                {**self._state, "backup_path": self._backup_path}
                if self._state is not None
                else self.read()
            )

    def read(self):
        with self._lock:
            state = dict(schema_version=1, revision=0, enabled=False, status="ok")
            try:
                raw, digest = self._reader(self.path)
            except OSError, ValueError:
                raw, digest = None, None
                state["status"] = "settings_unreadable"
            if raw is not None:
                try:
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError()
                    version = body.get("schema_version")
                    if type(version) is int and version > 1:
                        state["status"] = "settings_schema_newer"
                    elif (
                        type(version) is not int
                        or version != 1
                        or type(body.get("revision")) is not int
                        or body["revision"] < 1
                        or type(body.get("enabled")) is not bool
                    ):
                        raise ValueError()
                    else:
                        state.update(revision=body["revision"], enabled=body["enabled"])
                except ValueError, UnicodeError:
                    state["status"] = "settings_invalid"
            self._digest = digest
            state.update(fingerprint=digest, backup_path=self._backup_path)
            self._state = state
            return dict(state)

    def _check(self, expected_revision):
        state = self.snapshot()
        if type(expected_revision) is not int or expected_revision != state["revision"]:
            raise BehaviourError("settings_conflict", "stale_revision")
        try:
            raw, digest = self._reader(self.path)
        except (OSError, ValueError) as exc:
            raise BehaviourError("settings_unreadable") from exc
        if digest != self._digest:
            raise BehaviourError("settings_conflict", "external_change")
        return state, raw, digest

    def save(self, *, expected_revision, enabled):
        with self._lock:
            if type(enabled) is not bool:
                raise BehaviourError("settings_invalid")
            state, _, digest = self._check(expected_revision)
            if state["status"] != "ok":
                raise BehaviourError(state["status"])
            return self._write(state["revision"] + 1, enabled, digest)

    def recover(self, *, expected_revision, fingerprint):
        with self._lock:
            state, raw, digest = self._check(expected_revision)
            if state["status"] == "ok" or raw is None or digest is None:
                raise BehaviourError("recovery_not_available")
            if fingerprint != digest:
                raise BehaviourError("settings_conflict", "external_change")
            try:
                self._backup(raw)
            except (OSError, ValueError) as exc:
                raise BehaviourError(
                    "settings_write_failed", backup_path=self._backup_path
                ) from exc
            return self._write(state["revision"] + 1, False, digest)

    def _write(self, revision, enabled, digest):
        payload = json.dumps(
            dict(schema_version=1, revision=revision, enabled=enabled),
            separators=(",", ":"),
        ).encode()
        try:
            self._writer(self.path, payload, digest)
            confirmed, confirmed_digest = self._reader(self.path)
            if confirmed != payload:
                raise OSError("readback_failed")
        except DiskConflict as exc:
            raise BehaviourError(
                "settings_conflict", "external_change", backup_path=self._backup_path
            ) from exc
        except (OSError, ValueError) as exc:
            # Rename may already have happened. No success snapshot until an
            # explicit fresh read; the backup reference survives partial writes.
            self._state.update(
                status="settings_write_unconfirmed",
                enabled=False,
                backup_path=self._backup_path,
            )
            raise BehaviourError(
                "settings_write_failed", backup_path=self._backup_path
            ) from exc
        # Publish the exact readback already verified above. A second read
        # can fail independently and must not turn a successful write into a
        # successful response containing the opposite default configuration.
        self._digest = confirmed_digest
        self._state = dict(
            schema_version=1,
            revision=revision,
            enabled=enabled,
            status="ok",
            fingerprint=confirmed_digest,
            backup_path=self._backup_path,
        )
        return dict(self._state)

    def _backup_bytes(self, raw):
        path = self.path.with_name(self.path.name + ".backup-" + uuid.uuid4().hex)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._backup_path = str(path)
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if self._reader(path)[0] != raw:
            raise OSError("backup_readback_failed")
