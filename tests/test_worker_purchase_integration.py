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

import app.worker as worker_module
from app.worker import tick
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from purchase.base import CheckoutResult, PurchaseConnector
from purchase.config import PurchasePolicy
from purchase.models import RevalidationResult
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


class FakeSucceedingConnector(PurchaseConnector):
    """Phase 35 section 9: a real (if fake) end-to-end success —
    revalidate() and checkout() both complete cleanly so the full worker
    -> attempt_purchase() -> run_hot_path() -> connector chain can be
    observed all the way through."""

    def __init__(self) -> None:
        self.revalidate_calls = 0
        self.checkout_calls = 0

    def revalidate(self, intent):
        self.revalidate_calls += 1
        return RevalidationResult(
            available=True,
            price=intent.observed_price,
            shipping_cost=Decimal("0"),
            quantity_available=None,
        )

    def checkout(self, intent, revalidated):
        self.checkout_calls += 1
        return CheckoutResult(
            success=True,
            order_reference="FAKE-ORDER-1",
            final_price=revalidated.price,
            shipping_cost=Decimal("0"),
            total_cost=revalidated.price,
            failure_reason=None,
        )


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


def test_worker_tick_routes_a_real_instock_signal_through_the_fast_path(
    monkeypatch, session: Session
) -> None:
    """Phase 35 section 9: proves a real, simulated worker tick — not
    just an isolated fast_path benchmark — actually drives a stock-
    positive signal through attempt_purchase()'s delegated run_hot_path()
    all the way to a connector's checkout(). outcome.trace is only ever
    populated inside run_hot_path/attempt_purchase, so its presence here
    is direct proof the real fast path executed."""
    _setup_rule(session)
    connector = FakeStoreConnector(products={"etb-1": _fake_product()})
    fake_purchase_connector = FakeSucceedingConnector()
    notifier = FakeNotifier()

    from purchase.engine import attempt_purchase as real_attempt_purchase

    captured: list[object] = []

    async def _spy(*args: object, **kwargs: object) -> object:
        outcome = await real_attempt_purchase(*args, **kwargs)
        captured.append(outcome)
        return outcome

    monkeypatch.setattr("app.worker.attempt_purchase", _spy)
    # app/worker.py's real wiring passes policy_provider=load_purchase_policy
    # (Phase 33 P0#3's fresh kill-switch re-check), which reads the REAL
    # .env — PURCHASES_ENABLED=false throughout this whole project,
    # including every test run. This test uses fake connectors and a
    # fake merchant domain (no real transaction risk either way), and
    # exists specifically to prove the fast path reaches a real
    # checkout() call — so it substitutes its own enabled synthetic
    # policy for that one re-read, exactly like
    # scripts/benchmark_purchase_pipeline.py already does; it never
    # touches or bypasses the real environment variable.
    monkeypatch.setattr("app.worker.load_purchase_policy", _policy)

    async def scenario() -> None:
        registry = ConnectorRegistry()
        registry.register("Kairyu", connector)
        t0 = datetime.now(UTC)
        await tick(session, registry, notifier, now=t0)  # baseline, no event yet
        connector.update_product("etb-1", price=55.0)
        await tick(
            session,
            registry,
            notifier,
            now=t0 + timedelta(seconds=2),
            purchase_registry=_purchase_registry(fake_purchase_connector),
            purchase_policy=_policy(),
        )
        await _drain_background_tasks()

    asyncio.run(scenario())

    assert len(captured) == 1
    outcome = captured[0]
    assert outcome.trace is not None
    assert outcome.trace.claim_acquired_ns is not None
    assert outcome.trace.stock_received_ns <= outcome.trace.claim_acquired_ns
    assert outcome.trace.checkout_dispatch_ns is not None
    assert fake_purchase_connector.revalidate_calls == 1
    assert fake_purchase_connector.checkout_calls == 1
    assert outcome.status.value == "purchased"


def test_worker_tick_fails_loudly_if_the_hot_path_is_bypassed(
    monkeypatch, session: Session
) -> None:
    """The explicit "must fail if rerouted to the legacy path" proof: if
    attempt_purchase() ever stops delegating to run_hot_path (e.g. a
    future edit inlines the decision+claim logic again), this sentinel
    can never fire and the assertion below fails."""
    _setup_rule(session)
    connector = FakeStoreConnector(products={"etb-1": _fake_product()})
    notifier = FakeNotifier()

    class _LegacyPathProof(Exception):
        pass

    def _sentinel(*args: object, **kwargs: object) -> object:
        raise _LegacyPathProof("run_hot_path was actually called")

    monkeypatch.setattr("purchase.engine.run_hot_path", _sentinel)

    captured_exceptions: list[BaseException] = []

    async def scenario() -> None:
        registry = ConnectorRegistry()
        registry.register("Kairyu", connector)
        t0 = datetime.now(UTC)
        await tick(session, registry, notifier, now=t0)
        connector.update_product("etb-1", price=55.0)
        await tick(
            session,
            registry,
            notifier,
            now=t0 + timedelta(seconds=2),
            purchase_registry=_purchase_registry(FakeSucceedingConnector()),
            purchase_policy=_policy(),
        )
        pending = list(worker_module._background_tasks)
        await _drain_background_tasks()
        for task in pending:
            if task.done() and not task.cancelled() and task.exception() is not None:
                captured_exceptions.append(task.exception())

    asyncio.run(scenario())

    assert len(captured_exceptions) == 1
    assert isinstance(captured_exceptions[0], _LegacyPathProof)
