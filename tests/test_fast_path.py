"""Phase 35: purchase/fast_path.py::run_hot_path correctness + concurrency.

run_hot_path is the exact decide-then-claim sequence extracted out of
purchase/engine.py::attempt_purchase() — these tests exercise it
directly (no connector, no network), matching
tests/test_purchase_engine_integration.py's existing assertions about
what does/doesn't create a PurchaseAttempt row (a rejected decision must
leave zero rows — see test_wrong_ean_and_price_over_ceiling_create_no_row
below, mirroring test_purchase_disabled_creates_no_attempt_row in that
file).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from products.matcher import match_product
from products.observation import ProductObservation
from purchase.config import PurchasePolicy
from purchase.fast_path import run_hot_path


def _observation(
    *, external_id: str = "etb-1", price: str = "59.90", ean: str | None = None
) -> ProductObservation:
    from datetime import UTC, datetime

    return ProductObservation(
        merchant="Kairyu",
        external_id=external_id,
        name="ETB Chaos Ascendant FR",
        price=Decimal(price),
        currency="EUR",
        available=True,
        url="https://kairyu.fr/products/etb-1",
        observed_at=datetime.now(UTC),
        ean=ean,
    )


def _policy(*, enabled: bool = True) -> PurchasePolicy:
    return PurchasePolicy(
        enabled=enabled,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )


def _seed(session: Session, *, ean: str | None = "0196214142145", max_price: str = "80") -> int:
    product = crud.create_product(session, "ETB Chaos Ascendant FR", ean=ean)
    merchant = crud.create_merchant(session, "Kairyu")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://kairyu.fr/products/etb-1",
        external_id="etb-1",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=30,
        max_quantity=1,
        max_price=Decimal(max_price),
    )
    return rule.id


def test_happy_path_claims_and_records_all_timestamps(session: Session) -> None:
    rule_id = _seed(session)
    rule = crud.get_watch_rule(session, rule_id)
    observation = _observation(ean="0196214142145")
    match = match_product(rule.product, observation)

    result = run_hot_path(session, rule, observation, match, _policy(), ("kairyu.fr",))

    assert result.proceed is True
    assert result.attempt is not None
    assert result.attempt.status == "created"
    trace = result.trace
    assert trace.stock_received_ns <= trace.identity_validated_ns <= trace.decision_completed_ns
    assert trace.claim_acquired_ns is not None
    assert trace.claim_acquired_ns >= trace.decision_completed_ns
    assert trace.checkout_dispatch_ns is None  # not yet dispatched at this layer
    assert trace.t0_to_claim_ms is not None and trace.t0_to_claim_ms >= 0


def test_wrong_ean_and_price_over_ceiling_create_no_row(session: Session) -> None:
    """Decide-then-claim (not claim-then-decide): a rejected decision
    must leave the database untouched — same guarantee
    test_purchase_disabled_creates_no_attempt_row already audits for
    attempt_purchase() itself."""
    rule_id = _seed(session, ean="0000000000001", max_price="80")
    rule = crud.get_watch_rule(session, rule_id)

    wrong_ean_obs = _observation(ean="9999999999999")
    match = match_product(rule.product, wrong_ean_obs)
    result = run_hot_path(session, rule, wrong_ean_obs, match, _policy(), ("kairyu.fr",))

    assert result.proceed is False
    assert result.attempt is None
    assert result.trace.claim_acquired_ns is None
    assert crud.list_purchase_attempts(session) == []

    too_expensive_obs = _observation(ean="0000000000001", price="500")
    match2 = match_product(rule.product, too_expensive_obs)
    result2 = run_hot_path(session, rule, too_expensive_obs, match2, _policy(), ("kairyu.fr",))

    assert result2.proceed is False
    assert result2.attempt is None
    assert crud.list_purchase_attempts(session) == []


def test_kill_switch_off_creates_no_row(session: Session) -> None:
    rule_id = _seed(session)
    rule = crud.get_watch_rule(session, rule_id)
    observation = _observation(ean="0196214142145")
    match = match_product(rule.product, observation)

    result = run_hot_path(session, rule, observation, match, _policy(enabled=False), ("kairyu.fr",))

    assert result.proceed is False
    assert result.attempt is None
    assert crud.list_purchase_attempts(session) == []
    assert (
        "kill switch" in result.decision.reason.lower()
        or "PURCHASES_ENABLED" in result.decision.reason
    )


def test_second_signal_after_a_claim_is_blocked_without_creating_a_new_row(
    session: Session,
) -> None:
    rule_id = _seed(session)
    rule = crud.get_watch_rule(session, rule_id)
    observation = _observation(ean="0196214142145")
    match = match_product(rule.product, observation)

    first = run_hot_path(session, rule, observation, match, _policy(), ("kairyu.fr",))
    assert first.proceed is True

    second = run_hot_path(session, rule, observation, match, _policy(), ("kairyu.fr",))

    assert second.proceed is False
    assert second.attempt is None
    assert len(crud.list_purchase_attempts(session)) == 1


def _seed_same_product_on_n_rules(session: Session, n: int) -> tuple[int, list]:
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


def test_hundred_concurrent_signals_same_product_exactly_one_claim(session: Session) -> None:
    """Section 10's explicit re-test after integration: 100 retailers
    signal stock for the same product at once — at most one may ever
    acquire the claim, even under this decide-then-claim ordering."""
    n = 100
    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({f"retailer{i}.example" for i in range(n)}),
        cooldown_seconds=0,
    )
    product_id, rules = _seed_same_product_on_n_rules(session, n)
    observation = _observation(ean="0196214142145")

    async def _one(rule) -> object:
        match = match_product(rule.product, observation)
        domain = f"retailer{rule.listing.merchant.name[8:]}.example"
        return run_hot_path(session, rule, observation, match, policy, (domain,))

    async def _run_all() -> list:
        return await asyncio.gather(*(_one(r) for r in rules))

    results = asyncio.run(_run_all())

    assert sum(1 for r in results if r.proceed) == 1
    blocking = crud.get_blocking_purchase_attempts_for_product(session, product_id)
    assert len(blocking) == 1
