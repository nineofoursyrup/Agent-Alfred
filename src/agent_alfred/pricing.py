"""Render-time price chain. Token facts stay on Usage / telemetry."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Protocol

PriceSource = Literal[
    "user_override",
    "catalog",
    "free_rule",
    "model_static",
    "endpoint_static",
]

BILLING = (
    ("uncached_input", "uncached_input_tokens"),
    ("cache_read", "cache_read_tokens"),
    ("cache_write", "cache_write_tokens"),
    ("output", "output_tokens"),
)
NECESSARY = ("uncached_input", "output")
INPUT_DIMS = ("uncached_input", "cache_read", "cache_write")
PER_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class PriceQuote:
    unit_price: Decimal
    source: PriceSource
    stale: bool = False
    catalog_fetched_at: str | None = None
    tiered: bool = False


class PriceBook(Protocol):
    def quote(
        self, endpoint_id: str, model_id: str, dimension: str
    ) -> PriceQuote | None: ...


@dataclass(frozen=True)
class UserPriceOverride:
    uncached_input: Decimal | None = None
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None
    output: Decimal | None = None

    def explicit(self, dimension: str) -> Decimal | None:
        return getattr(self, dimension)


class PriceChain:
    """user_override → catalog → free_rule → model_static → endpoint_static."""

    def __init__(
        self,
        *,
        override: UserPriceOverride | None = None,
        catalog: PriceBook | None = None,
        static: PriceBook | None = None,
    ):
        self._override = override
        self._catalog = catalog
        self._static = static

    def quote(
        self, endpoint_id: str, model_id: str, dimension: str
    ) -> PriceQuote | None:
        if self._override is not None:
            value = self._override.explicit(dimension)
            if value is not None:
                return PriceQuote(unit_price=value, source="user_override")
        if self._catalog is not None:
            found = self._catalog.quote(endpoint_id, model_id, dimension)
            if found is not None:
                return found
        if self._static is not None:
            found = self._static.quote(endpoint_id, model_id, dimension)
            if found is not None:
                return found
        return None


_TOML_DIMS = {
    "input": "uncached_input",
    "output": "output",
    "cache_read": "cache_read",
    "cache_write": "cache_write",
}


class StaticPriceBook:
    """model_static quotes from a models.dev snapshot. Historical, not live."""

    def __init__(self, payload: dict[str, Any]):
        self.snapshot_date = payload.get("meta", {}).get("snapshot_date")
        self._models = payload.get("endpoints", {})

    @classmethod
    def load(cls, path: Path) -> StaticPriceBook:
        return cls(tomllib.loads(path.read_text()))

    @classmethod
    def packaged(cls) -> StaticPriceBook:
        resource = files("agent_alfred.data").joinpath("prices.toml")
        return cls(tomllib.loads(resource.read_text(encoding="utf-8")))

    def quote(
        self, endpoint_id: str, model_id: str, dimension: str
    ) -> PriceQuote | None:
        models = self._models.get(endpoint_id, {}).get("models", {})
        entry = models.get(model_id)
        if not entry:
            return None
        toml_name = next(
            (key for key, dim in _TOML_DIMS.items() if dim == dimension), None
        )
        if toml_name is None or toml_name not in entry:
            return None
        source: PriceSource = "free_rule" if entry.get("free") else "model_static"
        return PriceQuote(
            unit_price=Decimal(str(entry[toml_name])),
            source=source,
            tiered=bool(entry.get("tiered")),
        )


def project_cost(
    usage: dict | None,
    prices: PriceBook | None = None,
    *,
    endpoint_id: str | None = None,
    model_id: str | None = None,
    computed_at: str | None = None,
) -> dict[str, Any]:
    usage = usage or {}
    reported = _reported_amount(usage.get("endpoint_reported_cost_usd"))
    if reported is not None:
        exact: dict[str, Any] = {
            "state": "exact",
            "amount": format(reported, "f"),
            "currency": "USD",
        }
        if computed_at is not None:
            exact["computed_at"] = computed_at
        return exact

    tokens = {dim: _token(usage.get(key)) for dim, key in BILLING}
    if not _input_allocated(usage.get("total_input_tokens"), tokens):
        return {"state": "unknown"}
    if any(tokens[dim] is None for dim in NECESSARY):
        return {"state": "unknown"}
    if prices is None or endpoint_id is None or model_id is None:
        return {"state": "unknown"}

    components: list[dict[str, Any]] = []
    total = Decimal("0")
    for dim, _key in BILLING:
        count = tokens[dim]
        if count is None or count == 0:
            continue
        quote = prices.quote(endpoint_id, model_id, dim)
        if quote is None:
            return {"state": "unknown"}
        amount = Decimal(count) * quote.unit_price / PER_MILLION
        total += amount
        item: dict[str, Any] = {
            "dimension": dim,
            "tokens": count,
            "unit_price": format(quote.unit_price, "f"),
            "source": quote.source,
            "price_source": quote.source,
            "amount": format(amount, "f"),
            "stale": quote.stale,
            "tiered": quote.tiered,
        }
        if quote.catalog_fetched_at is not None:
            item["catalog_fetched_at"] = quote.catalog_fetched_at
        components.append(item)

    result: dict[str, Any] = {
        "state": "estimated",
        "amount": format(total, "f"),
        "currency": "USD",
        "price_components": components,
    }
    if computed_at is not None:
        result["computed_at"] = computed_at
    return result


def _reported_amount(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if amount.is_finite() and amount >= 0:
        return amount
    return None


def _token(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _input_allocated(total: object, tokens: dict[str, int | None]) -> bool:
    if total is None:
        return True
    accounted = sum(tokens[dim] or 0 for dim in INPUT_DIMS if tokens[dim] is not None)
    return accounted == int(total)
