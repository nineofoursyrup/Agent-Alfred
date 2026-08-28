"""Run bundle TraceSink: the persistence-critical trace writer.

Write-side closure only (ADR-0017, ADR-0018, ADR-0019): one bundle per Run
under ``traces/<UTC-date>/<HHMMSS>Z-<run_storage_id>/``, published through a
staging directory, drained by a single writer thread, ``fsync``-ed once at the
flush barrier, and circuit-broken at Run granularity on the first unrecoverable
write error. Readers, export, pruning, and retention are later tickets.

Ownership rules that keep ADR-0015 true:

- ``commit`` never waits on disk. It checks bounded in-memory state and does
  one bounded queue append; the queue is fail-closed, never blocking.
- The drain thread is the sole owner of the fd, every ``open``/``write``/
  ``fsync``/``rename``/``close``, and of the staging publication. No bundle
  state lock is ever held across a filesystem call.
- The flush barrier travels through the queue as an explicit barrier item, so
  "everything enqueued before the barrier has been written" holds by queue
  order, not by holding a lock while the disk catches up.
- A Run's final barrier closes its fd and retires its bundle; late commits are
  rejected fail-closed instead of reopening a published bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
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
REASON_QUEUE_FULL = "queue_overflow"

_FLUSH_TIMEOUT_S = 30.0
_WRITE_RETRIES = 2
_QUEUE_LIMIT = 8192
_STAGING_PREFIX = ".staging-"


class LateCommitRejected(RuntimeError):
    """A commit arrived after the Run's final barrier retired its bundle.

    The published bundle is never reopened or extended; the caller sees the
    refusal instead of a silently lost event.
    """


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


_RETRYABLE_WRITE_ERRORS = (InterruptedError, BlockingIOError)


def _write_all(fd: int, data: bytes, offset: int = 0) -> int:
    """Write ``data[offset:]`` following partial writes; return the new offset.

    One call owns the single byte offset for one queue item's whole retry
    lifecycle: a partial write advances the offset, a retryable interruption
    (EINTR/EAGAIN) resumes from it a bounded number of times, and an
    unrecoverable error or an exhausted retry budget propagates. The
    successfully written prefix is never resubmitted -- that is what keeps
    every published record a contiguous prefix with no internal bad line
    (ADR-0019).
    """
    view = memoryview(data)[offset:]
    retries_left = _WRITE_RETRIES
    while view:
        try:
            written = os.write(fd, view)
        except _RETRYABLE_WRITE_ERRORS:
            if retries_left == 0:
                raise
            retries_left -= 1
            continue
        offset += written
        view = view[written:]
    return offset


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
    run_dir: Path | None = None
    trace_fd: int | None = None
    dropped: int = 0
    broken: str | None = None
    first_error: str | None = None
    # Held only for the tiny state exchanges between commit threads and the
    # drain thread -- never across open/rename/write/fsync/close.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class _WriteBarrier:
    """A queue item marking a flush barrier (ADR-0019).

    The bundle snapshot is taken when the barrier is enqueued, so the drain
    thread fsyncs exactly the bundles whose items precede it in queue order,
    and a Run admitted later is never retired by someone else's barrier.
    """

    bundles: tuple[_RunBundle, ...]
    done: threading.Event = field(default_factory=threading.Event)
    dropped: int = 0
    failed: str | None = None


class RunBundleTraceSink:
    """The production EventSink whose flush the Run barrier waits on."""

    name = "trace"
    flush_at_run_end = True

    def __init__(self, *, root: Path, clock: Clock, process_instance_id: str):
        self._root = root
        self._clock = clock
        self._process_instance_id = process_instance_id
        self._bundles: dict[str, _RunBundle] = {}
        # Lightweight, recyclable termination record: run_ids whose final
        # barrier (or a close) retired their bundle. Only strings survive.
        self._terminated: set[str] = set()
        self._late_dropped = 0
        self._queue: deque[_WriteBarrier | tuple[str, str, SequencedEvent]] = (
            deque()
        )
        self._wake = threading.Condition()
        self._stopping = False
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
        if event.trace_policy != "persist":
            # ADR-0013: transient events are filtered by the event protocol's
            # trace_policy before any serialization or queue work. The no-op
            # prepared value never reaches the drain thread or the bundle.
            return None
        # Pure, lock-free, no IO: the payload's canonical JSON. Raising here on
        # an unknown payload fails closed -- the FanOutSink disables this sink
        # for the run and the barrier reports the loss.
        return _prepare_payload(event.payload)

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        if event.trace_policy != "persist":
            # The event protocol is the authority; a stale prepared value for
            # a transient event must not sneak past the prepare-side filter.
            return
        if not isinstance(prepared, str):
            raise ValueError(
                "trace sink commit requires the prepared payload string"
            )
        run_id = event.envelope.run_id
        if not run_id:
            raise ValueError("trace sink requires a run_id")
        with self._wake:
            if self._stopping or run_id in self._terminated:
                raise LateCommitRejected(
                    "late commit after the run's final barrier"
                )
            bundle = self._bundles.get(run_id)
            if bundle is None:
                bundle = _RunBundle(run_id=run_id)
                self._bundles[run_id] = bundle
            broken = bundle.broken
        if broken is not None:
            with bundle.lock:
                bundle.dropped += 1
            return
        with self._wake:
            if len(self._queue) >= _QUEUE_LIMIT:
                # Bounded queue, fail-closed: the event is lost and the Run's
                # trace is circuit-broken, never blocked on a slow drain.
                overflow = True
            else:
                self._queue.append((run_id, prepared, event))
                overflow = False
            self._wake.notify_all()
        if overflow:
            self._break(bundle, REASON_QUEUE_FULL)

    def _break(
        self, bundle: _RunBundle, reason: str, detail: str | None = None
    ) -> None:
        """Circuit-break one Run (ADR-0019). The first cause wins; later
        failures only advance the drop counter."""
        with bundle.lock:
            if bundle.first_error is None:
                bundle.first_error = detail if detail else reason
            if bundle.broken is None:
                bundle.broken = reason
            bundle.dropped += 1

    # -- drain thread: the sole owner of fds and filesystem calls ----------

    def _drain_loop(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._stopping:
                    self._wake.wait()
                if not self._queue:
                    return
                item = self._queue.popleft()
            if isinstance(item, _WriteBarrier):
                self._process_barrier(item)
                continue
            run_id, prepared, event = item
            try:
                self._write_item(run_id, prepared, event)
            except Exception as exc:
                # The drain thread must survive any single item: the Run's
                # trace fails closed instead of the thread dying and stalling
                # every later barrier. The reason keeps only the type name.
                with self._wake:
                    bundle = self._bundles.get(run_id)
                    if bundle is None:
                        self._late_dropped += 1
                if bundle is not None:
                    self._break(
                        bundle,
                        REASON_WRITE_FAILED,
                        f"{REASON_WRITE_FAILED} {type(exc).__name__}",
                    )

    def _write_item(self, run_id: str, prepared: str, event: SequencedEvent) -> None:
        with self._wake:
            bundle = self._bundles.get(run_id)
        if bundle is None:
            # Retired after its final barrier (or a close): the event is lost;
            # a bounded counter remembers it.
            with self._wake:
                self._late_dropped += 1
            return
        with bundle.lock:
            if bundle.broken is not None:
                bundle.dropped += 1
                return
            fd = bundle.trace_fd
        if fd is None:
            self._publish(bundle)  # staging I/O with no lock held
            with bundle.lock:
                broken = bundle.broken
                fd = bundle.trace_fd
            if broken is not None:
                return  # the failed publish already counted this event
        line = _compose_line(prepared, event) + "\n"
        data = line.encode("utf-8")
        try:
            _write_all(fd, data)  # one byte offset across the item's retries
        except Exception as exc:
            self._break(
                bundle,
                REASON_WRITE_FAILED,
                f"{REASON_WRITE_FAILED} {type(exc).__name__}",
            )

    def _publish(self, bundle: _RunBundle) -> None:
        run_id = bundle.run_id
        try:
            now: datetime = self._clock.wall_utc()
            storage_id = _storage_id(run_id)
            date_dir = self._root / now.strftime("%Y-%m-%d")
            dir_name = f"{now.strftime('%H%M%S')}Z-{storage_id}"
            target = date_dir / dir_name
            staging = date_dir / f"{_STAGING_PREFIX}{storage_id}"
            if target.exists():
                self._break(bundle, REASON_ID_COLLISION)
                return
            if staging.exists():
                # A leftover from a crashed publish is never reused (ADR-0017).
                self._break(bundle, REASON_STAGING_LEFTOVER)
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
            self._break(
                bundle,
                REASON_PUBLISH_FAILED,
                f"{REASON_PUBLISH_FAILED} {type(exc).__name__}",
            )

    # -- flush barrier (ADR-0019) ------------------------------------------

    def flush(self) -> FlushResult:
        with self._wake:
            if self._stopping:
                return BarrierFlushResult(
                    outcome="failed", dropped_events=0, detail=REASON_FLUSH_TIMEOUT
                )
            barrier = _WriteBarrier(bundles=tuple(self._bundles.values()))
            self._queue.append(barrier)
            self._wake.notify_all()
        # The one deliberate blocking point: the barrier item's position in
        # queue order guarantees everything before it has been written and
        # fsynced, without holding any lock while the disk catches up.
        if not barrier.done.wait(_FLUSH_TIMEOUT_S):
            return BarrierFlushResult(
                outcome="failed", dropped_events=0, detail=REASON_FLUSH_TIMEOUT
            )
        if barrier.failed is not None or barrier.dropped:
            return BarrierFlushResult(
                outcome="failed",
                dropped_events=barrier.dropped,
                detail=barrier.failed or "",
            )
        return BarrierFlushResult(outcome="flushed", dropped_events=0, detail="")

    def _process_barrier(self, barrier: _WriteBarrier) -> None:
        details: list[str] = []
        dropped = 0
        to_retire: list[tuple[_RunBundle, int | None, Path | None]] = []
        for bundle in barrier.bundles:
            with bundle.lock:
                broken = bundle.broken
                first_error = bundle.first_error
                dropped += bundle.dropped
                fd = bundle.trace_fd
                run_dir = bundle.run_dir
            if broken is not None:
                details.append(first_error or broken)
                to_retire.append((bundle, None, None))
                continue
            if fd is not None and run_dir is not None:
                error = self._fsync_bundle(fd, run_dir)
                if error is not None:
                    details.append(error)
            to_retire.append((bundle, fd, run_dir))
        # The writes are fsynced; now the drain releases each Run's fd and
        # retires the bundle, so nothing is held until sink.close().
        closed_fds: list[int] = []
        with self._wake:
            for bundle, fd, _run_dir in to_retire:
                self._bundles.pop(bundle.run_id, None)
                self._terminated.add(bundle.run_id)
                if fd is not None:
                    bundle.trace_fd = None
                    closed_fds.append(fd)
        for fd in closed_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        barrier.dropped = dropped
        barrier.failed = (
            "; ".join(dict.fromkeys(details))[:200] if details else None
        )
        barrier.done.set()

    def _fsync_bundle(self, fd: int, run_dir: Path) -> str | None:
        # ADR-0019 order: artifact files -> trace.jsonl -> artifacts/ -> Run dir.
        try:
            os.fsync(fd)
            _fsync_dir(run_dir / "artifacts")
            _fsync_dir(run_dir)
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
            self._bundles.clear()
        for bundle in bundles:
            fd = bundle.trace_fd
            bundle.trace_fd = None
            self._terminated.add(bundle.run_id)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
