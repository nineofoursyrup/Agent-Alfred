"""Partial independent readback cannot establish a global first-fault order."""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from .test_controlled_persistence import durable_fixture, reopen


@contextmanager
def unavailable(backend, reason):
    def fail(*args, **kwargs):
        raise ValueError(reason)

    with ExitStack() as stack:
        for method in ("read", "scan", "transaction"):
            stack.enter_context(patch.object(backend, method, fail))
        yield


def test_partial_fault_inventory_does_not_name_a_later_fault_as_first(tmp_path):
    f = durable_fixture(tmp_path)
    f["authority"].submit_job(f["submission"])
    try:
        with unavailable(f["anchor"].backend, "first_anchor_outage"):
            with pytest.raises(ValueError, match="first_anchor_outage"):
                f["authority"].status("controlled-job")
        auth = reopen(f, tmp_path)
        with unavailable(f["ledger"].backend, "later_store_outage"):
            report = auth.stop_report("controlled-job")
        assert report["first_fault_candidates"]
        assert report["first_fault_order_verified"] is False
        assert report["first_stop_reason"] == "fault_order_unverifiable"
        assert report["ledger_and_anchor_verified"] is False
        assert report["send_accounting"] is None
        assert not f["sends"]
    finally:
        f["ledger"].close()
        f["anchor"].close()


def test_partial_fault_inventory_preserves_the_previously_saved_stop(tmp_path):
    f = durable_fixture(tmp_path)
    auth = f["authority"]
    auth.submit_job(f["submission"])
    try:
        auth.revoke("controlled-job", principal="simulation:controller")
        first_stop = auth.status("controlled-job")["state"]["stop_reason"]
        with unavailable(f["anchor"].backend, "later_anchor_outage"):
            report = auth.stop_report("controlled-job")
        assert report["first_stop_reason"] == first_stop
        assert report["first_fault_order_verified"] is False
        assert report["blockers"] and report["first_fault_candidates"]
        assert not f["sends"]
    finally:
        f["ledger"].close()
        f["anchor"].close()
