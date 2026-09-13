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
online path.

P0 fix (found live, 2026-09-13): fetch_pickup_availability() (pure
network, no Session) and apply_local_stock_results() (all DB writes) are
split apart specifically so local_stock/worker.py can run the network
call in a worker thread via asyncio.to_thread while the DB writes stay
on the calling coroutine's own thread — "network concurrency yes, shared
SQLAlchemy Session concurrency no", the same rule engine/worker.py and
app/discovery.py already follow. The original bug: run_local_stock_tick
wrapped its *entire* body (network AND DB writes) in asyncio.to_thread,
so its commits ran on a separate OS thread from the main event loop's
own concurrent use of the same Session — confirmed live via a real
`sqlalchemy.exc.ResourceClosedError: This transaction is closed`
crashing both a background discovery task and the local-stock check at
the same instant. check_local_stock_for_listing() below still does both
steps inline for convenience (safe for a single-threaded caller, e.g.
tests or a one-off CLI check) — local_stock/worker.py does not use it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
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


def fetch_pickup_availability(
    client: RbsPlatformClient, *, sku: str, latitude: Decimal, longitude: Decimal
) -> set[str]:
    """Pure network call — no Session touched, safe to run in a worker
    thread (local_stock/worker.py does exactly that via
    asyncio.to_thread). Raises RbsPlatformError on a genuine network/API
    failure — the caller is expected to catch it per retailer, same
    posture as engine/worker.py catching a connector failure for one
    WatchRule."""
    results = client.check_pickup_availability(
        sku=sku, latitude=latitude, longitude=longitude, radius_km=3000
    )
    return {r.external_store_id for r in results if r.can_pick_up}


def apply_local_stock_results(
    session: Session,
    *,
    listing: Listing,
    located_stores: list[RetailStore],
    stores_with_pickup: set[str],
    product_name: str,
    price: str | None,
    now: datetime,
) -> int:
    """All DB writes for one already-fetched check — must run on
    whichever thread already owns `session` (the main event-loop thread
    in the real worker), never concurrently with any other use of it.
    Returns the number of stores updated."""
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
    """Convenience wrapper doing both steps inline — safe for a
    single-threaded caller (tests, a one-off CLI check) but NOT what
    local_stock/worker.py uses for the real, concurrent worker: see this
    module's docstring for why the real path calls
    fetch_pickup_availability()/apply_local_stock_results() separately
    instead, with only the former ever running off the main thread."""
    now = now or datetime.now(UTC)
    stores = crud.list_retail_stores(session, retailer=retailer)
    located_stores = [s for s in stores if s.latitude is not None and s.longitude is not None]
    if not located_stores:
        return 0

    # One national-radius call already covers every store for this
    # retailer — see module docstring on why this is never per-store.
    anchor = located_stores[0]
    stores_with_pickup = fetch_pickup_availability(
        client, sku=sku, latitude=anchor.latitude, longitude=anchor.longitude
    )
    return apply_local_stock_results(
        session,
        listing=listing,
        located_stores=located_stores,
        stores_with_pickup=stores_with_pickup,
        product_name=product_name,
        price=price,
        now=now,
    )


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
