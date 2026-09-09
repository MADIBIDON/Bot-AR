from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from database import crud


def test_create_and_get_product(session: Session) -> None:
    created = crud.create_product(session, "Duopack Evoli 30 ans", ean="1234567890123")

    fetched = crud.get_product(session, created.id)

    assert fetched is not None
    assert fetched.name == "Duopack Evoli 30 ans"
    assert fetched.ean == "1234567890123"
    assert fetched.status == "active"


def test_get_product_missing_returns_none(session: Session) -> None:
    assert crud.get_product(session, 999) is None


def test_list_products(session: Session) -> None:
    crud.create_product(session, "Product A")
    crud.create_product(session, "Product B")

    products = crud.list_products(session)

    assert {p.name for p in products} == {"Product A", "Product B"}


def test_list_products_filtered_by_status(session: Session) -> None:
    crud.create_product(session, "Active product", status="active")
    crud.create_product(session, "Paused product", status="paused")

    active = crud.list_products(session, status="active")

    assert [p.name for p in active] == ["Active product"]


def test_update_product(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans", max_price=Decimal("14.99"))

    updated = crud.update_product(session, product.id, max_price=Decimal("12.99"), status="paused")

    assert updated is not None
    assert updated.max_price == Decimal("12.99")
    assert updated.status == "paused"


def test_update_product_missing_returns_none(session: Session) -> None:
    assert crud.update_product(session, 999, max_price=Decimal("1.0")) is None


def test_create_merchant(session: Session) -> None:
    merchant = crud.create_merchant(session, "RetailerA", website_url="https://a.example")

    fetched = crud.get_merchant(session, merchant.id)

    assert fetched is not None
    assert fetched.name == "RetailerA"
    assert fetched.website_url == "https://a.example"


def test_create_listing_links_product_and_merchant(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")
    merchant = crud.create_merchant(session, "RetailerA")

    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/p/1",
        external_id="SKU-123",
    )

    fetched = crud.get_listing(session, listing.id)
    assert fetched is not None
    assert fetched.product_id == product.id
    assert fetched.merchant_id == merchant.id
    assert fetched.external_id == "SKU-123"


def test_list_listings_for_product_across_merchants(session: Session) -> None:
    product = crud.create_product(session, "ETB Pokemon 30 ans")
    merchant_a = crud.create_merchant(session, "RetailerA")
    merchant_b = crud.create_merchant(session, "RetailerB")
    crud.create_listing(
        session, product_id=product.id, merchant_id=merchant_a.id, url="https://a.example/p/1"
    )
    crud.create_listing(
        session, product_id=product.id, merchant_id=merchant_b.id, url="https://b.example/p/1"
    )

    listings = crud.list_listings_for_product(session, product.id)

    assert len(listings) == 2
    assert {listing.merchant_id for listing in listings} == {merchant_a.id, merchant_b.id}


def test_create_watch_rule_valid(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")

    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        target_price=Decimal("13.99"),
        max_price=Decimal("14.99"),
        check_interval=300,
        max_quantity=4,
        priority=7,
    )

    fetched = crud.get_watch_rule(session, rule.id)
    assert fetched is not None
    assert fetched.target_price == Decimal("13.99")
    assert fetched.max_price == Decimal("14.99")
    assert fetched.priority == 7
    assert fetched.enabled is True


def test_create_watch_rule_global_without_listing(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")

    rule = crud.create_watch_rule(
        session, product_id=product.id, check_interval=300, max_quantity=1
    )

    assert rule.listing_id is None


def test_create_watch_rule_targeted_on_listing(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")
    merchant = crud.create_merchant(session, "RetailerA")
    listing = crud.create_listing(
        session, product_id=product.id, merchant_id=merchant.id, url="https://a.example/p/1"
    )

    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=300,
        max_quantity=1,
    )

    assert rule.listing_id == listing.id


def test_create_watch_rule_rejects_listing_from_other_product(session: Session) -> None:
    product_a = crud.create_product(session, "Product A")
    product_b = crud.create_product(session, "Product B")
    merchant = crud.create_merchant(session, "RetailerA")
    listing_of_b = crud.create_listing(
        session, product_id=product_b.id, merchant_id=merchant.id, url="https://a.example/p/1"
    )

    with pytest.raises(ValueError, match="belongs to product"):
        crud.create_watch_rule(
            session,
            product_id=product_a.id,
            listing_id=listing_of_b.id,
            check_interval=300,
            max_quantity=1,
        )


def test_update_watch_rule_rejects_listing_from_other_product(session: Session) -> None:
    product_a = crud.create_product(session, "Product A")
    product_b = crud.create_product(session, "Product B")
    merchant = crud.create_merchant(session, "RetailerA")
    listing_of_b = crud.create_listing(
        session, product_id=product_b.id, merchant_id=merchant.id, url="https://a.example/p/1"
    )
    rule = crud.create_watch_rule(
        session, product_id=product_a.id, check_interval=300, max_quantity=1
    )

    with pytest.raises(ValueError, match="belongs to product"):
        crud.update_watch_rule(session, rule.id, listing_id=listing_of_b.id)


def test_enable_and_disable_watch_rule(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")
    rule = crud.create_watch_rule(
        session, product_id=product.id, check_interval=300, max_quantity=1
    )

    disabled = crud.disable_watch_rule(session, rule.id)
    assert disabled is not None
    assert disabled.enabled is False

    enabled = crud.enable_watch_rule(session, rule.id)
    assert enabled is not None
    assert enabled.enabled is True


def test_enable_watch_rule_missing_returns_none(session: Session) -> None:
    assert crud.enable_watch_rule(session, 999) is None


def test_update_watch_rule(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        target_price=Decimal("13.99"),
        check_interval=300,
        max_quantity=1,
    )

    updated = crud.update_watch_rule(session, rule.id, target_price=Decimal("12.49"), priority=9)

    assert updated is not None
    assert updated.target_price == Decimal("12.49")
    assert updated.priority == 9


def test_update_watch_rule_missing_returns_none(session: Session) -> None:
    assert crud.update_watch_rule(session, 999, priority=1) is None


def test_list_watch_rules_filtered_by_enabled(session: Session) -> None:
    product = crud.create_product(session, "Duopack Evoli 30 ans")
    active = crud.create_watch_rule(
        session, product_id=product.id, check_interval=300, max_quantity=1
    )
    paused = crud.create_watch_rule(
        session, product_id=product.id, check_interval=300, max_quantity=1, enabled=False
    )

    enabled_rules = crud.list_watch_rules(session, enabled=True)
    disabled_rules = crud.list_watch_rules(session, enabled=False)

    assert [r.id for r in enabled_rules] == [active.id]
    assert [r.id for r in disabled_rules] == [paused.id]


def test_list_watch_rules_filtered_by_product(session: Session) -> None:
    product_a = crud.create_product(session, "Product A")
    product_b = crud.create_product(session, "Product B")
    rule_a = crud.create_watch_rule(
        session, product_id=product_a.id, check_interval=300, max_quantity=1
    )
    crud.create_watch_rule(session, product_id=product_b.id, check_interval=300, max_quantity=1)

    rules = crud.list_watch_rules(session, product_id=product_a.id)

    assert [r.id for r in rules] == [rule_a.id]
