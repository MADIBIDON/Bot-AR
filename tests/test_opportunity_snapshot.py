"""No real network: market data comes from a FakeMarketDataSource, and
purchase-side data comes only from ObservationRecords already committed to
the (in-memory) test database — build_opportunity_candidate never fetches
anything itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.opportunity_snapshot import build_opportunity_candidate
from database import crud
from market_data.base import MarketDataSource
from market_data.cache import TTLCache
from market_data.models import MarketObservation
from market_data.registry import MarketDataRegistry
from products.observation import ProductObservation


class _FakeMarketSource(MarketDataSource):
    def __init__(self, observations: list[MarketObservation]) -> None:
        self._observations = observations

    def search(self, query: str, *, limit: int = 20) -> list[MarketObservation]:
        return self._observations


def _market_obs(price: str, name: str = "Duopack Evoli") -> MarketObservation:
    return MarketObservation(
        source="ebay",
        product_name=name,
        price=Decimal(price),
        currency="EUR",
        listing_url="https://ebay.example/1",
        external_id="1",
        observed_at=datetime.now(UTC),
    )


def _seed_rule_with_observation(
    session: Session, *, price: str = "13.99", available: bool = True
) -> int:
    product = crud.create_product(session, "Duopack Evoli")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/1",
        external_id="fake-1",
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=60, max_quantity=1
    )
    obs = ProductObservation(
        merchant="RetailerA",
        external_id="fake-1",
        name="Duopack Evoli",
        price=Decimal(price),
        currency="EUR",
        available=available,
        url="https://a.example/p/1",
        observed_at=datetime.now(UTC),
    )
    crud.create_observation_record(session, listing_id=listing.id, observation=obs)
    return rule.id


def test_rule_with_no_listing_returns_none(session: Session) -> None:
    product = crud.create_product(session, "Global Rule Product")
    rule = crud.create_watch_rule(session, product_id=product.id, check_interval=60, max_quantity=1)

    assert build_opportunity_candidate(session, rule, None, None) is None


def test_rule_with_no_observation_yet_returns_none(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/1",
        external_id="fake-1",
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=60, max_quantity=1
    )

    assert build_opportunity_candidate(session, rule, None, None) is None


def test_manual_mode_builds_candidate_with_opportunity(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session, price="74.90")
    crud.update_watch_rule(
        session,
        rule_id,
        estimated_resale_price=Decimal("110"),
        platform_fee_pct=Decimal("9"),
        shipping_cost=Decimal("6.50"),
    )
    rule = crud.get_watch_rule(session, rule_id)

    candidate = build_opportunity_candidate(session, rule, None, None)

    assert candidate is not None
    assert candidate.purchase_price == Decimal("74.90")
    assert candidate.estimated_resale_price == Decimal("110")
    assert candidate.net_profit == Decimal("18.70")
    assert candidate.resale_confidence is None
    assert candidate.match_confidence == 80  # external_id exact match via expected_listing


def test_manual_mode_without_estimated_price_has_no_opportunity(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session)
    rule = crud.get_watch_rule(session, rule_id)

    candidate = build_opportunity_candidate(session, rule, None, None)

    assert candidate is not None
    assert candidate.estimated_resale_price is None
    assert candidate.net_profit is None


def test_market_mode_builds_candidate_from_market_data(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session, price="60")
    crud.update_watch_rule(session, rule_id, resale_price_mode="market", market_source="ebay")
    rule = crud.get_watch_rule(session, rule_id)

    registry = MarketDataRegistry()
    registry.register(
        "ebay", _FakeMarketSource([_market_obs("90"), _market_obs("100"), _market_obs("110")])
    )

    candidate = build_opportunity_candidate(session, rule, registry, TTLCache())

    assert candidate is not None
    assert candidate.estimated_resale_price == Decimal("100")
    assert candidate.resale_confidence is not None
    assert candidate.market_sample_size == 3
    assert candidate.net_profit is not None


def test_market_mode_without_registry_has_no_estimate(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session)
    crud.update_watch_rule(session, rule_id, resale_price_mode="market", market_source="ebay")
    rule = crud.get_watch_rule(session, rule_id)

    candidate = build_opportunity_candidate(session, rule, None, None)

    assert candidate is not None
    assert candidate.estimated_resale_price is None
    assert candidate.resale_confidence is None


def test_out_of_stock_observation_is_reflected(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session, available=False)
    rule = crud.get_watch_rule(session, rule_id)

    candidate = build_opportunity_candidate(session, rule, None, None)

    assert candidate is not None
    assert candidate.in_stock is False
