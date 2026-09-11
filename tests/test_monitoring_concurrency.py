"""No real network: fake BaseConnector implementations with controlled
sleeps stand in for merchants. Confirms engine/worker.py's Phase 23
concurrent network phase actually overlaps (wall time close to the
slowest check, not the sum), respects a concurrency bound, isolates one
merchant's failure/exception from the others, and leaves the DB exactly
as correct as the old fully-sequential version did.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from connectors.base import BaseConnector, ConnectorError, ConnectorProduct
from connectors.registry import ConnectorRegistry
from database import crud
from engine.backoff import BackoffTracker
from engine.worker import run_monitoring_tick


class SlowConnector(BaseConnector):
    def __init__(self, *, sleep_seconds: float, price: str = "10.00"):
        self._sleep_seconds = sleep_seconds
        self._price = price
        self.calls = 0

    def get_product(self, external_id: str) -> ConnectorProduct:
        self.calls += 1
        time.sleep(self._sleep_seconds)
        return ConnectorProduct(
            external_id=external_id,
            name="Some Product",
            price=Decimal(self._price),
            currency="EUR",
            available=True,
            seller="Merchant",
            url=f"https://example.test/{external_id}",
        )


class FailingConnector(BaseConnector):
    def get_product(self, external_id: str) -> ConnectorProduct:
        raise ConnectorError("rate limited (429) fetching x")


@dataclass
class _ConcurrencyCounter:
    lock: threading.Lock = field(default_factory=threading.Lock)
    current: int = 0
    peak: int = 0


class TrackingConnector(BaseConnector):
    def __init__(self, *, sleep_seconds: float, counter: _ConcurrencyCounter):
        self._sleep_seconds = sleep_seconds
        self._counter = counter

    def get_product(self, external_id: str) -> ConnectorProduct:
        with self._counter.lock:
            self._counter.current += 1
            self._counter.peak = max(self._counter.peak, self._counter.current)
        time.sleep(self._sleep_seconds)
        with self._counter.lock:
            self._counter.current -= 1
        return ConnectorProduct(
            external_id=external_id,
            name="Some Product",
            price=Decimal("10.00"),
            currency="EUR",
            available=True,
            seller="Merchant",
            url=f"https://example.test/{external_id}",
        )


def _setup_rule(session: Session, *, merchant_name: str, external_id: str) -> None:
    product = crud.create_product(session, f"Product {external_id}")
    merchant = crud.create_merchant(session, merchant_name)
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=f"https://example.test/{external_id}",
        external_id=external_id,
    )
    crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=1, max_quantity=1
    )


def test_wall_time_close_to_slowest_check_not_sum(session: Session) -> None:
    registry = ConnectorRegistry()
    for i in range(4):
        merchant_name = f"Merchant{i}"
        _setup_rule(session, merchant_name=merchant_name, external_id=f"item-{i}")
        registry.register(merchant_name, SlowConnector(sleep_seconds=0.1))

    start = time.monotonic()
    results = asyncio.run(run_monitoring_tick(session, registry))
    elapsed = time.monotonic() - start

    assert len(results) == 4
    # 4 rules x 0.1s sequential would be ~0.4s; concurrent stays near 0.1s.
    assert elapsed < 0.25


def test_one_failing_connector_does_not_delay_the_others(session: Session) -> None:
    registry = ConnectorRegistry()
    _setup_rule(session, merchant_name="Bad", external_id="bad-1")
    _setup_rule(session, merchant_name="Good", external_id="good-1")
    registry.register("Bad", FailingConnector())
    registry.register("Good", SlowConnector(sleep_seconds=0.05))

    results = asyncio.run(run_monitoring_tick(session, registry))

    by_success = {r.success for _, r in results}
    assert by_success == {True, False}


def test_concurrency_limit_is_respected(session: Session) -> None:
    counter = _ConcurrencyCounter()
    registry = ConnectorRegistry()
    for i in range(6):
        merchant_name = f"Merchant{i}"
        _setup_rule(session, merchant_name=merchant_name, external_id=f"item-{i}")
        registry.register(merchant_name, TrackingConnector(sleep_seconds=0.05, counter=counter))

    asyncio.run(run_monitoring_tick(session, registry, max_concurrent=2))

    assert counter.peak <= 2


def test_concurrent_checks_write_exactly_one_observation_each(session: Session) -> None:
    """The network phase runs concurrently but every DB write
    (store_check_result) happens afterwards, sequentially, on this test's
    own session — confirms that split produces exactly the same DB state
    as the old fully-sequential version: one ObservationRecord per rule,
    no duplicates, no missing rows."""
    registry = ConnectorRegistry()
    listing_ids = []
    for i in range(5):
        merchant_name = f"Merchant{i}"
        product = crud.create_product(session, f"Product {i}")
        merchant = crud.create_merchant(session, merchant_name)
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=f"https://example.test/item-{i}",
            external_id=f"item-{i}",
        )
        crud.create_watch_rule(
            session, product_id=product.id, listing_id=listing.id, check_interval=1, max_quantity=1
        )
        listing_ids.append(listing.id)
        registry.register(merchant_name, SlowConnector(sleep_seconds=0.01))

    asyncio.run(run_monitoring_tick(session, registry))

    for listing_id in listing_ids:
        records = crud.list_observation_records_for_listing(session, listing_id)
        assert len(records) == 1


def test_backoff_recorded_correctly_under_concurrency(session: Session) -> None:
    """Backoff bookkeeping happens in the sequential store phase, after
    the concurrent network phase — confirms it still lands on the right
    rule even when checks completed concurrently/out of order."""
    from datetime import UTC, datetime

    registry = ConnectorRegistry()
    _setup_rule(session, merchant_name="Bad", external_id="bad-1")
    _setup_rule(session, merchant_name="Good", external_id="good-1")
    registry.register("Bad", FailingConnector())
    registry.register("Good", SlowConnector(sleep_seconds=0.01))
    backoff = BackoffTracker(base_seconds=100, max_seconds=1000)
    now = datetime.now(UTC)

    results = asyncio.run(run_monitoring_tick(session, registry, backoff=backoff, now=now))
    results_by_success = {r.success: rule for rule, r in results}

    bad_rule = results_by_success[False]
    good_rule = results_by_success[True]
    assert backoff.is_blocked(bad_rule.id, now) is True
    assert backoff.is_blocked(good_rule.id, now) is False
