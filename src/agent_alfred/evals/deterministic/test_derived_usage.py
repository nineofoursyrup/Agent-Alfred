"""Derived usage snapshot schema. No export command."""

from agent_alfred.evals.deterministic.test_cost_projection import SYNTHETIC
from agent_alfred.usage_export import SCHEMA_VERSION, derived_usage_snapshot


def test_derived_snapshot_keeps_unit_prices_and_computation_time():
    snapshot = derived_usage_snapshot(
        [
            {
                "attempt_id": "a1",
                "outcome": "committed",
                "model": {"endpoint_id": "openai", "model_id": "gpt-test"},
                "usage": {
                    "uncached_input_tokens": 10,
                    "output_tokens": 5,
                    "endpoint_reported_cost_usd": None,
                },
            }
        ],
        generated_at="2026-08-28T12:00:00Z",
        prices=SYNTHETIC,
    )
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["generated_at"] == "2026-08-28T12:00:00Z"
    charge = snapshot["records"][0]["cost"]
    assert charge["state"] == "estimated"
    assert charge["computed_at"] == "2026-08-28T12:00:00Z"
    assert charge["price_components"][0]["unit_price"] == "1.00"
    assert charge["amount"] == "0.00002"


def test_derived_exact_amount_still_records_computation_time():
    snapshot = derived_usage_snapshot(
        [
            {
                "attempt_id": "a1",
                "outcome": "committed",
                "model": {"endpoint_id": "xai", "model_id": "grok"},
                "usage": {"endpoint_reported_cost_usd": "0.0037756"},
            }
        ],
        generated_at="2026-08-28T12:00:00Z",
        prices=SYNTHETIC,
    )
    charge = snapshot["records"][0]["cost"]
    assert charge["state"] == "exact"
    assert charge["amount"] == "0.0037756"
    assert charge["computed_at"] == "2026-08-28T12:00:00Z"
    assert "price_components" not in charge


def test_cli_has_no_usage_export_command():
    from agent_alfred.gateway.cli import build_parser

    parser = build_parser()
    assert parser._subparsers is None
    assert "export" not in parser.format_help()
    assert "usage.jsonl" not in parser.format_help()
