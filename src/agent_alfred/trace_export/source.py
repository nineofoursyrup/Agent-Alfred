"""Whole-bundle descriptor reads. No DB lock is held during file processing."""

import hashlib
import os
import re
from datetime import datetime
from pathlib import PurePath

from agent_alfred.managed_state import ManagedStateDirectory
from agent_alfred.trace_export.errors import ExportError
from agent_alfred.trace_export.json_stream import Reader

LIMIT = 512 * 1024 * 1024
CHUNK = 64 * 1024


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Source:
    def __init__(self, root, run_id, resources, check):
        self.check = check
        self.files = {}
        self.directories = []
        self.total = 0
        self.resources = resources
        self.run_id = run_id
        digest = hashlib.sha256(run_id.encode()).hexdigest()[:32]
        paths = list(root.glob(f"????-??-??/??????Z-{digest}"))
        if not paths:
            raise ExportError("trace_missing")
        if len(paths) != 1:
            raise ExportError("unsafe_source")
        path = paths[0]
        base = ManagedStateDirectory.acquire(root, _rollback=resources)
        day = base.open_directory(PurePath(path.parent.name), _rollback=resources)
        self.bundle = day.open_directory(PurePath(path.name), _rollback=resources)
        self.anchors = (base, day, self.bundle)
        names = set(os.listdir(self.bundle.fd))
        if names - {"meta.json", "trace.jsonl", "artifacts"}:
            raise ExportError("unsupported_format")
        if not {"meta.json", "trace.jsonl"} <= names:
            raise ExportError("trace_missing")
        self.directories.append((self.bundle, names))
        meta = self.open(self.bundle, "meta.json", "meta.json")
        self.meta = Reader(meta.fd, 0, meta.stat().st_size, self.check).read()
        info = self.meta
        required = {
            "run_id",
            "run_storage_id",
            "run_dir_name",
            "created_at",
            "process_instance_id",
        }
        if not isinstance(info, dict) or not required <= info.keys():
            raise ExportError("unsupported_format")
        if any(k in info for k in ("version", "schema_version", "layout_version")):
            raise ExportError("unsupported_format")
        created = datetime.fromisoformat(info["created_at"].replace("Z", "+00:00"))
        if (
            info["run_id"] != run_id
            or info["run_storage_id"] != digest
            or info["run_dir_name"] != path.name
            or created.tzinfo is None
            or created.strftime("%Y-%m-%d") != path.parent.name
            or created.strftime("%H%M%S") + "Z-" + digest != path.name
            or not isinstance(info["process_instance_id"], str)
        ):
            raise ExportError("unsafe_source")
        self.trace = self.open(self.bundle, "trace.jsonl", "trace.jsonl")
        self.artifacts = {}
        if "artifacts" in names:
            directory = self.bundle.open_directory(
                PurePath("artifacts"), _rollback=resources
            )
            members = set(os.listdir(directory.fd))
            self.directories.append((directory, members))
            for name in sorted(members):
                if not re.fullmatch(r"tool-[1-9][0-9]*\.txt", name):
                    raise ExportError("unsupported_format")
                self.artifacts["artifacts/" + name] = self.open(
                    directory, name, "artifacts/" + name
                )
        self.verify()

    def open(self, directory, name, key):
        lease = directory.open_regular(
            PurePath(name), access="read", create=False, _rollback=self.resources
        )
        info = lease.stat()
        if info.st_nlink != 1:
            raise ExportError("unsafe_source")
        self.total += info.st_size
        if self.total > LIMIT:
            raise ExportError("source_limit")
        self.files[key] = (lease, identity(info))
        return lease

    def chunks(self, lease):
        size = lease.stat().st_size
        offset = 0
        while offset < size:
            self.check()
            raw = os.pread(lease.fd, min(CHUNK, size - offset), offset)
            if not raw:
                raise ExportError("io_failed")
            offset += len(raw)
            yield raw
        lease.verify_identity()

    def events(self):
        position = start = previous = 0
        for chunk in self.chunks(self.trace):
            search = 0
            while (newline := chunk.find(b"\n", search)) >= 0:
                end = position + newline
                event = Reader(self.trace.fd, start, end, self.check).read()
                if (
                    not isinstance(event, dict)
                    or event.get("run_id") != self.run_id
                    or event.get("process_instance_id")
                    != self.meta["process_instance_id"]
                    or type(event.get("seq")) is not int
                    or event["seq"] <= previous
                ):
                    raise ExportError("unsafe_source")
                previous = event["seq"]
                yield event
                start = end + 1
                search = newline + 1
            position += len(chunk)
        self.truncated = start < position

    def verify(self):
        self.check()
        self.verify_identity()

    def verify_identity(self):
        for lease in self.anchors:
            lease.verify_identity()
        for lease, names in self.directories:
            lease.verify_identity()
            if set(os.listdir(lease.fd)) != names:
                raise ExportError("source_changed")
        for lease, before in self.files.values():
            lease.verify_identity()
            if identity(lease.stat()) != before:
                raise ExportError("source_changed")
