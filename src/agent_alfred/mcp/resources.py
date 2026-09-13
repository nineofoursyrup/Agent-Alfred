"""Persist crash ownership; a stale PID never grants permission to kill."""

import json
import os

from agent_alfred.atomic_config import read_bytes, write_atomic


class Resources:
    def __init__(self, directory):
        self.path = directory / "mcp-resources.json"
        raw, self.fingerprint = read_bytes(self.path)
        self.records = json.loads(raw) if raw is not None else {}
        if not isinstance(self.records, dict) or any(
            not isinstance(k, str) or (v is not None and (type(v) is not int or v <= 0))
            for k, v in self.records.items()
        ):
            raise ValueError("resource_ownership_unconfirmed")
        self.inherited = set(self.records)

    def persist(self):
        self.fingerprint = write_atomic(
            self.path, json.dumps(self.records).encode(), self.fingerprint
        )

    def recovered(self, key):
        if key not in self.inherited:
            return True
        pid = self.records.get(key)
        if pid is None:
            return False
        try:
            os.killpg(pid, 0)
            return False
        except ProcessLookupError:
            self.inherited.remove(key)
            self.records.pop(key, None)
            self.persist()
            return True
        except OSError, OverflowError:
            return False

    def intent(self, key):
        if not self.recovered(key):
            raise ValueError("resource_ownership_unconfirmed")
        self.records[key] = None
        self.persist()

    def spawned(self, key, pid):
        self.records[key] = pid
        self.persist()

    def closed(self, key):
        if key not in self.inherited:
            self.records.pop(key, None)
            self.persist()
