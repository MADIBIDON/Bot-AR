from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from database.time_utils import ensure_utc


def test_naive_datetime_gets_utc_attached() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    result = ensure_utc(naive)
    assert result.tzinfo is UTC
    assert result.year == 2026 and result.hour == 12


def test_aware_utc_datetime_is_returned_unchanged() -> None:
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert ensure_utc(aware) is aware


def test_aware_non_utc_datetime_is_not_reinterpreted() -> None:
    """A genuinely non-UTC aware datetime must never be silently shifted
    or have its tzinfo replaced — only a naive value is ever touched."""
    other_tz = timezone(timedelta(hours=5))
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=other_tz)
    result = ensure_utc(aware)
    assert result.tzinfo == other_tz
    assert result.hour == 12
