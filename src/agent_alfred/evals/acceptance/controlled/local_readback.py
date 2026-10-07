"""Independently capture fixed actual files, then reuse the product's pure readers.

No worker-returned path, SQL, evidence object or Host enters these reads. The
durable snapshot is an observation of runner-owned data, not an audit anchor.
"""

import hashlib
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from agent_alfred.clock import SystemClock
from agent_alfred.redact import Redactor
from agent_alfred.runtime.evidence import read_evidence
from agent_alfred.runtime.recording import RecordingStore
from agent_alfred.runtime.tool_history import ToolHistory
from agent_alfred.tools.metering import ToolMetering

from ..artifacts import tool_projections
from ..schema import digest, encode


class _SnapshotReader:
    def __init__(self, conn, observed_at):
        self.store = RecordingStore(conn, threading.Lock())
        self.redactor = Redactor(())
        self.metering = ToolMetering(self.store, SystemClock())
        self.history = ToolHistory(self.store, self.redactor)
        self.observed_at = observed_at

    def tool_requests(self, run_id):
        return self.metering.read(run_id)

    def read_tool_history(self, root, **query):
        return self.history.read(root, **query)

    def tool_verification(self, run_id, step_index, call_id):
        request = next(
            (
                row
                for row in self.tool_requests(run_id)
                if (row["step_index"], row["call_id"]) == (step_index, call_id)
            ),
            None,
        )
        if request is None:
            raise ValueError("local_tool_request_missing")
        operation = request["operation_id"]
        value = {
            "operation_id": operation,
            "observed_at": self.observed_at,
            "state": "unrecorded",
            "related_run": None,
        }
        if operation:
            with self.store.reading() as conn:
                row = conn.execute(
                    "SELECT state,verified_at,evidence_source,related_run "
                    "FROM tool_operation_verifications WHERE operation_id=?",
                    (operation,),
                ).fetchone()
            if row:
                value.update(
                    zip(
                        ("state", "verified_at", "evidence_source", "related_run"),
                        row,
                        strict=True,
                    )
                )
        return value


def _capture_trace(root, destination, run_id, inventory):
    from .local_runner import _read_file

    try:
        traces = os.open(
            "traces", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
        )
    except FileNotFoundError:
        return
    try:
        dates = sorted(os.listdir(traces))
        if len(dates) > 32:
            raise ValueError("local_trace_capacity")
        suffix = hashlib.sha256(run_id.encode()).hexdigest()[:32]
        for date in dates:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is None:
                raise ValueError("local_trace_path_invalid")
            day = os.open(
                date, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=traces
            )
            try:
                bundles = sorted(os.listdir(day))
                if len(bundles) > 128:
                    raise ValueError("local_trace_capacity")
                for name in bundles:
                    if re.fullmatch(r"\d{6}Z-" + suffix, name) is None:
                        continue
                    bundle = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=day
                    )
                    try:
                        target = destination / "traces" / date / name
                        target.mkdir(parents=True)
                        for file, limit in (
                            ("meta.json", 8192),
                            ("trace.jsonl", 32 * 1024 * 1024),
                        ):
                            content = _read_file(bundle, file, limit)
                            if content is None:
                                continue
                            if content != _read_file(bundle, file, limit):
                                raise ValueError("local_trace_changed")
                            (target / file).write_bytes(content)
                            inventory[str(Path("traces") / date / name / file)] = (
                                hashlib.sha256(content).hexdigest()
                            )
                    finally:
                        os.close(bundle)
            finally:
                os.close(day)
    finally:
        os.close(traces)


def capture_evidence(state, destination, *, run_id, business):
    """Preserve independent snapshot + actual Run/Attempt/tool projections."""
    from .local_runner import _read_file

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    observed = datetime.now(UTC).isoformat()
    root = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    inventory = {}
    try:
        for name in ("db.sqlite3", "db.sqlite3-wal"):
            content = _read_file(root, name, 32 * 1024 * 1024)
            if content is None:
                continue
            if content != _read_file(root, name, 32 * 1024 * 1024):
                raise ValueError("local_readback_changed")
            (destination / name).write_bytes(content)
            inventory[name] = hashlib.sha256(content).hexdigest()
        _capture_trace(root, destination, run_id, inventory)
    finally:
        os.close(root)
    conn = sqlite3.connect((destination / "db.sqlite3").as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    reader = _SnapshotReader(conn, observed)
    try:
        evidence = read_evidence(
            reader.store,
            reader.redactor,
            run_id,
            destination / "traces",
            computed_at=observed,
        )
        if evidence is None:
            raise ValueError("local_run_evidence_missing")
        tools = reader.tool_requests(run_id)
        if len(tools) > 128:
            raise ValueError("local_tool_capacity")
        evidence["tool_projections"] = tool_projections(reader, destination, run_id)
        evidence["tool_verifications_after_reopen"] = [
            reader.tool_verification(run_id, row["step_index"], row["call_id"])
            for row in tools
        ]
    finally:
        reader.history.close()
        conn.close()
    files = business["file_inventory"]
    evidence["local_artifacts"] = {
        "observed_at": observed,
        "status": "missing" if files["outbox"] is None else "available",
        "files": [
            {"path": path, **content}
            for path, content in (files["outbox"] or {}).items()
        ],
    }
    evidence["local_business"] = {
        "observed_at": observed,
        "status": "available",
        "scope": "independent_bounded_actual_files_and_sqlite_snapshot",
        "calendar": {
            "status": "available",
            "entries": [
                dict(
                    zip(
                        (
                            "id",
                            "title",
                            "starts_at",
                            "ends_at",
                            "iana_time_zone",
                            "participants",
                            "notes",
                        ),
                        row,
                        strict=True,
                    )
                )
                for row in business["calendar"]
            ],
        },
        "persona": {"status": "available", "files": files["persona"]},
        "file_operations": business["files"],
    }
    (destination / "inventory.json").write_bytes(encode(inventory))
    evidence["independent_readback"] = {
        "contract": "V1-LOCAL-ACTUAL-READBACK",
        "version": 1,
        "observed_at": observed,
        "inventory_sha256": digest(inventory),
        "source": "actual_runner_files",
        "worker_record_used": False,
        "limitations": ["runner_owned_telemetry_is_observed_not_independently_true"],
    }
    return evidence, tools
