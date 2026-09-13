"""Regression test for a real P0 found live on 2026-09-13: running
local_stock/worker.py's entire check (network AND DB writes) inside
asyncio.to_thread meant its commits could land on a different OS thread
than whatever else the app was doing with the same SQLAlchemy Session at
that instant — confirmed live as `sqlalchemy.exc.ResourceClosedError:
This transaction is closed`, crashing a background discovery task and
the local-stock check at the same timestamp.

The fix: local_stock/worker.py::run_local_stock_tick is now a coroutine
that only ever offloads the actual network call
(local_stock.monitor.fetch_pickup_availability) to a thread — every
Session read/write happens on the awaiting coroutine's own thread. This
test proves both the shape of the fix (it's a coroutine function) and
the actual guarantee (running it concurrently with another coroutine
that also writes to the same Session, via asyncio.gather, never raises).
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from local_stock.rbs_platform import RbsStoreStock
from local_stock.worker import run_local_stock_tick


class FakeRbsClient:
    def __init__(self, *, pickup_store_ids: set[str] | None = None) -> None:
        self.pickup_store_ids = pickup_store_ids or set()

    def check_pickup_availability(self, *, sku, latitude, longitude, radius_km=3000):
        return [
            RbsStoreStock(external_store_id=sid, can_pick_up=True) for sid in self.pickup_store_ids
        ]


def test_run_local_stock_tick_is_a_coroutine_function() -> None:
    """A cheap, direct guard against re-introducing the bug: wrapping a
    *sync* run_local_stock_tick in asyncio.to_thread from app/worker.py
    is exactly what caused it. It must stay a real coroutine function so
    app/worker.py can (and does) await it directly."""
    assert inspect.iscoroutinefunction(run_local_stock_tick)


def _setup(session: Session):
    product = crud.create_product(session, "Duopack Evoli 30 ans", ean="1234567890123")
    merchant = crud.create_merchant(session, "JouéClub")
    crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://www.joueclub.fr/p/fake",
        external_id="fake",
    )
    crud.upsert_retail_store(
        session,
        retailer="JouéClub",
        external_store_id="1001",
        name="JouéClub PARIS",
        city="PARIS",
        postal_code="75002",
        latitude=Decimal("48.87"),
        longitude=Decimal("2.34"),
        now=datetime.now(UTC),
    )


def test_local_stock_tick_runs_concurrently_with_another_session_writer(
    session: Session, monkeypatch
) -> None:
    """Reproduces the real-world shape of the bug: a local-stock check
    and something else writing to the same Session "at the same time"
    (interleaved via asyncio.gather, exactly how app/worker.py fires
    discovery and the local-stock check as sibling background tasks).
    Must never raise ResourceClosedError or anything else."""
    _setup(session)
    monkeypatch.setattr(
        "local_stock.worker.build_rbs_client",
        lambda retailer: FakeRbsClient(pickup_store_ids={"1001"}),
    )

    async def other_writer() -> None:
        for i in range(5):
            await asyncio.sleep(0)  # yield, same as a real coroutine's own awaits
            product = crud.create_product(session, f"Other Product {i}")
            assert product.id is not None

    async def scenario() -> int:
        return (await asyncio.gather(run_local_stock_tick(session), other_writer()))[0]

    checked = asyncio.run(scenario())

    assert checked == 1
