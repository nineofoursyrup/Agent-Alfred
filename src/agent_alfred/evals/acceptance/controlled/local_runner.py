"""A sandbox Host and independent trusted Host meet at each model boundary.

Only native installation starts a real runner. The explicit synthetic entry uses
ordinary pipes and makes no operating-system isolation or paid-run claim.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
from time import monotonic
from uuid import uuid4

from agent_alfred.clock import SystemClock
from agent_alfred.connections import CredentialOverlay
from agent_alfred.messages import Message, TextBlock, ToolResultBlock
from agent_alfred.model import ModelCallInterrupted, ScriptedModel
from agent_alfred.resource_rollback import (
    ResumableRollback,
    RollbackSlot,
    dominant_error,
    reraise_failure,
    thread_start_effect_happened,
)
from agent_alfred.runtime.memory import InputEvidenceError

from ..runner import _run_case
from ..schema import digest, encode
from .contract import exact, wire_payload
from .local_ipc import CaseData, Frames, LocalDeliveryUnknown
from .local_projection import (
    case_input,
    result_from_wire,
    result_wire,
    runtime_inputs,
    validate_case_input,
)

PROTOCOL = "V1-LOCAL-RUNNER"
MAX_EXCHANGES = 128  # clocks + <=16 physical model attempts; never renews a Run


class _OneRun:
    def __init__(self, identity):
        self.identity, self.used = identity, False

    def __call__(self):
        if self.used:
            raise ValueError("local_case_run_already_started")
        self.used = True
        return self.identity


class _PromptClock(SystemClock):
    """Only displayed prompt time is shared; every deadline remains real time."""

    def __init__(self, observe):
        self.observe = observe

    def local_now(self):
        return datetime.fromisoformat(self.observe())


class _WorkerBridge:
    def __init__(self, frames, session, deadline, data):
        self.frames, self.session, self.deadline = frames, session, deadline
        self.data = data
        self.sequence = 0
        self.lock = threading.Lock()
        self.failure = None

    def exchange(self, kind, payload=None, request=None, deadline=None):
        with self.lock:
            if self.failure is not None:
                raise self.failure
            try:
                return self._exchange(kind, payload, request, deadline)
            except ModelCallInterrupted:
                # A validated model interruption retains its exact Attempt;
                # it is not malformed transport or missing response evidence.
                raise
            except Exception as failure:
                # The gate may degrade an ordinary model failure. A corrupt
                # channel cannot establish the evidence needed for another
                # dispatch, so use the existing fatal input-evidence boundary.
                self.failure = InputEvidenceError("local_protocol_evidence_unavailable")
                raise self.failure from failure
            except BaseException as failure:
                self.failure = failure
                raise

    def _exchange(self, kind, payload, request, deadline):
        self.sequence += 1
        if self.sequence > MAX_EXCHANGES:
            raise ValueError("local_exchange_limit")
        limit = min(self.deadline, deadline or self.deadline)
        value = {
            "session": self.session,
            "sequence": self.sequence,
            "kind": kind,
            "payload": payload,
        }
        self.frames.write(value, deadline=limit)
        notified = None
        while True:
            result = self.frames.read(deadline=limit)
            exact(result, "session sequence kind payload", "local_reply_invalid")
            if (result["session"], result["sequence"]) != (
                self.session,
                self.sequence,
            ):
                raise ValueError("local_reply_scope_mismatch")
            if result["kind"] == "attempt":
                if request is None or notified is not None:
                    raise ValueError("local_attempt_unexpected")
                notified = result["payload"]
                try:
                    request.notify_attempt_started(notified, deadline)
                except BaseException:
                    self.frames.write(
                        {
                            **value,
                            "kind": "ack",
                            "payload": {
                                "attempt_id": notified,
                                "ok": False,
                            },
                        },
                        deadline=limit,
                    )
                    raise
                self.frames.write(
                    {
                        **value,
                        "kind": "ack",
                        "payload": {
                            "attempt_id": notified,
                            "ok": True,
                        },
                    },
                    deadline=limit,
                )
                continue
            if result["kind"] == "error":
                raise LocalDeliveryUnknown()
            if result["kind"] == "clock" and kind == "clock":
                return result["payload"]
            if result["kind"] not in ("result", "interrupted") or kind != "request":
                raise ValueError("local_reply_kind_mismatch")
            model = result_from_wire(
                self.data.read(
                    result["payload"],
                    direction="host",
                    sequence=self.sequence,
                )
            )
            if [a.attempt_id for a in model.attempts] != [notified]:
                raise ValueError("local_attempt_receipt_mismatch")
            if result["kind"] == "interrupted":
                raise ModelCallInterrupted(LocalDeliveryUnknown(), model)
            return model

    def local_now(self):
        return self.exchange("clock")

    def create(self, snapshot):
        if (snapshot.endpoint_id, snapshot.model_id) != ("deepseek", "deepseek-flash"):
            raise ValueError("local_model_mismatch")
        return self

    def respond(self, request, *, events=None, deadline=None):
        del events
        request = replace(request, thinking="disabled")
        wire = wire_payload(request)
        return self.exchange(
            "request",
            {
                "sha256": digest(wire),
                "byte_length": len(encode(wire)),
            },
            request,
            deadline,
        )

    def close(self):
        pass


class _ShadowBridge:
    def __init__(self, deadline):
        self.events, self.stop = Queue(), threading.Event()
        self.deadline = deadline

    def ask(self, kind, payload=None, deadline=None):
        answer = Queue(maxsize=1)
        self.events.put((kind, payload, answer, deadline))
        limit = min(self.deadline, deadline or self.deadline)
        while not self.stop.is_set():
            left = limit - monotonic()
            if left <= 0:
                raise TimeoutError("local_shadow_deadline")
            try:
                value = answer.get(timeout=min(left, 0.1))
            except Empty:
                continue
            if isinstance(value, BaseException):
                raise value
            return value
        raise ValueError("local_shadow_stopped")

    def local_now(self):
        return self.ask("clock")

    def create(self, snapshot):
        if (snapshot.endpoint_id, snapshot.model_id) != ("deepseek", "deepseek-flash"):
            raise ValueError("local_model_mismatch")
        return self

    def respond(self, request, *, events=None, deadline=None):
        del events
        return self.ask("request", replace(request, thinking="disabled"), deadline)

    def close(self):
        pass


class _PairCleanup:
    """Retain child and shadow/Host cleanup if any release is interrupted."""

    def __init__(self, process, shadow, final):
        self.process, self.shadow, self.final = process, shadow, final
        self.thread = None
        self.nested = RollbackSlot()

    def close(self):
        self.shadow.stop.set()
        for pipe in (self.process.stdin, self.process.stdout):
            pipe.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.thread is not None and thread_start_effect_happened(self.thread):
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                return False
        while True:
            try:
                result = self.final.get_nowait()
            except Empty:
                break
            if isinstance(result, BaseException):
                self.nested.capture_failure(result)
        return self.nested.retry_propagating()


def worker_main(*, initial_frame=None, frames=None):
    """Native fixed entry: no command arguments, imports, paths or source factories."""
    frames = frames or Frames(sys.stdin.buffer, sys.stdout.buffer)
    start = (
        initial_frame
        if initial_frame is not None
        else frames.read(deadline=monotonic() + 30)
    )
    exact(start, "contract version session initial")
    if start["contract"] != PROTOCOL or start["version"] != 1:
        raise ValueError("local_protocol_invalid")
    data = CaseData(Path.cwd(), start["session"])
    initial = data.read(start["initial"], direction="host", sequence=0)
    exact(initial, "input prepared run_id synthetic")
    if type(initial["synthetic"]) is not bool:
        raise ValueError("local_mode_invalid")
    dto = initial["input"]
    validate_case_input(dto)
    batch, case = runtime_inputs(dto)
    bridge = _WorkerBridge(frames, start["session"], monotonic() + 900, data)
    try:
        record = _run_case(
            batch,
            case,
            Path.cwd(),
            factory=bridge,
            credentials=CredentialOverlay({}, None),
            capture_all=True,
            synthetic_replay=initial["synthetic"],
            _prepared=initial["prepared"],
            _run_id_factory=_OneRun(initial["run_id"]),
            _host_clock=_PromptClock(bridge.local_now),
        )
        frames.write(
            {
                "session": bridge.session,
                "sequence": bridge.sequence + 1,
                "kind": "done",
                "payload": data.put(
                    record, direction="worker", sequence=bridge.sequence + 1
                ),
            },
            deadline=bridge.deadline,
        )
    except BaseException as failure:
        # No arbitrary exception text, ambient data or Python traceback enters IPC.
        try:
            frames.write(
                {
                    "session": bridge.session,
                    "sequence": bridge.sequence + 1,
                    "kind": "failed",
                    "payload": type(failure).__name__,
                },
                deadline=min(bridge.deadline, monotonic() + 2),
            )
        except Exception:
            pass
        raise


def _read_file(directory, name, limit):
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("local_readback_file_invalid")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(raw) != before.st_size:
            raise ValueError("local_readback_changed")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _receipt(raw, state, target, operation):
    if raw is None:
        return None
    value = json.loads(raw)
    if type(value) is not dict or value.get("operation_id") != operation:
        raise ValueError("local_tool_receipt_mismatch")
    if "path" in value:
        if value["path"] != str(state / target):
            raise ValueError("local_tool_receipt_path_mismatch")
        value["path"] = target
    return value


def _flat_files(root, name):
    """Observe all entries, including files not declared by a tool receipt."""
    try:
        folder = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
        )
    except FileNotFoundError:
        return None
    try:
        names = sorted(os.listdir(folder))
        if len(names) > 128:
            raise ValueError("local_readback_capacity")
        result = {}
        for item in names:
            content = _read_file(folder, item, 1024 * 1024)
            if content is None:
                raise ValueError("local_readback_changed")
            result[name + "/" + item] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "content": content.decode(),
            }
        if names != sorted(os.listdir(folder)):
            raise ValueError("local_readback_changed")
        return result
    finally:
        os.close(folder)


def business_readback(state, *, run_id, scratch):
    """Read fixed files/SQL through bounded copies; never initialize actual state."""
    state = Path(state)
    root = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = os.fstat(root)
        names = ("db.sqlite3", "db.sqlite3-wal")
        originals = {name: _read_file(root, name, 32 * 1024 * 1024) for name in names}
        if originals["db.sqlite3"] is None:
            raise ValueError("local_readback_database_missing")
        # Fixed descriptors plus stable complete db/WAL reads; a concurrent writer
        # is a blocking observation, never a truncated success.
        if originals != {
            name: _read_file(root, name, 32 * 1024 * 1024) for name in names
        }:
            raise ValueError("local_readback_changed")
        with tempfile.TemporaryDirectory(prefix="readback-", dir=scratch) as target:
            for name, content in originals.items():
                if content is not None:
                    (Path(target) / name).write_bytes(content)
            uri = (Path(target) / "db.sqlite3").as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                queries = {
                    "calendar": (
                        "SELECT id,title,starts_at,ends_at,iana_time_zone,"
                        "participants,notes FROM calendar_entries ORDER BY id"
                    ),
                    "local_operations": (
                        "SELECT operation_id,fingerprint,receipt "
                        "FROM local_tool_operations ORDER BY operation_id"
                    ),
                    "tools": (
                        "SELECT tool_name,fingerprint,effect,status,call_id,run_id,"
                        "session_id,summary FROM tool_ledger ORDER BY id"
                    ),
                    "files": (
                        "SELECT operation_id,tool_name,target,content,expected_digest,"
                        "state,call_id,run_id,session_id,receipt,request_digest,"
                        "original_content FROM file_operations ORDER BY rowid"
                    ),
                    "facts": "SELECT * FROM facts ORDER BY id",
                    "episodes": "SELECT * FROM episodes ORDER BY id",
                }
                snapshot = {}
                for key, query in queries.items():
                    rows = conn.execute(query).fetchmany(1025)
                    if len(rows) > 1024:
                        raise ValueError("local_readback_capacity")
                    snapshot[key] = [list(row) for row in rows]
                terminal = conn.execute(
                    "SELECT phase,outcome,accepted_at,finished_at FROM runs "
                    "WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                snapshot["run"] = list(terminal) if terminal else None
                snapshot["messages"] = [
                    list(row)
                    for row in conn.execute(
                        "SELECT role,content FROM agent_log WHERE run_id=? ORDER BY id",
                        (run_id,),
                    ).fetchall()
                ]
                snapshot["seed_messages"] = [
                    list(row)
                    for row in conn.execute(
                        "SELECT run_id,session_id,role,content FROM agent_log "
                        "WHERE run_id IS NULL OR run_id<>? ORDER BY id",
                        (run_id,),
                    ).fetchall()
                ]
            finally:
                conn.close()
        files = {}
        latest = {row[2]: row[0] for row in snapshot["files"]}
        for row in snapshot["files"]:
            operation, tool, relative = row[:3]
            path = PurePosixPath(relative)
            if (
                tool not in ("draft_message", "update_persona")
                or path.is_absolute()
                or ".." in path.parts
                or (
                    tool == "draft_message"
                    and relative != "outbox/" + operation + ".md"
                )
                or (tool == "update_persona" and relative != "persona/persona.md")
            ):
                raise ValueError("local_tool_target_invalid")
            folder = os.open(
                path.parent.as_posix(),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root,
            )
            try:
                content = _read_file(folder, path.name, 1024 * 1024)
            finally:
                os.close(folder)
            files[relative] = (
                None
                if content is None
                else {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content": content.decode(),
                }
            )
            row[9] = _receipt(row[9], state, relative, operation)
            if row[5] == "complete" and latest[relative] == operation:
                fingerprints = [
                    tool_row[1]
                    for tool_row in snapshot["tools"]
                    if tool_row[0] == tool and tool_row[4:6] == row[6:8]
                ]
                if (
                    content is None
                    or hashlib.sha256(content).hexdigest() not in fingerprints
                ):
                    raise ValueError("local_tool_file_readback_mismatch")
        snapshot["file_contents"] = files
        snapshot["file_inventory"] = {
            name: _flat_files(root, name) for name in ("persona", "outbox")
        }
        current = state.lstat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError("local_readback_root_replaced")
        return snapshot
    finally:
        os.close(root)


def _business_equal(actual, shadow, *, terminal=False):
    keys = (
        "calendar",
        "local_operations",
        "tools",
        "files",
        "file_contents",
        "file_inventory",
        "facts",
        "episodes",
        "seed_messages",
    )
    if any(actual[key] != shadow[key] for key in keys):
        raise ValueError("local_business_projection_mismatch")
    if terminal and (
        actual["run"] is None
        or shadow["run"] is None
        or actual["run"][:2] != shadow["run"][:2]
    ):
        raise ValueError("local_terminal_projection_mismatch")


def _canonical_request(request, actual, shadow, actual_state, shadow_state):
    """Map only independently verified file-tool receipt paths, never plain text."""
    _business_equal(actual, shadow)
    records = {row[0]: row for row in shadow["files"]}
    changed, mappings = [], []
    for message in request.messages:
        blocks = []
        for block in message.blocks:
            if not isinstance(block, ToolResultBlock):
                blocks.append(block)
                continue
            parts = []
            for part in block.content:
                text = part.text
                try:
                    value = json.loads(text)
                except ValueError, TypeError:
                    value = None
                if (
                    isinstance(value, dict)
                    and "operation_id" in value
                    and "path" in value
                ):
                    row = records.get(value["operation_id"])
                    if row is None or row[6] != block.call_id:
                        raise ValueError("local_tool_projection_unbound")
                    expected = {**row[9], "path": str(shadow_state / row[2])}
                    if value != expected:
                        raise ValueError("local_tool_projection_mismatch")
                    value["path"] = str(actual_state / row[2])
                    mappings.append(
                        {
                            "operation_id": row[0],
                            "target": row[2],
                            "source_sha256": digest(expected),
                            "target_sha256": digest(value),
                        }
                    )
                    text = json.dumps(value, ensure_ascii=False)
                parts.append(TextBlock(text))
            blocks.append(ToolResultBlock(block.call_id, tuple(parts), block.is_error))
        changed.append(Message(message.role, tuple(blocks)))
    return replace(request, messages=tuple(changed)), mappings


def _terminal_messages(snapshot, original_state, target_state):
    """Only the Host's final structured system-receipt block changes roots."""
    records = {row[0]: row for row in snapshot["files"]}
    result = []
    for role, raw in snapshot["messages"]:
        blocks = json.loads(raw)
        if role == "assistant" and blocks:
            block = blocks[-1]
            prefix = "\n\n系统操作回执：\n"
            if block.get("type") == "text" and block["text"].startswith(prefix):
                lines = []
                for line in block["text"][len(prefix) :].splitlines():
                    value = json.loads(line)
                    if "path" not in value:
                        lines.append(line)
                        continue
                    row = records.get(value.get("operation_id"))
                    if row is None or value != {
                        **row[9],
                        "path": str(original_state / row[2]),
                    }:
                        raise ValueError("local_terminal_receipt_unbound")
                    value["path"] = str(target_state / row[2])
                    lines.append(json.dumps(value, ensure_ascii=False))
                blocks[-1] = {"type": "text", "text": prefix + "\n".join(lines)}
        result.append([role, blocks])
    return result


def _copy_seed(seed, destination, *, case_id, prepared):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("local_case_destination_not_empty")
    for path in Path(seed).rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("local_seed_symlink")
    for path in Path(seed).iterdir():
        shutil.copytree(path, destination / path.name, symlinks=False)
    # Seed-only local publication receipts name their newly prepared copy.
    # This is not a sampled Run, and no historical evidence store is modified.
    current = deepcopy(prepared)
    conn = sqlite3.connect(destination / case_id / "db.sqlite3")
    try:
        for operation, target, raw in conn.execute(
            "SELECT operation_id,target,receipt FROM file_operations "
            "WHERE receipt IS NOT NULL"
        ).fetchall():
            value = _receipt(raw, Path(seed) / case_id, target, operation)
            value["path"] = str(destination / case_id / target)
            relocated = json.dumps(value, ensure_ascii=False)
            conn.execute(
                "UPDATE file_operations SET receipt=? WHERE operation_id=?",
                (relocated, operation),
            )
            if (
                current["persona_receipt"]
                and current["persona_receipt"]["operation_id"] == operation
            ):
                current["persona_receipt"]["receipt"] = relocated
        conn.commit()
    finally:
        conn.close()
    return current


def _seal(workspace, name, value):
    raw = encode(value)
    with (workspace / name).open("xb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(raw).hexdigest()


def _verified_tool_rows(actual, expected, *, started, finished):
    # Every business field remains exact. Only independent wall-clock samples
    # differ, and their actual values are retained and bounded, never replaced.
    keys = (
        "run_id",
        "step_index",
        "call_id",
        "ordinal",
        "tool_name",
        "source_id",
        "capability_id",
        "effect",
        "start_confirmation",
        "result",
        "reason",
        "cost",
        "operation_id",
        "model_delivery",
    )
    if [{key: row[key] for key in keys} for row in actual] != [
        {key: row[key] for key in keys} for row in expected
    ]:
        raise ValueError("local_tool_metering_mismatch")
    for row in actual:
        for key in ("requested_at", "finished_at"):
            value = row[key]
            if value is not None and not (
                started.replace(microsecond=0)
                <= datetime.fromisoformat(value)
                <= finished
            ):
                raise ValueError("local_tool_timestamp_unverifiable")


def _run_pair(dto, workspace, *, actual_root, launch, sender, synthetic):
    validate_case_input(dto)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    seed, shadow_root = workspace / "seed", workspace / "shadow"
    batch, case = runtime_inputs(dto)
    prepared = _run_case(
        batch,
        case,
        seed,
        factory=object(),
        _prepare_only=True,
        synthetic_replay=True,
        credentials=CredentialOverlay({}, None),
    )
    shadow_prepared = _copy_seed(
        seed, shadow_root, case_id=case["id"], prepared=prepared
    )
    actual_prepared = _copy_seed(
        seed, actual_root, case_id=case["id"], prepared=prepared
    )
    started = datetime.now(UTC)
    deadline = monotonic() + 900
    run_id, session = uuid4().hex, uuid4().hex
    data = CaseData(actual_root, session, create=True)
    initial = data.put(
        {
            "input": dto,
            "prepared": actual_prepared,
            "run_id": run_id,
            "synthetic": synthetic,
        },
        direction="host",
        sequence=0,
    )
    shadow = _ShadowBridge(deadline)
    process = launch()
    frames = Frames(process.stdout, process.stdin)
    final = Queue(maxsize=1)
    cleanup = ResumableRollback()
    pair_cleanup = _PairCleanup(process, shadow, final)
    cleanup.own(pair_cleanup)
    trace, receipts, attempt_observations = [], {}, []

    def run_shadow():
        try:
            record = _run_case(
                batch,
                case,
                shadow_root,
                factory=shadow,
                credentials=CredentialOverlay({}, None),
                capture_all=True,
                synthetic_replay=True,
                _prepared=shadow_prepared,
                _run_id_factory=_OneRun(run_id),
                _host_clock=_PromptClock(shadow.local_now),
            )
            final.put(record)
            shadow.events.put(("done", record, None, None))
        except BaseException as failure:
            final.put(failure)
            shadow.events.put(("failed", type(failure).__name__, None, None))

    thread = threading.Thread(target=run_shadow, daemon=True, name="local-projection")
    pair_cleanup.thread = thread
    last = 0
    pending_answer = None
    try:
        thread.start()
        frames.write(
            {
                "contract": PROTOCOL,
                "version": 1,
                "session": session,
                "initial": initial,
            },
            deadline=deadline,
        )
        while True:
            observed = frames.read(deadline=deadline)
            exact(observed, "session sequence kind payload", "local_request_invalid")
            sequence = observed["sequence"]
            if observed["session"] != session or type(sequence) is not int:
                raise ValueError("local_request_scope_mismatch")
            if sequence in receipts:
                old, reply = receipts[sequence]
                if old != digest(observed):
                    raise ValueError("local_sequence_reused")
                frames.write(reply, deadline=deadline)
                continue
            if sequence != last + 1 or sequence > MAX_EXCHANGES:
                raise ValueError("local_sequence_invalid")
            kind, payload = observed["kind"], observed["payload"]
            expected, trusted, pending_answer, model_deadline = shadow.events.get(
                timeout=max(0.001, deadline - monotonic())
            )
            if kind != expected:
                raise ValueError("local_host_sequence_mismatch")
            if kind == "failed":
                failure = final.get_nowait()
                if isinstance(failure, BaseException):
                    raise failure
                raise ValueError("local_host_failed")
            if kind == "done":
                record = data.read(payload, direction="worker", sequence=sequence)
                trusted_record = trusted
                raw_observation_sha256 = _seal(
                    workspace,
                    "first-worker-record.untrusted.json",
                    {"trusted": False, "run_id": run_id, "record": record},
                )
                break
            reply = {
                "session": session,
                "sequence": sequence,
                "kind": kind,
                "payload": None,
            }
            if kind == "clock":
                if payload is not None:
                    raise ValueError("local_clock_request_invalid")
                reply["payload"] = SystemClock().local_now().isoformat()
                pending_answer.put(reply["payload"])
            elif kind == "request":
                actual_state = Path(actual_root) / case["id"]
                shadow_state = shadow_root / case["id"]
                actual = business_readback(
                    actual_state, run_id=run_id, scratch=workspace
                )
                mirror = business_readback(
                    shadow_state, run_id=run_id, scratch=workspace
                )
                request, mappings = _canonical_request(
                    trusted,
                    actual,
                    mirror,
                    actual_state,
                    shadow_state,
                )
                wire = wire_payload(request)
                expected_wire = {
                    "sha256": digest(wire),
                    "byte_length": len(encode(wire)),
                }
                if payload != expected_wire:
                    with (workspace / f"projection-mismatch-{sequence}.json").open(
                        "xb"
                    ) as saved:
                        saved.write(
                            encode(
                                {
                                    "observed": payload,
                                    "expected": expected_wire,
                                    "mappings": mappings,
                                }
                            )
                        )
                    raise ValueError("local_full_projection_mismatch")
                effective = min(deadline, model_deadline or deadline, monotonic() + 120)
                notified = []

                def notify(attempt_id, attempt_deadline=None):
                    if notified:
                        raise ValueError("local_multiple_attempts_for_projection")
                    notified.append(attempt_id)
                    observation = {
                        "sequence": sequence,
                        "attempt_id": attempt_id,
                        "payload_sha256": expected_wire["sha256"],
                        "state": "preflight_observed_not_dispatch_proof",
                    }
                    _seal(workspace, f"first-attempt-{sequence}.json", observation)
                    attempt_observations.append(observation)
                    trusted.notify_attempt_started(attempt_id, attempt_deadline)
                    frames.write(
                        {**reply, "kind": "attempt", "payload": attempt_id},
                        deadline=effective,
                    )
                    ack = frames.read(deadline=effective)
                    if ack != {
                        **observed,
                        "kind": "ack",
                        "payload": {
                            "attempt_id": attempt_id,
                            "ok": True,
                        },
                    }:
                        raise ValueError("local_attempt_preflight_failed")

                request = replace(
                    request, on_attempt_started=None, on_attempt_preflight=notify
                )
                interruption = None
                try:
                    result = sender.respond(request, deadline=effective)
                except ModelCallInterrupted as failure:
                    result, interruption = failure.result, failure
                if [a.attempt_id for a in result.attempts] != notified or len(
                    notified
                ) != 1:
                    raise ValueError("local_attempt_receipt_mismatch")
                response_wire = result_wire(result)
                _seal(workspace, f"first-model-response-{sequence}.json", response_wire)
                reply.update(
                    kind="interrupted" if interruption else "result",
                    payload=data.put(
                        response_wire, direction="host", sequence=sequence
                    ),
                )
                # Both Hosts consume the very same wire-decoded response. JSON
                # canonicalization also fixes tool-argument mapping order; the
                # authority retains the untouched provider response separately.
                delivered = result_from_wire(json.loads(encode(response_wire)))
                pending_answer.put(
                    ModelCallInterrupted(interruption.cause, delivered)
                    if interruption
                    else delivered
                )
                trace.append(
                    {
                        "sequence": sequence,
                        "payload_sha256": expected_wire["sha256"],
                        "business_sha256": digest(actual),
                        "receipt_mappings": mappings,
                        "attempt_ids": notified,
                        "result_sha256": digest(response_wire),
                    }
                )
            else:
                raise ValueError("local_operation_not_allowed")
            receipts[sequence] = (digest(observed), deepcopy(reply))
            last = sequence
            frames.write(reply, deadline=deadline)
            pending_answer = None
        if process.wait(timeout=max(0.001, deadline - monotonic())) != 0:
            raise ValueError("local_runner_exit_failed")
        thread.join(timeout=max(0.001, deadline - monotonic()))
        if thread.is_alive():
            raise ValueError("local_shadow_not_closed")
        actual = business_readback(
            Path(actual_root) / case["id"], run_id=run_id, scratch=workspace
        )
        mirror = business_readback(
            shadow_root / case["id"], run_id=run_id, scratch=workspace
        )
        _business_equal(actual, mirror, terminal=True)
        actual_messages = _terminal_messages(
            actual, Path(actual_root) / case["id"], Path(actual_root) / case["id"]
        )
        expected_messages = _terminal_messages(
            mirror, shadow_root / case["id"], Path(actual_root) / case["id"]
        )
        if actual_messages != expected_messages:
            raise ValueError("local_terminal_projection_mismatch")
        saved_output = next(
            (
                "".join(block["text"] for block in blocks if block["type"] == "text")
                for role, blocks in actual_messages
                if role == "assistant"
            ),
            None,
        )
        if (
            any(
                record.get(key) != trusted_record.get(key)
                for key in (
                    "case_id",
                    "profile_id",
                    "run_id",
                    "outcome",
                    "error",
                    "recorded",
                )
            )
            or record.get("run_id") != run_id
        ):
            raise ValueError("local_result_projection_mismatch")
        if record["output"] != (
            saved_output if record["recorded"] else trusted_record["output"]
        ):
            raise ValueError("local_output_readback_mismatch")
        if record["outcome"] != actual["run"][1] or record["recorded"] != any(
            row[0] == "assistant" for row in actual["messages"]
        ):
            raise ValueError("local_result_readback_mismatch")
        sampled = datetime.fromisoformat(actual["run"][2])
        # SQLite uses second precision; this is an actual observed acceptance time.
        if not started.replace(microsecond=0) <= sampled <= datetime.now(UTC):
            raise ValueError("local_sample_time_unverifiable")
        from .local_readback import capture_evidence

        evidence, tool_rows = capture_evidence(
            Path(actual_root) / case["id"],
            workspace / "actual-observed",
            run_id=run_id,
            business=actual,
        )
        _verified_tool_rows(
            tool_rows,
            trusted_record["tools"],
            started=started,
            finished=datetime.now(UTC),
        )
        after_read = business_readback(
            Path(actual_root) / case["id"], run_id=run_id, scratch=workspace
        )
        if actual != after_read:
            raise ValueError("local_final_readback_changed")
        evidence.update(
            outcome=actual["run"][1],
            setup={
                "status": "verified",
                "source": "declared_local_seed",
                "declared": deepcopy(case["setup"]),
                "initial_business": deepcopy(prepared["before_business"]),
                "session_seed": deepcopy(prepared["session_rows"]),
                "seed_copied_once": True,
                "independent_business_comparison": "matched",
            },
            captured_inputs=[
                {"attempt_id": attempt, "host_request_sha256": row["payload_sha256"]}
                for row in trace
                for attempt in row["attempt_ids"]
            ],
        )
        evidence["local_bridge"] = {
            "contract": PROTOCOL,
            "version": 1,
            "mode": "SYNTHETIC_PIPE_ONLY" if synthetic else "LOCAL_INSTALLED_RUNNER",
            "real_sample": not synthetic,
            "projection": trace,
            "attempt_observations": attempt_observations,
            "independent_business_readback": actual,
            "shadow_business_sha256": digest(mirror),
            "shadow_is_product_sample": False,
            "worker_record_trusted": False,
            "raw_worker_observation_sha256": raw_observation_sha256,
        }
        evidence["execution_provenance"] = {
            "mode": "SYNTHETIC_PIPE_ONLY" if synthetic else "LOCAL_INSTALLED_RUNNER",
            "actual_sampled_at": None if synthetic else sampled.isoformat(),
            "real_sample": not synthetic,
            "seed_calls": "declared_setup_only_no_product_semantic_work",
        }
        # No other worker-controlled record or shadow evidence enters grading.
        verified_result = {
            "id": uuid4().hex,
            "batch_id": batch["batch_id"],
            "case_id": case["id"],
            "profile_id": dto["profile"]["id"],
            "source": "simulation" if synthetic else "online",
            "sampled_at": None if synthetic else sampled.isoformat(),
            "finished_at": actual["run"][3],
            "run_id": run_id,
            "outcome": actual["run"][1],
            "output": saved_output,
            "error": trusted_record["error"],
            "recorded": record["recorded"],
            "evidence": evidence,
            "tools": tool_rows,
            "persisted_messages": len(actual["messages"])
            + len(actual["seed_messages"]),
        }
    except BaseException as failure:
        failed = {
            "run_id": run_id,
            "session": session,
            "type": type(failure).__name__,
            "reason": str(failure),
            "projection": trace,
            "attempt_observations": attempt_observations,
            "first_result_only": True,
            "automatic_retry_permitted": False,
        }
        try:
            failed["actual_business_observation"] = business_readback(
                Path(actual_root) / case["id"],
                run_id=run_id,
                scratch=workspace,
            )
        except BaseException as read_failure:
            failed["readback_failure"] = type(read_failure).__name__
        try:
            _seal(workspace, "first-bridge-failure.json", failed)
        except BaseException as seal_failure:
            failure.add_note("local_failure_seal_failed:" + type(seal_failure).__name__)
        pair_cleanup.nested.capture_failure(failure)
        cleanup.raise_failure(failure)
    else:
        cleanup.close()
        return verified_result


def _synthetic_launch(root):
    import agent_alfred

    source = str(Path(agent_alfred.__file__).resolve().parent.parent)
    script = (
        "import runpy,sys;sys.path.insert(0,"
        + repr(source)
        + ");runpy.run_module("
        + repr(__name__)
        + ",run_name='__main__')"
    )
    return subprocess.Popen(
        [sys.executable, "-s", "-c", script],
        cwd=root,
        env={"PYTHONNOUSERSITE": "1"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def run_synthetic_case(batch, case, workspace, *, model):
    """Ordinary pipe engineering test; cannot be a real runtime installation."""
    if batch["simulation"] is not True or type(model) is not ScriptedModel:
        raise ValueError("local_synthetic_fixture_required")
    root = Path(workspace)
    return _run_pair(
        case_input(batch, case),
        root / "trusted",
        actual_root=root / "actual",
        launch=lambda: _synthetic_launch(root / "actual"),
        sender=model,
        synthetic=True,
    )


def run_local_case(driver, case, plan, original):
    from ..controlled_calibration import _ControllerFactory, _operation
    from .local_runtime import LocalInstalledRuntime
    from .native import prepare_runner_slot

    runtime = driver.authority.runtime
    if type(runtime) is not LocalInstalledRuntime:
        raise ValueError("local_installed_runtime_required")
    config = runtime.config
    operations = [op for op in plan["operations"] if op["kind"] == "product"]
    operation = _operation(plan, "product", case["id"])
    slot_index = operations.index(operation)
    workspace = driver.workspace / driver.job_id / (case["id"] + "-local")
    slot = prepare_runner_slot(
        config["bundle_root"],
        runtime.bundle_manifest,
        slot_index=slot_index,
        protected_root=config["protected_root"],
        job_id=driver.job_id,
        operation_id=operation["id"],
        verifier=runtime.bundle_verifier,
    )
    try:
        with slot:
            result = _run_pair(
                case_input(original, case),
                workspace,
                actual_root=slot.root,
                launch=slot.launch,
                sender=_ControllerFactory(driver, operation),
                synthetic=False,
            )
    except BaseException as failure:
        if workspace.is_dir():
            try:
                _seal_native_cleanup(workspace, slot)
            except BaseException as seal_failure:
                primary = dominant_error(failure, seal_failure)
                earlier = seal_failure if primary is failure else failure
                reraise_failure(primary, earlier=earlier)
        raise
    result["evidence"]["native_cleanup"] = _seal_native_cleanup(workspace, slot)
    return result


def _seal_native_cleanup(workspace, slot):
    observation = slot.cleanup_observation()
    fingerprint = _seal(workspace, "native-cleanup.json", observation)
    return {"observation": observation, "sha256": fingerprint}


if __name__ == "__main__":
    worker_main()
