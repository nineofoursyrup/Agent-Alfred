"""Bounded snapshots of the trial's local outbox, never arbitrary model paths."""

import hashlib
import os
import stat
from contextlib import contextmanager
from datetime import UTC, datetime

from agent_alfred.resource_rollback import (
    OwnedDescriptor,
    ResumableRollback,
    raise_if_rollback_pending,
)


@contextmanager
def opened(path, flags, *, parent=None):
    rollback = ResumableRollback()
    try:
        descriptor = OwnedDescriptor.open(
            rollback, path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        yield descriptor.fd
    except BaseException as failure:
        rollback.raise_failure(failure)
    else:
        rollback.close()


def drafts(state):
    result = {"observed_at": datetime.now(UTC).isoformat(), "files": []}
    try:
        with opened(state, os.O_RDONLY | os.O_DIRECTORY) as root:
            try:
                with opened("outbox", os.O_RDONLY | os.O_DIRECTORY, parent=root) as box:
                    names = sorted(os.listdir(box))
                    if len(names) > 16:
                        raise ValueError("artifact_count_limit")
                    for name in names:
                        if not name.endswith(".md"):
                            continue
                        with opened(name, os.O_RDONLY, parent=box) as file:
                            before = os.fstat(file)
                            if not stat.S_ISREG(before.st_mode):
                                raise ValueError("artifact_not_regular")
                            content = os.read(file, 65537)
                            if len(content) > 65536:
                                raise ValueError("artifact_size_limit")
                            after = os.fstat(file)
                            if (
                                len(content) != before.st_size
                                or before.st_mtime_ns != after.st_mtime_ns
                                or before.st_size != after.st_size
                            ):
                                raise ValueError("artifact_changed_or_incomplete")
                        result["files"].append(
                            {
                                "path": "outbox/" + name,
                                "content": content.decode("utf-8"),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                        )
            except FileNotFoundError as failure:
                raise_if_rollback_pending(failure)
                return {**result, "status": "missing"}
    except (OSError, ValueError) as failure:
        raise_if_rollback_pending(failure)
        return {**result, "status": "unavailable"}
    return {**result, "status": "available"}


def seal_observation(state, record):
    """Keep the original observation durable before closing/recovering its Host."""
    import json

    from .schema import encode

    rollback = ResumableRollback()
    payload = encode(record)
    try:
        with opened(state, os.O_RDONLY | os.O_DIRECTORY) as directory:
            descriptor = OwnedDescriptor.open(
                rollback, "original-observation.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                dir_fd=directory,
            )
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor.fd, remaining)
                if written <= 0:
                    raise OSError("original_observation_write_incomplete")
                remaining = remaining[written:]
            os.fsync(descriptor.fd)
            rollback.close()
            os.fsync(directory)
    except BaseException as failure:
        rollback.raise_failure(failure)
    return {
        "path": "original-observation.json",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "record": json.loads(payload),
    }


@contextmanager
def local_services(state):
    """Reopen only this case's managed DB and use public business services."""
    import threading

    from agent_alfred.clock import SystemClock
    from agent_alfred.database import open_database
    from agent_alfred.managed_state import ManagedStateDirectory
    from agent_alfred.resource_rollback import ConstructionOwner
    from agent_alfred.runtime.recording import RecordingStore
    from agent_alfred.tools.calendar import CalendarTools
    from agent_alfred.tools.files import FileTools

    owner = ConstructionOwner()
    try:
        directory = ManagedStateDirectory.acquire(state, _rollback=owner.rollback)
        conn = open_database(directory, _rollback=owner.rollback)
        clock = SystemClock()
        store = RecordingStore(conn, threading.Lock())
        files = FileTools(store, directory, clock)
        owner.rollback.own(files)
        yield CalendarTools(store, clock), files, clock
    except BaseException as failure:
        owner.fail(failure)
    else:
        owner.rollback.close()


def local_business(state, settings, *, operation_ids=()):
    """Bounded readback, separate from the original call's result/verification."""
    import json
    import uuid

    from agent_alfred.tools import ToolContext, ToolSuccess
    from agent_alfred.tools.persona import PersonaTools

    observed = {
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "dedicated_case_state_public_business_reads",
    }
    try:
        with local_services(state) as (calendar, files, _):
            context = ToolContext(
                "acceptance-readback",
                None,
                uuid.uuid4().hex,
                "acceptance_fixture",
                float("inf"),
            )
            calendar_result = calendar.query({}, context)
            persona_result = PersonaTools(files, settings).read({}, context)
            if not all(
                isinstance(x, ToolSuccess) for x in (calendar_result, persona_result)
            ):
                raise ValueError("business_readback_failed")
            calendar_text = "".join(x.text for x in calendar_result.content)
            if len(calendar_text.encode()) > 524288:
                raise ValueError("business_readback_size_limit")
            entries = json.loads(calendar_text)
            if len(entries) > 128:
                raise ValueError("business_readback_count_limit")
            persona_text = "".join(x.text for x in persona_result.content)
            if len(persona_text.encode()) > 65536:
                raise ValueError("business_readback_size_limit")
            observed.update(
                calendar={"status": "available", "entries": entries},
                persona={"status": "available", **json.loads(persona_text)},
                file_operations=[files.get_operation(op) for op in operation_ids],
            )
    except (OSError, ValueError) as failure:
        raise_if_rollback_pending(failure)
        return {
            **observed,
            "status": "unavailable",
            "calendar": {"status": "unknown"},
            "persona": {"status": "unknown"},
        }
    return {**observed, "status": "available"}


def tool_projections(host, state, run_id):
    """Read actual persisted projections; missing projections remain unknown.

    ToolHistory validates trace identity/content on each read. We refuse an
    over-limit whole collection instead of selecting a convenient subset.
    A persisted model projection does not establish model transport delivery.
    """
    rows = host.tool_requests(run_id)
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "sampled_run_saved_tool_projections",
        "requests": [],
    }
    if len(rows) > 128:
        return {**result, "status": "unavailable", "reason": "tool_request_count_limit"}
    if any(row["tool_name"] in ("delete_memory", "delete_fact") for row in rows):
        return {
            **result,
            "status": "unavailable",
            "reason": "deletion_body_export_withheld",
        }
    size = 0
    for request in rows:
        row = {
            key: request[key]
            for key in (
                "tool_name",
                "effect",
                "run_id",
                "step_index",
                "call_id",
                "result",
                "start_confirmation",
                "operation_id",
            )
            if key in request
        }
        for projection in ("parameters", "model", "audit"):
            try:
                page = host.read_tool_history(
                    state / "traces",
                    run_id=run_id,
                    step_index=request["step_index"],
                    call_id=request["call_id"],
                    projection=projection,
                )
                if page["next_cursor"] is not None or page["start"] != 0:
                    raise ValueError("projection_size_limit")
                size += page["total_bytes"]
                if size > 1048576:
                    return {
                        **result,
                        "requests": [],
                        "status": "unavailable",
                        "reason": "projection_collection_size_limit",
                    }
                row[projection] = {
                    key: page[key]
                    for key in ("text", "start", "end", "total_bytes", "delivery")
                } | {"status": "available"}
            except (OSError, ValueError) as failure:
                raise_if_rollback_pending(failure)
                row[projection] = {
                    "status": "unknown",
                    "reason": "projection_unavailable_or_over_limit",
                }
        row["verification"] = host.tool_verification(
            run_id, request["step_index"], request["call_id"]
        )
        result["requests"].append(row)
    complete = all(
        row[p]["status"] == "available"
        for row in result["requests"]
        for p in ("parameters", "model", "audit")
    )
    return {**result, "status": "available" if complete else "partial"}
