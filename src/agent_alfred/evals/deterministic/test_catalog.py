"""Online catalog fetch. Transport and clock are injected; no live catalog calls."""

from agent_alfred.clock import FakeClock
from agent_alfred.endpoints import ModelEndpoint
from agent_alfred.runtime.transport import VersionedTransportPool


class ScriptedTransport:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def get(self, url, *, headers, timeout):
        secret = {"authorization", "x-api-key"}
        redacted = {
            key: ("<redacted>" if key.lower() in secret else value)
            for key, value in headers.items()
        }
        self.calls.append(
            {
                "url": url,
                "headers": redacted,
                "timeout": timeout,
                "raw_headers": headers,
            }
        )
        if not self._responses:
            raise AssertionError(f"unexpected catalog request {url}")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _pool():
    return VersionedTransportPool(lambda snapshot: snapshot)


def test_missing_key_does_not_send_a_catalog_request():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport([{"status": 200, "body": {"data": []}}])
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    state = list_models(
        endpoint,
        api_key=None,
        transport=transport,
        clock=FakeClock(),
        pool=_pool(),
    )
    assert transport.calls == []
    assert state.health == "unfetched"
    assert state.models == ()


def test_catalog_url_wins_over_base_models_and_uses_ten_second_timeout():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [{"status": 200, "body": {"data": [{"id": "m1"}]}}]
    )
    endpoint = ModelEndpoint(
        "openai",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        catalog_url="https://catalog.example/models",
    )
    state = list_models(
        endpoint,
        api_key="sk-test",
        transport=transport,
        clock=FakeClock(),
        pool=_pool(),
    )
    assert [call["url"] for call in transport.calls] == [
        "https://catalog.example/models"
    ]
    assert transport.calls[0]["timeout"] == 10.0
    assert "sk-test" not in transport.calls[0]["url"]
    assert state.health == "fresh"
    assert [model.model_id for model in state.models] == ["m1"]


def test_missing_catalog_url_uses_base_models_path():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [{"status": 200, "body": {"data": [{"id": "only-id"}]}}]
    )
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    list_models(
        endpoint,
        api_key="sk-test",
        transport=transport,
        clock=FakeClock(),
        pool=_pool(),
    )
    assert transport.calls[0]["url"] == "https://api.openai.com/v1/models"


def test_success_cache_lasts_five_minutes_on_injected_clock():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [
            {"status": 200, "body": {"data": [{"id": "m1"}]}},
            {"status": 200, "body": {"data": [{"id": "m2"}]}},
        ]
    )
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    clock = FakeClock(monotonic_value=0)
    pool = _pool()
    first = list_models(
        endpoint, api_key="sk", transport=transport, clock=clock, pool=pool
    )
    clock.monotonic_value = 299
    second = list_models(
        endpoint, api_key="sk", transport=transport, clock=clock, pool=pool
    )
    assert len(transport.calls) == 1
    assert first.models == second.models
    clock.monotonic_value = 300
    third = list_models(
        endpoint, api_key="sk", transport=transport, clock=clock, pool=pool
    )
    assert len(transport.calls) == 2
    assert [model.model_id for model in third.models] == ["m2"]


def test_failed_refresh_keeps_last_success_as_stale():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [
            {"status": 200, "body": {"data": [{"id": "m1"}]}},
            {"status": 500, "body": {"error": "boom"}},
        ]
    )
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    clock = FakeClock(monotonic_value=0)
    pool = _pool()
    list_models(endpoint, api_key="sk", transport=transport, clock=clock, pool=pool)
    clock.monotonic_value = 300
    stale = list_models(
        endpoint, api_key="sk", transport=transport, clock=clock, pool=pool
    )
    assert stale.health == "stale"
    assert [model.model_id for model in stale.models] == ["m1"]
    assert stale.last_success_at is not None
    assert stale.last_error == "http_500"
    assert stale.retry_at == "2026-08-28T12:01:00Z"


def test_pricing_prompt_completion_are_remembered_and_id_only_is_not():
    from decimal import Decimal

    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [
            {
                "status": 200,
                "body": {
                    "data": [
                        {
                            "id": "priced",
                            "pricing": {"prompt": 0.22, "completion": 0.66},
                        },
                        {"id": "bare"},
                    ]
                },
            }
        ]
    )
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    state = list_models(
        endpoint,
        api_key="sk",
        transport=transport,
        clock=FakeClock(),
        pool=_pool(),
    )
    assert "priced" in state.prices
    assert "bare" not in state.prices
    assert state.prices["priced"]["uncached_input"].unit_price == Decimal("0.22")
    assert state.prices["priced"]["output"].unit_price == Decimal("0.66")
    assert state.prices["priced"]["uncached_input"].source == "catalog"
    assert state.prices["priced"]["uncached_input"].catalog_fetched_at is not None


def test_anthropic_pages_follow_last_id():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [
            {
                "status": 200,
                "body": {
                    "data": [{"id": "a", "display_name": "A"}],
                    "has_more": True,
                    "last_id": "a",
                },
            },
            {
                "status": 200,
                "body": {
                    "data": [{"id": "b", "display_name": "B"}],
                    "has_more": False,
                    "last_id": "b",
                },
            },
        ]
    )
    endpoint = ModelEndpoint(
        "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"
    )
    state = list_models(
        endpoint,
        api_key="sk-ant",
        transport=transport,
        clock=FakeClock(),
        pool=_pool(),
    )
    assert [call["url"] for call in transport.calls] == [
        "https://api.anthropic.com/v1/models",
        "https://api.anthropic.com/v1/models?after_id=a",
    ]
    assert transport.calls[0]["raw_headers"]["x-api-key"] == "sk-ant"
    assert "Authorization" not in transport.calls[0]["raw_headers"]
    assert [model.model_id for model in state.models] == ["a", "b"]
    assert state.models[0].display_name == "A"


def test_xai_native_price_fields_map_to_catalog_quotes():
    from decimal import Decimal

    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [
            {
                "status": 200,
                "body": {
                    "data": [
                        {
                            "id": "grok",
                            "prompt_text_token_price": 2200,
                            "completion_text_token_price": 6600,
                        }
                    ]
                },
            }
        ]
    )
    endpoint = ModelEndpoint("xai", "https://api.x.ai/v1", "XAI_API_KEY")
    state = list_models(
        endpoint,
        api_key="xai-key",
        transport=transport,
        clock=FakeClock(),
        pool=_pool(),
    )
    assert state.prices["grok"]["uncached_input"].unit_price == Decimal("0.22")
    assert state.prices["grok"]["output"].unit_price == Decimal("0.66")


def test_catalog_price_book_reads_pool_slot():
    from decimal import Decimal

    from agent_alfred.catalog import CatalogPriceBook, list_models

    transport = ScriptedTransport(
        [
            {
                "status": 200,
                "body": {
                    "data": [
                        {
                            "id": "priced",
                            "pricing": {"prompt": 0.22, "completion": 0.66},
                        }
                    ]
                },
            }
        ]
    )
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    pool = _pool()
    list_models(
        endpoint, api_key="sk", transport=transport, clock=FakeClock(), pool=pool
    )
    book = CatalogPriceBook(pool)
    quote = book.quote("openai", "priced", "uncached_input")
    assert quote is not None
    assert quote.source == "catalog"
    assert quote.unit_price == Decimal("0.22")
    assert book.quote("openai", "missing", "output") is None


def test_catalog_success_does_not_write_connection_observation():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [{"status": 200, "body": {"data": [{"id": "m1"}]}}]
    )
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    pool = _pool()
    list_models(
        endpoint, api_key="sk", transport=transport, clock=FakeClock(), pool=pool
    )
    assert pool.cached_observation("openai") is None


def test_unavailable_failure_is_cached_for_about_one_minute():
    from agent_alfred.catalog import list_models

    transport = ScriptedTransport(
        [
            {"status": 500, "body": {}},
            {"status": 200, "body": {"data": [{"id": "m1"}]}},
        ]
    )
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    clock = FakeClock(monotonic_value=0)
    pool = _pool()
    first = list_models(
        endpoint, api_key="sk", transport=transport, clock=clock, pool=pool
    )
    assert first.health == "unavailable"
    clock.monotonic_value = 59
    second = list_models(
        endpoint, api_key="sk", transport=transport, clock=clock, pool=pool
    )
    assert second.health == "unavailable"
    assert len(transport.calls) == 1
    clock.monotonic_value = 60
    third = list_models(
        endpoint, api_key="sk", transport=transport, clock=clock, pool=pool
    )
    assert third.health == "fresh"
    assert len(transport.calls) == 2


def test_new_pool_has_no_catalog_cache():
    from agent_alfred.catalog import list_models

    body = {"status": 200, "body": {"data": [{"id": "m1"}]}}
    endpoint = ModelEndpoint("openai", "https://api.openai.com/v1", "OPENAI_API_KEY")
    list_models(
        endpoint,
        api_key="sk",
        transport=ScriptedTransport([body]),
        clock=FakeClock(),
        pool=_pool(),
    )
    transport = ScriptedTransport([body])
    list_models(
        endpoint,
        api_key="sk",
        transport=transport,
        clock=FakeClock(),
        pool=_pool(),
    )
    assert len(transport.calls) == 1
