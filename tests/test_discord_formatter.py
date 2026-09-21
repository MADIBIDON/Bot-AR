from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from engine.alerting import AlertTier, OpportunityIntelligence
from engine.change_detection import EventType, MonitoringEvent
from engine.opportunity import OpportunityConfig, evaluate_opportunity
from engine.ranking import Priority
from notifications.discord.formatter import format_event_embed, format_purchase_embed
from products.matcher import MatchResult
from products.observation import ProductObservation
from purchase.models import PurchaseIntent


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
    """Product name is the clickable headline and the merchant sits
    under it — the reference drop-monitor layout."""
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert "Duopack Evoli 30 ans" in embed.description
    assert "https://fake-store.example/p/fake-123" in embed.description
    assert "FakeStore" in embed.description


def test_embed_shows_pid_and_links() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert _field(embed, "PID") == "fake-123"
    links = _field(embed, "Links")
    assert "[Link](https://fake-store.example/p/fake-123)" in links
    assert "[StockX]" in links


def test_embed_shows_product_thumbnail_when_the_merchant_exposes_one() -> None:
    embed = format_event_embed(
        _event(EventType.PRICE_DROP),
        _observation(image_url="https://images.example/p.jpg"),
        _match(),
    )
    assert embed.thumbnail.url == "https://images.example/p.jpg"


def test_embed_has_no_thumbnail_when_no_image_is_known() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert embed.thumbnail.url is None


def test_embed_contains_price() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert _field(embed, "Price") == "13.99 EUR"


def test_embed_contains_url() -> None:
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match())
    assert embed.url == "https://fake-store.example/p/fake-123"


def test_embed_omits_internal_diagnostics() -> None:
    """Match confidence is an internal signal. On a drop it sits between
    the reader and the buy button, so it is deliberately not rendered —
    it stays available via `watch.py show`."""
    embed = format_event_embed(_event(EventType.PRICE_DROP), _observation(), _match(confidence=90))
    assert _field(embed, "Match confidence") is None


def test_price_drop_event_shows_previous_price() -> None:
    event = _event(EventType.PRICE_DROP, previous_value="16.99", current_value="13.99")
    embed = format_event_embed(event, _observation(), _match())
    assert embed.title == "💰 Price Drop"
    assert "16.99" in _field(embed, "Price")


def test_stock_available_event_has_no_previous_price() -> None:
    event = _event(EventType.STOCK_AVAILABLE, previous_value="False", current_value="True")
    embed = format_event_embed(event, _observation(available=True), _match())
    assert embed.title == "📦 Back in Stock"
    assert "avant" not in _field(embed, "Price")
    assert _field(embed, "Status") == "In stock"


def test_unavailable_observation_shows_out_of_stock() -> None:
    embed = format_event_embed(
        _event(EventType.STOCK_UNAVAILABLE), _observation(available=False), _match()
    )
    assert _field(embed, "Status") == "Out of stock"


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


def _evaluation(**overrides: object) -> OpportunityIntelligence:
    defaults: dict[str, object] = dict(
        alert_tier=AlertTier.HIGH,
        reason_codes=("HIGH_ROI", "GOOD_NET_PROFIT"),
        score=Decimal("78.50"),
        priority=Priority.HIGH,
        net_profit=Decimal("25"),
        roi_pct=Decimal("33"),
        net_margin_pct=Decimal("22"),
        estimated_resale_price=Decimal("110"),
        resale_confidence="high",
        resale_price_source="manual",
        match_confidence=95,
        market_sample_size=None,
        in_stock=True,
    )
    defaults.update(overrides)
    return OpportunityIntelligence(**defaults)  # type: ignore[arg-type]


def test_embed_shows_score_priority_and_reason_codes_for_a_real_opportunity() -> None:
    embed = format_event_embed(
        _event(EventType.STOCK_AVAILABLE), _observation(), _match(), evaluation=_evaluation()
    )

    assert _field(embed, "Score") == "78.50/100"
    assert _field(embed, "Priority") == "HIGH"
    assert _field(embed, "Resale price source") == "manual"
    assert _field(embed, "Reason codes") == "HIGH_ROI, GOOD_NET_PROFIT"
    assert _field(embed, "⚠️ Market Data") is None


def test_embed_shows_market_data_missing_instead_of_a_bad_verdict() -> None:
    evaluation = _evaluation(
        alert_tier=AlertTier.NEEDS_MARKET_DATA,
        reason_codes=("NEEDS_MARKET_DATA",),
        net_profit=None,
        roi_pct=None,
        net_margin_pct=None,
        estimated_resale_price=None,
        resale_confidence=None,
        resale_price_source=None,
    )

    embed = format_event_embed(
        _event(EventType.STOCK_AVAILABLE), _observation(), _match(), evaluation=evaluation
    )

    assert "MARKET DATA MISSING" in _field(embed, "⚠️ Market Data")
    assert _field(embed, "Score") is None
    assert _field(embed, "Priority") is None


def test_embed_without_evaluation_has_no_new_fields() -> None:
    embed = format_event_embed(_event(EventType.STOCK_AVAILABLE), _observation(), _match())

    assert _field(embed, "Score") is None
    assert _field(embed, "Priority") is None
    assert _field(embed, "Reason codes") is None
    assert _field(embed, "⚠️ Market Data") is None


def test_opportunity_score_improved_event_has_its_own_title() -> None:
    embed = format_event_embed(
        _event(EventType.OPPORTUNITY_SCORE_IMPROVED),
        _observation(),
        _match(),
        evaluation=_evaluation(),
    )

    assert embed.title == "🚀 Opportunity Improved"


# --- Phase 40 section 21/22: purchase-lifecycle Discord cockpit --------


def _intent() -> PurchaseIntent:
    return PurchaseIntent(
        watch_rule_id=1,
        product_id=1,
        listing_id=1,
        merchant="Cultura",
        product_name="ETB 30e",
        url="https://www.cultura.com/p-etb-30e.html",
        observed_price=Decimal("59.99"),
        max_price_allowed=Decimal("90"),
        quantity=1,
        match_confidence=100,
        created_at=datetime.now(UTC),
    )


def test_buy_now_embed_has_direct_url_and_action_fields() -> None:
    embed = format_purchase_embed("🚨 BUY NOW", _intent(), reason="No PurchaseConnector.")

    assert _field(embed, "Direct URL") == _intent().url
    assert _field(embed, "Action") == "OPEN CHECKOUT"


def test_human_action_required_embed_has_direct_url_and_action_fields() -> None:
    embed = format_purchase_embed(
        "🟠 HUMAN ACTION REQUIRED", _intent(), reason="3-D Secure required."
    )

    assert _field(embed, "Direct URL") == _intent().url
    assert _field(embed, "Action") == "OPEN CHECKOUT"


def test_purchase_started_embed_has_no_action_fields() -> None:
    """Only the two "a human must act now" titles get the actionable
    fields — a plain lifecycle update (started/success/failed) never
    needs a "go open the checkout" instruction."""
    embed = format_purchase_embed(
        "⚡ AUTO PURCHASE STARTED", _intent(), reason="Checkout in progress."
    )

    assert _field(embed, "Direct URL") is None
    assert _field(embed, "Action") is None


def test_embed_timestamp_is_utc_not_shifted_by_local_offset() -> None:
    """Regression (21/09): occurred_at is stored naive-UTC. discord.py
    reads a naive datetime as LOCAL time, which shifted every footer by
    the machine's UTC offset — alerts sent seconds after a restock
    displayed a time hours earlier, and looked badly late. The embed
    timestamp must always carry UTC."""
    naive_utc = datetime(2026, 9, 16, 8, 18, 54)
    event = _event(EventType.STOCK_AVAILABLE, occurred_at=naive_utc)

    embed = format_event_embed(event, _observation(), _match())

    assert embed.timestamp is not None
    assert embed.timestamp.tzinfo is not None
    assert embed.timestamp.utcoffset().total_seconds() == 0
    assert embed.timestamp.hour == 8  # never shifted to 06:18 or 10:18
