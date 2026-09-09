"""SQLAlchemy models: Merchant, Product, Listing.

A Product is not a URL: it can be tracked as a Listing on several Merchants.
Price/stock observations are not modeled here — that is monitoring data,
added in a later phase.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint, func
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
    sku: Mapped[str | None] = mapped_column(default=None)
    asin: Mapped[str | None] = mapped_column(default=None)

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

    def __repr__(self) -> str:
        return f"Product(id={self.id!r}, name={self.name!r}, ean={self.ean!r})"


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("merchant_id", "url", name="uq_listing_merchant_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)

    external_id: Mapped[str | None] = mapped_column(default=None)
    url: Mapped[str] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    product: Mapped[Product] = relationship(back_populates="listings")
    merchant: Mapped[Merchant] = relationship(back_populates="listings")

    def __repr__(self) -> str:
        return (
            f"Listing(id={self.id!r}, product_id={self.product_id!r}, "
            f"merchant_id={self.merchant_id!r})"
        )
