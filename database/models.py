"""SQLAlchemy models: Merchant, Product, Listing.

A Product is not a URL: it can be tracked as a Listing on several Merchants.
Price/stock observations are not modeled here — that is monitoring data,
added in a later phase.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    website_url: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    listings: Mapped[list[Listing]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Merchant(id={self.id!r}, name={self.name!r})"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    brand: Mapped[str | None] = mapped_column(default=None)
    category: Mapped[str | None] = mapped_column(default=None)

    ean: Mapped[str | None] = mapped_column(unique=True, default=None)
    gtin: Mapped[str | None] = mapped_column(default=None)
    mpn: Mapped[str | None] = mapped_column(default=None)

    keywords: Mapped[str | None] = mapped_column(default=None)
    image_url: Mapped[str | None] = mapped_column(default=None)

    target_price: Mapped[float | None] = mapped_column(default=None)
    max_price: Mapped[float | None] = mapped_column(default=None)
    estimated_resale_price: Mapped[float | None] = mapped_column(default=None)
    max_quantity: Mapped[int | None] = mapped_column(default=None)

    priority: Mapped[int] = mapped_column(default=0, nullable=False)
    status: Mapped[str] = mapped_column(default="active", nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    listings: Mapped[list[Listing]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    watch_rules: Mapped[list[WatchRule]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Product(id={self.id!r}, name={self.name!r}, ean={self.ean!r})"


class Listing(Base):
    """A merchant's page for a product.

    Identity depends on what the merchant exposes:
    - if `external_id` is known, it is the stable identity and `url` may be
      updated in place (site redesigns, slug changes) without creating a
      duplicate row;
    - if `external_id` is absent, `url` is the only available identity.
    """

    __tablename__ = "listings"
    __table_args__ = (
        Index(
            "uq_listing_merchant_external_id",
            "merchant_id",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(
            "uq_listing_merchant_url_no_external_id",
            "merchant_id",
            "url",
            unique=True,
            sqlite_where=text("external_id IS NULL"),
            postgresql_where=text("external_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)

    external_id: Mapped[str | None] = mapped_column(default=None)
    url: Mapped[str] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    product: Mapped[Product] = relationship(back_populates="listings")
    merchant: Mapped[Merchant] = relationship(back_populates="listings")
    watch_rules: Mapped[list[WatchRule]] = relationship(back_populates="listing")

    def __repr__(self) -> str:
        return (
            f"Listing(id={self.id!r}, product_id={self.product_id!r}, "
            f"merchant_id={self.merchant_id!r})"
        )


class WatchRule(Base):
    """A monitoring rule: which product (optionally scoped to one listing) to
    watch, at what price targets, how often, and whether it is active.

    No scraping, Discord, or purchase logic here — the monitoring engine
    reads these rules in a later phase; this model only stores and validates
    them. Cross-field validation (listing must belong to the same product)
    cannot be expressed as a column constraint and is enforced in
    database/crud.py instead.
    """

    __tablename__ = "watch_rules"
    __table_args__ = (
        CheckConstraint(
            "target_price IS NULL OR target_price > 0",
            name="ck_watch_rule_target_price_positive",
        ),
        CheckConstraint(
            "max_price IS NULL OR max_price > 0",
            name="ck_watch_rule_max_price_positive",
        ),
        CheckConstraint("check_interval > 0", name="ck_watch_rule_check_interval_positive"),
        CheckConstraint("max_quantity > 0", name="ck_watch_rule_max_quantity_positive"),
        CheckConstraint("priority >= 0 AND priority <= 10", name="ck_watch_rule_priority_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), default=None)

    target_price: Mapped[float | None] = mapped_column(default=None)
    max_price: Mapped[float | None] = mapped_column(default=None)

    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    check_interval: Mapped[int] = mapped_column(nullable=False)
    max_quantity: Mapped[int] = mapped_column(nullable=False)
    priority: Mapped[int] = mapped_column(default=5, nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    product: Mapped[Product] = relationship(back_populates="watch_rules")
    listing: Mapped[Listing | None] = relationship(back_populates="watch_rules")

    def __repr__(self) -> str:
        return (
            f"WatchRule(id={self.id!r}, product_id={self.product_id!r}, "
            f"listing_id={self.listing_id!r}, enabled={self.enabled!r})"
        )
