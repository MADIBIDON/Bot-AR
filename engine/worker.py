"""Monitoring-only scheduling: which WatchRules are due right now, run them.

No Discord dependency here — deciding whether to notify and sending it
lives in app/worker.py, which is allowed to depend on both this module and
notifications/discord/ (see app/notify.py). Keeps engine/ free of
discord.py, as established in Phase 9.

Uses the last ObservationRecord's timestamp as "last checked", rather than
adding a WatchRule.last_checked_at column — no migration needed, and a
rule with no observation yet is always due (first run).

Phase 23: due rules' network fetches (engine.monitoring.run_check — pure,
no DB) run concurrently, bounded by MAX_CONCURRENT_MONITOR_CHECKS, each
offloaded to a worker thread via asyncio.to_thread so a slow merchant
never blocks the others. Every actual Session query happens on the
calling thread: due-ness/backoff reads happen up front, and every write
(engine.monitoring.store_check_result) happens afterwards, one rule at a
time — the same "concurrent network, sequential Session" split already
used for purchase attempts and discovery (see purchase/engine.py,
app/discovery.py).

One subtlety that split doesn't fully cover on its own: run_check()
accesses watch_rule.product and watch_rule.listing.merchant, which are
lazy-loaded SQLAlchemy relationships — the *first* access to either is
itself a Session query. Left alone, two rules' threads could both trigger
that first lazy-load at the same instant, which is exactly the "bad
parameter or other API misuse" SQLite error that means a Session/
connection got used from two threads at once — a real, timing-dependent
bug this project hit in testing (Phase 25). _warm_relationships() forces
both to load in the sequential due-rules pass below, before any thread
ever sees the WatchRule, so every relationship access after that point is
just a plain in-memory attribute read.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from database import crud
from database.time_utils import ensure_utc
from engine.backoff import BackoffTracker
from engine.monitoring import run_check, store_check_result
from engine.release_awareness import dynamic_check_interval

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from connectors.registry import ConnectorRegistry
    from database.models import WatchRule
    from engine.monitoring import MonitoringResult

logger = logging.getLogger(__name__)

MIN_CHECK_INTERVAL_SECONDS = 30

DEFAULT_MAX_CONCURRENT_MONITOR_CHECKS = 4


def max_concurrent_monitor_checks() -> int:
    """MONITOR_MAX_CONCURRENT_CHECKS env override, else the default. Read
    fresh on each tick (cheap) rather than cached once at import time."""
    raw = os.environ.get("MONITOR_MAX_CONCURRENT_CHECKS", "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENT_MONITOR_CHECKS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_MONITOR_CHECKS
    return value if value > 0 else DEFAULT_MAX_CONCURRENT_MONITOR_CHECKS


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


def _base_check_interval(watch_rule: WatchRule, now: datetime) -> int:
    """watch_rule.check_interval, unless the rule has a real,
    source-published scheduled_release_at (Phase 31 section 12 — e.g. the
    Nike SNKRS launch page's own commerceStartDate) close enough to shrink
    it. Every rule without one (the overwhelming majority) is completely
    unaffected — this is a pure passthrough in that case."""
    if watch_rule.scheduled_release_at is None:
        return watch_rule.check_interval
    return dynamic_check_interval(
        ensure_utc(watch_rule.scheduled_release_at), now, watch_rule.check_interval
    )


def is_due(watch_rule: WatchRule, last_observed_at: datetime | None, now: datetime) -> bool:
    """True if `check_interval` seconds (plus this rule's small jitter)
    have elapsed since the last observation — or if there has never been
    one (first run)."""
    if last_observed_at is None:
        return True
    base_interval = _base_check_interval(watch_rule, now)
    effective_interval = base_interval + jitter_seconds(watch_rule.id, base_interval)
    elapsed = (now - last_observed_at).total_seconds()
    return elapsed >= effective_interval


def _last_observed_at(session: Session, watch_rule: WatchRule) -> datetime | None:
    if watch_rule.listing_id is None:
        return None
    records = crud.list_observation_records_for_listing(session, watch_rule.listing_id)
    if not records:
        return None
    return ensure_utc(records[-1].observed_at)


def _warm_relationships(watch_rule: WatchRule) -> None:
    """Forces watch_rule.product and watch_rule.listing.merchant to load
    now, on the caller's thread, before this rule is handed to a
    concurrent check — see the module docstring for why. A no-op once
    already loaded (e.g. by a prior tick's warm-up, or eager loading
    added later), so calling it is always safe."""
    _ = watch_rule.product
    listing = watch_rule.listing
    if listing is not None:
        _ = listing.merchant


def _backoff_key(watch_rule: WatchRule) -> str:
    """Backoff is scoped to the merchant being hit, not the individual
    rule (Phase 26 audit, section 11) — three rules all watching the same
    struggling merchant must all back off together once one of them gets
    a 429/timeout/5xx, while a rule on a different merchant is
    unaffected. Falls back to a per-rule key only if a rule somehow has
    no linked listing/merchant yet (not the normal case for an enabled,
    monitored rule)."""
    listing = watch_rule.listing
    if listing is not None and listing.merchant is not None:
        return f"merchant:{listing.merchant.name}"
    return f"rule:{watch_rule.id}"


def _run_check_safe(rule: WatchRule, registry: ConnectorRegistry) -> MonitoringResult:
    """run_check() wrapped so a genuine bug in one rule's check (never
    expected — connector/observation failures already return
    success=False without raising) can't blow up asyncio.gather() for
    every other rule running concurrently. Runs inside a worker thread —
    logger calls are thread-safe, no Session is touched here."""
    try:
        return run_check(rule, registry)
    except Exception as exc:  # noqa: BLE001 - must never crash the gather()
        logger.exception("unexpected error checking watch_rule=%s", rule.id)
        return MonitoringResult(
            watch_rule_id=rule.id,
            observation=None,
            match_result=None,
            success=False,
            error=f"unexpected error: {exc}",
            checked_at=datetime.now(UTC),
        )


async def run_monitoring_tick(
    session: Session,
    registry: ConnectorRegistry,
    *,
    now: datetime | None = None,
    backoff: BackoffTracker | None = None,
    max_concurrent: int | None = None,
) -> list[tuple[WatchRule, MonitoringResult]]:
    """Run every enabled WatchRule that is due right now and not currently
    backed off after recent retryable failures (429/timeout/network/5xx).

    Two phases (Phase 23 — see module docstring):
      1. Due-ness/backoff is decided sequentially first (cheap Session
         reads, no network) to build the list of rules to actually check.
      2. Those rules' run_check() network calls run concurrently, each in
         its own worker thread via asyncio.to_thread, bounded by a
         semaphore (max_concurrent, default MAX_CONCURRENT_MONITOR_CHECKS)
         — so 20 listings no longer means 20x a single check's latency,
         while a slow or hanging merchant still can't starve the others
         past the bound. Results are then stored (store_check_result)
         sequentially, back on this session's own thread, in the same
         order the rules were checked in.

    A single rule raising is logged and skipped — it never stops the
    others from being checked. run_check() already never raises for
    expected connector/observation failures (it returns success=False);
    _run_check_safe's try/except guards only against a genuine bug.

    Pass the *same* BackoffTracker across ticks for it to mean anything —
    a fresh default here (no memory across calls) is fine for a one-off
    check but not for a running worker.
    """
    now = now or datetime.now(UTC)
    backoff = backoff or BackoffTracker()
    max_concurrent = max_concurrent or max_concurrent_monitor_checks()

    due_rules: list[WatchRule] = []
    for rule in crud.list_watch_rules(session, enabled=True):
        last_observed_at = _last_observed_at(session, rule)
        if not is_due(rule, last_observed_at, now):
            continue
        # Warmed here (still sequential, still safe) so _backoff_key can
        # read watch_rule.listing.merchant below without a lazy load.
        _warm_relationships(rule)
        key = _backoff_key(rule)
        if backoff.is_blocked(key, now):
            logger.info(
                "rule=%s skipped (merchant %s backing off %.0fs after recent failures)",
                rule.id,
                key,
                backoff.current_delay_seconds(key),
            )
            continue
        due_rules.append(rule)

    if not due_rules:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _bounded_check(rule: WatchRule) -> MonitoringResult:
        async with semaphore:
            logger.info("rule=%s check started", rule.id)
            return await asyncio.to_thread(_run_check_safe, rule, registry)

    raw_results = await asyncio.gather(*(_bounded_check(rule) for rule in due_rules))

    results: list[tuple[WatchRule, MonitoringResult]] = []
    # Backoff outcomes are reconciled once per merchant *after* this loop,
    # not inline per rule: several rules on the same merchant can land in
    # the same tick (e.g. Kairyu A/B/C), and calling record_success() for
    # a same-tick sibling that happened to succeed would immediately wipe
    # out a sibling's record_failure() on the very same merchant key,
    # defeating the isolation section 11 asks for. A failure anywhere on
    # a merchant this tick wins over a success elsewhere on it this tick.
    failed_keys: dict[str, str] = {}
    succeeded_keys: set[str] = set()
    for rule, raw_result in zip(due_rules, raw_results, strict=True):
        try:
            result = store_check_result(session, rule, raw_result)
            results.append((rule, result))
            key = _backoff_key(rule)

            if result.success:
                succeeded_keys.add(key)
                obs = result.observation
                logger.info(
                    "rule=%s price=%.2f stock=%s",
                    rule.id,
                    obs.price,
                    str(obs.available).lower(),
                )
            else:
                failed_keys.setdefault(key, result.error or "")
                logger.warning("rule=%s check failed: %s", rule.id, result.error)
        except Exception:
            logger.exception("unexpected error storing watch_rule=%s result", rule.id)

    for key, error in failed_keys.items():
        backoff.record_failure(key, error, now)
    for key in succeeded_keys - failed_keys.keys():
        backoff.record_success(key)
    return results
