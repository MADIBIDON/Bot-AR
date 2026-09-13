"""Orchestrates one local-stock check pass across every Listing whose
retailer supports it — Phase 29.

P0 fix (found live, 2026-09-13): this used to be a synchronous function
that app/worker.py wrapped whole in asyncio.to_thread — meaning its DB
writes ran on a separate OS thread from the main event loop's own
concurrent use of the same SQLAlchemy Session, and crashed both a
background discovery task and this check with a real
`sqlalchemy.exc.ResourceClosedError: This transaction is closed` the
first time they overlapped. Now async: only the actual network call
(local_stock.monitor.fetch_pickup_availability) runs in a worker thread
per listing; every DB read/write (crud calls,
local_stock.monitor.apply_local_stock_results) stays on this coroutine's
own thread — "network concurrency yes, shared Session concurrency no",
the same rule engine/worker.py and app/discovery.py already follow.
app/worker.py now awaits this directly (still fired via
asyncio.ensure_future, never blocking the fast monitoring path — see
that module).

One retailer's failure (network error, API shape change) is caught and
logged here, never allowed to stop the others — same posture as
engine/worker.py's per-rule try/except and app/discovery.py's
per-merchant isolation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from connectors.defaults import LOCAL_STOCK, MERCHANTS
from database import crud
from local_stock.monitor import apply_local_stock_results, fetch_pickup_availability
from local_stock.rbs_platform import RbsPlatformError
from local_stock.store_discovery import build_rbs_client

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_LOCAL_STOCK_RETAILERS = tuple(m.name for m in MERCHANTS if LOCAL_STOCK in m.capabilities)


async def run_local_stock_tick(session: Session, *, now: datetime | None = None) -> int:
    """Returns the number of (listing, retailer) pairs actually checked."""
    now = now or datetime.now(UTC)
    checked_total = 0

    for retailer in _LOCAL_STOCK_RETAILERS:
        client = build_rbs_client(retailer)
        if client is None:
            continue
        merchant = crud.get_merchant_by_name(session, retailer)
        if merchant is None:
            continue
        listings = crud.list_listings_for_merchant(session, merchant.id)
        for listing in listings:
            product = crud.get_product(session, listing.product_id)
            sku = product.ean if product is not None else None
            if not sku:
                continue  # no reliable SKU to check store-by-store stock with

            stores = crud.list_retail_stores(session, retailer=retailer)
            located_stores = [
                s for s in stores if s.latitude is not None and s.longitude is not None
            ]
            if not located_stores:
                continue
            anchor = located_stores[0]

            try:
                stores_with_pickup = await asyncio.to_thread(
                    fetch_pickup_availability,
                    client,
                    sku=sku,
                    latitude=anchor.latitude,
                    longitude=anchor.longitude,
                )
            except RbsPlatformError as exc:
                logger.warning(
                    "local stock check failed for %s listing=%s: %s", retailer, listing.id, exc
                )
                continue

            checked_total += apply_local_stock_results(
                session,
                listing=listing,
                located_stores=located_stores,
                stores_with_pickup=stores_with_pickup,
                product_name=product.name,
                price=None,
                now=now,
            )

    return checked_total
