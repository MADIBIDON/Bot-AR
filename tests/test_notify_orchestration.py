"""No discord.py Client is ever constructed here — FakeNotifier only
implements the EmbedSender protocol, so these tests never touch the
network, matching the "no network during pytest" requirement.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.notify import notify_events_if_allowed
from database.models import WatchRule
from engine.change_detection import EventType, MonitoringEvent
from engine.decision import DecisionCode
from products.matcher import MatchResult
from products.observation import ProductObservation


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


def _rule(**overrides: object) -> WatchRule:
    defaults: dict[str, object] = dict(
        id=1,
        product_id=1,
        check_interval=300,
        max_quantity=1,
        enabled=True,
        target_price=None,
        max_price=None,
    )
    defaults.update(overrides)
    return WatchRule(**defaults)  # type: ignore[arg-type]


def _observation(**overrides: object) -> ProductObservation:
    defaults: dict[str, object] = dict(
        merchant="FakeStore",
        external_id="fake-123",
        name="Duopack Evoli 30 ans",
        price=Decimal("13.99"),
        currency="EUR",
        available=True,
        url="https://fake-store.example/p/fake-123",
        observed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ProductObservation(**defaults)  # type: ignore[arg-type]


def _match(matched: bool = True, confidence: int = 100) -> MatchResult:
    return MatchResult(matched=matched, confidence=confidence, method="ean_exact", reason="test")


def _event() -> MonitoringEvent:
    return MonitoringEvent(
        event_type=EventType.PRICE_DROP,
        listing_id=1,
        watch_rule_id=1,
        occurred_at=datetime.now(UTC),
        reason="test",
        previous_value="16.99",
        current_value="13.99",
    )


def test_allowed_decision_with_events_sends_notification() -> None:
    notifier = FakeNotifier()

    decision = asyncio.run(
        notify_events_if_allowed(_rule(), _observation(), _match(), (_event(),), notifier)
    )

    assert decision.decision_code == DecisionCode.ALLOW
    assert len(notifier.sent_embeds) == 1


def test_rejected_decision_sends_no_notification() -> None:
    notifier = FakeNotifier()
    rule = _rule(max_price=Decimal("5"))  # observation price 13.99 > 5 -> rejected

    decision = asyncio.run(
        notify_events_if_allowed(rule, _observation(), _match(), (_event(),), notifier)
    )

    assert decision.allowed is False
    assert decision.decision_code == DecisionCode.PRICE_ABOVE_MAX
    assert notifier.sent_embeds == []


def test_disabled_rule_sends_no_notification() -> None:
    notifier = FakeNotifier()

    decision = asyncio.run(
        notify_events_if_allowed(
            _rule(enabled=False), _observation(), _match(), (_event(),), notifier
        )
    )

    assert decision.decision_code == DecisionCode.RULE_DISABLED
    assert notifier.sent_embeds == []


def test_allowed_but_no_events_sends_no_notification() -> None:
    notifier = FakeNotifier()

    decision = asyncio.run(
        notify_events_if_allowed(_rule(), _observation(), _match(), (), notifier)
    )

    assert decision.decision_code == DecisionCode.ALLOW
    assert notifier.sent_embeds == []


def test_opportunity_configured_but_no_events_still_sends_no_notification() -> None:
    """Phase 15 regression: an opportunity score never triggers a
    notification on its own — the existing policy (allowed decision + at
    least one event) is unchanged."""
    notifier = FakeNotifier()
    rule = _rule(
        estimated_resale_price=Decimal("110"),
        platform_fee_pct=Decimal("9"),
        shipping_cost=Decimal("6.50"),
    )

    decision = asyncio.run(notify_events_if_allowed(rule, _observation(), _match(), (), notifier))

    assert decision.decision_code == DecisionCode.ALLOW
    assert notifier.sent_embeds == []


def test_opportunity_enriches_embed_when_event_and_configured() -> None:
    notifier = FakeNotifier()
    rule = _rule(
        estimated_resale_price=Decimal("110"),
        platform_fee_pct=Decimal("9"),
        shipping_cost=Decimal("6.50"),
    )

    asyncio.run(
        notify_events_if_allowed(
            rule, _observation(price=Decimal("74.90")), _match(), (_event(),), notifier
        )
    )

    assert len(notifier.sent_embeds) == 1
    embed = notifier.sent_embeds[0]
    field_names = {field.name for field in embed.fields}
    assert "Opportunity" in field_names
    assert "ROI" in field_names


def test_multiple_events_send_one_embed_each() -> None:
    notifier = FakeNotifier()
    events = (_event(), _event())

    asyncio.run(notify_events_if_allowed(_rule(), _observation(), _match(), events, notifier))

    assert len(notifier.sent_embeds) == 2


def test_market_mode_without_registry_sends_embed_with_no_opportunity() -> None:
    """Phase 16: a market-mode rule with no registry provided (e.g. eBay
    not configured) must never crash the pipeline — the embed is still
    sent for the event, just without opportunity/market enrichment."""
    from database.models import Product

    notifier = FakeNotifier()
    rule = _rule(resale_price_mode="market", market_source="ebay", product=Product(id=1, name="X"))

    decision = asyncio.run(
        notify_events_if_allowed(rule, _observation(), _match(), (_event(),), notifier)
    )

    assert decision.allowed is True
    assert len(notifier.sent_embeds) == 1
    field_names = {field.name for field in notifier.sent_embeds[0].fields}
    assert "Opportunity" not in field_names
    assert "Market source" not in field_names


def test_market_mode_with_registry_enriches_embed() -> None:
    from database.models import Product
    from market_data.base import MarketDataSource
    from market_data.cache import TTLCache
    from market_data.models import MarketObservation
    from market_data.registry import MarketDataRegistry

    class FakeSource(MarketDataSource):
        def search(self, query: str, *, limit: int = 20) -> list[MarketObservation]:
            return [
                MarketObservation(
                    source="ebay",
                    product_name="Pokemon ETB Test",
                    price=Decimal(p),
                    currency="EUR",
                    listing_url="https://ebay.example/1",
                    external_id="1",
                    observed_at=datetime.now(UTC),
                )
                for p in ("90", "100", "110")
            ]

    registry = MarketDataRegistry()
    registry.register("ebay", FakeSource())

    notifier = FakeNotifier()
    rule = _rule(
        resale_price_mode="market",
        market_source="ebay",
        product=Product(id=1, name="Pokemon ETB Test"),
    )

    asyncio.run(
        notify_events_if_allowed(
            rule,
            _observation(price=Decimal("74.90")),
            _match(),
            (_event(),),
            notifier,
            registry,
            TTLCache(),
        )
    )

    assert len(notifier.sent_embeds) == 1
    field_names = {field.name for field in notifier.sent_embeds[0].fields}
    assert "Market source" in field_names
    assert "Opportunity" in field_names
