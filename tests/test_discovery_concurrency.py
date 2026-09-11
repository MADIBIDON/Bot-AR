"""No real network: fake RetailDiscoverySource implementations with
controlled sleeps stand in for merchants. Confirms app/discovery.py's
Phase 23 concurrent-search phase actually overlaps (wall time close to
the slowest merchant, not the sum), respects a concurrency bound, isolates
one merchant's failure from the others, and never corrupts the DB.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.discovery import run_discovery_for_product
from connectors.base import ConnectorProduct
from database import crud
from discovery.base import DiscoveryError, RetailDiscoverySource
from discovery.registry import DiscoveryRegistry


class SlowDiscoverySource(RetailDiscoverySource):
    def __init__(self, *, sleep_seconds: float, results: list[ConnectorProduct] | None = None):
        self._sleep_seconds = sleep_seconds
        self._results = results or []
        self.calls = 0

    def search(self, query, *, ean=None, mpn=None, limit=10):
        self.calls += 1
        time.sleep(self._sleep_seconds)
        return self._results


class FailingDiscoverySource(RetailDiscoverySource):
    def search(self, query, *, ean=None, mpn=None, limit=10):
        raise DiscoveryError("merchant is down")


@dataclass
class _ConcurrencyCounter:
    lock: threading.Lock = field(default_factory=threading.Lock)
    current: int = 0
    peak: int = 0


class TrackingDiscoverySource(RetailDiscoverySource):
    """Records the peak number of concurrently in-flight search() calls
    across every instance sharing the same counter."""

    def __init__(self, *, sleep_seconds: float, counter: _ConcurrencyCounter):
        self._sleep_seconds = sleep_seconds
        self._counter = counter

    def search(self, query, *, ean=None, mpn=None, limit=10):
        with self._counter.lock:
            self._counter.current += 1
            self._counter.peak = max(self._counter.peak, self._counter.current)
        time.sleep(self._sleep_seconds)
        with self._counter.lock:
            self._counter.current -= 1
        return []


def _candidate(**overrides: object) -> ConnectorProduct:
    defaults: dict[str, object] = dict(
        external_id="etb-1",
        name="ETB Pokemon Chaos Ascendant FR",
        price=Decimal("59.90"),
        currency="EUR",
        available=True,
        seller="Kairyu",
        url="https://kairyu.fr/products/etb-1",
        ean=None,
        mpn=None,
    )
    defaults.update(overrides)
    return ConnectorProduct(**defaults)


def test_wall_time_close_to_slowest_merchant_not_sum(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    registry = DiscoveryRegistry()
    for name in ("Kairyu", "RelicTCG", "Fuji Store"):
        registry.register(name, SlowDiscoverySource(sleep_seconds=0.1))

    start = time.monotonic()
    asyncio.run(run_discovery_for_product(session, product, registry))
    elapsed = time.monotonic() - start

    # 3 merchants x 0.1s sequential would be ~0.3s; concurrent stays near 0.1s.
    assert elapsed < 0.2


def test_one_merchant_failure_does_not_block_or_delay_others(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    registry = DiscoveryRegistry()
    registry.register("Kairyu", FailingDiscoverySource())
    registry.register("RelicTCG", SlowDiscoverySource(sleep_seconds=0.05, results=[_candidate()]))

    result = asyncio.run(run_discovery_for_product(session, product, registry))

    statuses = {m.merchant: m.status for m in result.merchants}
    assert statuses == {"Kairyu": "error", "RelicTCG": "ok"}
    relictcg = next(m for m in result.merchants if m.merchant == "RelicTCG")
    assert len(relictcg.candidates) == 1


def test_concurrency_limit_is_respected(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    counter = _ConcurrencyCounter()
    registry = DiscoveryRegistry()
    for i in range(6):
        registry.register(
            f"Merchant{i}", TrackingDiscoverySource(sleep_seconds=0.05, counter=counter)
        )

    asyncio.run(run_discovery_for_product(session, product, registry, max_concurrent=2))

    assert counter.peak <= 2


def test_concurrent_searches_do_not_corrupt_db_writes(session: Session) -> None:
    """The network phase runs concurrently but every DB write happens
    afterwards, sequentially, on this test's own session — confirms no
    duplicate/missing rows come out of that split."""
    product = crud.create_product(
        session, "ETB Pokemon Chaos Ascendant FR", max_price=Decimal("60")
    )
    registry = DiscoveryRegistry()
    registry.register(
        "Kairyu",
        SlowDiscoverySource(sleep_seconds=0.02, results=[_candidate(seller="Kairyu")]),
    )
    registry.register(
        "RelicTCG",
        SlowDiscoverySource(
            sleep_seconds=0.02,
            results=[
                _candidate(
                    seller="RelicTCG",
                    external_id="etb-2",
                    url="https://www.relictcg.com/products/etb-2",
                )
            ],
        ),
    )
    registry.register("Fuji Store", SlowDiscoverySource(sleep_seconds=0.02, results=[]))

    asyncio.run(run_discovery_for_product(session, product, registry))

    rules = crud.list_watch_rules(session, product_id=product.id)
    assert len(rules) == 2
    assert {r.listing.merchant.name for r in rules} == {"Kairyu", "RelicTCG"}
    assert len(crud.list_listings_for_product(session, product.id)) == 2
