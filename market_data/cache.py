"""In-memory TTL cache — avoids re-fetching market data on every tick.

Same lifetime-and-simplicity philosophy as engine/backoff.py's
BackoffTracker: lives only for the running process, one instance shared
across ticks/calls, no external dependency (no need for a real caching
library for a single TTL rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_TTL_SECONDS = 1800.0  # 30 minutes


@dataclass
class _CacheEntry:
    value: object
    expires_at: datetime


class TTLCache:
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str, *, now: datetime | None = None) -> object | None:
        now = now or datetime.now(UTC)
        entry = self._store.get(key)
        if entry is None or now >= entry.expires_at:
            return None
        return entry.value

    def set(self, key: str, value: object, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        self._store[key] = _CacheEntry(value=value, expires_at=expires_at)

    def clear(self) -> None:
        self._store.clear()
