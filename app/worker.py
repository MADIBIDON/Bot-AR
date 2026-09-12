"""Continuous worker: ties engine.worker (monitoring) to app.notify
(decision + Discord) and, when a decision allows and purchasing is wired
up, to purchase.engine (Phase 19), plus the slow, periodic multi-merchant
DISCOVERY PATH (Phase 22, app/discovery.py). The one place allowed to
depend on all four, same reasoning as app/notify.py in Phase 9.

No scheduling framework: a plain asyncio loop, woken every `poll_interval`
seconds (or as soon as `stop_event` is set). Each WatchRule's own
check_interval (plus a small deterministic jitter) decides whether it
actually runs on a given tick (engine.worker.is_due) — the poll interval
is just how often the worker looks, not how often any one rule is
checked. Each watched Product's own discovery_interval similarly decides
whether a *discovery* run is due (app.discovery.is_discovery_due) —
independently of, and typically much slower than, monitoring.

A purchase attempt is fired via `asyncio.create_task(...)` — never
awaited inline — so a slow or failing checkout can never delay checking
the next WatchRule; see purchase/engine.py's module docstring for the
full reasoning. Tasks are kept in `_background_tasks` only so they are
not garbage-collected mid-flight (a well-known asyncio footgun), and
removed once done; any exception inside one is logged, never raised into
the tick loop.

Discovery is fired as a background task exactly like a purchase attempt
(see purchase/engine.py's module docstring): `tick()` never awaits it,
so a slow discovery (a real merchant search can take several seconds)
can never delay the next due WatchRule's monitoring check, the next
tick's Discord alert, or the next tick even starting — run_forever()'s
loop moves on as soon as `tick()` returns, regardless of whether a
discovery task is still running in the background. Inside
app.discovery.run_discovery_for_product, each merchant's own network
search is further offloaded to a worker thread via `asyncio.to_thread`
(same reasoning as a purchase connector's revalidate()/checkout()) so it
never blocks the shared event loop either; only the matching + DB-write
logic around it runs synchronously on the caller's session, interleaved
with (never concurrent with) the monitoring loop's own synchronous DB
work, which is safe for a single-threaded event loop even with a
non-async, non-thread-safe SQLAlchemy Session.

At most one discovery runs at a time (`_discovery_in_flight`): a second
due Product simply waits for the next tick where the first has finished
— there is no due-product queue, no Celery/Redis, no second service.
This keeps discovery a bounded background trickle rather than something
that could pile up or compete with itself for the same Session.

Phase 27: every tick also fires app.delivery.process_due_deliveries() as
a background task — recovers any alert left pending or mid-retry by a
crash, and retries whatever else is due (see app/delivery.py). Never
awaited here for the same reason a purchase attempt or discovery run
isn't: a backlog of deliveries, or one slow send, must never delay the
next tick's monitoring.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.delivery import process_due_deliveries
from app.discovery import is_discovery_due, run_discovery_for_product
from app.notify import compute_opportunity, notify_events_if_allowed
from app.resale import resolve_resale_confidence
from connectors.defaults import domains_for_merchant
from database import crud
from engine.backoff import BackoffTracker
from engine.decision import is_profitability_mode
from engine.worker import run_monitoring_tick
from purchase.engine import attempt_purchase

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from connectors.registry import ConnectorRegistry
    from discovery.registry import DiscoveryRegistry
    from engine.monitoring import MonitoringResult
    from market_data.cache import TTLCache
    from market_data.registry import MarketDataRegistry
    from purchase.config import PurchasePolicy
    from purchase.registry import PurchaseConnectorRegistry

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 20

_background_tasks: set[asyncio.Task[object]] = set()

_discovery_in_flight = False


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


def _discovery_task_done(task: asyncio.Task[object]) -> None:
    global _discovery_in_flight
    _discovery_in_flight = False
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("background discovery crashed", exc_info=exc)


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
    discovery_registry: DiscoveryRegistry | None = None,
) -> list[MonitoringResult]:
    """One full pass: run due checks, notify for whatever the Decision
    Engine allows, then (if discovery_registry is given) run discovery
    for at most one due Product. Never notifies when a check failed, when
    there are no events, or when the decision rejects — matches the
    guarantees already built into engine.monitoring and engine.decision;
    nothing is re-implemented here.

    purchase_registry/purchase_policy default to None so every existing
    caller (manual-purchase-mode setups) is unaffected; when both are
    given, an ALLOW decision additionally fires a background purchase
    attempt (see module docstring) — never awaited here, never able to
    delay the next WatchRule in this loop.
    """
    pairs = await run_monitoring_tick(session, registry, now=now, backoff=backoff)
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
                # Phase 26: the actual Discord send is fired through this
                # instead of awaited inline, so a slow/rate-limited
                # Discord call can never delay this tick's return (and,
                # by extension, the next tick's monitoring of every other
                # WatchRule). Decision/opportunity computation above this
                # line is unaffected — those still happen synchronously.
                dispatch=_fire_and_forget,
                # Phase 27: durable delivery tracking (app/delivery.py) —
                # see notify_events_if_allowed's docstring.
                session=session,
            )
        except Exception:
            logger.exception("notification failed watch_rule=%s", watch_rule.id)
            continue

        if decision.allowed:
            logger.info("rule=%s notification dispatched", watch_rule.id)
            if purchase_registry is not None and purchase_policy is not None:
                merchant_domains = domains_for_merchant(result.observation.merchant)
                opportunity = resale_confidence = None
                if is_profitability_mode(watch_rule):
                    # Phase 25: recomputed here (cheap — see
                    # compute_opportunity's docstring, market mode's
                    # result is cache-backed) rather than threaded through
                    # from notify_events_if_allowed, so purchase/engine.py
                    # never has to depend on app/ or market_data/ itself.
                    opportunity, _resale_estimate = await compute_opportunity(
                        watch_rule, result.observation.price, market_registry, market_cache
                    )
                    resale_confidence = resolve_resale_confidence(watch_rule, _resale_estimate)
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
                        opportunity=opportunity,
                        resale_confidence=resale_confidence,
                    )
                )
        else:
            logger.info(
                "rule=%s notification skipped reason=%s",
                watch_rule.id,
                decision.decision_code.value,
            )

    if discovery_registry is not None:
        _start_discovery_if_due(session, discovery_registry, now=now)

    # Phase 27: recovers any pending/overdue-retry alert — including one
    # left behind by a crash before this process started — and retries
    # whatever is due, every tick. Fired as a background task, same as a
    # purchase attempt or discovery run, so a backlog of deliveries (or a
    # single slow one) can never delay the next tick's monitoring either.
    _fire_and_forget(process_due_deliveries(session, notifier, now=now))

    return results


def _start_discovery_if_due(
    session: Session, discovery_registry: DiscoveryRegistry, *, now: datetime | None = None
) -> None:
    """Fires discovery for at most one due Product as a background task
    — never awaited here, see module docstring. Skips if a discovery is
    already in flight (concurrency capped at 1) so it never has to share
    a Session write with itself."""
    global _discovery_in_flight
    if _discovery_in_flight:
        return
    now = now or datetime.now(UTC)
    for product in crud.list_products(session, status="active"):
        if not is_discovery_due(product, now):
            continue
        _discovery_in_flight = True
        task = asyncio.ensure_future(
            run_discovery_for_product(session, product, discovery_registry, now=now)
        )
        _background_tasks.add(task)
        task.add_done_callback(_discovery_task_done)
        return  # one due product per tick, regardless of outcome


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
    discovery_registry: DiscoveryRegistry | None = None,
) -> None:
    """Runs `tick()` in a loop until `stop_event` is set.

    One BackoffTracker is created here and reused for every tick of this
    run, so a rule that starts failing actually backs off across ticks
    instead of being retried every single poll. market_registry/
    market_cache/purchase_registry/purchase_policy/discovery_registry are
    similarly created once by the caller (app/main_worker.py) and reused
    across ticks; all default to None so a manual-mode-only, no-auto-
    purchase, no-discovery setup needs none of them.

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
                discovery_registry=discovery_registry,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
    finally:
        logger.info("worker stopped")


async def drain_background_tasks(timeout: float = 10.0) -> None:
    """Waits for every currently in-flight background task (a purchase
    attempt fired by _fire_and_forget, or a discovery run fired by
    _start_discovery_if_due) to finish, up to `timeout` seconds, then
    cancels whatever is still running and waits briefly for that
    cancellation to actually land.

    Call this after run_forever() returns and before closing the Session
    or disconnecting Discord (app/main_worker.py does both) — otherwise a
    task that resumes after its last `await` (e.g. right after finishing
    a network call, about to write to the Session or send a Discord
    embed) could run against resources that are already gone.
    """
    pending = list(_background_tasks)
    if not pending:
        return
    _done, still_pending = await asyncio.wait(pending, timeout=timeout)
    if not still_pending:
        return
    logger.warning(
        "%d background task(s) still running after %.0fs at shutdown — cancelling",
        len(still_pending),
        timeout,
    )
    for task in still_pending:
        task.cancel()
    await asyncio.gather(*still_pending, return_exceptions=True)
