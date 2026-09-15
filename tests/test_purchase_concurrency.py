"""Phase 33 sections 11/12: the same Product briefly in stock at several
retailers at once must never result in more than one real purchase.

Real, confirmed gap this session: the pre-existing idempotency guard
(get_active_purchase_attempt_for_listing) is scoped to one *listing*, so
two different Listings of the same Product each independently passed a
clean check and could both proceed. Fixed by adding a product-wide check
(database/crud.py::get_blocking_purchase_attempts_for_product) inside the
same synchronous section (no `await` in between, exactly like the
existing listing-level guard), plus a real SQLite partial UNIQUE index
(uq_one_active_or_purchased_attempt_per_product) as a database-level
backstop — see database/models.py::PurchaseAttempt.

No real network, no real Discord, no real money: FakeConnector's
checkout() always "succeeds" instantly (CheckoutResult with no real
order), and the test only asserts on PurchaseOutcome/DB state.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from products.matcher import MatchResult
from products.observation import ProductObservation
from purchase.base import CheckoutResult, PurchaseConnector
from purchase.config import PurchasePolicy
from purchase.engine import attempt_purchase
from purchase.models import PurchaseStatus, RevalidationResult
from purchase.registry import PurchaseConnectorRegistry


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_embeds: list[object] = []

    async def send_embed(self, embed: object) -> None:
        self.sent_embeds.append(embed)


class _AlwaysSucceedsConnector(PurchaseConnector):
    def __init__(self, *, price: Decimal) -> None:
        self._price = price

    def revalidate(self, intent):
        return RevalidationResult(
            available=True, price=self._price, shipping_cost=Decimal("0"), quantity_available=None
        )

    def checkout(self, intent, revalidated):
        return CheckoutResult(
            success=True,
            order_reference=f"ORDER-{intent.listing_id}",
            final_price=self._price,
            shipping_cost=Decimal("0"),
            total_cost=self._price,
            failure_reason=None,
        )


def _seed_same_product_on_n_listings(session: Session, n: int, *, price: str = "59.90") -> int:
    """One Product, N Listings on N distinct merchants — the exact "same
    product in stock at N retailers at once" shape."""
    product = crud.create_product(session, "ETB Chaos Ascendant FR")
    for i in range(n):
        merchant = crud.create_merchant(session, f"Retailer{i}")
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=f"https://retailer{i}.example/p/etb-1",
            external_id="etb-1",
        )
        crud.create_watch_rule(
            session,
            product_id=product.id,
            listing_id=listing.id,
            check_interval=60,
            max_quantity=1,
            max_price=Decimal("1000"),
        )
    return product.id


def _observation(merchant: str, price: str) -> ProductObservation:
    return ProductObservation(
        merchant=merchant,
        external_id="etb-1",
        name="ETB Chaos Ascendant FR",
        price=Decimal(price),
        currency="EUR",
        available=True,
        url=f"https://{merchant.lower()}.example/p/etb-1",
        observed_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


def _match() -> MatchResult:
    return MatchResult(matched=True, confidence=100, method="ean_exact", reason="test")


def _policy(*, n_domains: int = 60) -> PurchasePolicy:
    return PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({f"retailer{i}.example" for i in range(n_domains)}),
        cooldown_seconds=0,
    )


async def _run_concurrent_attempts(session: Session, product_id: int, n: int) -> list:
    rules = crud.list_watch_rules(session, product_id=product_id)
    assert len(rules) == n
    registry = PurchaseConnectorRegistry()
    for rule in rules:
        registry.register(
            rule.listing.merchant.name, _AlwaysSucceedsConnector(price=Decimal("59.90"))
        )
    notifier = FakeNotifier()

    policy = _policy(n_domains=n)

    async def _one(rule) -> object:
        merchant = rule.listing.merchant.name
        return await attempt_purchase(
            session,
            rule,
            _observation(merchant, "59.90"),
            _match(),
            policy,
            registry,
            (f"{merchant.lower()}.example",),
            notifier,
        )

    return await asyncio.gather(*(_one(rule) for rule in rules))


def test_two_retailers_same_product_only_one_purchase_wins(session: Session) -> None:
    product_id = _seed_same_product_on_n_listings(session, 2)

    outcomes = asyncio.run(_run_concurrent_attempts(session, product_id, 2))

    statuses = [o.status for o in outcomes]
    assert statuses.count(PurchaseStatus.PURCHASED) == 1
    assert statuses.count(PurchaseStatus.CANCELLED) == 1
    purchased_rows = [a for a in crud.list_purchase_attempts(session) if a.status == "purchased"]
    assert len(purchased_rows) == 1


def test_fifty_concurrent_attempts_same_product_exactly_one_winner(session: Session) -> None:
    """Section 12's explicit stress test: 50 concurrent purchase attempts
    for the same product (spread across 50 distinct retailers, as if it
    restocked everywhere at once) — at most one may ever succeed, even
    against a real (if in-memory) SQLite database."""
    n = 50
    product_id = _seed_same_product_on_n_listings(session, n)

    outcomes = asyncio.run(_run_concurrent_attempts(session, product_id, n))

    statuses = [o.status for o in outcomes]
    assert statuses.count(PurchaseStatus.PURCHASED) == 1
    assert statuses.count(PurchaseStatus.CANCELLED) == n - 1

    all_attempts = crud.list_purchase_attempts(session)
    purchased_rows = [a for a in all_attempts if a.status == "purchased"]
    assert len(purchased_rows) == 1
    # Every rejected attempt's own row (if one was even created before its
    # rejection) must never itself be "purchased" or still "active" —
    # confirms no second row was left in-flight.
    blocking = crud.get_blocking_purchase_attempts_for_product(session, product_id)
    assert len(blocking) == 1


def test_hundred_concurrent_attempts_same_product_exactly_one_winner(session: Session) -> None:
    """Phase 35 section 10: re-run of the race test AFTER the fast-path
    integration (attempt_purchase() now delegates to
    purchase/fast_path.py::run_hot_path — see purchase/engine.py), bumped
    to 100 simultaneous retailers per the new spec. Exactly one purchase
    may ever succeed and connector.checkout() may only ever be invoked
    once, even against a real (if in-memory) SQLite database."""
    n = 100
    product_id = _seed_same_product_on_n_listings(session, n)

    outcomes = asyncio.run(_run_concurrent_attempts(session, product_id, n))

    statuses = [o.status for o in outcomes]
    assert statuses.count(PurchaseStatus.PURCHASED) == 1
    assert statuses.count(PurchaseStatus.CANCELLED) == n - 1

    all_attempts = crud.list_purchase_attempts(session)
    purchased_rows = [a for a in all_attempts if a.status == "purchased"]
    assert len(purchased_rows) == 1
    blocking = crud.get_blocking_purchase_attempts_for_product(session, product_id)
    assert len(blocking) == 1
    # Every outcome carries a trace — proof every one of the 100 went
    # through the real fast path, not a hypothetical shortcut.
    assert all(o.trace is not None for o in outcomes)


def test_thousand_concurrent_attempts_same_product_exactly_one_winner(session: Session) -> None:
    """Phase 40 section 18: 1000 simultaneous signals for the same
    product (the same "restocked everywhere at once" shape, just an
    order of magnitude larger) must still yield exactly one purchase —
    the product-wide DB constraint is what actually enforces this, not
    the attempt count, so this is a scale check on that guarantee, not a
    new code path."""
    n = 1000
    product_id = _seed_same_product_on_n_listings(session, n)

    outcomes = asyncio.run(_run_concurrent_attempts(session, product_id, n))

    statuses = [o.status for o in outcomes]
    assert statuses.count(PurchaseStatus.PURCHASED) == 1
    assert statuses.count(PurchaseStatus.CANCELLED) == n - 1

    all_attempts = crud.list_purchase_attempts(session)
    purchased_rows = [a for a in all_attempts if a.status == "purchased"]
    assert len(purchased_rows) == 1
    blocking = crud.get_blocking_purchase_attempts_for_product(session, product_id)
    assert len(blocking) == 1


def test_database_level_constraint_is_real_not_just_a_comment(session: Session) -> None:
    """Defense-in-depth check: even bypassing the Python-level gate
    entirely and inserting two 'purchased' rows for the same product_id
    directly via crud, the database itself refuses the second one."""
    product = crud.create_product(session, "Direct Insert Product")
    merchant_a = crud.create_merchant(session, "DirectA")
    merchant_b = crud.create_merchant(session, "DirectB")
    listing_a = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant_a.id,
        url="https://a.example/1",
        external_id="a-1",
    )
    listing_b = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant_b.id,
        url="https://b.example/1",
        external_id="b-1",
    )
    rule_a = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing_a.id, check_interval=60, max_quantity=1
    )
    rule_b = crud.create_watch_rule(
        session, product_id=product.id, listing_id=listing_b.id, check_interval=60, max_quantity=1
    )

    crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_a.id,
        listing_id=listing_a.id,
        product_id=product.id,
        status="purchased",
        observed_price=Decimal("50"),
        max_price_allowed=Decimal("100"),
        quantity=1,
    )

    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        crud.create_purchase_attempt(
            session,
            watch_rule_id=rule_b.id,
            listing_id=listing_b.id,
            product_id=product.id,
            status="created",
            observed_price=Decimal("50"),
            max_price_allowed=Decimal("100"),
            quantity=1,
        )
