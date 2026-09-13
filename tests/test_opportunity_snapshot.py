"""No real network: market data comes from a FakeMarketDataSource, and
purchase-side data comes only from ObservationRecords already committed to
the (in-memory) test database — build_opportunity_candidate never fetches
anything itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.opportunity_snapshot import (
    best_buy_source_for_product,
    build_opportunity_candidate,
    evaluate_opportunity_intelligence,
)
from database import crud
from engine.alerting import NEEDS_MARKET_DATA, AlertTier
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
    # Phase 31: an untrusted manual resale price (estimated_resale_trusted
    # defaults False) is "low" confidence, never a free 100% — see
    # engine/ranking.py's OpportunityCandidate docstring.
    assert candidate.resale_confidence == "low"
    assert candidate.resale_price_source == "manual"
    assert candidate.match_confidence == 80  # external_id exact match via expected_listing


def test_manual_mode_trusted_price_is_high_confidence(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session, price="74.90")
    crud.update_watch_rule(
        session,
        rule_id,
        estimated_resale_price=Decimal("110"),
        estimated_resale_trusted=True,
    )
    rule = crud.get_watch_rule(session, rule_id)

    candidate = build_opportunity_candidate(session, rule, None, None)

    assert candidate is not None
    assert candidate.resale_confidence == "high"


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
    assert candidate.resale_price_source == "ebay"
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


def test_product_level_fee_config_is_honored_not_just_rule_level(session: Session) -> None:
    """Phase 26: this used to build OpportunityConfig from the WatchRule's
    own fee fields only, silently using 0 fees for a rule whose config
    lives on its Product (Phase 25's shared-config pattern) — producing a
    different net_profit here than the real Discord alert would compute
    for the exact same opportunity. Must match now."""
    rule_id = _seed_rule_with_observation(session, price="60")
    rule = crud.get_watch_rule(session, rule_id)
    crud.update_product(
        session,
        rule.product_id,
        estimated_resale_price=Decimal("120"),
        platform_fee_pct=Decimal("10"),
        shipping_cost=Decimal("5"),
    )
    session.refresh(rule)

    candidate = build_opportunity_candidate(session, rule, None, None)

    assert candidate is not None
    assert candidate.estimated_resale_price == Decimal("120")
    # net = 120 - 60 - (12 platform fee) - 5 shipping = 43
    assert candidate.net_profit == Decimal("43.00")


def test_evaluate_opportunity_intelligence_needs_market_data_when_no_resale_price(
    session: Session,
) -> None:
    rule_id = _seed_rule_with_observation(session)
    rule = crud.get_watch_rule(session, rule_id)

    evaluation = evaluate_opportunity_intelligence(session, rule, None, None)

    assert evaluation is not None
    assert evaluation.alert_tier == AlertTier.NEEDS_MARKET_DATA
    assert evaluation.reason_codes == (NEEDS_MARKET_DATA,)
    assert evaluation.net_profit is None


def test_evaluate_opportunity_intelligence_none_when_no_observation_yet(session: Session) -> None:
    product = crud.create_product(session, "Global Rule Product")
    rule = crud.create_watch_rule(session, product_id=product.id, check_interval=60, max_quantity=1)

    assert evaluate_opportunity_intelligence(session, rule, None, None) is None


def test_evaluate_opportunity_intelligence_scores_a_real_opportunity(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session, price="60")
    crud.update_watch_rule(
        session,
        rule_id,
        estimated_resale_price=Decimal("150"),
        estimated_resale_trusted=True,
    )
    rule = crud.get_watch_rule(session, rule_id)

    evaluation = evaluate_opportunity_intelligence(session, rule, None, None)

    assert evaluation is not None
    assert evaluation.net_profit == Decimal("90.00")
    assert evaluation.resale_confidence == "high"
    assert evaluation.resale_price_source == "manual"
    assert evaluation.alert_tier in (AlertTier.WATCH, AlertTier.HIGH, AlertTier.URGENT)


def _seed_multi_retailer_product(session: Session) -> int:
    """One real Product, two real Listings on different merchants — the
    shape best_buy_source_for_product() is meant for (Phase 31 section 10:
    "mêmes produits présents sur plusieurs retailers")."""
    product = crud.create_product(session, "Pokémon My Partner Evoli")
    cheap_merchant = crud.create_merchant(session, "JouéClub")
    pricey_merchant = crud.create_merchant(session, "La Grande Récré")
    cheap_listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=cheap_merchant.id,
        url="https://joueclub.example/p/1",
        external_id="jc-1",
    )
    pricey_listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=pricey_merchant.id,
        url="https://lgr.example/p/1",
        external_id="lgr-1",
    )
    cheap_rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=cheap_listing.id,
        check_interval=60,
        max_quantity=1,
    )
    crud.update_watch_rule(
        session,
        cheap_rule.id,
        estimated_resale_price=Decimal("110"),
        estimated_resale_trusted=True,
    )
    pricey_rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=pricey_listing.id,
        check_interval=60,
        max_quantity=1,
    )
    crud.update_watch_rule(
        session,
        pricey_rule.id,
        estimated_resale_price=Decimal("110"),
        estimated_resale_trusted=True,
    )
    crud.create_observation_record(
        session,
        listing_id=cheap_listing.id,
        observation=ProductObservation(
            merchant="JouéClub",
            external_id="jc-1",
            name="Pokémon My Partner Evoli",
            price=Decimal("32.99"),
            currency="EUR",
            available=True,
            url="https://joueclub.example/p/1",
            observed_at=datetime.now(UTC),
        ),
    )
    crud.create_observation_record(
        session,
        listing_id=pricey_listing.id,
        observation=ProductObservation(
            merchant="La Grande Récré",
            external_id="lgr-1",
            name="Pokémon My Partner Evoli",
            price=Decimal("31.99"),
            currency="EUR",
            available=True,
            url="https://lgr.example/p/1",
            observed_at=datetime.now(UTC),
        ),
    )
    return product.id


def test_best_buy_source_picks_the_cheaper_equally_good_listing(session: Session) -> None:
    product_id = _seed_multi_retailer_product(session)
    product = crud.get_product(session, product_id)

    best = best_buy_source_for_product(session, product, None, None)

    assert best is not None
    # Same resale estimate/confidence on both -> the cheaper purchase price
    # (La Grande Récré, 31.99) yields strictly better net_profit/ROI.
    assert best.net_profit == Decimal("78.01")


def test_best_buy_source_other_listings_stay_monitored(session: Session) -> None:
    """Comparing must never disable or delete the losing Listing/WatchRule
    — both stay enabled and independently monitored."""
    product_id = _seed_multi_retailer_product(session)
    product = crud.get_product(session, product_id)

    best_buy_source_for_product(session, product, None, None)

    rules = crud.list_watch_rules(session, product_id=product_id)
    assert len(rules) == 2
    assert all(r.enabled for r in rules)


def test_best_buy_source_none_when_no_watch_rule_has_data(session: Session) -> None:
    product = crud.create_product(session, "No Data Yet")
    assert best_buy_source_for_product(session, product, None, None) is None
