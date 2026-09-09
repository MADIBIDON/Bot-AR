"""Local demo: FakeStore -> monitoring -> decision -> Discord, worker-driven.

Seeds one Product/Merchant/Listing/WatchRule in a throwaway SQLite file,
runs one baseline tick, simulates a price drop through the target price,
then runs a second tick — using an injected clock, no real sleep — through
app.worker.tick(), the exact same function the continuous worker calls.
Sends the resulting notification to your real configured Discord channel.

No real merchant, no purchase action.

Usage:
    python scripts/demo_worker.py
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from app.worker import tick
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from database.session import create_all, get_engine, get_session_factory
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import load_discord_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    load_dotenv(override=True)
    config = load_discord_config()

    db_path = Path(tempfile.mkstemp(prefix="demo_worker_", suffix=".db")[1])
    db_engine = get_engine(f"sqlite:///{db_path}")
    create_all(db_engine)
    session = get_session_factory(db_engine)()

    product = crud.create_product(session, "Duopack Evoli 30 ans (demo)", ean="1234567890123")
    merchant = crud.create_merchant(session, "FakeStore")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://fake-store.example/p/demo-1",
        external_id="demo-1",
    )
    crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=1,
        max_quantity=1,
        target_price=Decimal("14.99"),
        max_price=Decimal("19.99"),
    )

    connector = FakeStoreConnector(
        products={
            "demo-1": {
                "name": "Duopack Evoli 30 ans (demo)",
                "price": 16.99,
                "available": True,
                "seller": "FakeStore",
                "url": "https://fake-store.example/p/demo-1",
                "ean": "1234567890123",
            }
        }
    )
    registry = ConnectorRegistry()
    registry.register("FakeStore", connector)

    try:
        async with DiscordNotifier(config) as notifier:
            t0 = datetime.now(UTC)

            logger.info("=== tick 1: baseline observation (price 16.99, above target) ===")
            await tick(session, registry, notifier, now=t0)

            logger.info("=== simulating a price drop to 12.49 (below target 14.99) ===")
            connector.update_product("demo-1", price=12.49)

            logger.info("=== tick 2: expect PRICE_DROP + TARGET_PRICE_REACHED, notified ===")
            await tick(session, registry, notifier, now=t0 + timedelta(seconds=2))
    finally:
        session.close()
        db_path.unlink(missing_ok=True)

    logger.info("Demo complete — check your Discord alert channel.")


if __name__ == "__main__":
    asyncio.run(main())
