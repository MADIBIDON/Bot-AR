"""Continuous monitoring worker entrypoint. FakeStore only — no real
merchant connector exists yet.

Populate WatchRules/Listings/Products via database/crud.py before running
this (see scripts/demo_worker.py for a full seeded example). Registering a
merchant with ConnectorRegistry only wires up *which* connector answers
for that merchant name — the product data itself lives wherever that
connector gets it (FakeStoreConnector's `products=` dict for now).

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
