"""Continuous monitoring worker entrypoint.

Populate WatchRules/Listings/Products via database/crud.py before running
this (see scripts/demo_worker.py for a full FakeStore-seeded example).
Registering a merchant with ConnectorRegistry only wires up *which*
connector answers for that merchant name — for Kairyu, a real Shopify
storefront, that means a real HTTP GET per check
(connectors/shopify.py); for a WatchRule targeting Kairyu, set
Listing.external_id to the product's URL handle (see
connectors/shopify.py's docstring for the `handle:variant_sku` format).

Usage:
    python -m app.main_worker

Stops cleanly on Ctrl-C (SIGINT) or SIGTERM: the current tick always
finishes, then Discord and the database session are closed.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from dotenv import load_dotenv

from app.worker import DEFAULT_POLL_INTERVAL_SECONDS, run_forever
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from connectors.shopify import ShopifyConnector
from database.session import create_all, get_engine, get_session_factory
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import load_discord_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    load_dotenv(override=True)
    config = load_discord_config()

    engine = get_engine()
    create_all(engine)
    session = get_session_factory(engine)()

    registry = ConnectorRegistry()
    registry.register("FakeStore", FakeStoreConnector())
    registry.register("Kairyu", ShopifyConnector(shop_domain="kairyu.fr", merchant_name="Kairyu"))

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
            )
    finally:
        session.close()


if __name__ == "__main__":
    asyncio.run(main())
