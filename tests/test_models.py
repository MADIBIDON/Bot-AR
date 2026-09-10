from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import Listing, Merchant, Product, WatchRule


def test_create_merchant_product_listing(session: Session) -> None:
    merchant = Merchant(name="RetailerA")
    product = Product(name="Duopack Evoli 30 ans", ean="1234567890123")
    session.add_all([merchant, product])
    session.commit()

    listing = Listing(product_id=product.id, merchant_id=merchant.id, url="https://a.example/p/1")
    session.add(listing)
    session.commit()

    assert listing.id is not None
    assert listing.product_id == product.id
    assert listing.merchant_id == merchant.id


def test_product_to_listing_relationship(session: Session) -> None:
    merchant_a = Merchant(name="RetailerA")
    merchant_b = Merchant(name="RetailerB")
    product = Product(name="ETB Pokemon 30 ans")
    session.add_all([merchant_a, merchant_b, product])
    session.commit()

    listing_a = Listing(
        product_id=product.id, merchant_id=merchant_a.id, url="https://a.example/p/1"
    )
    listing_b = Listing(
        product_id=product.id, merchant_id=merchant_b.id, url="https://b.example/p/1"
    )
    session.add_all([listing_a, listing_b])
    session.commit()
    session.refresh(product)

    assert len(product.listings) == 2
    assert {listing.merchant.name for listing in product.listings} == {"RetailerA", "RetailerB"}


def test_listing_belongs_to_one_merchant(session: Session) -> None:
    merchant = Merchant(name="RetailerA")
    product = Product(name="Duopack Evoli 30 ans")
    session.add_all([merchant, product])
    session.commit()

    listing = Listing(product_id=product.id, merchant_id=merchant.id, url="https://a.example/p/1")
    session.add(listing)
    session.commit()
    session.refresh(merchant)

    assert listing.merchant.id == merchant.id
    assert listing in merchant.listings


def test_product_ean_must_be_unique(session: Session) -> None:
    session.add(Product(name="Product A", ean="1234567890123"))
    session.commit()

    session.add(Product(name="Product B", ean="1234567890123"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_product_ean_can_be_null_for_multiple_products(session: Session) -> None:
    session.add_all([Product(name="Product A"), Product(name="Product B")])
    session.commit()  # must not raise: NULL is not considered a duplicate


def test_merchant_name_must_be_unique(session: Session) -> None:
    session.add(Merchant(name="RetailerA"))
    session.commit()

    session.add(Merchant(name="RetailerA"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_listing_without_external_id_unique_per_merchant_and_url(session: Session) -> None:
    merchant = Merchant(name="RetailerA")
    product = Product(name="Duopack Evoli 30 ans")
    session.add_all([merchant, product])
    session.commit()

    session.add(
        Listing(product_id=product.id, merchant_id=merchant.id, url="https://a.example/p/1")
    )
    session.commit()

    session.add(
        Listing(product_id=product.id, merchant_id=merchant.id, url="https://a.example/p/1")
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_listing_without_external_id_same_url_allowed_across_merchants(session: Session) -> None:
    merchant_a = Merchant(name="RetailerA")
    merchant_b = Merchant(name="RetailerB")
    product = Product(name="Duopack Evoli 30 ans")
    session.add_all([merchant_a, merchant_b, product])
    session.commit()

    session.add(
        Listing(product_id=product.id, merchant_id=merchant_a.id, url="https://shared.example/p/1")
    )
    session.add(
        Listing(product_id=product.id, merchant_id=merchant_b.id, url="https://shared.example/p/1")
    )
    session.commit()  # must not raise: different merchants, no external_id to collide on


def test_listing_external_id_must_be_unique_per_merchant(session: Session) -> None:
    merchant = Merchant(name="RetailerA")
    product = Product(name="Duopack Evoli 30 ans")
    session.add_all([merchant, product])
    session.commit()

    session.add(
        Listing(
            product_id=product.id,
            merchant_id=merchant.id,
            url="https://a.example/p/1",
            external_id="SKU-123",
        )
    )
    session.commit()

    session.add(
        Listing(
            product_id=product.id,
            merchant_id=merchant.id,
            url="https://a.example/p/2",
            external_id="SKU-123",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_listing_same_external_id_allowed_across_merchants(session: Session) -> None:
    merchant_a = Merchant(name="RetailerA")
    merchant_b = Merchant(name="RetailerB")
    product = Product(name="Duopack Evoli 30 ans")
    session.add_all([merchant_a, merchant_b, product])
    session.commit()

    session.add(
        Listing(
            product_id=product.id,
            merchant_id=merchant_a.id,
            url="https://a.example/p/1",
            external_id="SHARED-ID",
        )
    )
    session.add(
        Listing(
            product_id=product.id,
            merchant_id=merchant_b.id,
            url="https://b.example/p/1",
            external_id="SHARED-ID",
        )
    )
    session.commit()  # must not raise: external_id scoped per merchant


def test_listing_url_can_change_when_external_id_known(session: Session) -> None:
    merchant = Merchant(name="RetailerA")
    product = Product(name="Duopack Evoli 30 ans")
    session.add_all([merchant, product])
    session.commit()

    listing = Listing(
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://a.example/old-slug",
        external_id="SKU-123",
    )
    session.add(listing)
    session.commit()

    listing.url = "https://a.example/new-slug"
    session.commit()  # must not raise: identity is external_id, url is mutable

    session.refresh(listing)
    assert listing.url == "https://a.example/new-slug"
    assert listing.external_id == "SKU-123"


def test_watch_rule_requires_a_product(session: Session) -> None:
    session.add(WatchRule(check_interval=300, max_quantity=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_global_without_listing(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    rule = WatchRule(product_id=product.id, check_interval=300, max_quantity=2)
    session.add(rule)
    session.commit()

    assert rule.listing_id is None
    assert rule in product.watch_rules


def test_watch_rule_targeted_on_listing(session: Session) -> None:
    merchant = Merchant(name="RetailerA")
    product = Product(name="Duopack Evoli 30 ans")
    session.add_all([merchant, product])
    session.commit()
    listing = Listing(product_id=product.id, merchant_id=merchant.id, url="https://a.example/p/1")
    session.add(listing)
    session.commit()

    rule = WatchRule(
        product_id=product.id, listing_id=listing.id, check_interval=300, max_quantity=1
    )
    session.add(rule)
    session.commit()

    assert rule in listing.watch_rules
    assert rule.listing.id == listing.id


def test_watch_rule_target_price_must_be_positive(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(
        WatchRule(product_id=product.id, target_price=0, check_interval=300, max_quantity=1)
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_max_price_must_be_positive(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(WatchRule(product_id=product.id, max_price=-1, check_interval=300, max_quantity=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_check_interval_must_be_positive(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(WatchRule(product_id=product.id, check_interval=0, max_quantity=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_max_quantity_must_be_positive(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(WatchRule(product_id=product.id, check_interval=300, max_quantity=0))
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_priority_must_be_within_bounds(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(WatchRule(product_id=product.id, check_interval=300, max_quantity=1, priority=11))
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_estimated_resale_price_must_be_positive(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(
        WatchRule(
            product_id=product.id,
            check_interval=300,
            max_quantity=1,
            estimated_resale_price=0,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_platform_fee_pct_must_be_below_100(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(
        WatchRule(product_id=product.id, check_interval=300, max_quantity=1, platform_fee_pct=100)
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_platform_fee_pct_must_not_be_negative(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(
        WatchRule(product_id=product.id, check_interval=300, max_quantity=1, platform_fee_pct=-1)
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_fixed_fee_must_not_be_negative(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(WatchRule(product_id=product.id, check_interval=300, max_quantity=1, fixed_fee=-1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_shipping_cost_zero_is_allowed(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    rule = WatchRule(product_id=product.id, check_interval=300, max_quantity=1, shipping_cost=0)
    session.add(rule)
    session.commit()  # must not raise: zero shipping is a legitimate cost

    assert rule.shipping_cost == 0


def test_watch_rule_other_costs_must_not_be_negative(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    session.add(
        WatchRule(product_id=product.id, check_interval=300, max_quantity=1, other_costs=-1)
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_watch_rule_opportunity_fields_default_to_none(session: Session) -> None:
    product = Product(name="Duopack Evoli 30 ans")
    session.add(product)
    session.commit()

    rule = WatchRule(product_id=product.id, check_interval=300, max_quantity=1)
    session.add(rule)
    session.commit()

    assert rule.estimated_resale_price is None
    assert rule.platform_fee_pct is None
    assert rule.fixed_fee is None
    assert rule.shipping_cost is None
    assert rule.other_costs is None
