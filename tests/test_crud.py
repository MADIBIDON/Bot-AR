from __future__ import annotations

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
    product = crud.create_product(session, "Duopack Evoli 30 ans", max_price=14.99)

    updated = crud.update_product(session, product.id, max_price=12.99, status="paused")

    assert updated is not None
    assert updated.max_price == 12.99
    assert updated.status == "paused"


def test_update_product_missing_returns_none(session: Session) -> None:
    assert crud.update_product(session, 999, max_price=1.0) is None


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
