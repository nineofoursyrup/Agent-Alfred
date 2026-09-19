"""Descriptor-backed JSON strings; large event text never becomes one buffer."""

import codecs
import json
import os
import re
from dataclasses import dataclass

from agent_alfred.trace_export.errors import ExportError

BLOCK = 65536
SPECIAL = re.compile(rb'["\\\x00-\x1f]')


@dataclass
class StringSpan:
    fd: int
    start: int
    end: int
    check: object

    def chunks(self):
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending = ""
        position = self.start
        while position < self.end:
            self.check()
            raw = os.pread(self.fd, min(BLOCK, self.end - position), position)
            if not raw:
                raise ExportError("io_failed")
            position += len(raw)
            pending += decoder.decode(raw)
            cut = max(0, len(pending) - 32)
            # Do not split an escape or a UTF-16 surrogate pair.
            slash = pending.rfind("\\", max(0, cut - 12), cut)
            if slash >= 0:
                cut = slash
                while cut and pending[cut - 1] == "\\":
                    cut -= 1
            if cut:
                yield json.loads('"' + pending[:cut] + '"').encode("utf-8")
                pending = pending[cut:]
        pending += decoder.decode(b"", final=True)
        yield json.loads('"' + pending + '"').encode("utf-8")


class Reader:
    def __init__(self, fd, start, end, check):
        self.fd, self.pos, self.end, self.check = fd, start, end, check
        self.buffer = b""
        self.begin = start

    def peek(self):
        if self.pos >= self.end:
            return None
        if not self.begin <= self.pos < self.begin + len(self.buffer):
            self.check()
            self.begin = self.pos
            self.buffer = os.pread(self.fd, min(BLOCK, self.end - self.pos), self.pos)
            if not self.buffer:
                raise ExportError("io_failed")
        return self.buffer[self.pos - self.begin]

    def whitespace(self):
        while self.peek() in (32, 9, 10, 13):
            self.pos += 1

    def string(self):
        self.pos += 1
        start = self.pos
        while self.peek() is not None:
            offset = self.pos - self.begin
            match = SPECIAL.search(self.buffer, offset)
            if match is None:
                self.pos = self.begin + len(self.buffer)
                continue
            self.pos = self.begin + match.start()
            char = self.peek()
            if char == 34:
                span = StringSpan(self.fd, start, self.pos, self.check)
                self.pos += 1
                if span.end - span.start <= BLOCK:
                    return b"".join(span.chunks()).decode()
                # Validate the full string before any output, without retaining it.
                for _ in span.chunks():
                    pass
                return span
            if char == 92:
                self.pos += 1
                escaped = self.peek()
                if escaped is None:
                    break
                self.pos += 1
            else:
                raise ExportError("corrupt_trace")
        raise ExportError("corrupt_trace")

    def value(self, depth=0):
        if depth > 128:
            raise ExportError("unsupported_format")
        self.whitespace()
        char = self.peek()
        if char == 34:
            return self.string()
        if char in (123, 91):
            mapping = char == 123
            close = 125 if mapping else 93
            self.pos += 1
            value = {} if mapping else []
            self.whitespace()
            if self.peek() == close:
                self.pos += 1
                return value
            while True:
                self.whitespace()
                if mapping:
                    if self.peek() != 34:
                        raise ExportError("corrupt_trace")
                    key = self.string()
                    if not isinstance(key, str):
                        raise ExportError("unsupported_format")
                    if key in value:
                        raise ExportError("corrupt_trace")
                    self.whitespace()
                    if self.peek() != 58:
                        raise ExportError("corrupt_trace")
                    self.pos += 1
                    value[key] = self.value(depth + 1)
                else:
                    value.append(self.value(depth + 1))
                self.whitespace()
                char = self.peek()
                self.pos += 1
                if char == close:
                    return value
                if char != 44:
                    raise ExportError("corrupt_trace")
        raw = bytearray()
        while self.peek() not in (None, 32, 9, 10, 13, 44, 93, 125):
            raw.append(self.peek())
            self.pos += 1
            if len(raw) > 128:
                raise ExportError("corrupt_trace")
        return json.loads(
            raw,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ExportError("corrupt_trace")
            ),
        )

    def read(self):
        value = self.value()
        self.whitespace()
        if self.pos != self.end:
            raise ExportError("corrupt_trace")
        return value
