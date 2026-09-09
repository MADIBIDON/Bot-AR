from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from database.models import ObservationRecord, WatchRule
from engine.change_detection import EventType, detect_changes
from products.observation import ProductObservation


def _record(**overrides: object) -> ObservationRecord:
    defaults: dict[str, object] = dict(
        id=1,
        listing_id=1,
        external_id="fake-123",
        name="Duopack Evoli 30 ans",
        price=Decimal("69.99"),
        currency="EUR",
        available=True,
        observed_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return ObservationRecord(**defaults)  # type: ignore[arg-type]


def _rule(**overrides: object) -> WatchRule:
    defaults: dict[str, object] = dict(
        id=1,
        product_id=1,
        check_interval=300,
        max_quantity=1,
        target_price=None,
    )
    defaults.update(overrides)
    return WatchRule(**defaults)  # type: ignore[arg-type]


def test_first_observation_produces_no_events() -> None:
    current = _record(id=1, price=Decimal("5"), available=False)
    events = detect_changes(None, current, _rule())
    assert events == []


def test_first_observation_below_target_produces_no_event() -> None:
    current = _record(id=1, price=Decimal("5"))
    events = detect_changes(None, current, _rule(target_price=Decimal("10")))
    assert events == []


def test_stock_becomes_available() -> None:
    previous = _record(id=1, available=False)
    current = _record(id=2, available=True)
    events = detect_changes(previous, current, _rule())
    assert [e.event_type for e in events] == [EventType.STOCK_AVAILABLE]


def test_stock_becomes_unavailable() -> None:
    previous = _record(id=1, available=True)
    current = _record(id=2, available=False)
    events = detect_changes(previous, current, _rule())
    assert [e.event_type for e in events] == [EventType.STOCK_UNAVAILABLE]


def test_stock_unchanged_produces_no_stock_event() -> None:
    previous = _record(id=1, available=True)
    current = _record(id=2, available=True)
    events = detect_changes(previous, current, _rule())
    assert events == []


def test_price_drop() -> None:
    previous = _record(id=1, price=Decimal("69.99"))
    current = _record(id=2, price=Decimal("59.99"))
    events = detect_changes(previous, current, _rule())
    assert [e.event_type for e in events] == [EventType.PRICE_DROP]
    assert events[0].previous_value == "69.99"
    assert events[0].current_value == "59.99"


def test_price_increase() -> None:
    previous = _record(id=1, price=Decimal("59.99"))
    current = _record(id=2, price=Decimal("69.99"))
    events = detect_changes(previous, current, _rule())
    assert [e.event_type for e in events] == [EventType.PRICE_INCREASE]


def test_price_unchanged_produces_no_price_event() -> None:
    previous = _record(id=1, price=Decimal("59.99"))
    current = _record(id=2, price=Decimal("59.99"))
    events = detect_changes(previous, current, _rule())
    assert events == []


def test_price_changed_event_is_never_emitted() -> None:
    previous = _record(id=1, price=Decimal("69.99"))
    current = _record(id=2, price=Decimal("59.99"))
    events = detect_changes(previous, current, _rule())
    assert EventType.PRICE_CHANGED not in [e.event_type for e in events]
    assert len(events) == 1


def test_target_price_reached_when_crossing_from_above() -> None:
    previous = _record(id=1, price=Decimal("15.99"))
    current = _record(id=2, price=Decimal("13.99"))
    events = detect_changes(previous, current, _rule(target_price=Decimal("14.99")))
    types = [e.event_type for e in events]
    assert EventType.TARGET_PRICE_REACHED in types


def test_target_price_reached_not_repeated_when_staying_below() -> None:
    previous = _record(id=1, price=Decimal("13.99"))
    current = _record(id=2, price=Decimal("12.99"))
    events = detect_changes(previous, current, _rule(target_price=Decimal("14.99")))
    types = [e.event_type for e in events]
    assert EventType.TARGET_PRICE_REACHED not in types
    assert EventType.PRICE_DROP in types


def test_no_target_price_reached_when_target_absent() -> None:
    previous = _record(id=1, price=Decimal("15.99"))
    current = _record(id=2, price=Decimal("13.99"))
    events = detect_changes(previous, current, _rule(target_price=None))
    types = [e.event_type for e in events]
    assert EventType.TARGET_PRICE_REACHED not in types


def test_target_price_re_fires_after_rising_back_above_and_dropping_again() -> None:
    rule = _rule(target_price=Decimal("14.99"))
    obs1 = _record(id=1, price=Decimal("15.99"))
    obs2 = _record(id=2, price=Decimal("13.99"))
    obs3 = _record(id=3, price=Decimal("16.99"))
    obs4 = _record(id=4, price=Decimal("12.99"))

    first_cross = detect_changes(obs1, obs2, rule)
    rise_above = detect_changes(obs2, obs3, rule)
    second_cross = detect_changes(obs3, obs4, rule)

    assert EventType.TARGET_PRICE_REACHED in [e.event_type for e in first_cross]
    assert EventType.TARGET_PRICE_REACHED not in [e.event_type for e in rise_above]
    assert EventType.TARGET_PRICE_REACHED in [e.event_type for e in second_cross]


def test_restock_and_price_drop_simultaneously() -> None:
    previous = _record(id=1, price=Decimal("69.99"), available=False)
    current = _record(id=2, price=Decimal("59.99"), available=True)
    events = detect_changes(previous, current, _rule())
    types = {e.event_type for e in events}
    assert types == {EventType.STOCK_AVAILABLE, EventType.PRICE_DROP}


def test_event_record_deduplication(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/1",
        external_id="fake-123",
    )
    rule = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=300, max_quantity=1
    )

    observation = ProductObservation(
        merchant="RetailerA",
        external_id="fake-123",
        name="Duopack Evoli 30 ans",
        price=Decimal("59.99"),
        currency="EUR",
        available=True,
        url="https://a.example/p/1",
        observed_at=datetime.now(UTC),
    )
    obs_record = crud.create_observation_record(
        session, listing_id=listing.id, observation=observation
    )

    first = crud.create_event_record(
        session,
        event_type=EventType.PRICE_DROP.value,
        listing_id=listing.id,
        watch_rule_id=rule.id,
        observation_record_id=obs_record.id,
        occurred_at=datetime.now(UTC),
        previous_value="69.99",
        current_value="59.99",
    )
    second = crud.create_event_record(
        session,
        event_type=EventType.PRICE_DROP.value,
        listing_id=listing.id,
        watch_rule_id=rule.id,
        observation_record_id=obs_record.id,
        occurred_at=datetime.now(UTC),
        previous_value="69.99",
        current_value="59.99",
    )

    assert first is not None
    assert second is None
    records = crud.list_event_records_for_listing(session, listing.id)
    assert len(records) == 1
