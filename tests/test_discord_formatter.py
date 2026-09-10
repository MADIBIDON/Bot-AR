from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from engine.change_detection import EventType, MonitoringEvent
from engine.opportunity import OpportunityConfig, evaluate_opportunity
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


def test_embed_without_opportunity_has_no_opportunity_fields() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert _field(embed, "Opportunity") is None
    assert _field(embed, "ROI") is None


def test_embed_with_opportunity_adds_metrics_fields() -> None:
    config = OpportunityConfig(
        estimated_resale_price=Decimal("110"),
        platform_fee_pct=Decimal("9"),
        shipping_cost=Decimal("6.50"),
    )
    opportunity = evaluate_opportunity(Decimal("74.90"), config)

    embed = format_event_embed(
        _event(EventType.PRICE_DROP),
        _observation(price=Decimal("74.90")),
        _match(),
        opportunity=opportunity,
    )

    assert _field(embed, "Estimated resale") == "110.00 EUR"
    assert _field(embed, "Net profit") == "18.70 EUR"
    assert _field(embed, "ROI") == "24.97%"
    assert _field(embed, "Margin") == "17.00%"
    assert _field(embed, "Opportunity") == "buy_candidate"


def test_embed_without_resale_estimate_has_no_market_fields() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert _field(embed, "Market source") is None


def test_embed_with_resale_estimate_adds_market_fields() -> None:
    from market_data.estimator import Confidence, ResaleEstimate

    estimate = ResaleEstimate(
        estimated_price=Decimal("100"),
        sample_size=5,
        min_price=Decimal("90"),
        max_price=Decimal("110"),
        median_price=Decimal("100"),
        mean_price=Decimal("100"),
        confidence=Confidence.MEDIUM,
        source="ebay",
        method="median",
        based_on_sold_data=False,
        reason="test",
    )

    embed = format_event_embed(
        _event(EventType.PRICE_DROP), _observation(), _match(), resale_estimate=estimate
    )

    assert _field(embed, "Market source") == "ebay"
    assert _field(embed, "Market sample size") == "5"
    assert _field(embed, "Market confidence") == "medium"


def test_embed_with_empty_resale_estimate_has_no_market_fields() -> None:
    from market_data.estimator import Confidence, ResaleEstimate

    estimate = ResaleEstimate(
        estimated_price=None,
        sample_size=0,
        min_price=None,
        max_price=None,
        median_price=None,
        mean_price=None,
        confidence=Confidence.LOW,
        source="unknown",
        method="none",
        based_on_sold_data=False,
        reason="no observations",
    )

    embed = format_event_embed(
        _event(EventType.PRICE_DROP), _observation(), _match(), resale_estimate=estimate
    )

    assert _field(embed, "Market source") is None
