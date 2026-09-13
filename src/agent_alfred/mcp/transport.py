"""Bounded legacy stdio session; owns its process group and pipe reader."""

import json
import os
import select
import signal
import subprocess
import threading
import time
from collections import deque

from agent_alfred.resource_rollback import (
    thread_exit_confirmed,
    thread_start_effect_happened,
)

MAX_FRAME = 1024 * 1024


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    if len(raw) > MAX_FRAME:
        raise ValueError("frame_limit")
    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("json_number")),
    )
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, (dict, list)):
            if depth >= 64:
                raise ValueError("depth_limit")
            pending.extend(
                (v, depth + 1)
                for v in (node.values() if isinstance(node, dict) else node)
            )
    return value


class TransportError(Exception):
    def __init__(self, reason, sent=0):
        super().__init__(reason)
        self.reason, self.sent = reason, sent


class Session:
    def __init__(self, redactor):
        self.redactor = redactor
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.process = None
        self.spawn_failed = False
        self.reader = None
        self.pending = None
        self.response = None
        self.error = None
        self.next_id = 0
        self.response_bytes = 0
        self.last_response_bytes = 0
        self.changed = False
        self.diagnostics = deque(maxlen=50)
        self.stopping = False
        self.closed = False

    def start(self, command, args, cwd, env):
        self.process = subprocess.Popen.__new__(subprocess.Popen)
        self.spawn_failed = False
        try:
            subprocess.Popen.__init__(
                self.process,
                [command, *args],
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except OSError:
            self.spawn_failed = True
            raise
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
        self.reader = threading.Thread(target=self._read, name="mcp-pipes", daemon=True)
        self.reader.start()

    def _diagnose(self, text):
        safe = self.redactor.redact_text(text)[-65536:]
        self.diagnostics.append(safe)
        while sum(len(s.encode("utf-8")) for s in self.diagnostics) > 65536:
            self.diagnostics.popleft()

    def _read(self):
        stdout, stderr = self.process.stdout, self.process.stderr
        streams = {stdout.fileno(): "stdout", stderr.fileno(): "stderr"}
        buffer = bytearray()
        stderr_buffer = bytearray()
        stderr_discard = False
        try:
            while streams:
                ready, _, _ = select.select(list(streams), [], [], 0.05)
                for fd in ready:
                    raw = os.read(fd, 65536)
                    if not raw:
                        if streams.pop(fd) == "stdout":
                            raise ValueError("stdout_eof")
                        if stderr_buffer and not stderr_discard:
                            self._diagnose(
                                stderr_buffer.decode("utf-8", errors="replace")
                            )
                        continue
                    if streams[fd] == "stderr":
                        for part in raw.splitlines(keepends=True):
                            ends = part.endswith(b"\n")
                            if not stderr_discard:
                                stderr_buffer.extend(part)
                                if len(stderr_buffer) > 65536:
                                    stderr_buffer.clear()
                                    stderr_discard = True
                            if ends:
                                if stderr_discard:
                                    self._diagnose("[stderr line exceeded 64 KiB]")
                                else:
                                    self._diagnose(
                                        stderr_buffer.decode(
                                            "utf-8", errors="replace"
                                        ).rstrip("\r\n")
                                    )
                                stderr_buffer.clear()
                                stderr_discard = False
                        continue
                    buffer.extend(raw)
                    while b"\n" in buffer:
                        raw, _, remaining = buffer.partition(b"\n")
                        buffer = bytearray(remaining)
                        self._receive(decode(raw), len(raw))
                    if len(buffer) > MAX_FRAME:
                        raise ValueError("frame_limit")
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            with self.condition:
                self.error = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and str(exc)
                    in (
                        "stdout_eof",
                        "frame_limit",
                        "depth_limit",
                        "protocol_error",
                        "duplicate_json_key",
                        "json_number",
                    )
                    else "protocol_error"
                )
                self.condition.notify_all()

    def _receive(self, message, size):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("protocol_error")
        if "method" in message:
            if "result" in message or "error" in message:
                raise ValueError("protocol_error")
            if "params" in message and not isinstance(message["params"], (dict, list)):
                raise ValueError("protocol_error")
            if not isinstance(message["method"], str):
                raise ValueError("protocol_error")
            method = message["method"]
            if "id" not in message:
                if method == "notifications/tools/list_changed":
                    self.changed = True
                return
            identity = message["id"]
            if type(identity) not in (str, int):
                raise ValueError("protocol_error")
            reply = {"jsonrpc": "2.0", "id": identity}
            if method == "ping":
                reply["result"] = {}
            else:
                reply["error"] = {
                    "code": -32602 if method == "elicitation/create" else -32601,
                    "message": "Client capability not supported",
                }
            try:
                self.send(reply, time.monotonic() + 0.2)
            except TransportError:
                raise ValueError("protocol_error") from None
            return
        if type(message.get("id")) not in (str, int) or ("result" in message) == (
            "error" in message
        ):
            raise ValueError("protocol_error")
        if "error" in message:
            error = message["error"]
            if (
                not isinstance(error, dict)
                or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)
            ):
                raise ValueError("protocol_error")
        with self.condition:
            if (
                type(message["id"]) is int
                and message["id"] == self.pending
                and self.response is None
            ):
                self.response_bytes = size
                self.response = message
                self.condition.notify_all()
            else:
                self._diagnose("late_or_duplicate_response")
                self.condition.notify_all()

    def send(self, message, deadline, monotonic=time.monotonic):
        data = (
            json.dumps(
                message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        if len(data) - 1 > MAX_FRAME:
            raise TransportError("frame_limit")
        sent = 0
        remaining = deadline - monotonic()
        if remaining <= 0 or not self.write_lock.acquire(timeout=max(0, remaining)):
            raise TransportError("deadline")
        try:
            if self.stopping or self.process.stdin.closed:
                raise TransportError("connection_unavailable")
            fd = self.process.stdin.fileno()
            while sent < len(data):
                left = deadline - monotonic()
                if left <= 0:
                    raise TransportError("deadline", sent)
                _, ready, _ = select.select([], [fd], [], min(left, 0.05))
                if ready:
                    try:
                        sent += os.write(fd, data[sent:])
                    except BlockingIOError:
                        continue
            return sent
        except OSError:
            raise TransportError("pipe_write", sent) from None
        finally:
            self.write_lock.release()

    def notify(self, method, params, deadline, monotonic=time.monotonic):
        return self.send(
            {"jsonrpc": "2.0", "method": method, "params": params}, deadline, monotonic
        )

    def request(self, method, params, deadline, monotonic=time.monotonic):
        with self.condition:
            self.next_id += 1
            identity = self.next_id
            self.pending, self.response = identity, None
        sent = 0
        try:
            sent = self.send(
                {"jsonrpc": "2.0", "id": identity, "method": method, "params": params},
                deadline,
                monotonic,
            )
            with self.condition:
                while self.response is None:
                    if self.error:
                        raise TransportError(self.error, sent)
                    left = deadline - monotonic()
                    if left <= 0:
                        raise TransportError("deadline", sent)
                    self.condition.wait(min(left, 0.05))
                self.last_response_bytes = self.response_bytes
                return self.response
        except TransportError as exc:
            if method == "tools/call" and (sent or exc.sent):
                try:
                    self.notify(
                        "notifications/cancelled",
                        {"requestId": identity},
                        time.monotonic() + 0.1,
                    )
                except TransportError:
                    pass
            raise
        finally:
            with self.condition:
                self.pending, self.response = None, None

    def _group_alive(self):
        self.process.poll()
        try:
            os.killpg(self.process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def close(self, deadline=None):
        if self.closed:
            return True
        if self.process is None:
            self.closed = True
            return True
        if getattr(self.process, "pid", None) is None:
            if not self.spawn_failed or getattr(self.process, "_child_created", False):
                return False  # Native creation was interrupted, not disproved.
            for name in ("stdin", "stdout", "stderr"):
                pipe = getattr(self.process, name, None)
                if pipe is not None:
                    pipe.close()
            self.closed = True
            return True
        deadline = time.monotonic() + 6 if deadline is None else deadline
        self.stopping = True
        with self.write_lock:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None and self._group_alive():
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    pass
                except OSError:
                    return False
            end = min(deadline, time.monotonic() + 2)
            while self._group_alive() and time.monotonic() < end:
                # Wait on process exit rather than guessing an execution order.
                try:
                    self.process.wait(timeout=min(0.05, max(0, end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            if not self._group_alive():
                break
            if time.monotonic() >= deadline:
                return False
        if self._group_alive():
            return False
        if self.reader is not None and thread_start_effect_happened(self.reader):
            if not thread_exit_confirmed(
                self.reader, max(0, deadline - time.monotonic())
            ):
                return False
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        self.closed = True
        return True
