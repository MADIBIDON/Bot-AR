"""No real network: a FakeMarketDataSource stands in for eBay everywhere
here — resolve_resale_price_for_opportunity only needs the
MarketDataSource/MarketDataRegistry/TTLCache shapes, never a real
connector.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.resale import resolve_resale_estimate, resolve_resale_price_for_opportunity
from database.models import Product, WatchRule
from market_data.base import MarketDataError, MarketDataSource
from market_data.cache import TTLCache
from market_data.models import MarketObservation
from market_data.registry import MarketDataRegistry


class FakeMarketDataSource(MarketDataSource):
    def __init__(
        self, observations: list[MarketObservation] | None = None, *, error: bool = False
    ) -> None:
        self._observations = observations or []
        self._error = error
        self.calls = 0

    def search(self, query: str, *, limit: int = 20) -> list[MarketObservation]:
        self.calls += 1
        if self._error:
            raise MarketDataError("simulated source failure")
        return self._observations


def _obs(price: str, name: str = "Pokemon ETB Ecarlate Violet FR") -> MarketObservation:
    return MarketObservation(
        source="ebay",
        product_name=name,
        price=Decimal(price),
        currency="EUR",
        listing_url="https://ebay.example/item/1",
        external_id="1",
        observed_at=datetime.now(UTC),
    )


def _rule(**overrides: object) -> WatchRule:
    product = Product(id=1, name="Pokemon ETB Ecarlate Violet FR")
    defaults: dict[str, object] = dict(
        id=1,
        product_id=1,
        product=product,
        check_interval=300,
        max_quantity=1,
        enabled=True,
        resale_price_mode="manual",
        market_source=None,
        estimated_resale_price=None,
    )
    defaults.update(overrides)
    return WatchRule(**defaults)  # type: ignore[arg-type]


def _registry(source: MarketDataSource, name: str = "ebay") -> MarketDataRegistry:
    registry = MarketDataRegistry()
    registry.register(name, source)
    return registry


def test_manual_mode_returns_estimated_resale_price_unchanged() -> None:
    rule = _rule(resale_price_mode="manual", estimated_resale_price=Decimal("120"))

    price, estimate = resolve_resale_price_for_opportunity(rule)

    assert price == Decimal("120")
    assert estimate is None


def test_manual_mode_ignores_market_registry_even_if_provided() -> None:
    source = FakeMarketDataSource([_obs("999")])
    rule = _rule(resale_price_mode="manual", estimated_resale_price=Decimal("120"))

    price, estimate = resolve_resale_price_for_opportunity(rule, _registry(source), TTLCache())

    assert price == Decimal("120")
    assert estimate is None
    assert source.calls == 0


def test_market_mode_without_registry_returns_none() -> None:
    rule = _rule(resale_price_mode="market", market_source="ebay")

    price, estimate = resolve_resale_price_for_opportunity(rule)

    assert price is None
    assert estimate is None


def test_market_mode_computes_estimate_from_comparable_observations() -> None:
    source = FakeMarketDataSource([_obs("90"), _obs("100"), _obs("110")])
    rule = _rule(resale_price_mode="market", market_source="ebay")

    price, estimate = resolve_resale_price_for_opportunity(rule, _registry(source), TTLCache())

    assert price == Decimal("100")
    assert estimate is not None
    assert estimate.sample_size == 3


def test_market_mode_rejects_non_comparable_observations() -> None:
    source = FakeMarketDataSource([_obs("100", name="Yu-Gi-Oh Structure Deck")])
    rule = _rule(resale_price_mode="market", market_source="ebay")

    price, estimate = resolve_resale_price_for_opportunity(rule, _registry(source), TTLCache())

    assert price is None
    assert estimate is not None
    assert estimate.sample_size == 0


def test_market_mode_missing_market_source_returns_none() -> None:
    rule = _rule(resale_price_mode="market", market_source=None)

    estimate = resolve_resale_estimate(rule, _registry(FakeMarketDataSource([])), TTLCache())

    assert estimate is None


def test_market_source_error_never_raises_and_returns_none() -> None:
    source = FakeMarketDataSource(error=True)
    rule = _rule(resale_price_mode="market", market_source="ebay")

    price, estimate = resolve_resale_price_for_opportunity(rule, _registry(source), TTLCache())

    assert price is None
    assert estimate is None


def test_cache_hit_avoids_second_fetch() -> None:
    source = FakeMarketDataSource([_obs("100")])
    rule = _rule(resale_price_mode="market", market_source="ebay")
    cache = TTLCache()
    registry = _registry(source)

    resolve_resale_estimate(rule, registry, cache)
    resolve_resale_estimate(rule, registry, cache)

    assert source.calls == 1


def test_cache_expiration_triggers_refetch() -> None:
    source = FakeMarketDataSource([_obs("100")])
    rule = _rule(resale_price_mode="market", market_source="ebay")
    cache = TTLCache(ttl_seconds=0.01)
    registry = _registry(source)

    resolve_resale_estimate(rule, registry, cache)
    import time

    time.sleep(0.02)
    resolve_resale_estimate(rule, registry, cache)

    assert source.calls == 2
