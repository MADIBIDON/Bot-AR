"""Phase 35 section 11: multi-retailer fastest-wins.

Retailer A's stock-positive signal arrives at T+80ms, B at T+100ms, C at
T+500ms (three real, staggered asyncio delays, not a synchronous burst
like tests/test_purchase_concurrency.py's own race tests). A must be
able to acquire the claim as soon as its own signal arrives — the still-
pending, much-slower C must never delay it. Existing policy (never
touched here) is what decides whether a later retailer can retry after
an earlier one failed before any order confirmed — see
test_a_failing_before_any_order_lets_b_retry below.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
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
    def revalidate(self, intent):
        return RevalidationResult(
            available=True,
            price=intent.observed_price,
            shipping_cost=Decimal("0"),
            quantity_available=None,
        )

    def checkout(self, intent, revalidated):
        return CheckoutResult(
            success=True,
            order_reference=f"ORDER-{intent.listing_id}",
            final_price=revalidated.price,
            shipping_cost=Decimal("0"),
            total_cost=revalidated.price,
            failure_reason=None,
        )


class _AlwaysFailsCheckoutConnector(PurchaseConnector):
    """revalidate() succeeds (stock/price look fine) but checkout()
    reports a genuine failure — never HumanActionRequired/Unsupported,
    a plain FAILED, so the attempt is NOT left in a blocking status."""

    def revalidate(self, intent):
        return RevalidationResult(
            available=True,
            price=intent.observed_price,
            shipping_cost=Decimal("0"),
            quantity_available=None,
        )

    def checkout(self, intent, revalidated):
        return CheckoutResult(
            success=False,
            order_reference=None,
            final_price=None,
            shipping_cost=None,
            total_cost=None,
            failure_reason="Simulated merchant-side checkout rejection.",
        )


def _seed_same_product_on_n_listings(session: Session, n: int, *, price: str = "59.90") -> tuple:
    product = crud.create_product(session, "ETB Chaos Ascendant FR", ean="0196214142145")
    rules = []
    for i in range(n):
        merchant = crud.create_merchant(session, f"Retailer{i}")
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=f"https://retailer{i}.example/p/etb-1",
            external_id="etb-1",
        )
        rule = crud.create_watch_rule(
            session,
            product_id=product.id,
            listing_id=listing.id,
            check_interval=60,
            max_quantity=1,
            max_price=Decimal("1000"),
        )
        rules.append(rule)
    return product.id, rules


def _observation(merchant: str, price: str = "59.90") -> ProductObservation:
    return ProductObservation(
        merchant=merchant,
        external_id="etb-1",
        name="ETB Chaos Ascendant FR",
        price=Decimal(price),
        currency="EUR",
        available=True,
        url=f"https://{merchant.lower()}.example/p/etb-1",
        observed_at=datetime.now(UTC),
    )


def _match() -> MatchResult:
    return MatchResult(matched=True, confidence=100, method="ean_exact", reason="test")


def _policy(n: int) -> PurchasePolicy:
    return PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({f"retailer{i}.example" for i in range(n)}),
        cooldown_seconds=0,
    )


def test_fastest_retailer_claims_immediately_slow_one_never_blocks_it(
    session: Session,
) -> None:
    n = 3
    product_id, rules = _seed_same_product_on_n_listings(session, n)
    registry = PurchaseConnectorRegistry()
    for rule in rules:
        registry.register(rule.listing.merchant.name, _AlwaysSucceedsConnector())
    notifier = FakeNotifier()
    policy = _policy(n)
    completion_order: list[str] = []

    async def _signal(rule, delay_seconds: float):
        await asyncio.sleep(delay_seconds)
        merchant = rule.listing.merchant.name
        outcome = await attempt_purchase(
            session,
            rule,
            _observation(merchant),
            _match(),
            policy,
            registry,
            (f"{merchant.lower()}.example",),
            notifier,
        )
        completion_order.append(merchant)
        return outcome

    async def scenario():
        t_start = time.monotonic()
        outcomes = await asyncio.gather(
            _signal(rules[0], 0.08),  # A: T+80ms
            _signal(rules[1], 0.10),  # B: T+100ms
            _signal(rules[2], 0.50),  # C: T+500ms
        )
        return outcomes, time.monotonic() - t_start

    outcomes, elapsed = asyncio.run(scenario())

    a_outcome, b_outcome, c_outcome = outcomes
    assert a_outcome.status == PurchaseStatus.PURCHASED  # first to arrive, first to claim
    assert b_outcome.status == PurchaseStatus.CANCELLED  # arrived 20ms later, already claimed
    assert c_outcome.status == PurchaseStatus.CANCELLED  # arrived 420ms later, already claimed
    # A and B both complete well before C's 500ms signal even arrives —
    # proof the slow retailer never delayed the fast ones.
    assert completion_order[0] == "Retailer0"
    assert elapsed < 0.55  # not 0.08+0.10+0.50=0.68s sequential
    purchased = [a for a in crud.list_purchase_attempts(session) if a.status == "purchased"]
    assert len(purchased) == 1


def test_a_failing_before_any_order_lets_b_retry(session: Session) -> None:
    """Existing policy, unchanged: a FAILED attempt (checkout genuinely
    rejected, no order ever confirmed) is not a blocking status — a
    later signal for the same product may still legitimately claim and
    succeed. Never touches evaluate_purchase_intent/the blocking-status
    set — this only proves the existing behavior held after Phase 35's
    integration."""
    n = 2
    product_id, rules = _seed_same_product_on_n_listings(session, n)
    registry = PurchaseConnectorRegistry()
    registry.register(rules[0].listing.merchant.name, _AlwaysFailsCheckoutConnector())
    registry.register(rules[1].listing.merchant.name, _AlwaysSucceedsConnector())
    notifier = FakeNotifier()
    policy = _policy(n)

    async def scenario():
        merchant_a = rules[0].listing.merchant.name
        outcome_a = await attempt_purchase(
            session,
            rules[0],
            _observation(merchant_a),
            _match(),
            policy,
            registry,
            (f"{merchant_a.lower()}.example",),
            notifier,
        )
        merchant_b = rules[1].listing.merchant.name
        outcome_b = await attempt_purchase(
            session,
            rules[1],
            _observation(merchant_b),
            _match(),
            policy,
            registry,
            (f"{merchant_b.lower()}.example",),
            notifier,
        )
        return outcome_a, outcome_b

    outcome_a, outcome_b = asyncio.run(scenario())

    assert outcome_a.status == PurchaseStatus.FAILED
    assert outcome_b.status == PurchaseStatus.PURCHASED
    purchased = [a for a in crud.list_purchase_attempts(session) if a.status == "purchased"]
    assert len(purchased) == 1
