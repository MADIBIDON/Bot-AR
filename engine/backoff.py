"""In-memory, per-rule exponential backoff for retryable connector errors.

Retryable: 429, timeout, network error, 5xx — the kinds of failure that
mean "the merchant is having a bad moment", not "this rule is
misconfigured" (a 404 or a disabled rule is never retried faster or
slower because of this — it isn't this module's concern).

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
class _RuleBackoffState:
    consecutive_failures: int = 0
    blocked_until: datetime | None = None


class BackoffTracker:
    """One instance per running worker, shared across ticks."""

    def __init__(
        self,
        *,
        base_seconds: float = BASE_BACKOFF_SECONDS,
        max_seconds: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self._base = base_seconds
        self._max = max_seconds
        self._state: dict[int, _RuleBackoffState] = {}

    def is_blocked(self, watch_rule_id: int, now: datetime) -> bool:
        state = self._state.get(watch_rule_id)
        if state is None or state.blocked_until is None:
            return False
        return now < state.blocked_until

    def record_failure(self, watch_rule_id: int, error: str, now: datetime) -> None:
        """No-op for a non-retryable error — those aren't this module's
        concern (e.g. a 404 shouldn't back off; it just won't ever
        succeed differently)."""
        if not is_retryable_error(error):
            return
        state = self._state.setdefault(watch_rule_id, _RuleBackoffState())
        state.consecutive_failures += 1
        delay = min(self._base * (2 ** (state.consecutive_failures - 1)), self._max)
        state.blocked_until = now + timedelta(seconds=delay)

    def record_success(self, watch_rule_id: int) -> None:
        self._state.pop(watch_rule_id, None)

    def current_delay_seconds(self, watch_rule_id: int) -> float:
        state = self._state.get(watch_rule_id)
        if state is None:
            return 0.0
        return min(self._base * (2 ** max(state.consecutive_failures - 1, 0)), self._max)
