from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud


def _seed_rule(session: Session) -> tuple[int, int, int]:
    product = crud.create_product(session, "ETB Chaos Ascendant FR")
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
        check_interval=60,
        max_quantity=1,
        max_price=Decimal("60"),
    )
    return rule.id, listing.id, product.id


def test_no_active_attempt_by_default(session: Session) -> None:
    _rule_id, listing_id, _product_id = _seed_rule(session)

    assert crud.get_active_purchase_attempt_for_listing(session, listing_id) is None


def test_active_attempt_found_for_in_flight_statuses(session: Session) -> None:
    rule_id, listing_id, product_id = _seed_rule(session)
    for status in ("created", "validating", "checkout_started"):
        crud.create_purchase_attempt(
            session,
            watch_rule_id=rule_id,
            listing_id=listing_id,
            product_id=product_id,
            status=status,
            observed_price=Decimal("59.90"),
            max_price_allowed=Decimal("60"),
            quantity=1,
        )
        assert crud.get_active_purchase_attempt_for_listing(session, listing_id) is not None
        crud.update_purchase_attempt(
            session,
            crud.get_active_purchase_attempt_for_listing(session, listing_id).id,
            status="cancelled",
        )


def test_terminal_statuses_are_not_active(session: Session) -> None:
    rule_id, listing_id, product_id = _seed_rule(session)
    for status in ("purchased", "failed", "human_action_required", "cancelled"):
        crud.create_purchase_attempt(
            session,
            watch_rule_id=rule_id,
            listing_id=listing_id,
            product_id=product_id,
            status=status,
            observed_price=Decimal("59.90"),
            max_price_allowed=Decimal("60"),
            quantity=1,
        )
    assert crud.get_active_purchase_attempt_for_listing(session, listing_id) is None


def test_update_purchase_attempt_unknown_field_raises(session: Session) -> None:
    rule_id, listing_id, product_id = _seed_rule(session)
    attempt = crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=product_id,
        status="created",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )

    try:
        crud.update_purchase_attempt(session, attempt.id, not_a_real_field=True)
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError")


def test_update_missing_attempt_returns_none(session: Session) -> None:
    assert crud.update_purchase_attempt(session, 999, status="failed") is None


def test_sum_purchased_total_only_counts_purchased_status(session: Session) -> None:
    rule_id, listing_id, product_id = _seed_rule(session)
    crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=product_id,
        status="failed",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )
    purchased = crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=product_id,
        status="created",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )
    crud.update_purchase_attempt(
        session, purchased.id, status="purchased", total_cost=Decimal("59.90")
    )

    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    total = crud.sum_purchased_total_since(session, since)

    assert total == Decimal("59.90")


def test_sum_purchased_total_excludes_old_attempts(session: Session) -> None:
    rule_id, listing_id, product_id = _seed_rule(session)
    attempt = crud.create_purchase_attempt(
        session,
        watch_rule_id=rule_id,
        listing_id=listing_id,
        product_id=product_id,
        status="purchased",
        observed_price=Decimal("59.90"),
        max_price_allowed=Decimal("60"),
        quantity=1,
    )
    crud.update_purchase_attempt(session, attempt.id, total_cost=Decimal("59.90"))

    since = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)  # future -> excludes all
    total = crud.sum_purchased_total_since(session, since)

    assert total == Decimal("0")


def test_sum_purchased_total_with_no_attempts_is_zero(session: Session) -> None:
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)

    assert crud.sum_purchased_total_since(session, since) == Decimal("0")


def test_list_purchase_attempts_respects_limit(session: Session) -> None:
    rule_id, listing_id, product_id = _seed_rule(session)
    for _ in range(3):
        crud.create_purchase_attempt(
            session,
            watch_rule_id=rule_id,
            listing_id=listing_id,
            product_id=product_id,
            status="failed",
            observed_price=Decimal("59.90"),
            max_price_allowed=Decimal("60"),
            quantity=1,
        )

    assert len(crud.list_purchase_attempts(session)) == 3
    assert len(crud.list_purchase_attempts(session, limit=2)) == 2
