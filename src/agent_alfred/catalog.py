"""Process-in model catalog. Never writes connection observations or assignments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Mapping, Protocol

from agent_alfred.clock import format_instant
from agent_alfred.endpoints import ModelEndpoint
from agent_alfred.pricing import PriceQuote

CatalogHealth = Literal["unfetched", "fresh", "stale", "unavailable"]
SUCCESS_TTL_S = 5 * 60
FAILURE_TTL_S = 60
CATALOG_TIMEOUT_S = 10.0
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class CatalogModel:
    model_id: str
    display_name: str | None = None
    context_length: int | None = None
    tools: bool | None = None


@dataclass(frozen=True)
class CatalogState:
    health: CatalogHealth
    models: tuple[CatalogModel, ...] = ()
    fetched_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    retry_at: str | None = None
    prices: Mapping[str, Mapping[str, PriceQuote]] = field(default_factory=dict)
    expires_at: float | None = None
    success_expires_at: float | None = None


class CatalogTransport(Protocol):
    def get(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> Mapping[str, Any]: ...


def list_models(
    endpoint: ModelEndpoint,
    *,
    api_key: str | None,
    transport: CatalogTransport,
    clock,
    pool,
    refresh: bool = False,
    epoch: int | None = None,
) -> CatalogState:
    cached = pool.cached_catalog(endpoint.endpoint_id)
    state = cached if isinstance(cached, CatalogState) else CatalogState("unfetched")
    if not (api_key or "").strip():
        return state
    now = clock.monotonic()
    if (
        not refresh
        and state.expires_at is not None
        and now < state.expires_at
        and state.health in ("fresh", "stale", "unavailable")
    ):
        return state
    if epoch is None:
        epoch = pool.catalog_epoch(endpoint.endpoint_id)
    try:
        payload = _fetch(endpoint, api_key=api_key, transport=transport)
        fetched_at = format_instant(clock.wall_utc())
        prices = {
            model_id: {
                dim: PriceQuote(
                    unit_price=quote.unit_price,
                    source="catalog",
                    stale=False,
                    catalog_fetched_at=fetched_at,
                    tiered=quote.tiered,
                )
                for dim, quote in dims.items()
            }
            for model_id, dims in payload["prices"].items()
        }
        fresh = CatalogState(
            health="fresh",
            models=payload["models"],
            fetched_at=fetched_at,
            last_success_at=fetched_at,
            prices=prices,
            expires_at=now + SUCCESS_TTL_S,
            success_expires_at=now + SUCCESS_TTL_S,
        )
        pool.store_catalog(endpoint.endpoint_id, fresh, epoch=epoch)
        return fresh
    except Exception as exc:
        return _remember_failure(
            pool, endpoint.endpoint_id, state, clock, exc, epoch=epoch
        )


def catalog_url_for(endpoint: ModelEndpoint) -> str:
    if endpoint.catalog_url:
        return endpoint.catalog_url
    return endpoint.base_url.rstrip("/") + "/models"


def catalog_headers(endpoint: ModelEndpoint, api_key: str) -> dict[str, str]:
    if endpoint.endpoint_id == "anthropic":
        return {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
    return {"Authorization": f"Bearer {api_key}"}


def _fetch(endpoint, *, api_key, transport) -> dict[str, Any]:
    url = catalog_url_for(endpoint)
    headers = catalog_headers(endpoint, api_key)
    models: list[CatalogModel] = []
    prices: dict[str, dict[str, PriceQuote]] = {}
    after = None
    while True:
        page_url = url if after is None else _with_after(url, after)
        response = transport.get(
            page_url, headers=headers, timeout=CATALOG_TIMEOUT_S
        )
        status = response.get("status")
        if status != 200:
            raise CatalogFetchError(f"http_{status}")
        body = response.get("body") or {}
        page_models, page_prices, cursor = parse_catalog_page(body)
        models.extend(page_models)
        for model_id, quote in page_prices.items():
            prices[model_id] = quote
        if not cursor:
            break
        after = cursor
    return {"models": tuple(models), "prices": prices}


def parse_catalog_page(
    body: Mapping[str, Any],
) -> tuple[list[CatalogModel], dict[str, dict[str, PriceQuote]], str | None]:
    rows = body.get("data")
    if not isinstance(rows, list):
        rows = []
    models: list[CatalogModel] = []
    prices: dict[str, dict[str, PriceQuote]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("id"):
            continue
        model_id = str(row["id"])
        context = row.get("max_input_tokens")
        if context is None and isinstance(row.get("limit"), Mapping):
            context = row["limit"].get("context")
        tools = None
        caps = row.get("capabilities")
        if isinstance(caps, Mapping) and "tools" in caps:
            tools = bool(caps["tools"])
        elif "tool_call" in row:
            tools = bool(row["tool_call"])
        models.append(
            CatalogModel(
                model_id=model_id,
                display_name=row.get("display_name") or row.get("name"),
                context_length=int(context) if context is not None else None,
                tools=tools,
            )
        )
        remembered = remember_price(row)
        if remembered:
            prices[model_id] = remembered
    cursor = None
    if body.get("has_more") and body.get("last_id"):
        cursor = str(body["last_id"])
    return models, prices, cursor


def remember_price(row: Mapping[str, Any]) -> dict[str, PriceQuote] | None:
    pricing = row.get("pricing")
    quotes: dict[str, PriceQuote] = {}
    if isinstance(pricing, Mapping) and "prompt" in pricing and "completion" in pricing:
        quotes["uncached_input"] = _catalog_quote(pricing["prompt"])
        quotes["output"] = _catalog_quote(pricing["completion"])
        if "cache_read" in pricing:
            quotes["cache_read"] = _catalog_quote(pricing["cache_read"])
        if "cache_write" in pricing:
            quotes["cache_write"] = _catalog_quote(pricing["cache_write"])
        return quotes
    native = _xai_quotes(row)
    return native or None


def _xai_quotes(row: Mapping[str, Any]) -> dict[str, PriceQuote] | None:
    prompt = row.get("prompt_text_token_price")
    completion = row.get("completion_text_token_price")
    if prompt is None or completion is None:
        return None
    quotes = {
        "uncached_input": _catalog_quote(_cents_per_1e8_to_usd_per_million(prompt)),
        "output": _catalog_quote(_cents_per_1e8_to_usd_per_million(completion)),
    }
    cached = row.get("cached_prompt_text_token_price") or row.get(
        "prompt_cache_read_token_price"
    )
    if cached is not None:
        quotes["cache_read"] = _catalog_quote(
            _cents_per_1e8_to_usd_per_million(cached)
        )
    return quotes


def _cents_per_1e8_to_usd_per_million(value: object):
    from decimal import Decimal

    cents = Decimal(str(value))
    return cents / Decimal("10000")


def _catalog_quote(unit_price) -> PriceQuote:
    from decimal import Decimal

    return PriceQuote(
        unit_price=Decimal(str(unit_price)),
        source="catalog",
        stale=False,
    )


def _with_after(url: str, after_id: str) -> str:
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}after_id={after_id}"


class CatalogPriceBook:
    """Per-dimension catalog quotes from the transport-pool catalog slot."""

    def __init__(self, pool):
        self._pool = pool

    def quote(
        self, endpoint_id: str, model_id: str, dimension: str
    ) -> PriceQuote | None:
        state = self._pool.cached_catalog(endpoint_id)
        if not isinstance(state, CatalogState):
            return None
        return state.prices.get(model_id, {}).get(dimension)


def _remember_failure(
    pool, endpoint_id, previous: CatalogState, clock, exc, *, epoch: int | None = None
) -> CatalogState:
    now = clock.monotonic()
    retry_at = format_instant(clock.wall_utc() + timedelta(seconds=FAILURE_TTL_S))
    reason = getattr(exc, "reason", None) or type(exc).__name__
    if previous.last_success_at and previous.models:
        failed = CatalogState(
            health="stale",
            models=previous.models,
            fetched_at=previous.fetched_at,
            last_success_at=previous.last_success_at,
            last_error=str(reason),
            retry_at=retry_at,
            prices=_stale_prices(previous.prices),
            expires_at=now + FAILURE_TTL_S,
            success_expires_at=previous.success_expires_at,
        )
    else:
        failed = CatalogState(
            health="unavailable",
            last_error=str(reason),
            retry_at=retry_at,
            expires_at=now + FAILURE_TTL_S,
        )
    pool.store_catalog(endpoint_id, failed, epoch=epoch)
    return failed


def _stale_prices(prices: Mapping[str, Mapping[str, PriceQuote]]):
    out: dict[str, dict[str, PriceQuote]] = {}
    for model_id, dims in prices.items():
        out[model_id] = {
            dim: PriceQuote(
                unit_price=quote.unit_price,
                source=quote.source,
                stale=True,
                catalog_fetched_at=quote.catalog_fetched_at,
                tiered=quote.tiered,
            )
            for dim, quote in dims.items()
        }
    return out


class CatalogFetchError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
