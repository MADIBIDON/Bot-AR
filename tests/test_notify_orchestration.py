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


def test_multiple_events_send_one_embed_each() -> None:
    notifier = FakeNotifier()
    events = (_event(), _event())

    asyncio.run(notify_events_if_allowed(_rule(), _observation(), _match(), events, notifier))

    assert len(notifier.sent_embeds) == 2
