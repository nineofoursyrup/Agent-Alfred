"""Cost discriminant at the evidence.cost seam. Synthetic prices only."""

from decimal import Decimal

from agent_alfred.pricing import PriceChain, PriceQuote, UserPriceOverride
from agent_alfred.runtime.evidence import cost


class ForbiddenPrices:
    def quote(self, endpoint_id, model_id, dimension):
        raise AssertionError("exact must not consult per-dimension prices")


class FixedPrices:
    """Synthetic USD / million tokens. Not a live catalog."""

    def __init__(self, table):
        self._table = table

    def quote(self, endpoint_id, model_id, dimension):
        row = self._table.get((endpoint_id, model_id))
        if row is None or dimension not in row:
            return None
        unit = row[dimension]
        if isinstance(unit, PriceQuote):
            return unit
        return PriceQuote(unit_price=unit, source="model_static")


_PAPER = {
    "uncached_input": Decimal("1.00"),
    "cache_read": Decimal("0.10"),
    "cache_write": Decimal("1.25"),
    "output": Decimal("2.00"),
}
SYNTHETIC = FixedPrices(
    {
        ("openai", "gpt-test"): _PAPER,
        ("anthropic", "claude-test"): _PAPER,
    }
)


def test_endpoint_reported_cost_is_exact_and_skips_price_components():
    usage = {
        "total_input_tokens": None,
        "uncached_input_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "endpoint_reported_cost_usd": "0.0037756",
    }
    result = cost(
        usage,
        prices=ForbiddenPrices(),
        endpoint_id="xai",
        model_id="grok-4.6",
    )
    assert result == {
        "state": "exact",
        "amount": "0.0037756",
        "currency": "USD",
    }


def test_openai_total_without_cache_details_is_unknown_even_when_prices_exist():
    usage = {
        "total_input_tokens": 20,
        "uncached_input_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": 3,
        "endpoint_reported_cost_usd": None,
    }
    result = cost(
        usage,
        prices=SYNTHETIC,
        endpoint_id="openai",
        model_id="gpt-test",
        computed_at="2026-08-28T12:00:00Z",
    )
    assert result == {"state": "unknown"}
    assert "amount" not in result
    assert "price_components" not in result


def test_allocated_tokens_with_prices_are_estimated_per_dimension():
    usage = {
        "total_input_tokens": 20,
        "uncached_input_tokens": 13,
        "cache_read_tokens": 7,
        "cache_write_tokens": None,
        "output_tokens": 3,
        "endpoint_reported_cost_usd": None,
    }
    result = cost(
        usage,
        prices=SYNTHETIC,
        endpoint_id="openai",
        model_id="gpt-test",
        computed_at="2026-08-28T12:00:00Z",
    )
    assert result["state"] == "estimated"
    assert result["currency"] == "USD"
    assert result["amount"] == "0.0000197"
    assert result["computed_at"] == "2026-08-28T12:00:00Z"
    assert result["price_components"] == [
        {
            "dimension": "uncached_input",
            "tokens": 13,
            "unit_price": "1.00",
            "source": "model_static",
            "price_source": "model_static",
            "amount": "0.000013",
            "stale": False,
            "tiered": False,
        },
        {
            "dimension": "cache_read",
            "tokens": 7,
            "unit_price": "0.10",
            "source": "model_static",
            "price_source": "model_static",
            "amount": "0.0000007",
            "stale": False,
            "tiered": False,
        },
        {
            "dimension": "output",
            "tokens": 3,
            "unit_price": "2.00",
            "source": "model_static",
            "price_source": "model_static",
            "amount": "0.000006",
            "stale": False,
            "tiered": False,
        },
    ]


def test_anthropic_uncached_without_cache_fields_can_be_estimated():
    usage = {
        "total_input_tokens": None,
        "uncached_input_tokens": 10,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": 4,
        "endpoint_reported_cost_usd": None,
    }
    result = cost(
        usage,
        prices=SYNTHETIC,
        endpoint_id="anthropic",
        model_id="claude-test",
        computed_at="2026-08-28T12:00:00Z",
    )
    assert result["state"] == "estimated"
    assert result["amount"] == "0.000018"
    assert [row["dimension"] for row in result["price_components"]] == [
        "uncached_input",
        "output",
    ]


def test_zero_tokens_do_not_require_a_price():
    usage = {
        "total_input_tokens": 0,
        "uncached_input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 5,
        "endpoint_reported_cost_usd": None,
    }
    prices = FixedPrices({("openai", "gpt-test"): {"output": Decimal("2.00")}})
    result = cost(usage, prices=prices, endpoint_id="openai", model_id="gpt-test")
    assert result["state"] == "estimated"
    assert result["amount"] == "0.00001"
    assert [row["dimension"] for row in result["price_components"]] == ["output"]


def test_uncached_none_with_output_is_unknown():
    usage = {
        "uncached_input_tokens": None,
        "output_tokens": 5,
        "endpoint_reported_cost_usd": None,
    }
    result = cost(
        usage, prices=SYNTHETIC, endpoint_id="openai", model_id="gpt-test"
    )
    assert result == {"state": "unknown"}


def test_missing_price_for_positive_tokens_is_unknown_without_default():
    usage = {
        "uncached_input_tokens": 10,
        "output_tokens": 5,
        "endpoint_reported_cost_usd": None,
    }
    prices = FixedPrices({("openai", "gpt-test"): {"output": Decimal("2.00")}})
    result = cost(usage, prices=prices, endpoint_id="openai", model_id="gpt-test")
    assert result == {"state": "unknown"}
    assert "amount" not in result


def test_omitted_override_looks_down_the_chain():
    usage = {
        "uncached_input_tokens": 10,
        "output_tokens": 5,
        "endpoint_reported_cost_usd": None,
    }
    prices = PriceChain(
        override=UserPriceOverride(output=Decimal("0")),
        static=SYNTHETIC,
    )
    result = cost(usage, prices=prices, endpoint_id="openai", model_id="gpt-test")
    assert result["state"] == "estimated"
    by_dim = {row["dimension"]: row for row in result["price_components"]}
    assert by_dim["uncached_input"]["source"] == "model_static"
    assert by_dim["uncached_input"]["amount"] == "0.00001"
    assert by_dim["output"]["source"] == "user_override"
    assert by_dim["output"]["amount"] == "0"
    assert result["amount"] == "0.00001"


def test_explicit_zero_override_does_not_invent_other_dimension_prices():
    usage = {
        "uncached_input_tokens": 10,
        "output_tokens": 5,
        "endpoint_reported_cost_usd": None,
    }
    prices = PriceChain(
        override=UserPriceOverride(uncached_input=Decimal("0")),
        static=FixedPrices({}),
    )
    result = cost(usage, prices=prices, endpoint_id="openai", model_id="gpt-test")
    assert result == {"state": "unknown"}


def test_stale_catalog_price_is_still_estimated():
    usage = {
        "uncached_input_tokens": 10,
        "output_tokens": 5,
        "endpoint_reported_cost_usd": None,
    }
    catalog = FixedPrices(
        {
            ("openai", "gpt-test"): {
                "uncached_input": PriceQuote(
                    unit_price=Decimal("1.00"),
                    source="catalog",
                    stale=True,
                    catalog_fetched_at="2026-08-28T11:00:00Z",
                ),
                "output": PriceQuote(
                    unit_price=Decimal("2.00"),
                    source="catalog",
                    stale=True,
                    catalog_fetched_at="2026-08-28T11:00:00Z",
                ),
            }
        }
    )
    result = cost(
        usage,
        prices=PriceChain(catalog=catalog),
        endpoint_id="openai",
        model_id="gpt-test",
        computed_at="2026-08-28T12:00:00Z",
    )
    assert result["state"] == "estimated"
    assert result["price_components"][0]["stale"] is True
    assert result["price_components"][0]["catalog_fetched_at"] == "2026-08-28T11:00:00Z"
    assert result["price_components"][0]["source"] == "catalog"


def test_reasoning_tokens_are_not_billed_again():
    usage = {
        "uncached_input_tokens": 10,
        "output_tokens": 5,
        "reasoning_tokens": 4,
        "endpoint_reported_cost_usd": None,
    }
    result = cost(
        usage, prices=SYNTHETIC, endpoint_id="openai", model_id="gpt-test"
    )
    assert result["state"] == "estimated"
    assert result["amount"] == "0.00002"
    assert [row["dimension"] for row in result["price_components"]] == [
        "uncached_input",
        "output",
    ]
