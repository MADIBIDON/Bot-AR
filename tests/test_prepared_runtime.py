"""purchase/prepared_runtime.py — build + cache + invalidation
(Phase 34/35 sections 4/5)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from purchase.config import PurchasePolicy
from purchase.prepared_runtime import (
    DropNotArmableError,
    PreparedDropRuntimeCache,
    build_prepared_drop_runtime,
)

_seed_counter = [0]


def _seed(session: Session, *, max_price: str | None = "80") -> int:
    _seed_counter[0] += 1
    n = _seed_counter[0]
    product = crud.create_product(session, "ETB Chaos Ascendant FR", ean=f"{n:013d}")
    merchant = crud.create_merchant(session, f"Kairyu{n}")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=f"https://kairyu.fr/products/etb-{n}",
        external_id=f"etb-{n}",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=30,
        max_quantity=1,
        max_price=Decimal(max_price) if max_price else None,
    )
    return rule.id


def _policy() -> PurchasePolicy:
    return PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )


def test_build_captures_static_identity_fields(session: Session) -> None:
    rule_id = _seed(session)
    rule = crud.get_watch_rule(session, rule_id)
    expected_ean = rule.product.ean
    expected_merchant = rule.listing.merchant.name

    runtime = build_prepared_drop_runtime(
        session, rule_id, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
    )

    assert runtime.expected_ean == expected_ean
    assert runtime.merchant == expected_merchant
    assert runtime.max_price_allowed == Decimal("80")
    assert runtime.quantity == 1


def test_build_refuses_a_rule_with_no_price_ceiling(session: Session) -> None:
    rule_id = _seed(session, max_price=None)

    try:
        build_prepared_drop_runtime(
            session, rule_id, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
        )
        raise AssertionError("expected DropNotArmableError")
    except DropNotArmableError:
        pass


def test_cache_builds_once_and_reuses_on_second_call(session: Session) -> None:
    rule_id = _seed(session)
    cache = PreparedDropRuntimeCache()

    first = cache.get_or_build(
        session, rule_id, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
    )
    second = cache.get_or_build(
        session, rule_id, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
    )

    assert first is second  # same object — no rebuild on the second call
    assert len(cache) == 1
    assert rule_id in cache


def test_invalidate_forces_a_rebuild_reflecting_new_state(session: Session) -> None:
    rule_id = _seed(session, max_price="80")
    cache = PreparedDropRuntimeCache()
    first = cache.get_or_build(
        session, rule_id, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
    )
    assert first.max_price_allowed == Decimal("80")

    crud.update_watch_rule(session, rule_id, max_price=Decimal("120"))
    cache.invalidate(rule_id)

    rebuilt = cache.get_or_build(
        session, rule_id, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
    )
    assert rebuilt is not first
    assert rebuilt.max_price_allowed == Decimal("120")


def test_invalidate_all_clears_every_entry(session: Session) -> None:
    rule_a = _seed(session)
    rule_b = _seed(session)
    cache = PreparedDropRuntimeCache()
    cache.get_or_build(
        session, rule_a, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
    )
    cache.get_or_build(
        session, rule_b, connector=None, policy=_policy(), merchant_domains=("kairyu.fr",)
    )
    assert len(cache) == 2

    cache.invalidate_all()

    assert len(cache) == 0
