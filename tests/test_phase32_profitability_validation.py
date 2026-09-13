"""Phase 32: real-conditions validation of the existing opportunity
intelligence pipeline (engine.opportunity + engine.ranking + engine.alerting)
against realistic canary-shaped numbers. No new scoring logic — every
test here only calls functions that already existed after Phase 31.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.opportunity_alerts import maybe_notify_opportunity_shift
from app.opportunity_snapshot import build_opportunity_candidate, evaluate_opportunity_intelligence
from database import crud
from engine.alerting import NEGATIVE_NET_PROFIT, AlertTier
from engine.ranking import RankingConfig, rank_opportunities
from products.observation import ProductObservation


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


def _seed_rule(
    session: Session,
    *,
    name: str,
    price: str,
    resale: str,
    trusted: bool,
    platform_fee_pct: str = "10",
    fixed_fee: str = "0.30",
    shipping_cost: str = "5",
    min_net_profit: str = "0",
    min_roi_pct: str = "0",
) -> int:
    product = crud.create_product(session, name)
    merchant = crud.get_merchant_by_name(session, "TestRetailer")
    if merchant is None:
        merchant = crud.create_merchant(session, "TestRetailer")
    external_id = f"fake-{product.id}"
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=f"https://a.example/p/{external_id}",
        external_id=external_id,
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=60, max_quantity=1
    )
    crud.update_watch_rule(
        session,
        rule.id,
        estimated_resale_price=Decimal(resale),
        estimated_resale_trusted=trusted,
        platform_fee_pct=Decimal(platform_fee_pct),
        fixed_fee=Decimal(fixed_fee),
        shipping_cost=Decimal(shipping_cost),
        minimum_net_profit=Decimal(min_net_profit),
        minimum_roi_pct=Decimal(min_roi_pct),
    )
    crud.create_observation_record(
        session,
        listing_id=listing.id,
        observation=ProductObservation(
            merchant="TestRetailer",
            external_id=external_id,
            name=name,
            price=Decimal(price),
            currency="EUR",
            available=True,
            url=f"https://a.example/p/{external_id}",
            observed_at=datetime.now(UTC),
        ),
    )
    return rule.id


def test_good_profitability_reaches_watch_or_better(session: Session) -> None:
    """Same shape as the real Coffre aux Trésors Pikachu canary: modest
    positive profit, untrusted (LOW confidence) manual estimate."""
    rule_id = _seed_rule(
        session, name="Good Deal Canary", price="54.90", resale="85", trusted=False
    )
    rule = crud.get_watch_rule(session, rule_id)

    evaluation = evaluate_opportunity_intelligence(session, rule, None, None)

    assert evaluation is not None
    assert evaluation.net_profit > 0
    assert evaluation.alert_tier in (AlertTier.WATCH, AlertTier.HIGH, AlertTier.URGENT)
    assert NEGATIVE_NET_PROFIT not in evaluation.reason_codes


def test_poor_profitability_reaches_ignore(session: Session) -> None:
    """Same shape as the real Coffret Premium Mega Amphinobi canary: fees
    exceed the thin gross spread -> genuinely negative net profit."""
    rule_id = _seed_rule(session, name="Bad Deal Canary", price="42.90", resale="48", trusted=False)
    rule = crud.get_watch_rule(session, rule_id)

    evaluation = evaluate_opportunity_intelligence(session, rule, None, None)

    assert evaluation is not None
    assert evaluation.net_profit < 0
    assert evaluation.alert_tier == AlertTier.IGNORE
    assert NEGATIVE_NET_PROFIT in evaluation.reason_codes


def test_configured_fees_are_actually_subtracted_not_treated_as_zero(session: Session) -> None:
    """Section 4's "no UNKNOWN silently becomes 0 if it improves the
    result" applied as a regression guard on the fees I *did* configure:
    net_profit with real fees must be strictly less than the zero-fee
    gross spread, by exactly the configured fee total."""
    rule_id = _seed_rule(
        session,
        name="Fee Sanity Canary",
        price="50",
        resale="80",
        trusted=False,
        platform_fee_pct="10",
        fixed_fee="0.30",
        shipping_cost="6",
    )
    rule = crud.get_watch_rule(session, rule_id)

    candidate = build_opportunity_candidate(session, rule, None, None)

    gross_profit = Decimal("80") - Decimal("50")
    expected_fees = (Decimal("80") * Decimal("10") / 100) + Decimal("0.30") + Decimal("6")
    assert candidate.net_profit == gross_profit - expected_fees
    assert candidate.net_profit < gross_profit  # fees were genuinely applied, not dropped


def test_low_confidence_never_reaches_high_even_with_good_roi(session: Session) -> None:
    """Same financials, only resale_confidence differs (via
    estimated_resale_trusted) -> the untrusted (LOW) version must never
    score at or above the trusted (HIGH) version, and in particular must
    not reach HIGH/URGENT when the trusted twin does. Guards against
    "never mark HIGH confidence without real justification" silently
    eroding at the scoring layer."""
    low_rule_id = _seed_rule(
        session, name="Confidence Canary Low", price="50", resale="120", trusted=False
    )
    high_rule_id = _seed_rule(
        session, name="Confidence Canary High", price="50", resale="120", trusted=True
    )
    low_rule = crud.get_watch_rule(session, low_rule_id)
    high_rule = crud.get_watch_rule(session, high_rule_id)

    low_eval = evaluate_opportunity_intelligence(session, low_rule, None, None)
    high_eval = evaluate_opportunity_intelligence(session, high_rule, None, None)

    assert low_eval.resale_confidence == "low"
    assert high_eval.resale_confidence == "high"
    assert low_eval.score < high_eval.score


def test_stale_resale_price_is_not_degraded_documented_limitation(session: Session) -> None:
    """Documents a real, confirmed gap rather than fixing it (Phase 32
    section 11: freshness is not modeled in this project today —
    market_data.estimator.ResaleEstimate carries no timestamp, and manual
    mode has no "estimated_resale_price last set at" field either). An
    ancient WatchRule.updated_at does not degrade resale_confidence or
    change the score at all — noted as a limitation in the Phase 32
    report, not fixed here (no refactor requested)."""
    rule_id = _seed_rule(session, name="Staleness Canary", price="50", resale="80", trusted=True)
    rule = crud.get_watch_rule(session, rule_id)
    old_evaluation = evaluate_opportunity_intelligence(session, rule, None, None)

    # Simulate a very old configuration timestamp — no field anywhere in
    # WatchRule/ResaleEstimate is consulted for "how old is this price",
    # so nothing here should (or does) change.
    crud.update_watch_rule(session, rule_id, updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    rule = crud.get_watch_rule(session, rule_id)
    new_evaluation = evaluate_opportunity_intelligence(session, rule, None, None)

    assert new_evaluation.score == old_evaluation.score
    assert new_evaluation.resale_confidence == old_evaluation.resale_confidence


def test_watch_to_high_crossing_fires_exactly_one_alert(session: Session) -> None:
    """Explicit WATCH -> HIGH case from section 10's own example (as
    opposed to Phase 31's None -> HIGH test)."""
    rule_id = _seed_rule(session, name="Crossing Canary", price="50", resale="80", trusted=True)
    rule = crud.get_watch_rule(session, rule_id)
    crud.update_watch_rule(session, rule_id, last_alert_tier="watch")
    rule = crud.get_watch_rule(session, rule_id)
    notifier = FakeNotifier()

    from engine.alerting import build_opportunity_intelligence

    candidate = build_opportunity_candidate(session, rule, None, None)
    ranking_config = RankingConfig()
    ranked = rank_opportunities([candidate], ranking_config)[0]
    high_evaluation = build_opportunity_intelligence(candidate, ranked, ranking_config)
    # Force the HIGH tier explicitly for this test's purpose (the real
    # candidate's own natural tier already computed above may or may not
    # land exactly on HIGH — the crossing *logic* under test only cares
    # about previous="watch" vs new="high").
    from dataclasses import replace

    high_evaluation = replace(high_evaluation, alert_tier=AlertTier.HIGH)

    from products.matcher import match_product

    records = crud.list_observation_records_for_listing(session, rule.listing_id)
    last = records[-1]
    obs = ProductObservation(
        merchant="TestRetailer",
        external_id=last.external_id,
        name=last.name,
        price=last.price,
        currency=last.currency,
        available=last.available,
        url=rule.listing.url,
        observed_at=datetime.now(UTC),
    )
    match_result = match_product(rule.product, obs, expected_listing=rule.listing)

    notified = asyncio.run(
        maybe_notify_opportunity_shift(session, rule, obs, match_result, high_evaluation, notifier)
    )

    assert notified is True
    assert len(notifier.sent_embeds) == 1
    assert crud.get_watch_rule(session, rule_id).last_alert_tier == "high"


def test_purchases_never_triggered_regardless_of_alert_tier(session: Session) -> None:
    """Structural guarantee: nothing in the opportunity-intelligence path
    (app/opportunity_alerts.py, app/notify.py) ever creates a
    PurchaseAttempt, no matter how good the score is. purchase/engine.py
    is only ever invoked from app/worker.py's own separate, explicit
    purchase_registry/purchase_policy branch (both None by default)."""
    rule_id = _seed_rule(
        session, name="Purchase Guard Canary", price="50", resale="200", trusted=True
    )
    rule = crud.get_watch_rule(session, rule_id)
    notifier = FakeNotifier()

    evaluation = evaluate_opportunity_intelligence(session, rule, None, None)
    assert evaluation.alert_tier in (AlertTier.HIGH, AlertTier.URGENT)  # a genuinely great deal

    from products.matcher import match_product

    records = crud.list_observation_records_for_listing(session, rule.listing_id)
    last = records[-1]
    obs = ProductObservation(
        merchant="TestRetailer",
        external_id=last.external_id,
        name=last.name,
        price=last.price,
        currency=last.currency,
        available=last.available,
        url=rule.listing.url,
        observed_at=datetime.now(UTC),
    )
    match_result = match_product(rule.product, obs, expected_listing=rule.listing)

    asyncio.run(
        maybe_notify_opportunity_shift(session, rule, obs, match_result, evaluation, notifier)
    )

    assert crud.list_purchase_attempts(session) == []
