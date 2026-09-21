"""No discord.py Client is ever constructed here — FakeNotifier only
implements the EmbedSender protocol, so these tests never touch the
network, matching the "no network during pytest" requirement.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

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
    """One embed per DISTINCT event. Two identical events are the same
    news twice and are collapsed on purpose — see notifications/dedup.py
    and tests/test_alert_dedup.py."""
    notifier = FakeNotifier()
    stock_event = MonitoringEvent(
        event_type=EventType.STOCK_AVAILABLE,
        listing_id=1,
        watch_rule_id=1,
        occurred_at=datetime.now(UTC),
        reason="test",
        previous_value="False",
        current_value="True",
    )

    asyncio.run(
        notify_events_if_allowed(
            _rule(), _observation(), _match(), (_event(), stock_event), notifier
        )
    )

    assert len(notifier.sent_embeds) == 2


def test_identical_repeated_event_is_sent_once() -> None:
    """The real 16/09 flapping case, at the orchestration level."""
    notifier = FakeNotifier()

    asyncio.run(
        notify_events_if_allowed(_rule(), _observation(), _match(), (_event(), _event()), notifier)
    )

    assert len(notifier.sent_embeds) == 1


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


def test_slow_market_source_does_not_block_the_event_loop() -> None:
    """Phase 23: a slow market-data lookup (real eBay latency) is
    offloaded to a thread so it can't starve other coroutines sharing the
    event loop — e.g. another WatchRule's own concurrent monitoring check
    or notification, running via asyncio.gather elsewhere in the worker.
    Proven here the same way as the discovery/purchase timing tests: a
    concurrently-scheduled coroutine finishes on time despite the slow
    lookup running "at the same time"."""
    import time

    from database.models import Product
    from market_data.base import MarketDataSource
    from market_data.cache import TTLCache
    from market_data.models import MarketObservation
    from market_data.registry import MarketDataRegistry

    class SlowSource(MarketDataSource):
        def search(self, query: str, *, limit: int = 20) -> list[MarketObservation]:
            time.sleep(0.2)
            return [
                MarketObservation(
                    source="ebay",
                    product_name="Pokemon ETB Test",
                    price=Decimal("100"),
                    currency="EUR",
                    listing_url="https://ebay.example/1",
                    external_id="1",
                    observed_at=datetime.now(UTC),
                )
            ]

    registry = MarketDataRegistry()
    registry.register("ebay", SlowSource())
    notifier = FakeNotifier()
    rule = _rule(
        resale_price_mode="market",
        market_source="ebay",
        product=Product(id=1, name="Pokemon ETB Test"),
    )

    async def other_coroutine_finished_first() -> bool:
        marker: list[str] = []

        async def other() -> None:
            await asyncio.sleep(0.02)
            marker.append("other")

        async def slow_notify() -> None:
            await notify_events_if_allowed(
                rule,
                _observation(price=Decimal("74.90")),
                _match(),
                (_event(),),
                notifier,
                registry,
                TTLCache(),
            )
            marker.append("notify")

        await asyncio.gather(slow_notify(), other())
        return marker[0] == "other"

    assert asyncio.run(other_coroutine_finished_first()) is True
    assert len(notifier.sent_embeds) == 1


# --- Phase 26: Discord send retry/timeout/dispatch --------------------------


class AlwaysFailingNotifier:
    def __init__(self) -> None:
        self.calls = 0

    async def send_embed(self, embed: object) -> None:
        self.calls += 1
        raise RuntimeError("simulated Discord outage")


class HangingNotifier:
    """Never returns — proves the timeout actually fires rather than
    hanging forever."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_embed(self, embed: object) -> None:
        self.calls += 1
        await asyncio.sleep(3600)


def test_failing_notifier_is_retried_once_then_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    notifier = AlwaysFailingNotifier()

    with caplog.at_level(logging.ERROR, logger="app.notify"):
        decision = asyncio.run(
            notify_events_if_allowed(_rule(), _observation(), _match(), (_event(),), notifier)
        )

    assert decision.allowed is True  # the decision itself is unaffected by delivery failure
    assert notifier.calls == 2  # one retry, per _SEND_MAX_ATTEMPTS
    assert any("notification FAILED after" in r.message for r in caplog.records)


def test_hanging_notifier_times_out_instead_of_blocking_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.notify as notify_module

    monkeypatch.setattr(notify_module, "_SEND_TIMEOUT_SECONDS", 0.2)
    notifier = HangingNotifier()

    async def scenario() -> float:
        start = time.monotonic()
        await notify_events_if_allowed(_rule(), _observation(), _match(), (_event(),), notifier)
        return time.monotonic() - start

    elapsed = asyncio.run(scenario())

    assert elapsed < 2.0  # both 0.2s-bounded attempts timed out, nothing actually hung
    assert notifier.calls == 2


def test_dispatch_callback_receives_send_coroutine_instead_of_being_awaited_inline() -> None:
    """When `dispatch` is given, notify_events_if_allowed must return
    without the embed having been sent yet — the caller decides when/how
    to run it (app/worker.py fires it as a background task)."""
    notifier = FakeNotifier()
    captured: list[object] = []

    def fake_dispatch(coro: object) -> None:
        captured.append(coro)
        coro.close()  # never actually run — just prove it was handed over, not awaited

    decision = asyncio.run(
        notify_events_if_allowed(
            _rule(), _observation(), _match(), (_event(),), notifier, dispatch=fake_dispatch
        )
    )

    assert decision.allowed is True
    assert len(captured) == 1
    assert notifier.sent_embeds == []  # nothing was actually sent inline
