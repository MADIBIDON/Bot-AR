"""Minimal CRUD operations for Merchant, Product, Listing.

Every function takes an explicit Session — no global/implicit session state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import (
    EventRecord,
    Listing,
    Merchant,
    ObservationRecord,
    Product,
    PurchaseAttempt,
    WatchRule,
)

if TYPE_CHECKING:
    from products.observation import ProductObservation


def create_merchant(session: Session, name: str, website_url: str | None = None) -> Merchant:
    merchant = Merchant(name=name, website_url=website_url)
    session.add(merchant)
    session.commit()
    session.refresh(merchant)
    return merchant


def get_merchant(session: Session, merchant_id: int) -> Merchant | None:
    return session.get(Merchant, merchant_id)


def get_merchant_by_name(session: Session, name: str) -> Merchant | None:
    stmt = select(Merchant).where(Merchant.name == name)
    return session.scalars(stmt).first()


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
    target_price: Decimal | None = None,
    max_price: Decimal | None = None,
    estimated_resale_price: Decimal | None = None,
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


def get_product_by_ean(session: Session, ean: str) -> Product | None:
    return session.scalars(select(Product).where(Product.ean == ean)).first()


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


def get_listing_by_merchant_and_external_id(
    session: Session, merchant_id: int, external_id: str
) -> Listing | None:
    stmt = select(Listing).where(
        Listing.merchant_id == merchant_id, Listing.external_id == external_id
    )
    return session.scalars(stmt).first()


def _validate_listing_belongs_to_product(
    session: Session, product_id: int, listing_id: int | None
) -> None:
    if listing_id is None:
        return
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise ValueError(f"Listing {listing_id} does not exist")
    if listing.product_id != product_id:
        raise ValueError(
            f"Listing {listing_id} belongs to product {listing.product_id}, "
            f"not product {product_id}"
        )


def create_watch_rule(
    session: Session,
    *,
    product_id: int,
    check_interval: int,
    max_quantity: int,
    listing_id: int | None = None,
    target_price: Decimal | None = None,
    max_price: Decimal | None = None,
    priority: int = 5,
    enabled: bool = True,
) -> WatchRule:
    _validate_listing_belongs_to_product(session, product_id, listing_id)
    rule = WatchRule(
        product_id=product_id,
        listing_id=listing_id,
        target_price=target_price,
        max_price=max_price,
        check_interval=check_interval,
        max_quantity=max_quantity,
        priority=priority,
        enabled=enabled,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def get_watch_rule(session: Session, watch_rule_id: int) -> WatchRule | None:
    return session.get(WatchRule, watch_rule_id)


def get_watch_rule_by_listing_id(session: Session, listing_id: int) -> WatchRule | None:
    stmt = select(WatchRule).where(WatchRule.listing_id == listing_id)
    return session.scalars(stmt).first()


def list_watch_rules(
    session: Session,
    *,
    enabled: bool | None = None,
    product_id: int | None = None,
) -> list[WatchRule]:
    stmt = select(WatchRule)
    if enabled is not None:
        stmt = stmt.where(WatchRule.enabled == enabled)
    if product_id is not None:
        stmt = stmt.where(WatchRule.product_id == product_id)
    return list(session.scalars(stmt))


def update_watch_rule(session: Session, watch_rule_id: int, **fields: object) -> WatchRule | None:
    rule = session.get(WatchRule, watch_rule_id)
    if rule is None:
        return None
    if "product_id" in fields or "listing_id" in fields:
        new_product_id = fields.get("product_id", rule.product_id)
        new_listing_id = fields.get("listing_id", rule.listing_id)
        _validate_listing_belongs_to_product(session, new_product_id, new_listing_id)
    for key, value in fields.items():
        if not hasattr(rule, key):
            raise AttributeError(f"WatchRule has no field {key!r}")
        setattr(rule, key, value)
    session.commit()
    session.refresh(rule)
    return rule


def enable_watch_rule(session: Session, watch_rule_id: int) -> WatchRule | None:
    return update_watch_rule(session, watch_rule_id, enabled=True)


def disable_watch_rule(session: Session, watch_rule_id: int) -> WatchRule | None:
    return update_watch_rule(session, watch_rule_id, enabled=False)


def delete_watch_rule(session: Session, watch_rule_id: int) -> bool:
    """Deletes the rule itself. Raises IntegrityError (uncaught, on the
    caller to handle) if EventRecords still reference it — disable instead
    of deleting to preserve that audit history."""
    rule = session.get(WatchRule, watch_rule_id)
    if rule is None:
        return False
    session.delete(rule)
    session.commit()
    return True


def create_observation_record(
    session: Session, *, listing_id: int, observation: ProductObservation
) -> ObservationRecord:
    record = ObservationRecord(
        listing_id=listing_id,
        external_id=observation.external_id,
        name=observation.name,
        price=observation.price,
        currency=observation.currency,
        available=observation.available,
        seller=observation.seller,
        ean=observation.ean,
        mpn=observation.mpn,
        observed_at=observation.observed_at,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def list_observation_records_for_listing(
    session: Session, listing_id: int
) -> list[ObservationRecord]:
    stmt = (
        select(ObservationRecord)
        .where(ObservationRecord.listing_id == listing_id)
        .order_by(ObservationRecord.observed_at)
    )
    return list(session.scalars(stmt))


def create_event_record(
    session: Session,
    *,
    event_type: str,
    listing_id: int,
    watch_rule_id: int,
    observation_record_id: int,
    occurred_at: datetime,
    previous_value: str | None = None,
    current_value: str | None = None,
) -> EventRecord | None:
    """Persist one event.

    Returns None if an identical event (same event_type, watch_rule_id, and
    observation_record_id) already exists — that is the dedup contract, not
    an unexpected failure: reprocessing the same observation can never
    create a duplicate row.
    """
    record = EventRecord(
        event_type=event_type,
        listing_id=listing_id,
        watch_rule_id=watch_rule_id,
        observation_record_id=observation_record_id,
        previous_value=previous_value,
        current_value=current_value,
        occurred_at=occurred_at,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    session.refresh(record)
    return record


def list_event_records_for_listing(session: Session, listing_id: int) -> list[EventRecord]:
    stmt = (
        select(EventRecord)
        .where(EventRecord.listing_id == listing_id)
        .order_by(EventRecord.occurred_at)
    )
    return list(session.scalars(stmt))


def get_most_recent_observation(session: Session) -> ObservationRecord | None:
    stmt = select(ObservationRecord).order_by(ObservationRecord.observed_at.desc()).limit(1)
    return session.scalars(stmt).first()


def get_most_recent_event(session: Session) -> EventRecord | None:
    stmt = select(EventRecord).order_by(EventRecord.occurred_at.desc()).limit(1)
    return session.scalars(stmt).first()


_ACTIVE_PURCHASE_STATUS_VALUES = ("created", "validating", "checkout_started")


def get_active_purchase_attempt_for_listing(
    session: Session, listing_id: int
) -> PurchaseAttempt | None:
    """The idempotency check: is there already an in-flight attempt for
    this listing? See database/models.py::PurchaseAttempt for why calling
    this immediately before create_purchase_attempt(), with no `await` in
    between, is a sufficient lock in this project's single-worker-process
    architecture."""
    stmt = select(PurchaseAttempt).where(
        PurchaseAttempt.listing_id == listing_id,
        PurchaseAttempt.status.in_(_ACTIVE_PURCHASE_STATUS_VALUES),
    )
    return session.scalars(stmt).first()


def get_most_recent_purchase_attempt_for_listing(
    session: Session, listing_id: int
) -> PurchaseAttempt | None:
    stmt = (
        select(PurchaseAttempt)
        .where(PurchaseAttempt.listing_id == listing_id)
        .order_by(PurchaseAttempt.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def create_purchase_attempt(
    session: Session,
    *,
    watch_rule_id: int,
    listing_id: int,
    status: str,
    observed_price: Decimal,
    max_price_allowed: Decimal,
    quantity: int,
) -> PurchaseAttempt:
    attempt = PurchaseAttempt(
        watch_rule_id=watch_rule_id,
        listing_id=listing_id,
        status=status,
        observed_price=observed_price,
        max_price_allowed=max_price_allowed,
        quantity=quantity,
    )
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    return attempt


def update_purchase_attempt(
    session: Session, purchase_attempt_id: int, **fields: object
) -> PurchaseAttempt | None:
    attempt = session.get(PurchaseAttempt, purchase_attempt_id)
    if attempt is None:
        return None
    for key, value in fields.items():
        if not hasattr(attempt, key):
            raise AttributeError(f"PurchaseAttempt has no field {key!r}")
        setattr(attempt, key, value)
    session.commit()
    session.refresh(attempt)
    return attempt


def list_purchase_attempts(session: Session, *, limit: int | None = None) -> list[PurchaseAttempt]:
    stmt = select(PurchaseAttempt).order_by(PurchaseAttempt.created_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def sum_purchased_total_since(session: Session, since: datetime) -> Decimal:
    """Total actually spent (PURCHASED attempts' total_cost) since
    `since` — used for the daily budget gate. Never counts a
    non-purchased attempt (FAILED/CANCELLED/etc. never spent anything)."""
    stmt = select(func.sum(PurchaseAttempt.total_cost)).where(
        PurchaseAttempt.status == "purchased",
        PurchaseAttempt.created_at >= since,
    )
    total = session.scalar(stmt)
    return total if total is not None else Decimal("0")
