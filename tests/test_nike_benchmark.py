"""app/nike_benchmark.py — release/detection/Discord latency reporting
for the Nike SNKRS scheduled-release test case. Never touches a real
Nike watch; a simulated timeline built directly in the (in-memory) test
database, same style as tests/test_delivery_integration.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.nike_benchmark import build_nike_benchmark_report
from database import crud
from products.observation import ProductObservation

RELEASE_AT = datetime(2026, 9, 15, 7, 0, 0, tzinfo=UTC)


def _seed_rule(session: Session, *, scheduled_release_at=RELEASE_AT) -> int:
    product = crud.create_product(session, "Nike SB Air Force 1 x Yuto")
    merchant = crud.create_merchant(session, "Nike SNKRS")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://www.nike.com/fr/launch/t/nike-sb-air-force-1-yuto",
        external_id="nike-sb-yuto",
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=30, max_quantity=1
    )
    crud.update_watch_rule(session, rule.id, scheduled_release_at=scheduled_release_at)
    return rule.id


def _observation(*, available: bool, observed_at: datetime) -> ProductObservation:
    return ProductObservation(
        merchant="Nike SNKRS",
        external_id="nike-sb-yuto",
        name="Nike SB Air Force 1 x Yuto",
        price=Decimal("119.99"),
        currency="EUR",
        available=available,
        url="https://www.nike.com/fr/launch/t/nike-sb-air-force-1-yuto",
        observed_at=observed_at,
    )


def test_no_data_when_rule_has_no_listing(session: Session) -> None:
    product = crud.create_product(session, "No Listing Yet")
    rule = crud.create_watch_rule(session, product_id=product.id, check_interval=30, max_quantity=1)

    report = build_nike_benchmark_report(session, rule.id)

    assert report.status == "no_data"


def test_not_yet_released_before_any_stock_available_event(session: Session) -> None:
    rule_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    crud.create_observation_record(
        session,
        listing_id=rule.listing_id,
        observation=_observation(available=False, observed_at=RELEASE_AT - timedelta(hours=1)),
    )

    report = build_nike_benchmark_report(session, rule_id)

    assert report.status == "not_yet_released"
    assert report.scheduled_release_at == RELEASE_AT
    assert report.event_detected_at is None


def test_awaiting_discord_send_once_detected_but_not_yet_notified(session: Session) -> None:
    rule_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    baseline_time = RELEASE_AT - timedelta(minutes=1)
    crud.create_observation_record(
        session,
        listing_id=rule.listing_id,
        observation=_observation(available=False, observed_at=baseline_time),
    )
    detected_at = RELEASE_AT + timedelta(seconds=12)
    current = crud.create_observation_record(
        session,
        listing_id=rule.listing_id,
        observation=_observation(available=True, observed_at=detected_at),
    )
    crud.create_event_record(
        session,
        event_type="stock_available",
        listing_id=rule.listing_id,
        watch_rule_id=rule.id,
        observation_record_id=current.id,
        occurred_at=detected_at,
    )

    report = build_nike_benchmark_report(session, rule_id)

    assert report.status == "awaiting_discord_send"
    assert report.event_detected_at == detected_at
    assert report.release_to_detection_seconds == 12.0
    assert report.discord_notified_at is None
    assert report.total_latency_seconds is None


def test_measured_end_to_end_latency(session: Session) -> None:
    rule_id = _seed_rule(session)
    rule = crud.get_watch_rule(session, rule_id)
    baseline_time = RELEASE_AT - timedelta(minutes=1)
    crud.create_observation_record(
        session,
        listing_id=rule.listing_id,
        observation=_observation(available=False, observed_at=baseline_time),
    )
    detected_at = RELEASE_AT + timedelta(seconds=20)
    current = crud.create_observation_record(
        session,
        listing_id=rule.listing_id,
        observation=_observation(available=True, observed_at=detected_at),
    )
    event = crud.create_event_record(
        session,
        event_type="stock_available",
        listing_id=rule.listing_id,
        watch_rule_id=rule.id,
        observation_record_id=current.id,
        occurred_at=detected_at,
    )
    delivery = crud.create_notification_delivery(
        session, event_id=event.id, provider="discord", payload_json="{}"
    )
    sent_at = detected_at + timedelta(seconds=3)
    crud.mark_delivery_sent(session, delivery.id, now=sent_at)

    report = build_nike_benchmark_report(session, rule_id)

    assert report.status == "measured"
    assert report.release_to_detection_seconds == 20.0
    assert report.detection_to_discord_seconds == 3.0
    assert report.total_latency_seconds == 23.0
