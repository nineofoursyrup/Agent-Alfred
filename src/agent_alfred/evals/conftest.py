"""Eval hooks: keyed tests skip when the named environment variable is unset."""

from __future__ import annotations

import os

import pytest


def pytest_runtest_setup(item: pytest.Item) -> None:
    marker = item.get_closest_marker("requires_key")
    if marker is None:
        return
    names = [str(arg) for arg in marker.args]
    if not names:
        pytest.skip(
            "requires_key: name the environment variable(s) this test needs"
        )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip("missing environment variable(s): " + ", ".join(missing))
