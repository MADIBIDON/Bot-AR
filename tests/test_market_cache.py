from __future__ import annotations

from datetime import UTC, datetime, timedelta

from market_data.cache import TTLCache


def test_miss_on_empty_cache() -> None:
    cache = TTLCache()
    assert cache.get("key") is None


def test_hit_returns_stored_value() -> None:
    cache = TTLCache()
    cache.set("key", "value")
    assert cache.get("key") == "value"


def test_expired_entry_is_a_miss() -> None:
    cache = TTLCache(ttl_seconds=60)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    cache.set("key", "value", now=now)

    later = now + timedelta(seconds=61)
    assert cache.get("key", now=later) is None


def test_entry_just_before_expiry_is_still_a_hit() -> None:
    cache = TTLCache(ttl_seconds=60)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    cache.set("key", "value", now=now)

    just_before = now + timedelta(seconds=59)
    assert cache.get("key", now=just_before) == "value"


def test_clear_removes_all_entries() -> None:
    cache = TTLCache()
    cache.set("a", 1)
    cache.set("b", 2)

    cache.clear()

    assert cache.get("a") is None
    assert cache.get("b") is None
