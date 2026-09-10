"""Continuous worker: ties engine.worker (monitoring) to app.notify
(decision + Discord). The one place allowed to depend on both, same
reasoning as app/notify.py in Phase 9.

No scheduling framework: a plain asyncio loop, woken every `poll_interval`
seconds (or as soon as `stop_event` is set). Each WatchRule's own
check_interval (plus a small deterministic jitter) decides whether it
actually runs on a given tick (engine.worker.is_due) — the poll interval
is just how often the worker looks, not how often any one rule is
checked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from app.notify import notify_events_if_allowed
from engine.backoff import BackoffTracker
from engine.worker import run_monitoring_tick

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from connectors.registry import ConnectorRegistry
    from engine.monitoring import MonitoringResult
    from market_data.cache import TTLCache
    from market_data.registry import MarketDataRegistry

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 20


async def tick(
    session: Session,
    registry: ConnectorRegistry,
    notifier: EmbedSender,
    *,
    now: datetime | None = None,
    backoff: BackoffTracker | None = None,
    market_registry: MarketDataRegistry | None = None,
    market_cache: TTLCache | None = None,
) -> list[MonitoringResult]:
    """One full pass: run due checks, notify for whatever the Decision
    Engine allows. Never notifies when a check failed, when there are no
    events, or when the decision rejects — matches the guarantees already
    built into engine.monitoring and engine.decision; nothing is
    re-implemented here.
    """
    pairs = run_monitoring_tick(session, registry, now=now, backoff=backoff)
    results: list[MonitoringResult] = []
    for watch_rule, result in pairs:
        results.append(result)
        if not (result.success and result.events):
            if result.success:
                logger.info("rule=%s no event", watch_rule.id)
            continue

        for event in result.events:
            logger.info("rule=%s event=%s", watch_rule.id, event.event_type.value)

        try:
            decision = await notify_events_if_allowed(
                watch_rule,
                result.observation,
                result.match_result,
                result.events,
                notifier,
                market_registry,
                market_cache,
            )
        except Exception:
            logger.exception("notification failed watch_rule=%s", watch_rule.id)
            continue

        if decision.allowed:
            logger.info("rule=%s notification sent", watch_rule.id)
        else:
            logger.info(
                "rule=%s notification skipped reason=%s",
                watch_rule.id,
                decision.decision_code.value,
            )
    return results


async def run_forever(
    session: Session,
    registry: ConnectorRegistry,
    notifier: EmbedSender,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
    market_registry: MarketDataRegistry | None = None,
    market_cache: TTLCache | None = None,
) -> None:
    """Runs `tick()` in a loop until `stop_event` is set.

    One BackoffTracker is created here and reused for every tick of this
    run, so a rule that starts failing actually backs off across ticks
    instead of being retried every single poll. market_registry/market_cache
    are similarly created once by the caller (app/main_worker.py) and
    reused across ticks so the TTL cache actually avoids re-fetching; both
    default to None so manual-mode-only setups need neither.

    Waits on the stop event with a timeout instead of a plain sleep, so
    shutdown is immediate rather than waiting out the rest of the poll
    interval. The current tick always finishes before the loop checks
    `stop_event` again — a stop request never interrupts a tick in
    progress.
    """
    stop_event = stop_event or asyncio.Event()
    backoff = BackoffTracker()
    logger.info("worker started")
    try:
        while not stop_event.is_set():
            await tick(
                session,
                registry,
                notifier,
                backoff=backoff,
                market_registry=market_registry,
                market_cache=market_cache,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
    finally:
        logger.info("worker stopped")
