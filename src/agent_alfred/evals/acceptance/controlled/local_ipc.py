"""Bounded anonymous-pipe frames, never network, pickle, paths or principals."""

import hashlib
import os
import re
import select
import stat
import struct
from pathlib import Path
from time import monotonic

from ..materials import strict_json
from ..schema import encode
from .contract import MAX_CONTROL_BYTES, exact

MAX_FRAME = MAX_CONTROL_BYTES
MAX_DATA_BYTES = 8 * 1024 * 1024


class LocalDeliveryUnknown(RuntimeError):
    possibly_sent = True

    def __init__(self):
        super().__init__("local_control_delivery_unknown")


class Frames:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    def _read(self, size, deadline):
        result = bytearray()
        while len(result) < size:
            remaining = deadline - monotonic()
            if (
                remaining <= 0
                or not select.select([self.reader.fileno()], [], [], remaining)[0]
            ):
                raise TimeoutError("local_frame_deadline")
            chunk = os.read(self.reader.fileno(), size - len(result))
            if not chunk:
                raise EOFError("local_frame_incomplete")
            result.extend(chunk)
        return bytes(result)

    def read(self, *, deadline):
        size = struct.unpack("!I", self._read(4, deadline))[0]
        if not 0 < size <= MAX_FRAME:
            raise ValueError("local_frame_size")
        return strict_json(self._read(size, deadline), limit=MAX_FRAME)

    def write(self, value, *, deadline):
        raw = encode(value)
        if not 0 < len(raw) <= MAX_FRAME:
            raise ValueError("local_frame_size")
        data = memoryview(struct.pack("!I", len(raw)) + raw)
        descriptor = self.writer.fileno()
        pipe_buf = os.fpathconf(descriptor, "PC_PIPE_BUF")
        if pipe_buf <= 0:
            raise OSError("local_pipe_buffer_unavailable")
        while data:
            remaining = deadline - monotonic()
            if (
                remaining <= 0
                or not select.select([], [descriptor], [], remaining)[1]
            ):
                raise TimeoutError("local_frame_deadline")
            # This channel has one writer. Readiness guarantees room for the
            # pipe's atomic write size, which is 512 on macOS, not 4096.
            written = os.write(descriptor, data[:pipe_buf])
            if written <= 0:
                raise EOFError("local_frame_incomplete")
            data = data[written:]


class CaseData:
    """Closed, content-addressed per-case data channel, separate from control.

    Neither peer supplies a path. A ref may address only its declared direction,
    current session and sequence, with independently checked size and SHA-256.
    These files confer no controller/material-store read capability.
    """

    def __init__(self, root, session, *, create=False):
        if (
            not isinstance(session, str)
            or re.fullmatch(r"[0-9a-f]{32}", session) is None
        ):
            raise ValueError("local_data_session_invalid")
        self.root, self.session = Path(root), session
        if create:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.mkdir(".alfred-ipc", 0o700, dir_fd=root_fd)
                parent = os.open(
                    ".alfred-ipc",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
                try:
                    os.mkdir(session, 0o700, dir_fd=parent)
                finally:
                    os.close(parent)
            finally:
                os.close(root_fd)

    def _directory(self):
        root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.open(
                ".alfred-ipc", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
            )
        finally:
            os.close(root)
        try:
            return os.open(
                self.session,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        finally:
            os.close(parent)

    def _name(self, ref, direction, sequence):
        exact(
            ref,
            "session direction sequence sha256 byte_length",
            "local_data_ref_invalid",
        )
        if (
            direction not in ("host", "worker")
            or type(sequence) is not int
            or not 0 <= sequence <= 129
            or ref["session"] != self.session
            or ref["direction"] != direction
            or type(ref["sequence"]) is not int
            or ref["sequence"] != sequence
            or type(ref["byte_length"]) is not int
            or not 0 < ref["byte_length"] <= MAX_DATA_BYTES
            or not isinstance(ref["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) is None
        ):
            raise ValueError("local_data_ref_invalid")
        return f"{direction}-{sequence}-{ref['sha256']}.json"

    def put(self, value, *, direction, sequence):
        raw = encode(value)
        ref = {
            "session": self.session,
            "direction": direction,
            "sequence": sequence,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_length": len(raw),
        }
        name = self._name(ref, direction, sequence)
        folder = self._directory()
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=folder,
            )
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise EOFError("local_data_write_incomplete")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(folder)
        return ref

    def read(self, ref, *, direction, sequence):
        name = self._name(ref, direction, sequence)
        folder = self._directory()
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder
            )
        finally:
            os.close(folder)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != ref["byte_length"]:
                raise ValueError("local_data_size_mismatch")
            raw = bytearray()
            while len(raw) < ref["byte_length"]:
                part = os.read(fd, min(65536, ref["byte_length"] - len(raw)))
                if not part:
                    raise EOFError("local_data_incomplete")
                raw.extend(part)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ) or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("local_data_digest_mismatch")
            return strict_json(bytes(raw), limit=MAX_DATA_BYTES)
        finally:
            os.close(fd)
