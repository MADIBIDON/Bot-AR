"""app/opportunity_alerts.py — the score-crossing Discord trigger used
when a tick produced no stock/price MonitoringEvent at all (see
app/worker.py's wiring). Uses a real (in-memory) session, same style as
tests/test_opportunity_snapshot.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.opportunity_alerts import maybe_notify_opportunity_shift
from database import crud
from engine.alerting import AlertTier, OpportunityIntelligence
from engine.ranking import Priority
from products.observation import ProductObservation


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


def _seed_rule_with_observation(session: Session) -> int:
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
    crud.create_observation_record(
        session,
        listing_id=listing.id,
        observation=ProductObservation(
            merchant="RetailerA",
            external_id="fake-1",
            name="Duopack Evoli",
            price=Decimal("60"),
            currency="EUR",
            available=True,
            url="https://a.example/p/1",
            observed_at=datetime.now(UTC),
        ),
    )
    return rule.id


def _evaluation(alert_tier: AlertTier, score: str = "80") -> OpportunityIntelligence:
    return OpportunityIntelligence(
        alert_tier=alert_tier,
        reason_codes=("HIGH_ROI",),
        score=Decimal(score),
        priority=Priority.HIGH,
        net_profit=Decimal("50"),
        roi_pct=Decimal("80"),
        net_margin_pct=Decimal("40"),
        estimated_resale_price=Decimal("150"),
        resale_confidence="high",
        resale_price_source="manual",
        match_confidence=90,
        market_sample_size=None,
        in_stock=True,
    )


def _observation() -> ProductObservation:
    return ProductObservation(
        merchant="RetailerA",
        external_id="fake-1",
        name="Duopack Evoli",
        price=Decimal("60"),
        currency="EUR",
        available=True,
        url="https://a.example/p/1",
        observed_at=datetime.now(UTC),
    )


def _match():
    from products.matcher import MatchResult

    return MatchResult(matched=True, confidence=90, method="ean_exact", reason="test")


def test_no_notification_when_evaluation_is_none(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session)
    rule = crud.get_watch_rule(session, rule_id)
    notifier = FakeNotifier()

    notified = asyncio.run(
        maybe_notify_opportunity_shift(session, rule, _observation(), _match(), None, notifier)
    )

    assert notified is False
    assert notifier.sent_embeds == []


def test_upward_crossing_into_high_notifies_and_persists_tier(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session)
    rule = crud.get_watch_rule(session, rule_id)
    notifier = FakeNotifier()

    notified = asyncio.run(
        maybe_notify_opportunity_shift(
            session, rule, _observation(), _match(), _evaluation(AlertTier.HIGH), notifier
        )
    )

    assert notified is True
    assert len(notifier.sent_embeds) == 1
    refreshed = crud.get_watch_rule(session, rule_id)
    assert refreshed.last_alert_tier == "high"


def test_staying_high_does_not_renotify(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session)
    rule = crud.get_watch_rule(session, rule_id)
    notifier = FakeNotifier()

    asyncio.run(
        maybe_notify_opportunity_shift(
            session, rule, _observation(), _match(), _evaluation(AlertTier.HIGH), notifier
        )
    )
    rule = crud.get_watch_rule(session, rule_id)
    notified_again = asyncio.run(
        maybe_notify_opportunity_shift(
            session, rule, _observation(), _match(), _evaluation(AlertTier.HIGH, "85"), notifier
        )
    )

    assert notified_again is False
    assert len(notifier.sent_embeds) == 1


def test_dropping_then_reentering_high_notifies_again(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session)
    rule = crud.get_watch_rule(session, rule_id)
    notifier = FakeNotifier()

    asyncio.run(
        maybe_notify_opportunity_shift(
            session, rule, _observation(), _match(), _evaluation(AlertTier.HIGH), notifier
        )
    )
    rule = crud.get_watch_rule(session, rule_id)
    asyncio.run(
        maybe_notify_opportunity_shift(
            session, rule, _observation(), _match(), _evaluation(AlertTier.WATCH), notifier
        )
    )
    rule = crud.get_watch_rule(session, rule_id)
    assert rule.last_alert_tier == "watch"

    # A real subsequent tick always writes a fresh ObservationRecord (even
    # when nothing changed) — reuse the same one here would trip
    # EventRecord's own (event_type, watch_rule_id, observation_record_id)
    # dedup constraint for a reason that has nothing to do with the tier
    # logic under test.
    crud.create_observation_record(session, listing_id=rule.listing_id, observation=_observation())

    notified_again = asyncio.run(
        maybe_notify_opportunity_shift(
            session, rule, _observation(), _match(), _evaluation(AlertTier.URGENT), notifier
        )
    )

    assert notified_again is True
    assert len(notifier.sent_embeds) == 2


def test_ignore_and_needs_market_data_never_notify(session: Session) -> None:
    rule_id = _seed_rule_with_observation(session)
    rule = crud.get_watch_rule(session, rule_id)
    notifier = FakeNotifier()

    for tier in (AlertTier.IGNORE, AlertTier.NEEDS_MARKET_DATA, AlertTier.WATCH):
        rule = crud.get_watch_rule(session, rule_id)
        notified = asyncio.run(
            maybe_notify_opportunity_shift(
                session, rule, _observation(), _match(), _evaluation(tier), notifier
            )
        )
        assert notified is False

    assert notifier.sent_embeds == []
