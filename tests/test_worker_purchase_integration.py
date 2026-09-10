"""Confirms app/worker.py's tick() only ever fires a purchase attempt
when explicitly wired up (purchase_registry+purchase_policy given), that
a purchase failure never crashes the monitoring loop, and that the tick
itself returns without waiting for the purchase attempt to finish (the
whole point of firing it as a background task) — no real network, no
real Discord, no real connector.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.worker import tick
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from purchase.base import PurchaseConnector
from purchase.config import PurchasePolicy
from purchase.registry import PurchaseConnectorRegistry


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


class SlowFailingConnector(PurchaseConnector):
    """revalidate() sleeps (in a real OS thread, via asyncio.to_thread)
    before raising — used to prove tick() does not wait for it."""

    def __init__(self, *, sleep_seconds: float = 0.2) -> None:
        self._sleep_seconds = sleep_seconds

    def revalidate(self, intent):
        time.sleep(self._sleep_seconds)
        raise RuntimeError("simulated connector bug")

    def checkout(self, intent, revalidated):
        raise AssertionError("checkout should never be reached in this test")


def _setup_rule(session: Session, *, max_price: str = "60") -> None:
    product = crud.create_product(session, "ETB Chaos Ascendant FR", ean="1234567890123")
    merchant = crud.create_merchant(session, "Kairyu")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://kairyu.fr/products/etb-1",
        external_id="etb-1",
    )
    crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=1,
        max_quantity=1,
        max_price=Decimal(max_price),
    )


def _fake_product(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "name": "ETB Chaos Ascendant FR",
        "price": 59.90,
        "available": True,
        "seller": "Kairyu",
        "url": "https://kairyu.fr/products/etb-1",
        "ean": "1234567890123",
    }
    defaults.update(overrides)
    return defaults


def _policy() -> PurchasePolicy:
    return PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )


def _purchase_registry(connector: PurchaseConnector) -> PurchaseConnectorRegistry:
    registry = PurchaseConnectorRegistry()
    registry.register("Kairyu", connector)
    return registry


async def _drain_background_tasks() -> None:
    from app.worker import _background_tasks

    while _background_tasks:
        await asyncio.sleep(0)


def test_no_purchase_attempt_without_registry_and_policy(session: Session) -> None:
    """Backward-compat: existing manual-only callers pass neither, and
    must never see a PurchaseAttempt row created."""
    _setup_rule(session)
    connector = FakeStoreConnector(products={"etb-1": _fake_product()})
    registry = ConnectorRegistry()
    registry.register("Kairyu", connector)
    notifier = FakeNotifier()

    t0 = datetime.now(UTC)
    asyncio.run(tick(session, registry, notifier, now=t0))
    connector.update_product("etb-1", price=55.0)  # still an event on 2nd tick
    asyncio.run(tick(session, registry, notifier, now=t0 + timedelta(seconds=2)))

    assert crud.list_purchase_attempts(session) == []


def test_purchase_connector_failure_does_not_crash_tick(session: Session) -> None:
    _setup_rule(session)
    retail_connector = FakeStoreConnector(products={"etb-1": _fake_product()})

    async def scenario() -> None:
        notifier = FakeNotifier()
        registry = ConnectorRegistry()
        registry.register("Kairyu", retail_connector)
        t0 = datetime.now(UTC)
        # First tick: no event yet (baseline observation).
        await tick(
            session,
            registry,
            notifier,
            now=t0,
            purchase_registry=_purchase_registry(SlowFailingConnector(sleep_seconds=0.01)),
            purchase_policy=_policy(),
        )
        retail_connector.update_product("etb-1", price=55.0)
        # Second tick: price drop event -> allowed decision -> purchase fires.
        await tick(
            session,
            registry,
            notifier,
            now=t0 + timedelta(seconds=2),
            purchase_registry=_purchase_registry(SlowFailingConnector(sleep_seconds=0.01)),
            purchase_policy=_policy(),
        )
        await _drain_background_tasks()

    asyncio.run(scenario())  # must not raise

    attempts = crud.list_purchase_attempts(session)
    assert len(attempts) == 1
    assert attempts[0].status == "failed"


def test_tick_returns_without_waiting_for_slow_purchase_attempt(session: Session) -> None:
    _setup_rule(session)
    retail_connector = FakeStoreConnector(products={"etb-1": _fake_product()})
    slow_connector = SlowFailingConnector(sleep_seconds=0.3)

    async def scenario() -> float:
        notifier = FakeNotifier()
        registry = ConnectorRegistry()
        registry.register("Kairyu", retail_connector)
        t0 = datetime.now(UTC)
        await tick(session, registry, notifier, now=t0)  # baseline, no purchase wiring
        retail_connector.update_product("etb-1", price=55.0)

        start = time.monotonic()
        await tick(
            session,
            registry,
            notifier,
            now=t0 + timedelta(seconds=2),
            purchase_registry=_purchase_registry(slow_connector),
            purchase_policy=_policy(),
        )
        elapsed = time.monotonic() - start
        await _drain_background_tasks()
        return elapsed

    elapsed = asyncio.run(scenario())

    assert elapsed < 0.15  # tick() returned well before the connector's 0.3s sleep finished
