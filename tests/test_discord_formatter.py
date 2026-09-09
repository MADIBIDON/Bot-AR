from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from engine.change_detection import EventType, MonitoringEvent
from notifications.discord.formatter import format_event_embed
from products.matcher import MatchResult
from products.observation import ProductObservation


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


def _match(confidence: int = 100) -> MatchResult:
    return MatchResult(matched=True, confidence=confidence, method="ean_exact", reason="test")


def _event(event_type: EventType, **overrides: object) -> MonitoringEvent:
    defaults: dict[str, object] = dict(
        event_type=event_type,
        listing_id=1,
        watch_rule_id=1,
        occurred_at=datetime.now(UTC),
        reason="test",
        previous_value=None,
        current_value=None,
    )
    defaults.update(overrides)
    return MonitoringEvent(**defaults)  # type: ignore[arg-type]


def _field(embed, name: str) -> str | None:
    for f in embed.fields:
        if f.name == name:
            return f.value
    return None


def test_embed_contains_product_and_merchant() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert _field(embed, "Product") == "Duopack Evoli 30 ans"
    assert _field(embed, "Merchant") == "FakeStore"


def test_embed_contains_price() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert _field(embed, "Price") == "13.99 EUR"


def test_embed_contains_url() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert embed.url == "https://fake-store.example/p/fake-123"


def test_embed_contains_match_confidence() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match(confidence=90))
    assert _field(embed, "Match confidence") == "90%"


def test_price_drop_event_shows_previous_price() -> None:
    event = _event(EventType.PRICE_DROP, previous_value="16.99", current_value="13.99")
    embed = format_event_embed(event, _observation(), _match())
    assert embed.title == "💰 Price Drop"
    assert _field(embed, "Previous price") == "16.99"


def test_stock_available_event_has_no_previous_price_field() -> None:
    event = _event(EventType.STOCK_AVAILABLE, previous_value="False", current_value="True")
    embed = format_event_embed(event, _observation(available=True), _match())
    assert embed.title == "📦 Back in Stock"
    assert _field(embed, "Previous price") is None
    assert _field(embed, "Availability") == "In stock"


def test_unavailable_observation_shows_out_of_stock() -> None:
    embed = format_event_embed(
        _event(EventType.STOCK_UNAVAILABLE), _observation(available=False), _match()
    )
    assert _field(embed, "Availability") == "Out of stock"


def test_embed_has_timestamp() -> None:
    event = _event(EventType.PRICE_DROP)
    embed = format_event_embed(event, _observation(), _match())
    assert embed.timestamp == event.occurred_at
