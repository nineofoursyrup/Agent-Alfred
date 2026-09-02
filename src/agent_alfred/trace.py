"""Run bundle TraceSink: the persistence-critical trace writer.

Write-side closure only (ADR-0017, ADR-0018, ADR-0019): one bundle per Run
under ``traces/<UTC-date>/<HHMMSS>Z-<run_storage_id>/``, published through a
staging directory, drained by a single writer thread, ``fsync``-ed once at the
flush barrier, and circuit-broken at Run granularity on the first unrecoverable
write error. Readers, export, pruning, and retention are later tickets.

Ownership rules that keep ADR-0015 true:

- ``commit`` never waits on disk. It checks bounded in-memory state and does
  one bounded queue append in a single short critical section; the queue is
  fail-closed, never blocking.
- The drain thread is the sole owner of the fd, every ``open``/``write``/
  ``fsync``/``rename``/``close``, and of the staging publication -- for the
  thread's whole life, including its unwind when the sink stops. No bundle
  state lock is ever held across a filesystem call.
- The flush barrier travels through the queue as an explicit barrier item
  scoped to the Run it finishes. That Run's bundle is sealed under the same
  lock commits need at the moment the barrier is enqueued, so "everything
  enqueued before the barrier has been written" holds by queue order and any
  later commit is rejected fail-closed instead of being enqueued behind the
  barrier and silently lost.
- A Run's final barrier closes its fd and retires its bundle; late commits
  are rejected fail-closed instead of reopening a published bundle.
- ``close`` never touches an fd: after a timed-out join the drain may be
  inside ``os.write`` on one. It answers still-queued barriers so no flusher
  hangs, and the drain releases the fds when it unwinds.
- The drain answers the barriers it still holds on **every** exit, including
  the unwind after a crash. It is the only thread that can answer one, so a
  crash that leaves a barrier queued would otherwise hang its waiter for the
  whole flush timeout and then report a timeout that never happened.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePath

from agent_alfred.clock import Clock, format_instant
from agent_alfred.events import (
    BarrierFlushResult,
    FlushResult,
    SequencedEvent,
    UnsequencedEvent,
)
from agent_alfred.events import (
    event_json_default as _json_default,
)
from agent_alfred.managed_state import (
    ManagedDirectoryLease,
    ManagedFileLease,
    ManagedPathSecurityError,
    ManagedTraceRoot,
)
from agent_alfred.resource_rollback import ResumableRollback

# Closed set of machine-judgeable causes. The barrier reason joins this with
# the exception type name only -- never paths, never payloads.
REASON_PUBLISH_FAILED = "publish_failed"
REASON_STAGING_LEFTOVER = "staging_leftover"
REASON_ID_COLLISION = "storage_id_collision"
REASON_WRITE_FAILED = "write_failed"
REASON_FSYNC_FAILED = "fsync_failed"
REASON_FLUSH_TIMEOUT = "flush_timeout"
REASON_QUEUE_OVERFLOW = "queue_overflow"
REASON_SINK_STOPPING = "sink_stopping"
REASON_SINK_CLOSED = "sink_closed"
# The platform offers no atomic no-replace rename, so the bundle cannot be
# published without risking an overwrite. Publication refuses rather than
# degrade: a bundle overwritten is a bundle silently destroyed.
REASON_NO_REPLACE_UNSUPPORTED = "no_replace_unsupported"
# The drain exited still holding a barrier it never reached: the writer is
# gone, not slow. Kept distinct from ``sink_closed`` (close() was called and
# the queue is deliberately abandoned) and from ``flush_timeout`` (the drain
# was given the whole budget and used it up) -- a flusher told "timeout"
# here would go looking for a slow disk that is not the problem.
REASON_SINK_FAILED = "sink_failed"

_FLUSH_TIMEOUT_S = 30.0
# How long close() waits for a drain that may be stuck on a slow disk. The
# wait's outcome changes nothing about fd ownership: close() never closes one.
_CLOSE_JOIN_TIMEOUT_S = 5.0
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


def _prepare_payload(payload: object) -> str:
    """Serialize the payload once, outside any lock. Pure function."""
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _is_persist(trace_policy: str) -> bool:
    """ADR-0013: only persist events reach the queue, the drain, the bundle."""
    return trace_policy == "persist"


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


def _exception_detail(reason: str, exc: BaseException) -> str:
    """The one shape an exception may leave in a barrier's detail: the
    closed-set reason plus the exception's type name. Never its text -- that
    may carry paths or payload fragments the closed set exists to keep out."""
    return f"{reason} {type(exc).__name__}"


def _take_bundle_resources(
    bundle: _RunBundle,
) -> tuple[
    ManagedFileLease | None,
    ManagedDirectoryLease | None,
    ManagedDirectoryLease | None,
]:
    """Drain-thread only: claim every capability a retired bundle owns."""
    with bundle.lock:
        resources = (bundle.trace_file, bundle.artifacts_dir, bundle.run_dir)
        bundle.trace_file = None
        bundle.artifacts_dir = None
        bundle.run_dir = None
    return resources


def _close_bundle_resources(
    resources: list[
        tuple[
            ManagedFileLease | None,
            ManagedDirectoryLease | None,
            ManagedDirectoryLease | None,
        ]
    ],
) -> BaseException | None:
    first_error: BaseException | None = None
    for trace_file, artifacts, run_dir in resources:
        for lease in (trace_file, artifacts, run_dir):
            if lease is not None:
                try:
                    lease.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
    return first_error


@dataclass
class _RunBundle:
    run_id: str
    run_dir: ManagedDirectoryLease | None = None
    artifacts_dir: ManagedDirectoryLease | None = None
    trace_file: ManagedFileLease | None = None
    dropped: int = 0
    broken: str | None = None
    first_error: str | None = None
    # Set under the sink wake lock when a barrier scoped to this Run is
    # enqueued; from that moment commits for this Run fail closed.
    sealed: bool = False
    # Held only for the tiny state exchanges between commit threads and the
    # drain thread -- never across open/rename/write/fsync/close. Lock order:
    # the sink wake lock may be held while taking this one, never the reverse.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def trace_fd(self) -> int | None:
        """Test observation only; production operations stay on the lease."""
        return None if self.trace_file is None else self.trace_file.fd

    def drop_if_broken(self) -> str | None:
        """Count one event as dropped when this Run is already broken and
        return the reason. Callers may hold the sink wake lock. The breaker
        state lives here, next to the lock that guards it, rather than on the
        sink that only ever reaches through to these fields."""
        with self.lock:
            if self.broken is not None:
                self.dropped += 1
            return self.broken

    def mark_broken(self, reason: str, detail: str | None = None) -> None:
        """Circuit-break this Run (ADR-0019). The first cause wins; later
        failures only advance the drop counter the barrier reports."""
        with self.lock:
            if self.first_error is None:
                self.first_error = detail if detail else reason
            if self.broken is None:
                self.broken = reason
            self.dropped += 1

    def mark_broken_with_exception(self, reason: str, exc: BaseException) -> None:
        self.mark_broken(reason, _exception_detail(reason, exc))


@dataclass(frozen=True)
class _WriteItem:
    """One queued persist event: which Run it belongs to, the payload the
    prepare stage serialized lock-free, and the sequenced event whose envelope
    the line is composed from. The three are only ever produced, queued, and
    written together, so they travel as one item instead of a positional
    triple whose slots the drain would have to remember."""

    run_id: str
    prepared: str
    event: SequencedEvent


@dataclass
class _WriteBarrier:
    """A queue item marking a flush barrier (ADR-0019).

    The barrier carries exactly the bundles scoped to its flush -- the
    finishing Run's bundle when the FanOut passes the run_id -- and those
    bundles are sealed at enqueue time, so the drain fsyncs and retires
    precisely them and never touches a bundle another Run admitted later.
    """

    bundles: tuple[_RunBundle, ...]
    done: threading.Event = field(default_factory=threading.Event)
    dropped: int = 0
    failed: str | None = None

    def answer(self, reason: str | None, dropped: int = 0) -> None:
        """Answer every waiter once. The count and the reason are written
        before the event is set, so a thread released by ``done`` sees them;
        every caller answers a given barrier exactly once and skips the ones
        already answered."""
        self.failed = reason
        self.dropped = dropped
        self.done.set()


class RunBundleTraceSink:
    """The production EventSink whose flush the Run barrier waits on."""

    name = "trace"
    flush_at_run_end = True

    def __init__(
        self,
        *,
        root: ManagedTraceRoot,
        clock: Clock,
        process_instance_id: str,
        _rollback: ResumableRollback | None = None,
    ):
        rollback = _rollback or ResumableRollback()
        rollback.own(root)
        self._root_lease = root
        self._clock = clock
        self._process_instance_id = process_instance_id
        self._bundles: dict[str, _RunBundle] = {}
        # Lightweight, recyclable termination record: run_ids whose final
        # barrier retired their bundle. Only strings survive.
        self._terminated: set[str] = set()
        self._queue: deque[_WriteBarrier | _WriteItem] = deque()
        self._wake = threading.Condition()
        self._stopping = False
        # Fail fast at assembly: an unusable traces root must be discovered
        # before the Host serves Runs, not at the first flush.
        try:
            self._drain = threading.Thread(
                target=self._drain_loop, name="trace-drain", daemon=False
            )
            self._drain.start()
        except BaseException as exc:
            if _rollback is None:
                rollback.raise_failure(exc)
            raise
        rollback.transfer(root)

    # -- two-phase publish (ADR-0015) -------------------------------------

    def prepare(self, event: UnsequencedEvent) -> object:
        if not _is_persist(event.trace_policy):
            # ADR-0013: transient events are filtered by the event protocol's
            # trace_policy before any serialization or queue work. The no-op
            # prepared value never reaches the drain thread or the bundle.
            return None
        # Pure, lock-free, no IO: the payload's canonical JSON. Raising here on
        # an unknown payload fails closed -- the FanOutSink disables this sink
        # for the run and the barrier reports the loss.
        return _prepare_payload(event.payload)

    def commit(self, prepared: object, event: SequencedEvent) -> None:
        if not _is_persist(event.trace_policy):
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
            # One critical section for the termination check, the bundle
            # lookup/creation, and the enqueue: the barrier seal (set under
            # this same lock when the barrier is enqueued) makes the check
            # atomic with the drain's retire, so an event can never be
            # enqueued behind the barrier that retires its bundle.
            bundle = self._bundles.get(run_id)
            sealed = bundle is not None and bundle.sealed
            if self._stopping or run_id in self._terminated or sealed:
                # Both refusals are the same fact -- the Run's final barrier
                # is already settled -- so they say it once.
                raise LateCommitRejected(
                    "late commit after the run's final barrier"
                )
            if bundle is None:
                bundle = _RunBundle(run_id=run_id)
                self._bundles[run_id] = bundle
            if bundle.drop_if_broken() is not None:
                # Circuit-broken: the first cause is kept; later events only
                # advance the drop counter the Run's barrier reports.
                return
            if len(self._queue) >= _QUEUE_LIMIT:
                # Bounded queue, fail-closed: the event is lost and the Run's
                # trace is circuit-broken, never blocked on a slow drain. The
                # break lands inside this same critical section, so a barrier
                # enqueued later always reports the drop.
                bundle.mark_broken(REASON_QUEUE_OVERFLOW)
            else:
                self._queue.append(
                    _WriteItem(run_id=run_id, prepared=prepared, event=event)
                )
            self._wake.notify_all()

    # -- drain thread: the sole owner of fds and filesystem calls ----------

    def _drain_loop(self) -> None:
        try:
            while True:
                with self._wake:
                    while not self._queue and not self._stopping:
                        self._wake.wait()
                    if not self._queue:
                        return
                    item = self._queue.popleft()
                if isinstance(item, _WriteBarrier):
                    if item.done.is_set():
                        # Cancelled while queued by close(): its waiters were
                        # already answered with the honest reason; the drain
                        # only skips it.
                        continue
                    self._process_barrier(item)
                    continue
                try:
                    self._write_item(item)
                except AssertionError:
                    # The enqueue/retire invariant broke; a silent drop here
                    # is exactly what the barrier seal exists to prevent.
                    # Failing the thread loudly makes every later barrier
                    # time out and report the damage instead of hiding it.
                    raise
                except Exception as exc:
                    # The drain thread must survive any single item: the Run's
                    # trace fails closed instead of the thread dying and
                    # stalling every later barrier. The reason keeps only the
                    # type name.
                    with self._wake:
                        bundle = self._bundles.get(item.run_id)
                    if bundle is not None:
                        bundle.mark_broken_with_exception(REASON_WRITE_FAILED, exc)
        finally:
            # The drain is the only thread that can answer a barrier, so its
            # unwind answers the ones it still holds -- on a crash as much as
            # on a clean stop. Otherwise their waiters hang for the whole
            # flush budget and then report a timeout that never happened,
            # which reads as a slow disk when the truth is a dead writer.
            # Nothing holds a lock here: every raise inside the loop escapes
            # from _write_item, which runs outside both locks.
            with self._wake:
                self._answer_queued_barriers_locked(REASON_SINK_FAILED)
            # The drain is the sole owner of every fd for the thread's whole
            # life, including its unwind: only it ever closes one.
            self._release_remaining_fds()

    def _write_item(self, item: _WriteItem) -> None:
        run_id = item.run_id
        with self._wake:
            bundle = self._bundles.get(run_id)
        if bundle is None:
            # Unreachable by construction: an event is only enqueued before
            # its Run's barrier is sealed (same lock), and queue order writes
            # it before that barrier retires the bundle. A silent drop here
            # would be invisible to the Run's FlushResult, so fail loudly.
            raise AssertionError(f"trace item for unknown bundle {run_id!r}")
        if bundle.drop_if_broken() is not None:
            return  # the Run is broken; the drop was counted under its lock
        with bundle.lock:
            trace_file = bundle.trace_file
        if trace_file is None:
            self._publish(bundle)  # staging I/O with no lock held
            with bundle.lock:
                broken = bundle.broken
                trace_file = bundle.trace_file
            if broken is not None:
                return  # the failed publish already counted this event
        if trace_file is None:
            raise AssertionError("published trace bundle has no trace capability")
        line = _compose_line(item.prepared, item.event) + "\n"
        data = line.encode("utf-8")
        try:
            trace_file.write_all(data, retries=_WRITE_RETRIES)
        except Exception as exc:
            bundle.mark_broken_with_exception(REASON_WRITE_FAILED, exc)

    def _publish(self, bundle: _RunBundle) -> None:
        run_id = bundle.run_id
        staging: ManagedDirectoryLease | None = None
        artifacts: ManagedDirectoryLease | None = None
        date_lease: ManagedDirectoryLease | None = None
        published = False
        try:
            now: datetime = self._clock.wall_utc()
            storage_id = _storage_id(run_id)
            date_lease = self._root_lease.ensure_directory(
                PurePath(now.strftime("%Y-%m-%d"))
            )
            dir_name = f"{now.strftime('%H%M%S')}Z-{storage_id}"
            staging_name = f"{_STAGING_PREFIX}{storage_id}"
            try:
                staging = date_lease.create_directory(
                    PurePath(staging_name), role="bundle staging directory"
                )
            except FileExistsError:
                bundle.mark_broken(REASON_STAGING_LEFTOVER)
                return
            meta_file = staging.open_regular(
                PurePath("meta.json"),
                access="exclusive_write",
                create=True,
                role="bundle metadata",
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
                meta_file.write_all(meta, retries=_WRITE_RETRIES)
                meta_file.fsync()
            finally:
                meta_file.close()
            initial_trace = staging.open_regular(
                PurePath("trace.jsonl"),
                access="exclusive_write",
                create=True,
                role="bundle trace",
            )
            try:
                initial_trace.fsync()
            finally:
                initial_trace.close()
            artifacts = staging.create_directory(
                PurePath("artifacts"), role="bundle artifacts directory"
            )
            staging.fsync()
            try:
                # Publication proper. The target's absence is deliberately
                # not checked first: a check that ran before the rename would
                # be a TOCTOU window, not exclusivity. The primitive refuses
                # an existing target on its own, atomically.
                date_lease.rename_directory_no_replace(staging, PurePath(dir_name))
            except FileExistsError:
                # Somebody already published a bundle under this Run's
                # storage identity -- a concurrent publisher, or a directory
                # left by an earlier Run. Either way it is not ours to
                # replace, merge into, or delete, so the Run's trace fails
                # closed and the existing bundle is left exactly as found.
                bundle.mark_broken(REASON_ID_COLLISION)
                return
            except ManagedPathSecurityError as exc:
                if exc.reason != "unsupported_nofollow":
                    raise
                bundle.mark_broken(REASON_NO_REPLACE_UNSUPPORTED)
                return
            published = True
            date_lease.fsync()
            trace_file = staging.open_regular(
                PurePath("trace.jsonl"),
                access="append",
                create=False,
                role="bundle trace",
            )
            with bundle.lock:
                bundle.run_dir = staging
                bundle.artifacts_dir = artifacts
                bundle.trace_file = trace_file
            staging = None
            artifacts = None
        except Exception as exc:
            bundle.mark_broken_with_exception(REASON_PUBLISH_FAILED, exc)
        finally:
            cleanup_error: BaseException | None = None
            if not published and staging is not None:
                for name in ("meta.json", "trace.jsonl"):
                    try:
                        staging.unlink_regular(PurePath(name), missing_ok=True)
                    except (OSError, ManagedPathSecurityError) as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                if artifacts is not None:
                    try:
                        staging.remove_directory_if_owned(artifacts)
                    except (OSError, ManagedPathSecurityError) as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                if date_lease is not None:
                    try:
                        date_lease.remove_directory_if_owned(staging)
                    except (OSError, ManagedPathSecurityError) as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
            # Until all three capabilities are installed on ``bundle`` they
            # remain locals owned by this frame.  Rename changes a name, not
            # ownership: a later fsync/reopen failure must still close them.
            for lease in (artifacts, staging, date_lease):
                if lease is None:
                    continue
                try:
                    lease.close()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                bundle.mark_broken_with_exception(
                    REASON_PUBLISH_FAILED, cleanup_error
                )

    # -- flush barrier (ADR-0019) ------------------------------------------

    def flush(self, run_id: str) -> FlushResult:
        with self._wake:
            if self._stopping:
                return BarrierFlushResult(
                    outcome="failed",
                    dropped_events=0,
                    detail=REASON_SINK_STOPPING,
                )
            # The FanOut always names the finishing Run, so the unscoped
            # "settle every bundle at once" shape is gone: a barrier is
            # always somebody's, and it retires exactly that somebody.
            bundle = self._bundles.get(run_id)
            snapshot = () if bundle is None else (bundle,)
            barrier = _WriteBarrier(bundles=snapshot)
            for bundle in snapshot:
                # Seal at enqueue, under the same lock commits need: any
                # commit for these Runs from now on is rejected fail-closed
                # instead of being enqueued behind this barrier and silently
                # dropped after it retires the bundle.
                bundle.sealed = True
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
        # Every bundle this barrier retires, broken or not. A broken Run
        # still owns an fd: it was opened before the write failed, and the
        # drain is the only thread that ever closes one.
        to_retire: list[_RunBundle] = []
        for bundle in barrier.bundles:
            with bundle.lock:
                broken = bundle.broken
                first_error = bundle.first_error
                dropped += bundle.dropped
                trace_file = bundle.trace_file
                artifacts = bundle.artifacts_dir
                run_dir = bundle.run_dir
            if broken is not None:
                details.append(first_error or broken)
            elif (
                trace_file is not None
                and artifacts is not None
                and run_dir is not None
            ):
                # Only an unbroken bundle gets fsynced: a broken one has
                # bytes on disk the barrier cannot vouch for, and fsyncing
                # them would be polishing a damaged audit file. Skipping the
                # fsync skips nothing else -- the fd below is still owned,
                # still claimed, and still closed.
                error = self._fsync_bundle(trace_file, artifacts, run_dir)
                if error is not None:
                    details.append(error)
            to_retire.append(bundle)
        # The writes are fsynced; now the drain releases each Run's fd and
        # retires the bundle, so nothing is held until sink.close().
        resources: list[
            tuple[
                ManagedFileLease | None,
                ManagedDirectoryLease | None,
                ManagedDirectoryLease | None,
            ]
        ] = []
        with self._wake:
            for bundle in to_retire:
                # Unpublished first: once the bundle is out of the map and
                # recorded as terminated, no commit thread can reach it or
                # the fd it referenced, so the close below cannot race a
                # write and cannot close a number the OS has already handed
                # to somebody else.
                self._bundles.pop(bundle.run_id, None)
                self._terminated.add(bundle.run_id)
                # Claimed under the bundle's lock (wake -> bundle.lock only),
                # so the bundle stops referencing the fd the drain is about
                # to close and a second claim gets nothing.
                resources.append(_take_bundle_resources(bundle))
        close_error = _close_bundle_resources(resources)
        if close_error is not None:
            details.append(_exception_detail(REASON_SINK_FAILED, close_error))
        barrier.answer(
            "; ".join(dict.fromkeys(details))[:200] if details else None, dropped
        )

    def _fsync_bundle(
        self,
        trace_file: ManagedFileLease,
        artifacts: ManagedDirectoryLease,
        run_dir: ManagedDirectoryLease,
    ) -> str | None:
        # ADR-0019 order: artifact files -> trace.jsonl -> artifacts/ -> Run dir.
        try:
            trace_file.fsync()
            artifacts.fsync()
            run_dir.fsync()
            return None
        except Exception as exc:
            return _exception_detail(REASON_FSYNC_FAILED, exc)

    def close(self, timeout: float | None = None) -> bool:
        """Ask the drain to stop and report whether its thread has exited.

        A timed-out join is retryable progress, not completion: the queue,
        bundles and descriptors stay owned by the same drain until it exits.
        """
        with self._wake:
            if not self._stopping:
                self._stopping = True
                # Waiters on barriers still queued must not hang on a drain
                # that a slow disk may keep busy indefinitely: answer them
                # now with the honest reason. The drain skips cancelled
                # barriers when it reaches them; queued events are still
                # written -- the queue and the fds stay with the drain.
                self._answer_queued_barriers_locked(REASON_SINK_CLOSED)
                self._wake.notify_all()
        wait = _CLOSE_JOIN_TIMEOUT_S if timeout is None else max(0.0, timeout)
        self._drain.join(timeout=wait)
        # No fd is closed here, even after a timed-out join: the drain thread
        # is the sole owner of every fd and may be inside os.write on one
        # right now. It closes what remains when it unwinds; as a non-daemon
        # owner it cannot be silently abandoned during interpreter shutdown.
        complete = not self._drain.is_alive()
        if complete and self._root_lease is not None:
            self._root_lease.close()
            self._root_lease = None
        return complete

    def close_with_timeout(self, timeout: float | None = None) -> bool:
        """TimedCloseSink capability used by FanOut without signature guessing."""
        return self.close(timeout=timeout)

    def _answer_queued_barriers_locked(self, reason: str) -> None:
        """Answer every barrier still in the queue that nobody has answered
        yet, and leave them in place: the drain skips an answered barrier
        when it reaches one. Caller holds the wake lock. Used wherever the
        only thread that could answer them is going away -- close(), and the
        drain's own unwind."""
        for item in self._queue:
            if isinstance(item, _WriteBarrier) and not item.done.is_set():
                item.answer(reason)

    def _release_remaining_fds(self) -> None:
        """Drain-thread unwind: close what no barrier retired."""
        with self._wake:
            bundles = list(self._bundles.values())
            self._bundles.clear()
        resources = [_take_bundle_resources(bundle) for bundle in bundles]
        _close_bundle_resources(resources)
