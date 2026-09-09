"""Minimal CRUD operations for Merchant, Product, Listing.

Every function takes an explicit Session — no global/implicit session state.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Listing, Merchant, Product


def create_merchant(session: Session, name: str, website_url: str | None = None) -> Merchant:
    merchant = Merchant(name=name, website_url=website_url)
    session.add(merchant)
    session.commit()
    session.refresh(merchant)
    return merchant


def get_merchant(session: Session, merchant_id: int) -> Merchant | None:
    return session.get(Merchant, merchant_id)


def create_product(
    session: Session,
    name: str,
    *,
    brand: str | None = None,
    category: str | None = None,
    ean: str | None = None,
    gtin: str | None = None,
    mpn: str | None = None,
    keywords: str | None = None,
    image_url: str | None = None,
    target_price: float | None = None,
    max_price: float | None = None,
    estimated_resale_price: float | None = None,
    max_quantity: int | None = None,
    priority: int = 0,
    status: str = "active",
) -> Product:
    product = Product(
        name=name,
        brand=brand,
        category=category,
        ean=ean,
        gtin=gtin,
        mpn=mpn,
        keywords=keywords,
        image_url=image_url,
        target_price=target_price,
        max_price=max_price,
        estimated_resale_price=estimated_resale_price,
        max_quantity=max_quantity,
        priority=priority,
        status=status,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def get_product(session: Session, product_id: int) -> Product | None:
    return session.get(Product, product_id)


def list_products(session: Session, *, status: str | None = None) -> list[Product]:
    stmt = select(Product)
    if status is not None:
        stmt = stmt.where(Product.status == status)
    return list(session.scalars(stmt))


def update_product(session: Session, product_id: int, **fields: object) -> Product | None:
    product = session.get(Product, product_id)
    if product is None:
        return None
    for key, value in fields.items():
        if not hasattr(product, key):
            raise AttributeError(f"Product has no field {key!r}")
        setattr(product, key, value)
    session.commit()
    session.refresh(product)
    return product


def create_listing(
    session: Session,
    *,
    product_id: int,
    merchant_id: int,
    url: str,
    external_id: str | None = None,
) -> Listing:
    listing = Listing(
        product_id=product_id,
        merchant_id=merchant_id,
        url=url,
        external_id=external_id,
    )
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def get_listing(session: Session, listing_id: int) -> Listing | None:
    return session.get(Listing, listing_id)


def list_listings_for_product(session: Session, product_id: int) -> list[Listing]:
    stmt = select(Listing).where(Listing.product_id == product_id)
    return list(session.scalars(stmt))
