"""Run bundle TraceSink: the persistence-critical trace writer.

Write-side closure only (ADR-0017, ADR-0018, ADR-0019): one bundle per Run
under ``traces/<UTC-date>/<HHMMSS>Z-<run_storage_id>/``, published through a
staging directory, drained by a single writer thread, ``fsync``-ed once at the
flush barrier, and circuit-broken at Run granularity on the first unrecoverable
write error. Readers, export, pruning, and retention are later tickets.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field, is_dataclass
from dataclasses import fields as dc_fields
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import (
    BarrierFlushResult,
    FlushResult,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    blocks_to_jsonable,
)
from agent_alfred.model import ModelError, ModelRef, Usage

# Closed set of machine-judgeable causes. The barrier reason joins this with
# the exception type name only -- never paths, never payloads.
REASON_PUBLISH_FAILED = "publish_failed"
REASON_STAGING_LEFTOVER = "staging_leftover"
REASON_ID_COLLISION = "storage_id_collision"
REASON_WRITE_FAILED = "write_failed"
REASON_FSYNC_FAILED = "fsync_failed"
REASON_FLUSH_TIMEOUT = "flush_timeout"

_FLUSH_TIMEOUT_S = 30.0
_WRITE_RETRIES = 2
_STAGING_PREFIX = ".staging-"


def _storage_id(run_id: str) -> str:
    """ADR-0018: opaque run_id never enters a path; only its digest does."""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


def _json_default(value: object) -> object:
    if isinstance(value, Message):
        return {"role": value.role, "blocks": blocks_to_jsonable(value.blocks)}
    if isinstance(value, (TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock)):
        return blocks_to_jsonable([value])[0]
    if isinstance(value, Usage):
        cost = value.endpoint_reported_cost_usd
        return {
            "total_input_tokens": value.total_input_tokens,
            "uncached_input_tokens": value.uncached_input_tokens,
            "cache_read_tokens": value.cache_read_tokens,
            "cache_write_tokens": value.cache_write_tokens,
            "output_tokens": value.output_tokens,
            "reasoning_tokens": value.reasoning_tokens,
            "endpoint_reported_cost_usd": (
                None if cost is None else format(cost, "f")
            ),
            "raw": value.raw,
        }
    if isinstance(value, ModelError):
        return {
            "retryable": value.retryable,
            "status_code": value.status_code,
            "body_excerpt": value.body_excerpt,
            "attempt_id": value.attempt_id,
            "code": value.code,
        }
    if isinstance(value, ModelRef):
        return {"endpoint_id": value.endpoint_id, "model_id": value.model_id}
    if isinstance(value, Decimal):
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "_type": type(value).__name__,
            **{f.name: getattr(value, f.name) for f in dc_fields(value)},
        }
    raise TypeError(f"unserializable trace payload part {type(value).__name__}")


def _prepare_payload(payload: object) -> str:
    """Serialize the payload once, outside any lock. Pure function."""
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _compose_line(prepared_payload: str, event: SequencedEvent) -> str:
    envelope = event.envelope
    header = json.dumps(
        {
            "seq": event.seq,
            "process_instance_id": event.process_instance_id,
            "ts": envelope.ts,
            "run_id": envelope.run_id,
            "session_id": envelope.session_id,
            "step_index": envelope.step_index,
            "attempt_id": envelope.attempt_id,
            "node_id": envelope.node_id,
            "source": envelope.source,
            "trace_policy": event.trace_policy,
            "payload_name": getattr(
                event.payload, "name", type(event.payload).__name__
            ),
        },
        ensure_ascii=False,
    )
    return header[:-1] + ',"payload":' + prepared_payload + "}"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class _RunBundle:
    run_id: str
    published: bool = False
    flushed_once: bool = False
    run_dir: Path | None = None
    trace_fd: int | None = None
    dropped: int = 0
    broken: str | None = None
    first_error: str | None = None
    fsync_error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class RunBundleTraceSink:
    """The production EventSink whose flush the Run barrier waits on."""

    name = "trace"
    flush_at_run_end = True

    def __init__(self, *, root: Path, clock: Clock, process_instance_id: str):
        self._root = root
        self._clock = clock
        self._process_instance_id = process_instance_id
        self._bundles: dict[str, _RunBundle] = {}
        self._queue: deque[tuple[str, str, SequencedEvent]] = deque()
        self._wake = threading.Condition()
        self._stopping = False
        self._writing = False
        # Fail fast at assembly: an unusable traces root must be discovered
        # before the Host serves Runs, not at the first flush.
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        self._drain = threading.Thread(
            target=self._drain_loop, name="trace-drain", daemon=True
        )
        self._drain.start()

    # -- two-phase publish (ADR-0015) -------------------------------------

    def prepare(self, event: UnsequencedEvent) -> object:
        # Pure, lock-free, no IO: the payload's canonical JSON. Raising here on
        # an unknown payload fails closed -- the FanOutSink disables this sink
        # for the run and the barrier reports the loss.
        return _prepare_payload(event.payload)

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        run_id = event.envelope.run_id
        if not run_id:
            raise ValueError("trace sink requires a run_id")
        bundle = self._bundle_for(run_id)
        with bundle.lock:
            if bundle.broken is not None:
                bundle.dropped += 1
                return
        with self._wake:
            self._queue.append((run_id, prepared, event))
            self._wake.notify_all()

    def _bundle_for(self, run_id: str) -> _RunBundle:
        with self._wake:
            bundle = self._bundles.get(run_id)
            if bundle is None:
                bundle = _RunBundle(run_id=run_id)
                self._bundles[run_id] = bundle
            return bundle

    # -- drain thread ------------------------------------------------------

    def _drain_loop(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._stopping:
                    self._wake.wait()
                if self._stopping and not self._queue:
                    return
                run_id, prepared, event = self._queue.popleft()
                self._writing = True
            try:
                self._write_item(run_id, prepared, event)
            finally:
                with self._wake:
                    self._writing = False
                    self._wake.notify_all()

    def _write_item(self, run_id: str, prepared: str, event: SequencedEvent) -> None:
        bundle = self._bundle_for(run_id)
        with bundle.lock:
            if bundle.broken is not None:
                bundle.dropped += 1
                return
            if not bundle.published:
                self._publish_locked(bundle)
                if bundle.broken is not None:
                    return
            line = _compose_line(prepared, event) + "\n"
            data = line.encode("utf-8")
            for attempt in range(_WRITE_RETRIES + 1):
                try:
                    _write_all(bundle.trace_fd, data)
                    return
                except Exception as exc:
                    if attempt == 0:
                        bundle.first_error = (
                            f"{REASON_WRITE_FAILED} {type(exc).__name__}"
                        )
                    if attempt == _WRITE_RETRIES:
                        bundle.broken = REASON_WRITE_FAILED
                        bundle.dropped += 1

    def _publish_locked(self, bundle: _RunBundle) -> None:
        run_id = bundle.run_id
        try:
            now: datetime = self._clock.wall_utc()
            storage_id = _storage_id(run_id)
            date_dir = self._root / now.strftime("%Y-%m-%d")
            dir_name = f"{now.strftime('%H%M%S')}Z-{storage_id}"
            target = date_dir / dir_name
            staging = date_dir / f"{_STAGING_PREFIX}{storage_id}"
            if target.exists():
                bundle.broken = REASON_ID_COLLISION
                bundle.first_error = REASON_ID_COLLISION
                return
            if staging.exists():
                # A leftover from a crashed publish is never reused (ADR-0017).
                bundle.broken = REASON_STAGING_LEFTOVER
                bundle.first_error = REASON_STAGING_LEFTOVER
                return
            date_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            date_dir.chmod(0o700)
            staging.mkdir(mode=0o700)
            staging.chmod(0o700)
            meta_fd = os.open(
                staging / "meta.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                meta = json.dumps(
                    {
                        "run_id": run_id,
                        "run_storage_id": storage_id,
                        "run_dir_name": dir_name,
                        "created_at": format_instant(now),
                        "process_instance_id": self._process_instance_id,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                _write_all(meta_fd, meta)
                os.fsync(meta_fd)
            finally:
                os.close(meta_fd)
            trace_fd = os.open(
                staging / "trace.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.fsync(trace_fd)
            os.close(trace_fd)
            (staging / "artifacts").mkdir(mode=0o700)
            (staging / "artifacts").chmod(0o700)
            _fsync_dir(staging)
            os.rename(staging, target)
            _fsync_dir(date_dir)
            bundle.run_dir = target
            bundle.trace_fd = os.open(
                target / "trace.jsonl", os.O_WRONLY | os.O_APPEND, 0o600
            )
            bundle.published = True
        except Exception as exc:
            bundle.broken = REASON_PUBLISH_FAILED
            bundle.first_error = f"{REASON_PUBLISH_FAILED} {type(exc).__name__}"

    # -- flush barrier (ADR-0019) ------------------------------------------

    def flush(self) -> FlushResult:
        deadline = time.monotonic() + _FLUSH_TIMEOUT_S
        with self._wake:
            while (self._queue or self._writing) and time.monotonic() < deadline:
                self._wake.wait(0.05)
            timed_out = bool(self._queue or self._writing)
        if timed_out:
            return BarrierFlushResult(
                outcome="failed", dropped_events=0, detail=REASON_FLUSH_TIMEOUT
            )
        with self._wake:
            bundles = [b for b in self._bundles.values() if not b.flushed_once]
            for bundle in bundles:
                bundle.flushed_once = True
        dropped = 0
        details: list[str] = []
        for bundle in bundles:
            with bundle.lock:
                dropped += bundle.dropped
                if bundle.broken is not None:
                    details.append(
                        bundle.first_error or bundle.broken
                    )
                    continue
                if bundle.published and bundle.trace_fd is not None:
                    error = self._fsync_bundle(bundle)
                    if error is not None:
                        details.append(error)
        if details or dropped:
            return BarrierFlushResult(
                outcome="failed",
                dropped_events=dropped,
                detail="; ".join(dict.fromkeys(details))[:200],
            )
        return BarrierFlushResult(outcome="flushed", dropped_events=0, detail="")

    def _fsync_bundle(self, bundle: _RunBundle) -> str | None:
        # ADR-0019 order: artifact files -> trace.jsonl -> artifacts/ -> Run dir.
        try:
            os.fsync(bundle.trace_fd)
            _fsync_dir(bundle.run_dir / "artifacts")
            _fsync_dir(bundle.run_dir)
            return None
        except Exception as exc:
            return f"{REASON_FSYNC_FAILED} {type(exc).__name__}"

    def close(self) -> None:
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        self._drain.join(timeout=5)
        with self._wake:
            bundles = list(self._bundles.values())
        for bundle in bundles:
            with bundle.lock:
                if bundle.trace_fd is not None:
                    try:
                        os.close(bundle.trace_fd)
                    except OSError:
                        pass
                    bundle.trace_fd = None
