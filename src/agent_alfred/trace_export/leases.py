"""In-process bundle references and deletion reservations share one lock."""

import threading
from contextlib import contextmanager

from agent_alfred.trace_export.errors import ExportError


class BundleLeases:
    def __init__(self):
        self._lock = threading.Lock()
        self._readers = {}
        self._deleting = set()

    def acquire(self, run_id, owner):
        with self._lock:
            if run_id in self._deleting:
                raise ExportError("trace_pruned")
            self._readers.setdefault(run_id, set()).add(owner)

    def release(self, run_id, owner):
        with self._lock:
            readers = self._readers.get(run_id)
            if readers is not None:
                readers.discard(owner)
            if not readers:
                self._readers.pop(run_id, None)

    @contextmanager
    def deleting(self, run_id):
        with self._lock:
            if self._readers.get(run_id) or run_id in self._deleting:
                raise ExportError("busy")
            self._deleting.add(run_id)
        try:
            yield
        finally:
            with self._lock:
                self._deleting.remove(run_id)
