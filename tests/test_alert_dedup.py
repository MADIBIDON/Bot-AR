"""notifications/dedup.py — repeat-alert suppression."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from notifications.dedup import AlertCooldown

_T0 = datetime(2026, 9, 16, 8, 18, 54, tzinfo=UTC)


def _send(
    cd: AlertCooldown, *, now: datetime, price: str = "14.99", event: str = "stock_available"
):
    return cd.should_send(watch_rule_id=37, event_type=event, price=price, now=now)


def test_first_alert_always_goes_out() -> None:
    assert _send(AlertCooldown(), now=_T0) is True


def test_identical_repeat_inside_cooldown_is_suppressed() -> None:
    """The real 16/09 case: stock flapped and the same Duopack alert
    fired five times in a row with identical content."""
    cd = AlertCooldown()
    assert _send(cd, now=_T0) is True
    assert _send(cd, now=_T0 + timedelta(minutes=2)) is False
    assert _send(cd, now=_T0 + timedelta(minutes=8)) is False


def test_same_alert_after_cooldown_gets_through() -> None:
    """A genuine restock hours later is real news."""
    cd = AlertCooldown()
    assert _send(cd, now=_T0) is True
    assert _send(cd, now=_T0 + timedelta(minutes=11)) is True


def test_price_change_always_gets_through() -> None:
    """A price move is new information even seconds later."""
    cd = AlertCooldown()
    assert _send(cd, now=_T0, price="14.99") is True
    assert _send(cd, now=_T0 + timedelta(seconds=30), price="12.49") is True


def test_different_event_type_is_not_suppressed() -> None:
    cd = AlertCooldown()
    assert _send(cd, now=_T0, event="stock_available") is True
    assert _send(cd, now=_T0 + timedelta(seconds=30), event="stock_unavailable") is True


def test_different_watch_rule_is_not_suppressed() -> None:
    cd = AlertCooldown()
    assert cd.should_send(watch_rule_id=37, event_type="s", price="1", now=_T0) is True
    assert cd.should_send(watch_rule_id=38, event_type="s", price="1", now=_T0) is True
