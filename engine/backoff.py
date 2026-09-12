"""In-memory, per-merchant exponential backoff for retryable connector
errors.

Retryable: 429, timeout, network error, 5xx — the kinds of failure that
mean "the merchant is having a bad moment", not "this rule is
misconfigured" (a 404 or a disabled rule is never retried faster or
slower because of this — it isn't this module's concern).

Phase 26 audit (section 11): this was originally keyed by watch_rule_id,
which meant three separate WatchRules all pointed at the same struggling
merchant (e.g. Kairyu rule A, B, C) would each track their own backoff —
so a 429 on rule A never actually protected B or C from immediately
hammering the same merchant right after. The failure belongs to the
merchant/endpoint being hit, not to any one rule watching it, so the key
is now the merchant identity (engine/worker.py builds it) — a rule with
no linked merchant yet falls back to a per-rule key, but that's not the
normal case for an enabled, monitored rule.

State lives only for the lifetime of the running worker process. A
restart clears it — a deliberate simplification: persisting it would need
a schema change, and the goal is "don't hammer a merchant while it's
struggling", not a durable retry ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

BASE_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 3600.0

_RETRYABLE_MARKERS = ("429", "timeout", "network error")
_SERVER_ERROR_RE = re.compile(r"\bHTTP 5\d\d\b")


def is_retryable_error(error: str) -> bool:
    lowered = error.lower()
    if any(marker in lowered for marker in _RETRYABLE_MARKERS):
        return True
    return bool(_SERVER_ERROR_RE.search(error))


@dataclass
class _BackoffState:
    consecutive_failures: int = 0
    blocked_until: datetime | None = None


class BackoffTracker:
    """One instance per running worker, shared across ticks. Keyed by an
    opaque string key — engine/worker.py builds one per merchant, so all
    rules sharing a merchant share the same backoff state."""

    def __init__(
        self,
        *,
        base_seconds: float = BASE_BACKOFF_SECONDS,
        max_seconds: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self._base = base_seconds
        self._max = max_seconds
        self._state: dict[str, _BackoffState] = {}

    def is_blocked(self, key: str, now: datetime) -> bool:
        state = self._state.get(key)
        if state is None or state.blocked_until is None:
            return False
        return now < state.blocked_until

    def record_failure(self, key: str, error: str, now: datetime) -> None:
        """No-op for a non-retryable error — those aren't this module's
        concern (e.g. a 404 shouldn't back off; it just won't ever
        succeed differently)."""
        if not is_retryable_error(error):
            return
        state = self._state.setdefault(key, _BackoffState())
        state.consecutive_failures += 1
        delay = min(self._base * (2 ** (state.consecutive_failures - 1)), self._max)
        state.blocked_until = now + timedelta(seconds=delay)

    def record_success(self, key: str) -> None:
        self._state.pop(key, None)

    def current_delay_seconds(self, key: str) -> float:
        state = self._state.get(key)
        if state is None:
            return 0.0
        return min(self._base * (2 ** max(state.consecutive_failures - 1, 0)), self._max)
