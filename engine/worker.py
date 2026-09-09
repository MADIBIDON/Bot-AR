"""Monitoring-only scheduling: which WatchRules are due right now, run them.

No Discord dependency here — deciding whether to notify and sending it
lives in app/worker.py, which is allowed to depend on both this module and
notifications/discord/ (see app/notify.py). Keeps engine/ free of
discord.py, as established in Phase 9.

Uses the last ObservationRecord's timestamp as "last checked", rather than
adding a WatchRule.last_checked_at column — no migration needed, and a
rule with no observation yet is always due (first run).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from database import crud
from engine.backoff import BackoffTracker
from engine.monitoring import run_check_and_store

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from connectors.registry import ConnectorRegistry
    from database.models import WatchRule
    from engine.monitoring import MonitoringResult

logger = logging.getLogger(__name__)

MIN_CHECK_INTERVAL_SECONDS = 30

_JITTER_MAX_SECONDS = 5.0
_JITTER_MAX_FRACTION = 0.1
_JITTER_HASH_MULTIPLIER = 2654435761  # Knuth's multiplicative hash constant


def jitter_seconds(watch_rule_id: int, check_interval: int) -> float:
    """Deterministic, per-rule offset so rules sharing a check_interval
    don't all fire on the exact same tick. Bounded to at most 5s or 10% of
    the interval, whichever is smaller — enough to spread load across
    rules, never enough to meaningfully delay a check."""
    max_jitter = min(_JITTER_MAX_SECONDS, check_interval * _JITTER_MAX_FRACTION)
    if max_jitter <= 0:
        return 0.0
    fraction = (watch_rule_id * _JITTER_HASH_MULTIPLIER) % 1000 / 1000
    return fraction * max_jitter


def is_due(watch_rule: WatchRule, last_observed_at: datetime | None, now: datetime) -> bool:
    """True if `check_interval` seconds (plus this rule's small jitter)
    have elapsed since the last observation — or if there has never been
    one (first run)."""
    if last_observed_at is None:
        return True
    effective_interval = watch_rule.check_interval + jitter_seconds(
        watch_rule.id, watch_rule.check_interval
    )
    elapsed = (now - last_observed_at).total_seconds()
    return elapsed >= effective_interval


def _last_observed_at(session: Session, watch_rule: WatchRule) -> datetime | None:
    if watch_rule.listing_id is None:
        return None
    records = crud.list_observation_records_for_listing(session, watch_rule.listing_id)
    if not records:
        return None
    observed_at = records[-1].observed_at
    if observed_at.tzinfo is None:
        # SQLite does not preserve tzinfo across a round trip. Every
        # observed_at this system ever writes is UTC by construction
        # (ProductObservation guarantees it — products/observation.py), so
        # a naive value read back is safely re-attached to UTC here.
        observed_at = observed_at.replace(tzinfo=UTC)
    return observed_at


def run_monitoring_tick(
    session: Session,
    registry: ConnectorRegistry,
    *,
    now: datetime | None = None,
    backoff: BackoffTracker | None = None,
) -> list[tuple[WatchRule, MonitoringResult]]:
    """Run every enabled WatchRule that is due right now and not currently
    backed off after recent retryable failures (429/timeout/network/5xx).

    A single rule raising is logged and skipped — it never stops the
    others from being checked. run_check_and_store already never raises
    for expected connector/observation failures (it returns
    success=False); this try/except guards only against a genuine bug.

    Pass the *same* BackoffTracker across ticks for it to mean anything —
    a fresh default here (no memory across calls) is fine for a one-off
    check but not for a running worker.
    """
    now = now or datetime.now(UTC)
    backoff = backoff or BackoffTracker()
    results: list[tuple[WatchRule, MonitoringResult]] = []
    for rule in crud.list_watch_rules(session, enabled=True):
        try:
            last_observed_at = _last_observed_at(session, rule)
            if not is_due(rule, last_observed_at, now):
                continue
            if backoff.is_blocked(rule.id, now):
                logger.info(
                    "rule=%s skipped (backing off %.0fs after recent failures)",
                    rule.id,
                    backoff.current_delay_seconds(rule.id),
                )
                continue

            logger.info("rule=%s check started", rule.id)
            result = run_check_and_store(session, rule, registry)
            results.append((rule, result))

            if result.success:
                backoff.record_success(rule.id)
                obs = result.observation
                logger.info(
                    "rule=%s price=%.2f stock=%s",
                    rule.id,
                    obs.price,
                    str(obs.available).lower(),
                )
            else:
                backoff.record_failure(rule.id, result.error or "", now)
                logger.warning("rule=%s check failed: %s", rule.id, result.error)
        except Exception:
            logger.exception("unexpected error checking watch_rule=%s", rule.id)
    return results
