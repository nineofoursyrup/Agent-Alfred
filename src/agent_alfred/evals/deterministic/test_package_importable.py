"""Smoke test: the distribution is importable as agent_alfred."""

import agent_alfred


def test_package_importable() -> None:
    assert agent_alfred.__file__ is not None
