"""No real network: a fake RetailDiscoverySource stands in for a real
merchant. Confirms app/worker.py's tick() runs discovery only when wired
up, only for due Products, never more than one concurrently, and never
lets a slow discovery delay tick() itself (Phase 22 performance fix —
discovery now fires as a background task, same shape as a purchase
attempt; see app/worker.py's module docstring).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.worker import tick
from connectors.base import ConnectorProduct
from connectors.registry import ConnectorRegistry
from database import crud
from discovery.base import RetailDiscoverySource
from discovery.registry import DiscoveryRegistry


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


class FakeDiscoverySource(RetailDiscoverySource):
    def __init__(self, results: list[ConnectorProduct] | None = None) -> None:
        self._results = results or []
        self.calls = 0

    def search(self, query, *, ean=None, mpn=None, limit=10):
        self.calls += 1
        return self._results


class SlowDiscoverySource(RetailDiscoverySource):
    """search() sleeps in a real OS thread — used to prove a slow
    discovery never delays tick() itself nor a second tick's monitoring
    checks."""

    def __init__(self, *, sleep_seconds: float, results: list[ConnectorProduct] | None = None):
        self._sleep_seconds = sleep_seconds
        self._results = results or []
        self.calls = 0

    def search(self, query, *, ean=None, mpn=None, limit=10):
        self.calls += 1
        time.sleep(self._sleep_seconds)
        return self._results


def _candidate() -> ConnectorProduct:
    return ConnectorProduct(
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


async def _drain_background_tasks() -> None:
    from app.worker import _background_tasks

    while _background_tasks:
        await asyncio.sleep(0)


def test_tick_without_discovery_registry_never_runs_discovery(session: Session) -> None:
    crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    notifier = FakeNotifier()

    asyncio.run(tick(session, ConnectorRegistry(), notifier))  # no discovery_registry given

    # no crash, and nothing to assert on discovery since it never ran —
    # the absence of an exception/side effect *is* the assertion here.


def test_tick_runs_discovery_for_due_product(session: Session) -> None:
    product = crud.create_product(
        session, "ETB Pokemon Chaos Ascendant FR", max_price=Decimal("60")
    )
    assert product.last_discovery_at is None  # due immediately
    source = FakeDiscoverySource(results=[_candidate()])
    registry = DiscoveryRegistry()
    registry.register("Kairyu", source)
    notifier = FakeNotifier()

    async def scenario() -> None:
        await tick(session, ConnectorRegistry(), notifier, discovery_registry=registry)
        await _drain_background_tasks()

    asyncio.run(scenario())

    assert source.calls == 1
    assert len(crud.list_watch_rules(session, product_id=product.id)) == 1
    session.refresh(product)
    assert product.last_discovery_at is not None


def test_tick_skips_discovery_when_not_due(session: Session) -> None:
    now = datetime.now(UTC)
    product = crud.create_product(session, "ETB Pokemon Chaos Ascendant FR")
    crud.update_product(session, product.id, last_discovery_at=now, discovery_interval=1800)
    source = FakeDiscoverySource(results=[_candidate()])
    registry = DiscoveryRegistry()
    registry.register("Kairyu", source)
    notifier = FakeNotifier()

    async def scenario() -> None:
        await tick(session, ConnectorRegistry(), notifier, discovery_registry=registry, now=now)
        await _drain_background_tasks()

    asyncio.run(scenario())

    assert source.calls == 0


def test_tick_starts_at_most_one_due_product(session: Session) -> None:
    crud.create_product(session, "Product A")
    crud.create_product(session, "Product B")
    source = FakeDiscoverySource(results=[])
    registry = DiscoveryRegistry()
    registry.register("Kairyu", source)
    notifier = FakeNotifier()

    async def scenario() -> None:
        await tick(session, ConnectorRegistry(), notifier, discovery_registry=registry)
        await _drain_background_tasks()

    asyncio.run(scenario())

    assert source.calls == 1  # only one product's discovery ran this tick


def test_monitoring_known_listing_does_not_invoke_discovery(session: Session) -> None:
    """A WatchRule with a known Listing is handled entirely by the
    existing fast-path monitoring — discovery only concerns Products,
    never fires just because a monitoring check ran."""
    from connectors.fake_store import FakeStoreConnector

    product = crud.create_product(session, "Duopack Evoli", ean="1234567890123")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/fake-1",
        external_id="fake-1",
    )
    crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=1, max_quantity=1
    )
    discovery_source = FakeDiscoverySource(results=[])
    discovery_registry = DiscoveryRegistry()
    discovery_registry.register("RetailerA", discovery_source)

    connector_registry = ConnectorRegistry()
    connector_registry.register(
        "RetailerA",
        FakeStoreConnector(
            products={
                "fake-1": {
                    "name": "Duopack Evoli",
                    "price": 13.99,
                    "available": True,
                    "seller": "RetailerA",
                    "url": "https://a.example/p/fake-1",
                    "ean": "1234567890123",
                }
            }
        ),
    )
    notifier = FakeNotifier()

    async def scenario() -> None:
        # Product has never been discovered -> is_discovery_due is True,
        # so discovery *does* run here, but only because the Product
        # itself is due — not "because monitoring found a known listing".
        await tick(session, connector_registry, notifier, discovery_registry=discovery_registry)
        await _drain_background_tasks()
        assert discovery_source.calls == 1

        # Second tick, product now has a recent last_discovery_at -> not due.
        await tick(session, connector_registry, notifier, discovery_registry=discovery_registry)
        await _drain_background_tasks()
        assert discovery_source.calls == 1

    asyncio.run(scenario())


def test_tick_returns_without_waiting_for_slow_discovery(session: Session) -> None:
    """The core Phase 22 performance requirement: a slow discovery must
    never delay tick() itself, so the monitoring fast path (and any
    Discord alert it fires) is never held up by a merchant search that
    is taking several seconds."""
    crud.create_product(session, "ETB Pokemon Chaos Ascendant FR", max_price=Decimal("60"))
    slow_source = SlowDiscoverySource(sleep_seconds=0.3)
    registry = DiscoveryRegistry()
    registry.register("Kairyu", slow_source)
    notifier = FakeNotifier()

    async def scenario() -> float:
        start = time.monotonic()
        await tick(session, ConnectorRegistry(), notifier, discovery_registry=registry)
        elapsed = time.monotonic() - start
        await _drain_background_tasks()
        return elapsed

    elapsed = asyncio.run(scenario())

    assert elapsed < 0.15  # tick() returned well before the 0.3s search finished
    assert slow_source.calls == 1


def test_slow_discovery_does_not_delay_next_tick_monitoring(session: Session) -> None:
    """A due WatchRule's monitoring check must still run promptly on the
    very next tick even while a previous tick's discovery is still
    running in the background."""
    from connectors.fake_store import FakeStoreConnector

    product = crud.create_product(session, "Duopack Evoli", ean="1234567890123")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/fake-1",
        external_id="fake-1",
    )
    crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing.id, check_interval=1, max_quantity=1
    )
    slow_source = SlowDiscoverySource(sleep_seconds=0.3)
    discovery_registry = DiscoveryRegistry()
    discovery_registry.register("Kairyu", slow_source)

    connector = FakeStoreConnector(
        products={
            "fake-1": {
                "name": "Duopack Evoli",
                "price": 13.99,
                "available": True,
                "seller": "RetailerA",
                "url": "https://a.example/p/fake-1",
                "ean": "1234567890123",
            }
        }
    )
    connector_registry = ConnectorRegistry()
    connector_registry.register("RetailerA", connector)
    notifier = FakeNotifier()

    async def scenario() -> float:
        t0 = datetime.now(UTC)
        # First tick starts the slow background discovery for `product`.
        await tick(
            session, connector_registry, notifier, discovery_registry=discovery_registry, now=t0
        )

        # Second tick, immediately after (but far enough past check_interval
        # to be due again), while discovery is still asleep in its
        # background thread — the known listing's own monitoring check
        # must still complete promptly.
        connector.update_product("fake-1", price=9.99)
        start = time.monotonic()
        results = await tick(
            session,
            connector_registry,
            notifier,
            discovery_registry=discovery_registry,
            now=t0 + timedelta(seconds=2),
        )
        elapsed = time.monotonic() - start
        await _drain_background_tasks()
        assert results[0].success is True
        return elapsed

    elapsed = asyncio.run(scenario())

    assert elapsed < 0.15


def test_at_most_one_discovery_in_flight_at_a_time(session: Session) -> None:
    crud.create_product(session, "Product A")
    crud.create_product(session, "Product B")
    slow_source = SlowDiscoverySource(sleep_seconds=0.1)
    registry = DiscoveryRegistry()
    registry.register("Kairyu", slow_source)
    notifier = FakeNotifier()

    async def scenario() -> None:
        # Two ticks fired back-to-back: the in-flight flag is set
        # synchronously the instant the first discovery is launched, so
        # the second tick's guard check sees it immediately regardless of
        # whether the first task's thread has actually started yet.
        await tick(session, ConnectorRegistry(), notifier, discovery_registry=registry)
        await tick(session, ConnectorRegistry(), notifier, discovery_registry=registry)
        await _drain_background_tasks()

    asyncio.run(scenario())

    assert slow_source.calls == 1  # only the first tick's discovery ever ran
