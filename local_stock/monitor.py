"""Local-stock check + transition detection — Phase 29.

One RbsPlatformClient.check_pickup_availability() call per (retailer,
SKU) checks every store nationwide in a single request (see
local_stock/rbs_platform.py) — never one request per store, which is
exactly the "150 magasins x toutes les 30 secondes" this project's own
spec explicitly rules out. A store the response didn't return is
OUT_OF_STOCK for that SKU, not UNKNOWN: the API gives a definitive
present/absent answer for every store in radius, so absence is real
information, not a missing signal — UNKNOWN is reserved for "the check
itself failed" (network/HTTP error), which never overwrites a
previously-known state or fires an alert.

Transitions worth alerting on (per the spec): OUT_OF_STOCK -> IN_STOCK
(here: presence in the pickup results = CLICK_AND_COLLECT, the only
positive state this API can currently distinguish) — matches
"OUT_OF_STOCK -> CLICK_AND_COLLECT -> Discord". IN_STOCK -> IN_STOCK
(no change) never re-alerts; first-ever check for a (store, listing)
pair is a baseline, exactly like ObservationRecord's own "no previous
observation -> no event" rule — never treated as a transition.

This module is entirely synchronous (the RBS HTTP client is a plain
httpx.Client, same as connectors/) and only ever gets as far as
*creating* a pending LocalNotificationDelivery row — it never attempts
to send one. Actually sending goes through local_stock/delivery.py's
process_due_local_deliveries(), called from app/worker.py's tick() the
same way app/delivery.py::process_due_deliveries() already is for the
online path. Splitting it this way (rather than passing a notifier and
a dispatch callback in here) means a local-stock check can run inside
asyncio.to_thread() with no event loop of its own to worry about.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from database import crud
from local_stock.delivery import create_pending_local_delivery
from local_stock.formatter import format_local_stock_embed

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import Listing, RetailStore
    from local_stock.rbs_platform import RbsPlatformClient

logger = logging.getLogger(__name__)

_ALERTABLE_TRANSITIONS = {
    ("out_of_stock", "click_and_collect"): "local_click_and_collect",
    ("unknown", "click_and_collect"): "local_click_and_collect",
}


def check_local_stock_for_listing(
    session: Session,
    *,
    client: RbsPlatformClient,
    retailer: str,
    listing: Listing,
    sku: str,
    product_name: str,
    price: str | None,
    now: datetime | None = None,
) -> int:
    """Checks every enabled RetailStore for this retailer, updates each
    one's LocalStockState, and creates (never sends) a pending delivery
    for any OUT_OF_STOCK/UNKNOWN -> CLICK_AND_COLLECT transition. Returns
    the number of stores checked. Raises RbsPlatformError on a genuine
    network/API failure — the caller is expected to catch it per
    retailer, same posture as engine/worker.py catching a connector
    failure for one WatchRule."""
    now = now or datetime.now(UTC)
    stores = crud.list_retail_stores(session, retailer=retailer)
    located_stores = [s for s in stores if s.latitude is not None and s.longitude is not None]
    if not located_stores:
        return 0

    # One national-radius call already covers every store for this
    # retailer — see module docstring on why this is never per-store.
    anchor = located_stores[0]
    results = client.check_pickup_availability(
        sku=sku, latitude=anchor.latitude, longitude=anchor.longitude, radius_km=3000
    )
    stores_with_pickup = {r.external_store_id for r in results if r.can_pick_up}

    for store in located_stores:
        new_state = (
            "click_and_collect" if store.external_store_id in stores_with_pickup else "out_of_stock"
        )
        _apply_transition(
            session,
            store=store,
            listing=listing,
            new_state=new_state,
            product_name=product_name,
            price=price,
            now=now,
        )
    return len(located_stores)


def _apply_transition(
    session: Session,
    *,
    store: RetailStore,
    listing: Listing,
    new_state: str,
    product_name: str,
    price: str | None,
    now: datetime,
) -> None:
    previous = crud.get_local_stock_state(session, store_id=store.id, listing_id=listing.id)
    previous_state = previous.stock_state if previous is not None else None

    crud.upsert_local_stock_state(
        session,
        store_id=store.id,
        listing_id=listing.id,
        stock_state=new_state,
        click_and_collect=(new_state == "click_and_collect"),
        observed_at=now,
    )

    if previous_state is None:
        return  # baseline — never an event, matching ObservationRecord's own rule

    event_type = _ALERTABLE_TRANSITIONS.get((previous_state, new_state))
    if event_type is None:
        return

    event = crud.create_local_stock_event(
        session,
        store_id=store.id,
        listing_id=listing.id,
        event_type=event_type,
        previous_state=previous_state,
        current_state=new_state,
        occurred_at=now,
    )
    if event is None:
        return  # already recorded (dedup) — never a duplicate alert

    embed = format_local_stock_embed(
        event, store=store, product_name=product_name, price=price, url=store.url or ""
    )
    create_pending_local_delivery(session, local_stock_event_id=event.id, embed=embed)
    logger.info(
        "local_stock_event=%s store=%s listing=%s %s -> %s (delivery queued)",
        event.id,
        store.id,
        listing.id,
        previous_state,
        new_state,
    )
