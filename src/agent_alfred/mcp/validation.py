"""One validation process at a time, with actual bounded termination."""

import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path


class Validator:
    def __init__(self):
        self.process = None
        self.spawn_failed = False
        self.lock = threading.Lock()
        self.declarations = set()

    @staticmethod
    def available():
        return all(
            importlib.util.find_spec(name) is not None
            for name in ("jsonschema", "referencing")
        )

    def validate(self, schema, deadline, monotonic=time.monotonic, **instance):
        if not self.lock.acquire(blocking=False):
            return "validation_busy"
        try:
            if not self.close():
                return "validation_cleanup_incomplete"
            raw = json.dumps(
                {"schema": schema, **instance}, ensure_ascii=False, allow_nan=False
            ).encode()
            if len(json.dumps(schema, ensure_ascii=False).encode()) > 65536:
                return "schema_limit"
            if len(raw) > 2 * 1024 * 1024:
                return "validation_input_limit"
            remaining = min(2, deadline - monotonic())
            if remaining <= 0:
                return "validation_deadline"
            identity = hashlib.sha256(raw).digest()
            if not instance and identity in self.declarations:
                return None
            # Absolute source entry supports both installed artifacts and editable
            # builds, without passing Host environment or PYTHONPATH to the child.
            self.process = subprocess.Popen.__new__(subprocess.Popen)
            self.spawn_failed = False
            try:
                subprocess.Popen.__init__(
                    self.process,
                    [sys.executable, str(Path(__file__).with_name("schema_worker.py"))],
                    env={},
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                self.spawn_failed = True
                raise
            try:
                output, _ = self.process.communicate(raw, timeout=remaining)
                if self.process.returncode != 0 or len(output) > 4096:
                    return "validation_failed"
                error = json.loads(output).get("error")
                if error is None and not instance:
                    if len(self.declarations) >= 400:
                        self.declarations.clear()
                    self.declarations.add(identity)
                return error
            except subprocess.TimeoutExpired:
                return "validation_timeout"
            finally:
                self.close()
        except OSError, ValueError, TypeError:
            return "validation_failed"
        finally:
            self.lock.release()

    def close(self, deadline=None):
        if self.process is None:
            return True
        if getattr(self.process, "pid", None) is None:
            if not self.spawn_failed or getattr(self.process, "_child_created", False):
                return False  # Native creation was interrupted, not disproved.
            for name in ("stdin", "stdout", "stderr"):
                pipe = getattr(self.process, name, None)
                if pipe is not None:
                    pipe.close()
            self.process = None
            return True
        deadline = time.monotonic() + 2 if deadline is None else deadline
        if self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                return False
            try:
                self.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return False
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None and not pipe.closed:
                pipe.close()
        self.process = None
        return True
