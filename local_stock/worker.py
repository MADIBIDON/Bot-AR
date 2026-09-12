"""Orchestrates one local-stock check pass across every Listing whose
retailer supports it — Phase 29. Entirely synchronous (same reasoning as
local_stock/monitor.py); app/worker.py runs it via asyncio.to_thread so
it never blocks the event loop the fast online-monitoring path shares.

One retailer's failure (network error, API shape change) is caught and
logged here, never allowed to stop the others — same posture as
engine/worker.py's per-rule try/except and app/discovery.py's
per-merchant isolation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from connectors.defaults import LOCAL_STOCK, MERCHANTS
from database import crud
from local_stock.monitor import check_local_stock_for_listing
from local_stock.rbs_platform import RbsPlatformError
from local_stock.store_discovery import build_rbs_client

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_LOCAL_STOCK_RETAILERS = tuple(m.name for m in MERCHANTS if LOCAL_STOCK in m.capabilities)


def run_local_stock_tick(session: Session, *, now: datetime | None = None) -> int:
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
            try:
                checked = check_local_stock_for_listing(
                    session,
                    client=client,
                    retailer=retailer,
                    listing=listing,
                    sku=sku,
                    product_name=product.name,
                    price=None,
                    now=now,
                )
                checked_total += checked
            except RbsPlatformError as exc:
                logger.warning(
                    "local stock check failed for %s listing=%s: %s", retailer, listing.id, exc
                )
                continue

    return checked_total
