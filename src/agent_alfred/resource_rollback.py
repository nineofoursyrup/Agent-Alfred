"""Typed, resumable ownership for ordered resource rollback."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from itertools import starmap
from operator import call
from typing import Generic, NoReturn, Protocol, TypeVar, cast, runtime_checkable

_PROCESS_CONTROL = (KeyboardInterrupt, SystemExit, GeneratorExit)
_UNSET = object()
ResourceT = TypeVar("ResourceT")


def dominant_error(
    current: BaseException | None, candidate: BaseException | None,
) -> BaseException | None:
    """Keep the first failure, except that process control always wins."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    if not isinstance(current, _PROCESS_CONTROL) and isinstance(
        candidate, _PROCESS_CONTROL
    ):
        return candidate
    return current


@runtime_checkable
class CloseCompletion(Protocol):
    """Optional stable fact published before a close action returns."""

    def close_completed(self) -> bool:
        """Return whether the non-repeatable close effect completed."""
        ...


def thread_start_effect_happened(thread: object) -> bool:
    """Return whether an ambiguous ``Thread.start`` created a native thread.

    On the required CPython 3.14 runtime, ``Thread.start`` creates its
    ``_os_thread_handle`` before waiting for the child to publish ``_started``.
    Therefore ``join(0)`` is not an ownership probe in that window: it raises
    even though the native thread already exists. The handle's non-zero ident
    is the monotonic effect fact. Injected thread seams may expose an explicit
    ``start_effect_happened`` observer; the final join fallback exists only for
    legacy stand-ins whose contract matches ``Thread``. Any interrupted
    observation leaves the caller's tri-state unresolved and safely retryable.
    """
    observe = getattr(thread, "start_effect_happened", None)
    if observe is not None:
        return bool(observe())
    handle = getattr(thread, "_os_thread_handle", None)
    if handle is not None:
        return bool(handle.ident)
    join = getattr(thread, "join", None)
    if join is None:
        return False
    try:
        join(0)
    except RuntimeError:
        return False
    return True


def thread_exit_confirmed(thread: object, timeout: float | None) -> bool:
    """Wait for a start-effect thread without relying on ``_started``.

    The same CPython native handle remains joinable in the interval before
    ``Thread.join`` becomes legal. Its done bit is the monotonic completion
    fact. Explicit stand-ins use their ordinary join/is_alive contract.
    """
    handle = getattr(thread, "_os_thread_handle", None)
    if handle is not None and handle.ident:
        handle.join(timeout)
        return bool(handle.is_done())
    join = getattr(thread, "join", None)
    if join is None:
        return True
    join(timeout)
    is_alive = getattr(thread, "is_alive", None)
    return is_alive is None or not bool(is_alive())


def capture_call_result(
    receiver: object,
    attribute: str,
    operation: Callable[[], object],
) -> None:
    """Store one raw result without a caller-frame instruction boundary.

    ``starmap`` calls the operation and the outer ``map`` immediately feeds
    its result to the C ``setattr`` builtin. ``next`` drives both iterators in
    one C call. This closes only the caller's call-to-store edge; it cannot
    make an arbitrary Python operation's own ``PY_RETURN`` safe.
    """
    next(map(partial(setattr, receiver, attribute), starmap(operation, ((),))))


@dataclass
class _BackgroundCloseAttempt:
    """One retained worker and its exact completion result."""

    finished: threading.Event
    thread: threading.Thread | None = None
    thread_started: bool | None = None
    error: BaseException | None = None
    completion: Callable[[], bool] | None = None
    error_reported: bool = False


class BackgroundCloseStep:
    """Run one potentially blocking close action under a caller deadline.

    The concrete worker is published before it starts and remains reachable
    across every timeout and return edge. Concurrent callers wait on the same
    attempt; a failed action becomes retryable only after that worker exits.
    An action that is not safely repeatable may implement
    :class:`CloseCompletion`; a published completion fact retires an action
    whose return was interrupted without hiding that original exception.
    Other actions must remain idempotent across an ambiguous return. Workers
    are daemon threads because an honest bounded shutdown must not be turned
    back into an unbounded process exit by the action it isolated.
    """

    def __init__(self, name: str):
        self._name = name
        self._state_lock = threading.Lock()
        self._attempt: _BackgroundCloseAttempt | None = None

    @property
    def started(self) -> bool:
        with self._state_lock:
            return self._attempt is not None

    @property
    def completed(self) -> bool:
        with self._state_lock:
            attempt = self._attempt
            return (
                attempt is not None
                and attempt.thread_started is True
                and attempt.finished.is_set()
                and attempt.thread is not None
                and not attempt.thread.is_alive()
                and attempt.error is None
            )

    @property
    def thread(self) -> threading.Thread | None:
        with self._state_lock:
            attempt = self._attempt
            return None if attempt is None else attempt.thread

    def complete(self, action: Callable[[], None], timeout: float | None) -> bool:
        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, timeout)
        )
        with self._state_lock:
            attempt = self._attempt
            if attempt is not None and attempt.thread_started is not True:
                thread = attempt.thread
                assert thread is not None
                if attempt.thread_started is None:
                    attempt.thread_started = thread_start_effect_happened(
                        thread
                    )
                if attempt.thread_started is False:
                    if self._attempt is attempt:
                        self._attempt = None
                    attempt = None
            if attempt is None:
                completion = (
                    action.close_completed
                    if isinstance(action, CloseCompletion)
                    else None
                )
                attempt = _BackgroundCloseAttempt(
                    finished=threading.Event(), completion=completion
                )

                def run() -> None:
                    try:
                        action()
                    except BaseException as exc:  # noqa: BLE001 - owner retries
                        attempt.error = exc
                    finally:
                        attempt.finished.set()

                thread = threading.Thread(
                    target=run, name=self._name, daemon=True
                )
                attempt.thread = thread
                try:
                    self._attempt = attempt
                    thread.start()
                except BaseException as failure:
                    # A legal join proves the start effect happened. If start
                    # had no effect, retire this exact attempt so a retry may
                    # build another worker; otherwise it stays owned here. If
                    # the probe is interrupted, retain the unresolved identity
                    # so the next complete() call can ask the same safe probe.
                    try:
                        started = thread_start_effect_happened(thread)
                    except BaseException:
                        raise failure
                    attempt.thread_started = started
                    if not started and self._attempt is attempt:
                        self._attempt = None
                    raise failure
                attempt.thread_started = True
        if not attempt.finished.wait(self._remaining(deadline)):
            return False
        thread = attempt.thread
        assert thread is not None
        thread.join(self._remaining(deadline))
        if thread.is_alive():
            return False
        error = attempt.error
        if error is not None:
            published_complete = False
            completion = attempt.completion
            if completion is not None:
                try:
                    published_complete = completion()
                except BaseException:
                    # This observation cannot replace the close exception.
                    with self._state_lock:
                        if self._attempt is attempt:
                            attempt.error_reported = True
                    raise error
            if published_complete:
                with self._state_lock:
                    already_reported = attempt.error_reported
                    if self._attempt is attempt and attempt.error is error:
                        attempt.error = None
                        attempt.error_reported = True
                if already_reported:
                    return True
                raise error
            with self._state_lock:
                if self._attempt is attempt:
                    self._attempt = None
            raise error
        return True

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())


class OwnedDescriptor:
    """One FD number consumed before close so uncertain results are not retried."""

    def __init__(self, fd: int = -1) -> None:
        self.fd = fd

    @classmethod
    def open(
        cls,
        rollback: ResumableRollback,
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> OwnedDescriptor:
        """Open through the OS primitive after publishing an empty token."""
        descriptor = cls()
        rollback.own(descriptor)
        capture_call_result(
            descriptor,
            "fd",
            partial(os.open, path, flags, mode, dir_fd=dir_fd),
        )
        return descriptor

    @classmethod
    def duplicate(
        cls, rollback: ResumableRollback, fd: int
    ) -> OwnedDescriptor:
        """Duplicate through the OS primitive after publishing an empty token."""
        descriptor = cls()
        rollback.own(descriptor)
        capture_call_result(descriptor, "fd", partial(os.dup, fd))
        return descriptor

    def close(self) -> None:
        fd = self.fd
        if fd < 0:
            return
        _consume_descriptor(self, fd)


def _consume_descriptor(owner: OwnedDescriptor, fd: int) -> None:
    """Consume an fd number and invoke close without a Python boundary.

    A close error is necessarily uncertain: the kernel may already have
    released ``fd``, so retrying the number could close an unrelated recycled
    descriptor.  Conversely, publishing ``-1`` in one Python instruction and
    calling ``close`` in the next exposes a process-control edge that can leak
    the original descriptor.  ``deque`` drives these two C callables in one C
    loop: Python can interrupt before both operations, or observe the consumed
    token after the close attempt, but never the gap between them.
    """
    deque(
        starmap(
            call,
            (
                (setattr, owner, "fd", -1),
                (os.close, fd),
            ),
        ),
        maxlen=0,
    )


class OwnedResource(Generic[ResourceT]):
    """Pre-owned holder for one closeable result crossing a call boundary."""

    def __init__(
        self, close: Callable[[ResourceT], bool | None] | None = None
    ) -> None:
        self._resource: ResourceT | object = _UNSET
        self._close = close
        self._close_result: bool | None | object = _UNSET

    @classmethod
    def acquire(
        cls,
        rollback: ResumableRollback,
        initialize: Callable[[OwnedResource[ResourceT]], object],
        *,
        close: Callable[[ResourceT], bool | None] | None = None,
    ) -> ResourceT:
        """Let a factory initialize a holder that is already rollback-owned.

        A Python factory cannot safely return a newly created resource: its
        own ``PY_RETURN`` is an interruptible boundary before this caller can
        see the result. The initializer therefore receives this holder and
        must :meth:`publish` the resource before it returns.
        """
        owner = cls(close)
        rollback.own(owner)
        initialize(owner)
        resource = owner.resource
        rollback.replace(owner, resource)
        return resource

    def publish(self, resource: ResourceT) -> None:
        """Publish a factory result into its pre-existing rollback owner."""
        if self._resource is not _UNSET:
            raise RuntimeError("owned resource has already been acquired")
        self._resource = resource

    def capture_c_result(self, opener: Callable[[], ResourceT]) -> None:
        """Capture one caller-vetted C-level operation result, including None.

        The caller names this stronger boundary only for a concrete primitive;
        arbitrary Python factories must use :meth:`publish` from inside their
        own frame. Tests may wrap the primitive to observe its effect without
        changing the production seam being asserted.
        """
        if self._resource is not _UNSET:
            raise RuntimeError("owned resource has already been acquired")
        capture_call_result(self, "_resource", opener)

    @property
    def resource(self) -> ResourceT:
        resource = self._resource
        if resource is _UNSET:
            raise RuntimeError("owned resource has not been acquired")
        return cast(ResourceT, resource)

    def close(self) -> bool | None:
        resource = self._resource
        if resource is _UNSET:
            return None
        captured = cast(ResourceT, resource)
        if self._close_result is _UNSET:
            operation = (
                getattr(captured, "close")
                if self._close is None
                else partial(self._close, captured)
            )
            capture_call_result(self, "_close_result", operation)
        closed = self._close_result
        if closed is False:
            # False is an explicit refusal, not a completed close. Re-arm the
            # operation for the next retry while retaining the resource.
            self._close_result = _UNSET
            return False
        if closed is not False:
            self._resource = _UNSET
        return cast(bool | None, closed)

    def close_completed(self) -> bool:
        """Observe a published underlying close without advancing it."""
        result = self._close_result
        if result is not _UNSET:
            return result is not False
        resource = self._resource
        if resource is _UNSET:
            return True
        if isinstance(resource, CloseCompletion):
            return resource.close_completed()
        return False


@dataclass
class _RollbackStep:
    resource: object
    close: Callable[[], bool | None]
    completion: Callable[[], bool] | None = None
    completed: bool = False
    _observing: bool = False
    _result: bool | None | object = _UNSET

    def retry(self) -> bool:
        """Run or settle one close without repeating a returned success."""
        if self.completed:
            return True
        if self._observing:
            completion = self.completion
            assert completion is not None
            if completion():
                self.completed = True
                return True
            self._observing = False
            self._result = _UNSET
        if self._result is _UNSET:
            if self.completion is not None:
                self._observing = True
            try:
                capture_call_result(self, "_result", self.close)
            except BaseException:
                if self.completion is None:
                    self._observing = False
                raise
        result = self._result
        if result is False:
            self._observing = False
            self._result = _UNSET
            return False
        self.completed = True
        self._observing = False
        return True


@dataclass(frozen=True)
class _RetryResult:
    complete: bool
    errors: tuple[BaseException, ...]
    process_control: BaseException | None


class ResumableRollback:
    """Own reverse-order cleanup, exact progress, and failure normalization."""

    def __init__(self) -> None:
        self._steps: list[_RollbackStep] = []
        self.errors: tuple[BaseException, ...] = ()
        self.process_control: BaseException | None = None
        self._incomplete: IncompleteRollback | None = None

    def own(
        self,
        resource: object,
        close: Callable[[], bool | None] | None = None,
    ) -> None:
        """Own one release that is idempotent or publishes completion.

        A Python close can be interrupted at its own return after performing
        the effect. Non-repeatable resources therefore implement
        :class:`CloseCompletion`; every other close must be mechanically safe
        to retry after an ambiguous return.
        """
        if any(step.resource is resource for step in self._steps):
            return
        action = close or getattr(resource, "close")
        completion_source = (
            resource
            if isinstance(resource, CloseCompletion)
            else action
            if isinstance(action, CloseCompletion)
            else None
        )
        completion = (
            None
            if completion_source is None
            else completion_source.close_completed
        )
        self._steps.append(
            _RollbackStep(resource, action, completion=completion)
        )

    def transfer(self, resource: object) -> None:
        for step in self._steps:
            if step.resource is resource:
                step.completed = True
                return
        raise RuntimeError("resource is not owned by this rollback")

    def replace(self, resource: object, replacement: object) -> None:
        """Change one step's public identity without changing its owner."""
        if any(
            step.resource is replacement and not step.completed
            for step in self._steps
        ):
            raise RuntimeError("replacement is already owned by this rollback")
        for step in self._steps:
            if step.resource is resource:
                step.resource = replacement
                return
        raise RuntimeError("resource is not owned by this rollback")

    def transfer_many_to(
        self, target: ResumableRollback, resources: tuple[object, ...]
    ) -> None:
        """Move cleanup progress into an empty owner without an ownership gap.

        A non-empty target cannot preserve one global reverse order during a
        partial handoff, so it is rejected before either owner changes.
        Resources move in reverse close order. Each destination insertion
        precedes its source removal, so interruption can temporarily leave two
        references to the same progress step, never two independent owners --
        and a partial handoff still closes newest-first when the destination
        is settled before the source.
        """
        if target is self:
            return
        if any(not step.completed for step in target._steps):
            raise RuntimeError("target rollback must be empty")
        selected: list[_RollbackStep] = []
        for resource in resources:
            step = next(
                (
                    candidate
                    for candidate in self._steps
                    if candidate.resource is resource
                ),
                None,
            )
            if step is None:
                raise RuntimeError("resource is not owned by this rollback")
            selected.append(step)
        for step in reversed(selected):
            target._steps.insert(0, step)
            self._steps.remove(step)

    def retry(self) -> bool:
        errors: list[BaseException] = []
        process_control: BaseException | None = None
        complete = True
        for step in reversed(self._steps):
            if step.completed:
                continue
            try:
                step_complete = step.retry()
            except BaseException as exc:
                errors.append(exc)
                complete = False
                if process_control is None and isinstance(exc, _PROCESS_CONTROL):
                    process_control = exc
                break
            if not step_complete:
                complete = False
                break
        self.errors = tuple(errors)
        self.process_control = process_control
        if complete and self._incomplete is not None:
            self._incomplete.restore_cause()
            self._incomplete = None
        return complete

    def retry_propagating(self) -> bool:
        """Continue cleanup, but never reduce process control to ``False``."""
        complete = self.retry()
        control = self.process_control
        if control is not None:
            wrapper = self._incomplete
            if wrapper is None:
                self._bind_incomplete(control, control)
            else:
                wrapper.rebind(control)
            raise control
        return complete

    def _retry_owned(self, results: dict[int, _RetryResult]) -> _RetryResult:
        """Retry once per aggregate pass and replay that exact result."""
        identity = id(self)
        cached = results.get(identity)
        if cached is not None:
            self.errors = cached.errors
            self.process_control = cached.process_control
            return _RetryResult(cached.complete, (), None)
        complete = self.retry()
        result = _RetryResult(complete, self.errors, self.process_control)
        results[identity] = result
        return result

    def owns(self, owner: ResumableRollback | RollbackSlot) -> bool:
        return owner is self

    def raise_failure(self, failure: BaseException) -> NoReturn:
        cause = failure.__cause__
        if isinstance(cause, IncompleteRollback) and cause.owner is self:
            raise failure
        complete = self.retry()
        propagated = self.process_control or failure
        if not complete:
            self._bind_incomplete(propagated, failure)
        raise propagated

    def raise_incomplete(self, failure: BaseException) -> NoReturn:
        """Propagate the already-attempted, still-incomplete cleanup."""
        propagated = self.process_control or failure
        self._bind_incomplete(propagated, failure)
        raise propagated

    def close(self) -> None:
        if self.retry():
            return
        failure = self.process_control
        if failure is None:
            failure = next(
                iter(self.errors), RuntimeError("resource cleanup incomplete")
            )
        self._bind_incomplete(failure, failure)
        raise failure

    def _bind_incomplete(
        self, propagated: BaseException, failure: BaseException
    ) -> None:
        cause = propagated.__cause__
        if isinstance(cause, IncompleteRollback) and cause.owner is self:
            return
        wrapper = IncompleteRollback(
            owner=self,
            failure=failure,
            propagated=propagated,
            restored_cause=propagated.__cause__,
            wrapper_cause=None,
        )
        wrapper.bind(propagated)
        self._incomplete = wrapper


class ConstructionOwner:
    """The one rollback a construction seam builds in, offered or its own.

    Two facts recur wherever a seam mints a resource and hands it back:

    - the finished aggregate must be owned *before* the parts it took over
      retire, or an asynchronous exception at the return edge leaves them
      with no reachable owner. The overlap is safe because an aggregate here
      closes through the very tokens its parts hold, so each resource is
      still closed exactly once;
    - a caller that offered its own rollback keeps the aggregate owned across
      this seam's return and its own store. A seam nobody offered one to has
      no owner to outlive it, so it retires the aggregate itself.

    ``ConstructionOwner`` is where those two facts live, instead of being
    restated at every seam that mints something.
    """

    def __init__(self, offered: ResumableRollback | None = None) -> None:
        self._offered = offered
        self.rollback = offered if offered is not None else ResumableRollback()

    def publish(
        self,
        aggregate: object,
        *,
        parts: tuple[object | None, ...] = (),
        close: Callable[[], bool | None] | None = None,
    ) -> None:
        """Own the finished aggregate, then retire the parts it took over."""
        self.rollback.own(aggregate, close)
        for part in parts:
            if part is not None:
                self.rollback.transfer(part)
        if self._offered is None:
            self.rollback.transfer(aggregate)

    def fail(self, failure: BaseException) -> NoReturn:
        """Unwind this seam's construction, preserving its first failure."""
        self.rollback.raise_failure(failure)


class IncompleteRollback(RuntimeError):
    """Reachable typed handle for continuing one interrupted rollback."""

    def __init__(
        self,
        *,
        owner: ResumableRollback | RollbackSlot,
        failure: BaseException,
        propagated: BaseException,
        restored_cause: BaseException | None,
        wrapper_cause: BaseException | None,
    ) -> None:
        super().__init__("resource rollback is incomplete; retry cleanup")
        self.owner = owner
        self.failure = failure
        self._propagated = propagated
        self._restored_cause = restored_cause
        self.__cause__ = wrapper_cause

    @property
    def errors(self) -> tuple[BaseException, ...]:
        return self.owner.errors

    def retry(self) -> bool:
        return self.owner.retry_propagating()

    def rebind(self, propagated: BaseException) -> None:
        """Move this same progress handle onto a newly dominant control signal."""
        self.restore_cause()
        self.bind(propagated)

    def bind(self, propagated: BaseException) -> None:
        """Attach this handle without introducing a cycle in the error graph."""
        restored_cause = _cause_outside_owner(
            propagated.__cause__, owner=self.owner
        )
        self._propagated = propagated
        self._restored_cause = restored_cause
        wrapper_cause = self.failure
        if (
            wrapper_cause is self
            or _exception_graph_contains(wrapper_cause, propagated)
        ):
            wrapper_cause = restored_cause
        if (
            wrapper_cause is propagated
            or wrapper_cause is self
            or _exception_graph_contains(wrapper_cause, propagated)
        ):
            wrapper_cause = None
        self.__cause__ = wrapper_cause
        propagated.__cause__ = self

    def restore_cause(self) -> None:
        if self._propagated.__cause__ is self:
            self._propagated.__cause__ = self._restored_cause


class RollbackSlot:
    """Retain an assembly rollback owner until its cleanup reaches completion."""

    def __init__(self) -> None:
        self._pending: list[ResumableRollback | RollbackSlot] = []
        self.errors: tuple[BaseException, ...] = ()
        self.process_control: BaseException | None = None
        self._failure: BaseException | None = None
        self._incomplete: IncompleteRollback | None = None

    def begin(self, owner: ResumableRollback | RollbackSlot) -> None:
        if owner is self or owner.owns(self) or self.owns(owner):
            return
        self._pending = [
            pending for pending in self._pending if not owner.owns(pending)
        ]
        self._pending.append(owner)

    @property
    def settled(self) -> bool:
        """Whether no cleanup owner remains in this slot."""
        return not self._pending

    def complete(self, owner: ResumableRollback | RollbackSlot) -> None:
        self._pending = [pending for pending in self._pending if pending is not owner]

    def settle(self) -> None:
        """Retire the construction owners whose aggregate the caller stored.

        Assembly cannot retire its own owner: at its return edge the assembled
        object is still a value nobody has stored. The publisher confirms that
        store instead, which is what lets one reachable owner span the return
        and the assignment that follows it.
        """
        for owner in tuple(self._pending):
            self.complete(owner)

    def capture_failure(self, failure: BaseException) -> None:
        """Retain every nested rollback handle reachable from a failure."""
        if self._failure is None:
            self._failure = failure
        seen: set[int] = set()
        pending = [failure]
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, IncompleteRollback):
                self.begin(current.owner)
            if current.__context__ is not None:
                pending.append(current.__context__)
            if current.__cause__ is not None:
                pending.append(current.__cause__)

    def raise_failure(self, failure: BaseException) -> NoReturn:
        """Unwind every independent owner while preserving the first cause."""
        self.capture_failure(failure)
        complete = self._retry(propagate_control=False, results={})
        propagated = self.process_control or failure
        if not complete:
            if self._incomplete is None:
                self._bind_incomplete(propagated, failure)
            else:
                self._incomplete.rebind(propagated)
        raise propagated

    def close(self) -> None:
        """Settle every independent owner or expose resumable progress."""
        if self.retry():
            return
        failure = next(
            iter(self.errors), RuntimeError("resource cleanup incomplete")
        )
        if self._incomplete is None:
            self._bind_incomplete(failure, failure)
        else:
            self._incomplete.rebind(failure)
        raise failure

    def retry(self) -> bool:
        return self._retry(propagate_control=True, results={})

    def _retry_owned(self, results: dict[int, _RetryResult]) -> _RetryResult:
        """Retry under a parent slot, leaving control aggregation to it."""
        identity = id(self)
        cached = results.get(identity)
        if cached is not None:
            self.errors = cached.errors
            self.process_control = cached.process_control
            return _RetryResult(cached.complete, (), None)
        complete = self._retry(propagate_control=False, results=results)
        result = _RetryResult(complete, self.errors, self.process_control)
        results[identity] = result
        return result

    def _retry(
        self, *, propagate_control: bool, results: dict[int, _RetryResult]
    ) -> bool:
        if not self._pending:
            if self._incomplete is not None:
                self._incomplete.restore_cause()
                self._incomplete = None
            self.errors = ()
            self.process_control = None
            self._failure = None
            return True
        complete = True
        control: BaseException | None = None
        errors: list[BaseException] = []
        for owner in reversed(tuple(self._pending)):
            try:
                result = owner._retry_owned(results)
            except BaseException as exc:
                owner_complete = False
                errors.append(exc)
                if control is None and isinstance(exc, _PROCESS_CONTROL):
                    control = exc
            else:
                owner_complete = result.complete
                errors.extend(result.errors)
                if control is None and result.process_control is not None:
                    control = result.process_control
            if owner_complete:
                self.complete(owner)
            else:
                complete = False
        self.errors = tuple(errors)
        self.process_control = control
        if control is not None and propagate_control:
            failure = self._failure or control
            if self._incomplete is None:
                self._bind_incomplete(control, failure)
            else:
                self._incomplete.rebind(control)
            raise control
        if complete:
            if self._incomplete is not None:
                self._incomplete.restore_cause()
                self._incomplete = None
            self._failure = None
        return complete

    def retry_propagating(self) -> bool:
        return self.retry()

    def owns(self, owner: ResumableRollback | RollbackSlot) -> bool:
        return owner is self or any(pending.owns(owner) for pending in self._pending)

    def _bind_incomplete(
        self, propagated: BaseException, failure: BaseException
    ) -> None:
        wrapper = IncompleteRollback(
            owner=self,
            failure=failure,
            propagated=propagated,
            restored_cause=propagated.__cause__,
            wrapper_cause=None,
        )
        wrapper.bind(propagated)
        self._incomplete = wrapper


def _exception_graph_contains(
    root: BaseException | None, target: BaseException
) -> bool:
    seen: set[int] = set()
    pending = [root] if root is not None else []
    while pending:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return False


def _cause_outside_owner(
    cause: BaseException | None,
    *,
    owner: ResumableRollback | RollbackSlot,
) -> BaseException | None:
    """Skip retry handles already covered by the owner being attached."""
    seen: set[int] = set()
    while isinstance(cause, IncompleteRollback) and owner.owns(cause.owner):
        if id(cause) in seen:
            return None
        seen.add(id(cause))
        cause = cause._restored_cause
    return cause
