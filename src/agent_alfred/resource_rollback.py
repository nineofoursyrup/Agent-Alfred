"""Typed, resumable ownership for ordered resource rollback."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

_PROCESS_CONTROL = (KeyboardInterrupt, SystemExit, GeneratorExit)


class OwnedDescriptor:
    """One FD number consumed before close so uncertain results are not retried."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)


@dataclass
class _RollbackStep:
    resource: object
    close: Callable[[], bool | None]
    completed: bool = False


@dataclass(frozen=True)
class _RetryResult:
    complete: bool
    errors: tuple[BaseException, ...]
    process_control: BaseException | None
    error_contributions: tuple[BaseException, ...]
    control_contribution: BaseException | None


class ResumableRollback:
    """Own reverse-order cleanup, exact progress, and failure normalization."""

    def __init__(self) -> None:
        self._steps: list[_RollbackStep] = []
        self.errors: tuple[BaseException, ...] = ()
        self.process_control: BaseException | None = None
        self._error_contributions: tuple[BaseException, ...] = ()
        self._control_contribution: BaseException | None = None
        self._incomplete: IncompleteRollback | None = None

    def own(
        self,
        resource: object,
        close: Callable[[], bool | None] | None = None,
    ) -> None:
        if any(step.resource is resource for step in self._steps):
            return
        self._steps.append(_RollbackStep(resource, close or getattr(resource, "close")))

    def transfer(self, resource: object) -> None:
        for step in self._steps:
            if step.resource is resource:
                step.completed = True
                return

    def retry(self) -> bool:
        errors: list[BaseException] = []
        process_control: BaseException | None = None
        complete = True
        for step in reversed(self._steps):
            if step.completed:
                continue
            try:
                closed = step.close()
            except BaseException as exc:
                errors.append(exc)
                complete = False
                if process_control is None and isinstance(exc, _PROCESS_CONTROL):
                    process_control = exc
                continue
            if closed is False:
                complete = False
                continue
            step.completed = True
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

    def _retry_owned(self, results: dict[int, _RetryResult]) -> bool:
        """Retry once per aggregate pass and replay that exact result."""
        identity = id(self)
        cached = results.get(identity)
        if cached is not None:
            self.errors = cached.errors
            self.process_control = cached.process_control
            self._error_contributions = ()
            self._control_contribution = None
            return cached.complete
        complete = self.retry()
        self._error_contributions = self.errors
        self._control_contribution = self.process_control
        results[identity] = _RetryResult(
            complete,
            self.errors,
            self.process_control,
            self._error_contributions,
            self._control_contribution,
        )
        return complete

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
        self._error_contributions: tuple[BaseException, ...] = ()
        self._control_contribution: BaseException | None = None
        self._failure: BaseException | None = None
        self._incomplete: IncompleteRollback | None = None

    def begin(self, owner: ResumableRollback | RollbackSlot) -> None:
        if owner is self or owner.owns(self) or self.owns(owner):
            return
        self._pending = [
            pending for pending in self._pending if not owner.owns(pending)
        ]
        self._pending.append(owner)

    def complete(self, owner: ResumableRollback | RollbackSlot) -> None:
        self._pending = [pending for pending in self._pending if pending is not owner]

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

    def retry(self) -> bool:
        return self._retry(propagate_control=True, results={})

    def _retry_owned(self, results: dict[int, _RetryResult]) -> bool:
        """Retry under a parent slot, leaving control aggregation to it."""
        identity = id(self)
        cached = results.get(identity)
        if cached is not None:
            self.errors = cached.errors
            self.process_control = cached.process_control
            self._error_contributions = ()
            self._control_contribution = None
            return cached.complete
        complete = self._retry(propagate_control=False, results=results)
        results[identity] = _RetryResult(
            complete,
            self.errors,
            self.process_control,
            self._error_contributions,
            self._control_contribution,
        )
        return complete

    def _retry(
        self, *, propagate_control: bool, results: dict[int, _RetryResult]
    ) -> bool:
        if not self._pending:
            if self._incomplete is not None:
                self._incomplete.restore_cause()
                self._incomplete = None
            self.errors = ()
            self.process_control = None
            self._error_contributions = ()
            self._control_contribution = None
            self._failure = None
            return True
        complete = True
        control: BaseException | None = None
        errors: list[BaseException] = []
        for owner in reversed(tuple(self._pending)):
            try:
                owner_complete = owner._retry_owned(results)
            except BaseException as exc:
                owner_complete = False
                errors.append(exc)
                if control is None and isinstance(exc, _PROCESS_CONTROL):
                    control = exc
            else:
                errors.extend(owner._error_contributions)
                if control is None and owner._control_contribution is not None:
                    control = owner._control_contribution
            if owner_complete:
                self.complete(owner)
            else:
                complete = False
        self.errors = tuple(errors)
        self.process_control = control
        self._error_contributions = self.errors
        self._control_contribution = control
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
