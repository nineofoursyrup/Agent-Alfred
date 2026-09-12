"""Financial consistency, legacy coverage, quotas and calendar boundaries."""

import json
from decimal import Decimal

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_runtime import _host
from agent_alfred.messages import TextBlock
from agent_alfred.model import (
    AttemptRecord,
    ModelRef,
    ModelResponse,
    ModelResult,
    ScriptedModel,
    ScriptedModelFactory,
    Usage,
)
from agent_alfred.pricing import PriceQuote
from agent_alfred.runtime.accounting import AccountingError
from agent_alfred.runtime.host import SubmitRequest


def test_mixed_attempt_costs_and_prices_stay_frozen():
    class Prices:
        price = Decimal("1")

        def quote(self, e, m, d):
            return PriceQuote(self.price, "catalog")

    prices = Prices()

    class Factory(ScriptedModelFactory):
        def catalog_prices(self):
            return prices

    result = ModelResult(
        (
            AttemptRecord(
                "retry",
                False,
                "aborted",
                Usage(endpoint_reported_cost_usd=Decimal("1.25")),
            ),
            AttemptRecord(
                "estimate",
                False,
                "aborted",
                Usage(uncached_input_tokens=1000, output_tokens=2000),
                model=ModelRef("fixture", "m"),
            ),
            AttemptRecord("unknown", False, "committed", Usage(output_tokens=0)),
        ),
        ModelResponse((TextBlock("done"),), "end_turn", ModelRef("fixture", "m")),
        None,
    )
    host, _, _ = _host(factory=Factory(ScriptedModel([GATE, result])))
    host.start()
    try:
        identity = host.submit(SubmitRequest(message="Test cost")).run_id
        host.wait(identity)
        view = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert view["summary"]["exact_usd"] == "1.25"
        assert view["summary"]["estimated_usd"] == "0.003"
        assert view["summary"]["unknown_cost_attempts"] == 2  # gate and final attempt
        assert view["summary"]["attempt_count"] == 4
        assert view["summary"]["tokens"]["output_tokens"] == {
            "known": 2000,
            "missing_attempts": 2,
        }
        prices.price = Decimal("10")
        assert (
            host.accounting_detail(view["snapshot_id"], identity)["summary"][
                "estimated_usd"
            ]
            == "0.003"
        )
        new = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert new["summary"]["estimated_usd"] == "0.03"
        assert new["price_version"] != view["price_version"]
    finally:
        host.close()


def test_quota_expiry_and_returned_objects_cannot_mutate_snapshot(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, _, clock):
        identity, _ = run(host)
        views = [
            host.accounting_snapshot({"range": "all", "timezone": "UTC"})
            for _ in range(8)
        ]
        with pytest.raises(AccountingError, match="snapshot_quota"):
            host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        detail = host.accounting_detail(views[0]["snapshot_id"], identity)
        detail["run"]["attempts"].clear()
        assert host.accounting_detail(views[0]["snapshot_id"], identity)["run"][
            "attempts"
        ]
        clock.monotonic_value += 901
        with pytest.raises(AccountingError, match="snapshot_expired"):
            host.accounting_page(views[0]["snapshot_id"])
        assert (
            host.accounting_snapshot({"range": "all", "timezone": "UTC"})["summary"][
                "run_count"
            ]
            == 1
        )


@pytest.mark.parametrize(
    "start,end,expected_start,expected_end",
    [
        (
            "2026-03-08",
            "2026-03-09",
            "2026-03-08T05:00:00+00:00",
            "2026-03-09T04:00:00+00:00",
        ),
        (
            "2026-11-01",
            "2026-11-02",
            "2026-11-01T04:00:00+00:00",
            "2026-11-02T05:00:00+00:00",
        ),
    ],
)
def test_custom_calendar_boundaries_cover_dst(
    tmp_path, start, end, expected_start, expected_end
):
    with ops_host(tmp_path, []) as (host, _, _, _):
        view = host.accounting_snapshot(
            {
                "range": "custom",
                "timezone": "America/New_York",
                "start": start,
                "end": end,
            }
        )
        assert view["filters"]["start"] == expected_start
        assert view["filters"]["end"] == expected_end
        with pytest.raises(AccountingError, match="invalid_timezone"):
            host.accounting_snapshot({"range": "all", "timezone": "nonsense"})


def test_legacy_bad_row_is_isolated_and_exact_cost_without_model_survives(tmp_path):
    with ops_host(tmp_path, [GATE, "first", GATE, "second"]) as (host, _, conn, _):
        first, _ = run(host)
        second, _ = run(host)
        conn.execute("UPDATE runs SET telemetry=? WHERE run_id=?", ("[]", first))
        conn.execute(
            "UPDATE runs SET telemetry=? WHERE run_id=?",
            (
                json.dumps(
                    {
                        "attempts": [
                            {
                                "attempt_id": "legacy",
                                "outcome": "committed",
                                "usage": {"endpoint_reported_cost_usd": "2.75"},
                            }
                        ]
                    }
                ),
                second,
            ),
        )
        conn.commit()
        view = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert view["summary"]["run_count"] == 2
        assert view["summary"]["exact_usd"] == "2.75"
        assert view["summary"]["attempt_count"] == 1
        assert view["summary"]["incomplete_runs"] == 2
        assert host.accounting_detail(view["snapshot_id"], first)["run"]["coverage"]


def test_global_filters_do_not_change_session_and_include_system_runs(tmp_path):
    with ops_host(tmp_path, [GATE, "chat", "system"]) as (host, _, _, _):
        identity, _ = run(host)
        accepted = host.submit(
            SubmitRequest(
                message="probe",
                purpose="inference_probe",
                endpoint_id="fixture",
                model_id="m",
            )
        )
        assert accepted.kind == "accepted"
        host.wait(accepted.run_id)
        all_runs = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert all_runs["summary"]["run_count"] == 2
        system = host.accounting_snapshot(
            {"range": "all", "timezone": "UTC", "purpose": "inference_probe"}
        )
        assert system["summary"]["run_count"] == 1
        assert system["runs"][0]["run_id"] == accepted.run_id


def test_invalid_boolean_tool_cost_never_enters_sum(tmp_path):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.messages import ToolCallBlock

    with ops_host(
        tmp_path, [GATE, calls(ToolCallBlock("q", "query_events", {})), "done"]
    ) as (host, _, conn, _):
        identifier, _ = run(host)
        conn.execute(
            "UPDATE tool_metering SET cost=?",
            (
                json.dumps(
                    {
                        "kind": "reported",
                        "units": True,
                        "unit": "credits",
                        "service": "builtin",
                        "source": "receipt",
                    }
                ),
            ),
        )
        conn.commit()
        view = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert view["summary"]["tool_costs"] == []
        assert (
            host.accounting_detail(view["snapshot_id"], identifier)["run"]["tools"][0][
                "cost"
            ]["kind"]
            == "unknown"
        )


def test_snapshot_size_refusal_releases_transaction_and_never_publishes_partial(
    tmp_path,
):
    with ops_host(tmp_path, [GATE, "done"]) as (host, _, conn, _):
        run(host)
        host._accounting.max_bytes = 20
        with pytest.raises(AccountingError, match="snapshot_quota"):
            host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert not conn.in_transaction
        assert not host._accounting.snapshots


def test_invalid_time_isolated_and_filtered_membership_explicit(tmp_path):
    with ops_host(tmp_path, [GATE, "first", GATE, "second"]) as (host, _, conn, _):
        first, _ = run(host)
        second, _ = run(host)
        conn.execute("UPDATE runs SET started_at=? WHERE run_id=?", ("damaged", first))
        conn.commit()
        view = host.accounting_snapshot({"range": "all", "timezone": "UTC"})
        assert view["summary"]["run_count"] == 2
        assert (
            "time_unrecorded"
            in host.accounting_detail(view["snapshot_id"], first)["run"]["coverage"]
        )
        filtered = host.accounting_snapshot({"range": "7d", "timezone": "UTC"})
        assert filtered["summary"]["run_count"] == 1
        assert filtered["runs"][0]["run_id"] == second
        assert filtered["unresolved_membership"] == [first]


def test_ops_and_existing_run_evidence_map_recording_unavailable(tmp_path):
    from agent_alfred.gateway.web.api import DashboardApi

    with ops_host(tmp_path, [GATE, "done"]) as (host, _, _, _):
        identifier, _ = run(host)
        api = DashboardApi(facade=host, trace_root=tmp_path / "traces")
        host._store._poisoned.set()
        expected = (503, {"code": "recording_unavailable"})
        assert api.run_evidence({"run_id": identifier}) == expected
        assert (
            api.accounting_write(
                "/api/ops/snapshots", {"range": "all", "timezone": "UTC"}
            )
            == expected
        )
