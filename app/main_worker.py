"""Continuous monitoring worker entrypoint. Runs indefinitely.

Populate WatchRules/Listings/Products via database/crud.py (or
scripts/watch.py add) before running this. See connectors/defaults.py for
which merchants are wired up.

Usage:
    python -m app.main_worker

Stops cleanly on Ctrl-C (SIGINT) or SIGTERM: the current tick always
finishes, then Discord, the database session, and the PID file are
cleaned up.

Writes its PID to data/worker.pid on startup (app/pidfile.py) and removes
it on clean shutdown, so `scripts/watch.py status` can report whether a
worker looks to be running — a plain PID file, not a process manager. A
second instance refuses to start while a live one holds that file; a
stale file (left by a killed process) is cleaned up automatically.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from app import pidfile
from app.worker import DEFAULT_POLL_INTERVAL_SECONDS, drain_background_tasks, run_forever
from connectors.defaults import build_default_registry
from database.session import create_all, get_engine, get_session_factory
from discovery.defaults import build_default_discovery_registry
from market_data.cache import TTLCache
from market_data.defaults import build_default_market_registry
from market_data.ebay import MissingEbayConfigError
from market_data.registry import MarketDataRegistry
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import load_discord_config
from purchase.config import load_purchase_policy
from purchase.defaults import build_default_purchase_registry
from purchase.engine import reconcile_orphaned_purchase_attempts

PID_FILE = Path("data") / "worker.pid"
LOG_FILE = Path("logs") / "worker.log"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def _configure_logging() -> None:
    """Console + a rotating log file (5 x 1MB) — enough history to
    diagnose an overnight run without growing without bound. No secrets
    are ever logged (see notifications/discord/config.py and
    market_data/ebay.py — both log presence/errors, never values)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=handlers, force=True)


logger = logging.getLogger(__name__)


def _build_market_registry() -> MarketDataRegistry | None:
    """None (not an empty registry) when no market source is configured, so
    resolve_resale_price_for_opportunity's None-check falls back cleanly —
    WatchRules in "manual" mode are entirely unaffected either way, and a
    "market" mode rule just gets no estimate (logged) instead of a crash.
    """
    try:
        return build_default_market_registry()
    except MissingEbayConfigError as exc:
        logger.warning("market data disabled: %s", exc)
        return None


async def main() -> None:
    _configure_logging()

    try:
        pidfile.acquire(PID_FILE)
    except pidfile.WorkerAlreadyRunningError as exc:
        logger.error(str(exc))
        sys.exit(1)

    load_dotenv(override=True)
    config = load_discord_config()

    engine = get_engine()
    create_all(engine)
    session = get_session_factory(engine)()

    # Phase 33 section 24: a PurchaseAttempt left CREATED/VALIDATING/
    # CHECKOUT_STARTED means the previous process died mid-attempt — never
    # assume it succeeded, never leave it silently blocking every future
    # attempt at that product forever. Always 0 today (PURCHASES_ENABLED
    # has stayed false this whole project, so no real attempt has ever
    # existed to orphan) — this is what keeps that true after one does.
    reconciled = reconcile_orphaned_purchase_attempts(session)
    if reconciled:
        logger.warning("reconciled %d orphaned purchase attempt(s) from a previous run", reconciled)

    registry = build_default_registry()
    market_registry = _build_market_registry()
    market_cache = TTLCache()
    purchase_registry = build_default_purchase_registry()
    discovery_registry = build_default_discovery_registry()
    purchase_policy = load_purchase_policy()
    if purchase_policy.enabled:
        logger.info(
            "automated purchasing ENABLED — allowed merchants: %s",
            ", ".join(sorted(purchase_policy.allowed_merchant_domains)) or "(none configured)",
        )
    else:
        logger.info("automated purchasing disabled (PURCHASES_ENABLED != true)")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        async with DiscordNotifier(config) as notifier:
            await run_forever(
                session,
                registry,
                notifier,
                poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                stop_event=stop_event,
                market_registry=market_registry,
                market_cache=market_cache,
                purchase_registry=purchase_registry,
                purchase_policy=purchase_policy,
                discovery_registry=discovery_registry,
            )
            # Still inside the Discord connection and before the Session
            # closes below: a background discovery/purchase task started
            # by the last tick may still be running and could try to use
            # either right after its next `await` resumes.
            logger.info("waiting for in-flight background tasks to finish...")
            await drain_background_tasks()
    finally:
        session.close()
        pidfile.release(PID_FILE)


if __name__ == "__main__":
    asyncio.run(main())
