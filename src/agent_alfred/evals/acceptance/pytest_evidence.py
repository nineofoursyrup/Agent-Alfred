"""Opt-in pytest evidence recorder: collection and every execution phase."""

import json
from pathlib import Path

_state = None


def pytest_addoption(parser):
    parser.addoption("--acceptance-log", default=None)


def pytest_configure(config):
    global _state
    if config.getoption("--acceptance-log"):
        _state = {
            "collection": [],
            "deselected": [],
            "outcomes": {},
            "requires_key_ids": [],
            "phases": {},
        }


def pytest_collection_finish(session):
    if _state is not None:
        _state["collection"] = [item.nodeid for item in session.items]
        _state["requires_key_ids"] = [
            item.nodeid
            for item in session.items
            if item.get_closest_marker("requires_key")
        ]


def pytest_deselected(items):
    if _state is not None:
        _state["deselected"].extend(item.nodeid for item in items)


def pytest_runtest_logreport(report):
    if _state is not None:
        outcome = "xfailed" if hasattr(report, "wasxfail") else report.outcome
        _state["phases"].setdefault(report.nodeid, {})[report.when] = outcome
        _state["outcomes"].setdefault(report.nodeid, []).append(outcome)


def pytest_sessionfinish(session, exitstatus):
    if _state is not None:
        _state["exit_code"] = int(exitstatus)
        Path(session.config.getoption("--acceptance-log")).write_text(
            json.dumps(_state, indent=2),
            encoding="utf-8",
        )
