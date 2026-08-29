"""Helpers the trace sink tests share.

Both of these were previously duplicated verbatim in
``test_trace_bundle_integrity.py`` and ``test_trace_sink.py``. The value of
each one is that it measures the same thing every time it is used, so two
copies were two chances to drift -- a difference in probe order or in how
the hook is restored would silently change what one module asserts relative
to the other. One definition, one behaviour.

Deliberately not named ``test_*``: pytest must not collect this as a test
module. It holds no tests, only the two helpers below.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator

import pytest


def _open_fd_count() -> int:
    """Count the file descriptors this process currently holds.

    The two paths are tried in this order because neither is universal:
    ``/dev/fd`` is the BSD/macOS spelling, ``/proc/self/fd`` the Linux one.
    Where a platform has both they name the same table, so the order decides
    nothing about the count -- it only decides which spelling is preferred.

    Skips the calling test where neither exists rather than inventing a
    platform probe; a silent zero here would read as "no fd grew" and turn
    the leak assertions into no-ops.
    """
    for candidate in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(candidate))
        except OSError:
            continue
    pytest.skip("no /dev/fd or /proc/self/fd on this platform")


@contextlib.contextmanager
def _captured_thread_exception() -> Iterator[dict[str, object]]:
    """Make an expected thread crash observable instead of noisy.

    The drain is meant to die loudly on an invariant break. Left to the
    default hook that becomes ``PytestUnhandledThreadExceptionWarning`` noise
    that hides real ones, so the test installs its own hook for the duration
    and puts pytest's back afterwards.

    Yields the dict a dying thread fills in as ``exc_type`` / ``exc_value``.
    The previous hook is restored in a ``finally``, so a failure inside the
    block still puts it back -- the capture is scoped to this block and
    never becomes a process-wide suppression of thread crashes.
    """
    captured: dict[str, object] = {}
    previous = threading.excepthook

    def hook(args) -> None:
        captured["exc_type"] = args.exc_type
        captured["exc_value"] = args.exc_value

    threading.excepthook = hook
    try:
        yield captured
    finally:
        threading.excepthook = previous
