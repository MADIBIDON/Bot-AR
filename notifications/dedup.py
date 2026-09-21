"""Suppression of repeat alerts for the same thing.

Real case, 16/09: King Jouet's stock flipped roughly 40 times between
08:18 and 13:55 on drop day. Every flip produced its own "Back in Stock"
alert, so the same Duopack was announced five times in a row with
identical content. The signal became unreadable exactly when it mattered
most.

What is deliberately NOT deduplicated:
  * a different watch rule, or a different event type — those are
    genuinely different news;
  * the same event at a DIFFERENT price — a price move is new
    information and must always get through;
  * anything at all once the cooldown has elapsed, so a real restock
    hours later still alerts.

State is process-local and intentionally not persisted: after a worker
restart the first alert gets through again, which is the safe direction
to fail (an extra alert, never a missing one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

DEFAULT_COOLDOWN_SECONDS = 600


@dataclass
class AlertCooldown:
    cooldown: timedelta = timedelta(seconds=DEFAULT_COOLDOWN_SECONDS)
    _last_sent: dict[tuple[int, str, str], datetime] = field(default_factory=dict)

    def should_send(
        self, *, watch_rule_id: int, event_type: str, price: str, now: datetime
    ) -> bool:
        """True if this exact alert has not already gone out inside the
        cooldown. Records the send as a side effect when it returns
        True, so a caller never has to remember to."""
        key = (watch_rule_id, event_type, price)
        previous = self._last_sent.get(key)
        if previous is not None and now - previous < self.cooldown:
            return False
        self._last_sent[key] = now
        return True


_default = AlertCooldown()


def get_default_cooldown() -> AlertCooldown:
    return _default
