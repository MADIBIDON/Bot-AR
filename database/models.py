"""SQLAlchemy models: Merchant, Product, Listing.

A Product is not a URL: it can be tracked as a Listing on several Merchants.
Price/stock observations are not modeled here — that is monitoring data,
added in a later phase.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func, text
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

    target_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    estimated_resale_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    max_quantity: Mapped[int | None] = mapped_column(default=None)

    priority: Mapped[int] = mapped_column(default=0, nullable=False)
    status: Mapped[str] = mapped_column(default="active", nullable=False)

    # Phase 22 — universal product monitoring. target_price/max_price/
    # max_quantity above were unused until now (every real rule set its
    # own value on WatchRule instead); they are now the single
    # user-facing ceiling a `product watch` shares across every
    # auto-discovered Listing/WatchRule. See engine/decision.py's
    # effective_max_price()/effective_target_price() for the fallback
    # rule that keeps the 3 pre-Phase-22 WatchRules (which already set
    # their own max_price/target_price) working unchanged.
    discovery_interval: Mapped[int] = mapped_column(default=1800, nullable=False)
    last_discovery_at: Mapped[datetime | None] = mapped_column(default=None)

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
        CheckConstraint(
            "estimated_resale_price IS NULL OR estimated_resale_price > 0",
            name="ck_watch_rule_estimated_resale_price_positive",
        ),
        CheckConstraint(
            "platform_fee_pct IS NULL OR (platform_fee_pct >= 0 AND platform_fee_pct < 100)",
            name="ck_watch_rule_platform_fee_pct_range",
        ),
        CheckConstraint(
            "fixed_fee IS NULL OR fixed_fee >= 0", name="ck_watch_rule_fixed_fee_non_negative"
        ),
        CheckConstraint(
            "shipping_cost IS NULL OR shipping_cost >= 0",
            name="ck_watch_rule_shipping_cost_non_negative",
        ),
        CheckConstraint(
            "other_costs IS NULL OR other_costs >= 0",
            name="ck_watch_rule_other_costs_non_negative",
        ),
        CheckConstraint(
            "resale_price_mode IN ('manual', 'market')",
            name="ck_watch_rule_resale_price_mode_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), default=None)

    target_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)

    # Opportunity Engine config (Phase 15) — manually supplied, no live
    # marketplace lookup exists yet. All optional: no resale estimate
    # means engine.opportunity.evaluate_opportunity() has nothing to do.
    estimated_resale_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    platform_fee_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=None)
    fixed_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    other_costs: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)

    # Phase 16 — how to source the resale price for the Opportunity Engine.
    # "manual" (default) preserves Phase 15 behavior exactly: use
    # estimated_resale_price as-is. "market" fetches live market_data/
    # observations for market_source instead; estimated_resale_price is
    # then ignored (kept as an optional fallback value only).
    resale_price_mode: Mapped[str] = mapped_column(default="manual", nullable=False)
    market_source: Mapped[str | None] = mapped_column(default=None)

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


class ObservationRecord(Base):
    """Historical, append-only record of one ProductObservation.

    Deliberately separate from the business `ProductObservation` dataclass
    (products/observation.py) — this is the persistence representation,
    written once per successful monitoring check. Change detection between
    successive records is a later phase; this model only stores them.
    """

    __tablename__ = "observation_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)

    external_id: Mapped[str] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(nullable=False)
    available: Mapped[bool] = mapped_column(nullable=False)
    seller: Mapped[str | None] = mapped_column(default=None)
    ean: Mapped[str | None] = mapped_column(default=None)
    mpn: Mapped[str | None] = mapped_column(default=None)

    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"ObservationRecord(id={self.id!r}, listing_id={self.listing_id!r}, "
            f"observed_at={self.observed_at!r})"
        )


class EventRecord(Base):
    """One persisted MonitoringEvent (engine/change_detection.py).

    Deduplicated on (event_type, watch_rule_id, observation_record_id): the
    same observation can never produce the same event twice, even if a check
    is reprocessed. No `processed`/dispatch-tracking field yet — that
    belongs to whichever later phase adds Discord delivery.
    """

    __tablename__ = "event_records"
    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "watch_rule_id",
            "observation_record_id",
            name="uq_event_type_watch_rule_observation",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)
    watch_rule_id: Mapped[int] = mapped_column(ForeignKey("watch_rules.id"), nullable=False)
    observation_record_id: Mapped[int] = mapped_column(
        ForeignKey("observation_records.id"), nullable=False
    )

    previous_value: Mapped[str | None] = mapped_column(default=None)
    current_value: Mapped[str | None] = mapped_column(default=None)

    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"EventRecord(id={self.id!r}, event_type={self.event_type!r}, "
            f"watch_rule_id={self.watch_rule_id!r})"
        )


class PurchaseAttempt(Base):
    """One attempted automated purchase. purchase/engine.py is the only
    writer; this table is pure history/audit, never read by the
    monitoring fast path.

    Dry runs (scripts/watch.py purchase-test) never create a row here —
    only a real attempt (PURCHASES_ENABLED=true, past every safety gate)
    does, so this table's rows always mean "the engine actually tried
    something", not "someone ran a test". order_reference is a merchant
    order id/number only — never a payment token, card detail, or session
    cookie; see purchase/base.py for that guarantee end to end.

    Idempotency (Phase 19 spec): the purchase engine only ever creates a
    new row for a listing after checking there is no existing row for
    that listing_id whose status is still in _ACTIVE_PURCHASE_STATUSES,
    and does so without any `await` between that check and this insert —
    safe because app/pidfile.py already guarantees a single worker
    process, so nothing else can interleave on the same event loop
    between the check and the write.
    """

    __tablename__ = "purchase_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'validating', 'checkout_started', 'purchased', "
            "'failed', 'human_action_required', 'automated_checkout_unsupported', "
            "'cancelled')",
            name="ck_purchase_attempt_status_valid",
        ),
        CheckConstraint("quantity > 0", name="ck_purchase_attempt_quantity_positive"),
        CheckConstraint("observed_price > 0", name="ck_purchase_attempt_observed_price_positive"),
        CheckConstraint(
            "max_price_allowed > 0", name="ck_purchase_attempt_max_price_allowed_positive"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_rule_id: Mapped[int] = mapped_column(ForeignKey("watch_rules.id"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)

    status: Mapped[str] = mapped_column(nullable=False)

    observed_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    max_price_allowed: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False)

    final_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)

    order_reference: Mapped[str | None] = mapped_column(default=None)
    failure_reason: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    watch_rule: Mapped[WatchRule] = relationship()
    listing: Mapped[Listing] = relationship()

    def __repr__(self) -> str:
        return (
            f"PurchaseAttempt(id={self.id!r}, listing_id={self.listing_id!r}, "
            f"status={self.status!r})"
        )
