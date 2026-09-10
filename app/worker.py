"""Continuous worker: ties engine.worker (monitoring) to app.notify
(decision + Discord) and, when a decision allows and purchasing is wired
up, to purchase.engine (Phase 19). The one place allowed to depend on
all three, same reasoning as app/notify.py in Phase 9.

No scheduling framework: a plain asyncio loop, woken every `poll_interval`
seconds (or as soon as `stop_event` is set). Each WatchRule's own
check_interval (plus a small deterministic jitter) decides whether it
actually runs on a given tick (engine.worker.is_due) — the poll interval
is just how often the worker looks, not how often any one rule is
checked.

A purchase attempt is fired via `asyncio.create_task(...)` — never
awaited inline — so a slow or failing checkout can never delay checking
the next WatchRule; see purchase/engine.py's module docstring for the
full reasoning. Tasks are kept in `_background_tasks` only so they are
not garbage-collected mid-flight (a well-known asyncio footgun), and
removed once done; any exception inside one is logged, never raised into
the tick loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from app.notify import notify_events_if_allowed
from connectors.defaults import domains_for_merchant
from engine.backoff import BackoffTracker
from engine.worker import run_monitoring_tick
from purchase.engine import attempt_purchase

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from connectors.registry import ConnectorRegistry
    from engine.monitoring import MonitoringResult
    from market_data.cache import TTLCache
    from market_data.registry import MarketDataRegistry
    from purchase.config import PurchasePolicy
    from purchase.registry import PurchaseConnectorRegistry

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 20

_background_tasks: set[asyncio.Task[object]] = set()


def _fire_and_forget(coro: object) -> None:
    task = asyncio.ensure_future(coro)  # type: ignore[arg-type]
    _background_tasks.add(task)
    task.add_done_callback(_background_task_done)


def _background_task_done(task: asyncio.Task[object]) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("background purchase attempt crashed", exc_info=exc)


async def tick(
    session: Session,
    registry: ConnectorRegistry,
    notifier: EmbedSender,
    *,
    now: datetime | None = None,
    backoff: BackoffTracker | None = None,
    market_registry: MarketDataRegistry | None = None,
    market_cache: TTLCache | None = None,
    purchase_registry: PurchaseConnectorRegistry | None = None,
    purchase_policy: PurchasePolicy | None = None,
) -> list[MonitoringResult]:
    """One full pass: run due checks, notify for whatever the Decision
    Engine allows. Never notifies when a check failed, when there are no
    events, or when the decision rejects — matches the guarantees already
    built into engine.monitoring and engine.decision; nothing is
    re-implemented here.

    purchase_registry/purchase_policy default to None so every existing
    caller (manual-purchase-mode setups) is unaffected; when both are
    given, an ALLOW decision additionally fires a background purchase
    attempt (see module docstring) — never awaited here, never able to
    delay the next WatchRule in this loop.
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
            if purchase_registry is not None and purchase_policy is not None:
                merchant_domains = domains_for_merchant(result.observation.merchant)
                _fire_and_forget(
                    attempt_purchase(
                        session,
                        watch_rule,
                        result.observation,
                        result.match_result,
                        purchase_policy,
                        purchase_registry,
                        merchant_domains,
                        notifier,
                    )
                )
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
    purchase_registry: PurchaseConnectorRegistry | None = None,
    purchase_policy: PurchasePolicy | None = None,
) -> None:
    """Runs `tick()` in a loop until `stop_event` is set.

    One BackoffTracker is created here and reused for every tick of this
    run, so a rule that starts failing actually backs off across ticks
    instead of being retried every single poll. market_registry/
    market_cache/purchase_registry/purchase_policy are similarly created
    once by the caller (app/main_worker.py) and reused across ticks; all
    default to None so a manual-mode-only, no-auto-purchase setup needs
    none of them.

    Waits on the stop event with a timeout instead of a plain sleep, so
    shutdown is immediate rather than waiting out the rest of the poll
    interval. The current tick always finishes before the loop checks
    `stop_event` again — a stop request never interrupts a tick in
    progress (background purchase tasks it may have started are not
    awaited here either; see purchase/engine.py for why that is safe).
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
                purchase_registry=purchase_registry,
                purchase_policy=purchase_policy,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
    finally:
        logger.info("worker stopped")
