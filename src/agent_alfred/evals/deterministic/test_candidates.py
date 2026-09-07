"""Candidate merge and catalog scheduling (#34)."""

from __future__ import annotations

import threading

from agent_alfred.candidates import merge_candidates
from agent_alfred.catalog import CatalogModel, CatalogState
from agent_alfred.catalog_schedule import CatalogScheduler
from agent_alfred.clock import FakeClock
from agent_alfred.endpoints import ModelEndpoint, ModelRoute
from agent_alfred.evals.deterministic.test_catalog import ScriptedTransport
from agent_alfred.runtime.model_settings import PinRecord
from agent_alfred.runtime.transport import VersionedTransportPool


def _endpoint() -> ModelEndpoint:
    return ModelEndpoint(
        "openai",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        models={"gpt-test": ModelRoute("openai", "/chat/completions")},
    )


def test_merge_keeps_sources_as_a_set() -> None:
    pins = (PinRecord("openai", "gpt-test", display_name="钉选名"),)
    catalog = {
        "openai": CatalogState(
            health="fresh",
            models=(
                CatalogModel("gpt-test", display_name="目录名"),
                CatalogModel("extra"),
            ),
        )
    }
    rows = merge_candidates(
        endpoints=(_endpoint(),),
        pins=pins,
        catalogs=catalog,
    )
    by_id = {(row["endpoint_id"], row["model_id"]): row for row in rows}
    gpt = by_id[("openai", "gpt-test")]
    assert gpt["sources"] == {"builtin", "pinned", "catalog"}
    assert gpt["display_name"] == "钉选名"
    extra = by_id[("openai", "extra")]
    assert extra["sources"] == {"catalog"}
    assert "gpt-test" in {row["model_id"] for row in rows}


def test_open_models_fetches_only_the_assigned_endpoint() -> None:
    assigned = _endpoint()
    other = ModelEndpoint(
        "anthropic",
        "https://api.anthropic.com/v1",
        "ANTHROPIC_API_KEY",
        catalog_url="https://api.anthropic.com/v1/models",
    )
    transport = ScriptedTransport(
        [{"status": 200, "body": {"data": [{"id": "gpt-test"}]}}]
    )
    pool = VersionedTransportPool(lambda snapshot: snapshot)
    scheduler = CatalogScheduler(
        clock=FakeClock(),
        pool=pool,
        transport=transport,
        endpoints=(assigned, other),
    )
    states = scheduler.open_assigned(
        assigned_endpoint_id="openai",
        keys={"openai": "sk-test", "anthropic": "sk-other"},
    )
    assert [call["url"] for call in transport.calls] == [
        "https://api.openai.com/v1/models"
    ]
    assert states["openai"].health == "fresh"
    assert states["anthropic"].health == "unfetched"


def test_missing_key_makes_zero_requests_and_keeps_unfetched() -> None:
    transport = ScriptedTransport(
        [{"status": 200, "body": {"data": [{"id": "gpt-test"}]}}]
    )
    scheduler = CatalogScheduler(
        clock=FakeClock(),
        pool=VersionedTransportPool(lambda snapshot: snapshot),
        transport=transport,
        endpoints=(_endpoint(),),
    )
    states = scheduler.open_assigned(assigned_endpoint_id="openai", keys={})
    assert transport.calls == []
    assert states["openai"].health == "unfetched"


def test_same_endpoint_inflight_requests_merge() -> None:
    started = threading.Event()
    release = threading.Event()

    class GatedTransport(ScriptedTransport):
        def get(self, url, *, headers, timeout):
            started.set()
            assert release.wait(2)
            return super().get(url, headers=headers, timeout=timeout)

    transport = GatedTransport(
        [{"status": 200, "body": {"data": [{"id": "gpt-test"}]}}]
    )
    scheduler = CatalogScheduler(
        clock=FakeClock(),
        pool=VersionedTransportPool(lambda snapshot: snapshot),
        transport=transport,
        endpoints=(_endpoint(),),
    )
    results: list[CatalogState] = []

    def worker() -> None:
        results.append(scheduler.ensure(_endpoint(), api_key="sk-test"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(2)
    release.set()
    for thread in threads:
        thread.join(2)
    assert len(transport.calls) == 1
    assert [state.health for state in results] == ["fresh", "fresh"]


def test_stale_inflight_does_not_pollute_new_generation_cache() -> None:
    started = threading.Event()
    release = threading.Event()

    class GatedTransport(ScriptedTransport):
        def get(self, url, *, headers, timeout):
            started.set()
            assert release.wait(2)
            return super().get(url, headers=headers, timeout=timeout)

    transport = GatedTransport(
        [
            {"status": 200, "body": {"data": [{"id": "old-model"}]}},
            {"status": 200, "body": {"data": [{"id": "new-model"}]}},
        ]
    )
    pool = VersionedTransportPool(lambda snapshot: snapshot)
    scheduler = CatalogScheduler(
        clock=FakeClock(),
        pool=pool,
        transport=transport,
        endpoints=(_endpoint(),),
    )
    old: list[CatalogState] = []

    def worker() -> None:
        old.append(scheduler.ensure(_endpoint(), api_key="sk-old"))

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(2)
    pool.discard("openai")
    scheduler.bump()
    release.set()
    thread.join(2)
    fresh = scheduler.ensure(_endpoint(), api_key="sk-new")
    assert [model.model_id for model in fresh.models] == ["new-model"]
    cached = pool.cached_catalog("openai")
    assert cached is not None
    assert [model.model_id for model in cached.models] == ["new-model"]


def test_refresh_bypasses_ttl() -> None:
    transport = ScriptedTransport(
        [
            {"status": 200, "body": {"data": [{"id": "first"}]}},
            {"status": 200, "body": {"data": [{"id": "refreshed"}]}},
        ]
    )
    clock = FakeClock(monotonic_value=0)
    scheduler = CatalogScheduler(
        clock=clock,
        pool=VersionedTransportPool(lambda snapshot: snapshot),
        transport=transport,
        endpoints=(_endpoint(),),
    )
    first = scheduler.ensure(_endpoint(), api_key="sk-test")
    clock.monotonic_value = 10
    cached = scheduler.ensure(_endpoint(), api_key="sk-test")
    assert [model.model_id for model in cached.models] == ["first"]
    refreshed = scheduler.ensure(_endpoint(), api_key="sk-test", refresh=True)
    assert [model.model_id for model in first.models] == ["first"]
    assert [model.model_id for model in refreshed.models] == ["refreshed"]
    assert len(transport.calls) == 2


def test_refresh_joins_inflight_request_for_the_same_endpoint() -> None:
    started = threading.Event()
    release = threading.Event()

    class GatedTransport(ScriptedTransport):
        def get(self, url, *, headers, timeout):
            started.set()
            assert release.wait(2)
            return super().get(url, headers=headers, timeout=timeout)

    transport = GatedTransport(
        [{"status": 200, "body": {"data": [{"id": "gpt-test"}]}}]
    )
    scheduler = CatalogScheduler(
        clock=FakeClock(),
        pool=VersionedTransportPool(lambda snapshot: snapshot),
        transport=transport,
        endpoints=(_endpoint(),),
    )
    results: list[CatalogState] = []

    def worker() -> None:
        results.append(
            scheduler.ensure(_endpoint(), api_key="sk-test", refresh=True)
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(2)
    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert len(transport.calls) == 1
    assert [state.health for state in results] == ["fresh", "fresh"]


def test_refresh_does_not_write_connection_observation() -> None:
    pool = VersionedTransportPool(lambda snapshot: snapshot)
    transport = ScriptedTransport(
        [{"status": 200, "body": {"data": [{"id": "gpt-test"}]}}]
    )
    scheduler = CatalogScheduler(
        clock=FakeClock(),
        pool=pool,
        transport=transport,
        endpoints=(_endpoint(),),
    )
    scheduler.ensure(_endpoint(), api_key="sk-test", refresh=True)
    assert pool.cached_observation("openai") is None


def test_models_page_script_refresh_disables_while_inflight() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "ops" / "static" / "pages.js"
    ).read_text(encoding="utf-8")
    models = source.split("export function modelsPage", 1)[1]
    assert 'node("button", "刷新目录")' in models
    assert 'refresh: "1"' in models
    assert "button.disabled = true" in models
    assert "button.isConnected" in models
