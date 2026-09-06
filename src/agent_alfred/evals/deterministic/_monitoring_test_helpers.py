"""Exception-safe ownership for CPython monitoring tools used by tests."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import CodeType, FrameType


@contextmanager
def claimed_monitoring_tool(
    name: str, *, local_codes: tuple[CodeType, ...] = ()
) -> Iterator[int]:
    """Claim one tool ID and release all of its monitoring state on exit."""
    tool_id = next(
        candidate
        for candidate in range(6)
        if sys.monitoring.get_tool(candidate) is None
    )
    body_completed = False
    use_returned = False
    try:
        sys.monitoring.use_tool_id(tool_id, name)
        use_returned = True
        yield tool_id
        body_completed = True
    finally:
        body_failure = None if body_completed else sys.exception()
        cleanup_failure: BaseException | None = None
        owns_tool = use_returned
        if not owns_tool:
            for _attempt in range(2):
                try:
                    owns_tool = sys.monitoring.get_tool(tool_id) == name
                except BaseException as exc:  # noqa: BLE001
                    if cleanup_failure is None:
                        cleanup_failure = exc
                    continue
                break
        if owns_tool:
            for code in local_codes:
                try:
                    sys.monitoring.set_local_events(
                        tool_id, code, sys.monitoring.events.NO_EVENTS
                    )
                except BaseException as exc:  # noqa: BLE001
                    if cleanup_failure is None:
                        cleanup_failure = exc
            try:
                sys.monitoring.clear_tool_id(tool_id)
            except BaseException as exc:  # noqa: BLE001
                if cleanup_failure is None:
                    cleanup_failure = exc
            try:
                sys.monitoring.free_tool_id(tool_id)
            except BaseException as exc:  # noqa: BLE001
                if cleanup_failure is None:
                    cleanup_failure = exc
        if cleanup_failure is not None:
            if body_failure is None:
                raise cleanup_failure
            body_failure.add_note(
                f"monitoring cleanup also failed: {cleanup_failure!r}"
            )


@contextmanager
def interrupt_instruction_once(
    code: CodeType, offset: int, failure: BaseException, *, occurrence: int = 1,
    when: Callable[[FrameType], bool] | None = None,
) -> Iterator[list[bool]]:
    """Raise once at the selected occurrence of one exact instruction."""
    armed = [True]
    remaining = occurrence
    with claimed_monitoring_tool(
        "instruction-boundary-test", local_codes=(code,)
    ) as tool_id:

        def interrupt(actual_code: CodeType, actual_offset: int) -> None:
            nonlocal remaining
            if armed[0] and actual_code is code and actual_offset == offset:
                if when is not None and not when(sys._getframe(1)):
                    return
                remaining -= 1
                if remaining == 0:
                    armed[0] = False
                    raise failure

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.INSTRUCTION, interrupt
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.INSTRUCTION
        )
        yield armed


@contextmanager
def interrupt_py_return_once(
    name: str, code: CodeType, failure: BaseException
) -> Iterator[list[bool]]:
    """Raise once from one function's exact ``PY_RETURN`` event."""
    armed = [True]
    with claimed_monitoring_tool(name, local_codes=(code,)) as tool_id:

        def interrupt(
            actual_code: CodeType, offset: int, result: object
        ) -> None:
            del offset, result
            if armed[0] and actual_code is code:
                armed[0] = False
                raise failure

        sys.monitoring.register_callback(
            tool_id, sys.monitoring.events.PY_RETURN, interrupt
        )
        sys.monitoring.set_local_events(
            tool_id, code, sys.monitoring.events.PY_RETURN
        )
        yield armed
