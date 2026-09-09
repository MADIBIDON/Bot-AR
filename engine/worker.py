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
from engine.monitoring import run_check_and_store

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from connectors.registry import ConnectorRegistry
    from database.models import WatchRule
    from engine.monitoring import MonitoringResult

logger = logging.getLogger(__name__)


def is_due(watch_rule: WatchRule, last_observed_at: datetime | None, now: datetime) -> bool:
    """True if `check_interval` seconds have elapsed since the last
    observation — or if there has never been one (first run)."""
    if last_observed_at is None:
        return True
    elapsed = (now - last_observed_at).total_seconds()
    return elapsed >= watch_rule.check_interval


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
) -> list[tuple[WatchRule, MonitoringResult]]:
    """Run every enabled WatchRule that is due right now.

    A single rule raising is logged and skipped — it never stops the
    others from being checked. run_check_and_store already never raises
    for expected connector/observation failures (it returns
    success=False); this try/except guards only against a genuine bug.
    """
    now = now or datetime.now(UTC)
    results: list[tuple[WatchRule, MonitoringResult]] = []
    for rule in crud.list_watch_rules(session, enabled=True):
        try:
            last_observed_at = _last_observed_at(session, rule)
            if not is_due(rule, last_observed_at, now):
                continue
            merchant_name = rule.listing.merchant.name if rule.listing else "?"
            logger.info("checking watch_rule=%s merchant=%s", rule.id, merchant_name)
            result = run_check_and_store(session, rule, registry)
            results.append((rule, result))
            if result.success:
                logger.info("check succeeded watch_rule=%s", rule.id)
            else:
                logger.warning("check failed watch_rule=%s: %s", rule.id, result.error)
        except Exception:
            logger.exception("unexpected error checking watch_rule=%s", rule.id)
    return results
