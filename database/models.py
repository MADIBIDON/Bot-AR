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
    # Phase 36 (Cultura drop-window discovery): reuses the exact same
    # scheduled_release_at + dynamic-interval mechanism WatchRule already
    # has (Phase 31, engine/release_awareness.py) — a Product with no
    # known listing yet can now also ramp its DISCOVERY cadence up near a
    # release, not just a WatchRule's monitoring cadence. None (the
    # default) leaves discovery_interval as the sole, unchanged cadence —
    # see app/discovery.py::is_discovery_due().
    scheduled_release_at: Mapped[datetime | None] = mapped_column(default=None)

    # Phase 25 — profitability-based purchase decisions, mirrored from
    # WatchRule (Phase 15/16) so a Product Watch's many auto-discovered
    # WatchRules can share one set of resale/fee assumptions the same way
    # they already share max_price/target_price. See engine/decision.py's
    # effective_*() fallback functions.
    platform_fee_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=None)
    fixed_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    other_costs: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    resale_price_mode: Mapped[str] = mapped_column(default="manual", nullable=False)
    market_source: Mapped[str | None] = mapped_column(default=None)

    # target_price above doubles as "target buy price" (a particularly
    # good price, no longer a hard notification gate once these are set —
    # see engine.decision.evaluate()) and max_price doubles as an
    # optional hard ceiling ("hard_max_total"): leave it unset to let
    # profitability alone decide. minimum_resale_confidence is one of
    # market_data.estimator.Confidence's values ("low"/"medium"/"high");
    # estimated_resale_trusted opts a manually-entered
    # estimated_resale_price into counting as sufficiently confident for
    # auto-buy (mirrors "sold data" already doing that for market mode).
    minimum_net_profit: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    minimum_roi_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), default=None)
    minimum_resale_confidence: Mapped[str | None] = mapped_column(default=None)
    estimated_resale_trusted: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Phase 33 section 21: when estimated_resale_price was last actually
    # set — see database/crud.py::update_product's auto-stamping and
    # purchase/engine.py's STALE_MARKET_DATA gate. None for market mode
    # (freshness there is a `market_data` concern, not this field's) or
    # for a Product that has never had a manual resale price configured.
    resale_updated_at: Mapped[datetime | None] = mapped_column(default=None)

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
        CheckConstraint(
            "minimum_resale_confidence IS NULL OR minimum_resale_confidence IN "
            "('low', 'medium', 'high')",
            name="ck_watch_rule_minimum_resale_confidence_valid",
        ),
        CheckConstraint(
            "minimum_net_profit IS NULL OR minimum_net_profit >= 0",
            name="ck_watch_rule_minimum_net_profit_non_negative",
        ),
        CheckConstraint(
            "minimum_roi_pct IS NULL OR minimum_roi_pct >= 0",
            name="ck_watch_rule_minimum_roi_pct_non_negative",
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

    # Phase 25 — profitability-based purchase decisions. See the matching
    # fields on Product for the full explanation; effective_*() in
    # engine/decision.py falls back to the Product's value when a rule
    # leaves these unset, same pattern as max_price/target_price.
    minimum_net_profit: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    minimum_roi_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), default=None)
    minimum_resale_confidence: Mapped[str | None] = mapped_column(default=None)
    estimated_resale_trusted: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Phase 33 section 21 — see Product.resale_updated_at's own comment.
    resale_updated_at: Mapped[datetime | None] = mapped_column(default=None)

    # Phase 31 — Opportunity Intelligence. last_alert_tier remembers the
    # most recent engine.alerting.AlertTier this rule was notified (or
    # explicitly not notified) at, so app/opportunity_alerts.py can detect
    # a genuine upward crossing (e.g. WATCH -> HIGH) instead of re-alerting
    # on every tick the score merely stays HIGH. scheduled_release_at is
    # set only for a known scheduled-release product (e.g. the Nike SNKRS
    # test case) — engine/worker.py's is_due() consults it, via
    # engine/release_awareness.py, to check more frequently as a real,
    # Nike-published release time approaches; both stay NULL (no behavior
    # change at all) for every other WatchRule.
    last_alert_tier: Mapped[str | None] = mapped_column(default=None)
    scheduled_release_at: Mapped[datetime | None] = mapped_column(default=None)

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
    is reprocessed. Whether/how this event was ever delivered to Discord is
    tracked separately, in NotificationDelivery (Phase 27) — this table
    only ever records that the business event itself happened.
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


class NotificationDelivery(Base):
    """Phase 27: durable at-least-once delivery tracking for one
    (EventRecord, provider) pair. EventRecord only records that a business
    event happened; this tracks whether anyone was ever actually told
    about it, so a crash between "event persisted" and "Discord sent" is
    recoverable without either losing the alert or sending it twice.
    app/delivery.py is the only writer.

    payload_json is the fully-rendered notification (a discord.Embed's
    to_dict(), JSON-encoded) captured once, at the moment the alert is
    first prepared — every attempt, including a retry after a restart,
    resends this exact frozen payload rather than recomputing it (which
    would re-run resale/opportunity calculations against numbers that may
    have since drifted, and could re-hit a market data source). Contains
    only this project's own formatted business data — product name,
    price, merchant, ROI — never a token, credential, or other secret.

    UNIQUE(event_id, provider) is the idempotency anchor: creating a
    delivery row for an event that already has one (from an earlier,
    possibly-crashed attempt) always returns the existing row instead of
    a second one — the same get-or-create-with-IntegrityError-retry
    pattern already used for Merchant/Listing (see app/discovery.py).

    status transitions: pending -> sending -> sent, or pending/sending ->
    failed_retryable (until attempts run out) -> failed_permanent.
    Nothing ever moves backward out of 'sent'.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "provider", name="uq_notification_delivery_event_provider"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed_retryable', 'failed_permanent')",
            name="ck_notification_delivery_status_valid",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_notification_delivery_attempt_count_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event_records.id"), nullable=False)
    provider: Mapped[str] = mapped_column(nullable=False, default="discord")
    status: Mapped[str] = mapped_column(nullable=False, default="pending")
    payload_json: Mapped[str] = mapped_column(nullable=False)

    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error_type: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return (
            f"NotificationDelivery(id={self.id!r}, event_id={self.event_id!r}, "
            f"provider={self.provider!r}, status={self.status!r})"
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

    Phase 33 audit finding (real, confirmed gap — fixed here): the guard
    above is scoped to one *listing*, not one *product*. The same product
    is routinely watched on several Listings across different merchants
    (this project's whole multi-retailer point) — two of them detecting
    stock 20ms apart each pass their own listing-scoped check cleanly and
    would both be free to buy, a genuine double-purchase risk the spec
    explicitly calls out. product_id (denormalized from watch_rule.
    product_id at insert time — see purchase/engine.py) plus
    uq_one_active_or_purchased_attempt_per_product below closes that at
    the database level: at most one row per product_id may ever be in an
    active or purchased state at once, across every listing. This is a
    real backstop (SQLite enforces it, not just Python reasoning) on top
    of the same "no await between check and insert" argument, now applied
    product-wide (see purchase/engine.py's product-level count check).
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
        Index(
            "uq_one_active_or_purchased_attempt_per_product",
            "product_id",
            unique=True,
            sqlite_where=text(
                "status IN ('created', 'validating', 'checkout_started', 'purchased')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_rule_id: Mapped[int] = mapped_column(ForeignKey("watch_rules.id"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)

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


class RetailStore(Base):
    """One physical store belonging to a retailer — Phase 29.

    `retailer` is a plain string matching MerchantDefinition.name
    (connectors/defaults.py), not a foreign key to Merchant: a
    RetailStore can exist for a retailer discovered here before it has
    any online Listing/Merchant row at all (store discovery and online
    monitoring are independent). UNIQUE(retailer, external_store_id) is
    the idempotency anchor for store_discovery's upsert — running
    discovery twice a day never duplicates a store, and a store the
    retailer removes from its own directory simply stops being
    refreshed rather than being force-deleted (its history stays valid).
    """

    __tablename__ = "retail_stores"
    __table_args__ = (
        UniqueConstraint(
            "retailer", "external_store_id", name="uq_retail_store_retailer_external_id"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    retailer: Mapped[str] = mapped_column(nullable=False)
    external_store_id: Mapped[str] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    city: Mapped[str | None] = mapped_column(default=None)
    postal_code: Mapped[str | None] = mapped_column(default=None)
    address: Mapped[str | None] = mapped_column(default=None)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), default=None)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), default=None)
    url: Mapped[str | None] = mapped_column(default=None)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)

    last_discovered_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return (
            f"RetailStore(id={self.id!r}, retailer={self.retailer!r}, "
            f"external_store_id={self.external_store_id!r}, city={self.city!r})"
        )


class LocalStockState(Base):
    """Current in-store availability for one (store, listing) pair —
    Phase 29. Deliberately a single upserted row, not an append-only
    observation log like ObservationRecord: local stock isn't part of
    the mature, audited online pipeline and doesn't need full history —
    the previous value this row held (read right before the upsert) IS
    the "previous observation" a transition is computed from, exactly
    once, in local_stock/monitor.py. UNIQUE(store_id, listing_id) makes
    "upsert" well-defined.

    stock_state is one of the values in database/models.py's
    LOCAL_STOCK_STATES tuple; never fabricated — see
    local_stock/monitor.py's mapping from RbsStoreStock."""

    __tablename__ = "local_stock_states"
    __table_args__ = (
        UniqueConstraint("store_id", "listing_id", name="uq_local_stock_state_store_listing"),
        CheckConstraint(
            "stock_state IN ('unknown', 'out_of_stock', 'in_stock', 'low_stock', "
            "'click_and_collect', 'reservation_available', 'store_only')",
            name="ck_local_stock_state_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("retail_stores.id"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)
    stock_state: Mapped[str] = mapped_column(nullable=False, default="unknown")
    click_and_collect: Mapped[bool] = mapped_column(nullable=False, default=False)
    observed_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return (
            f"LocalStockState(store_id={self.store_id!r}, listing_id={self.listing_id!r}, "
            f"stock_state={self.stock_state!r})"
        )


class LocalStockEvent(Base):
    """One detected local-stock transition worth alerting on — Phase 29.
    Parallel to EventRecord, not a subtype of it: a local event needs
    store_id, which EventRecord's schema has no room for. Deduplicated
    on (store_id, listing_id, event_type, current_state) so reprocessing
    the same transition can never create a duplicate row — mirrors
    EventRecord's own dedup contract."""

    __tablename__ = "local_stock_events"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "listing_id",
            "event_type",
            "current_state",
            name="uq_local_stock_event_store_listing_type_state",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("retail_stores.id"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    previous_state: Mapped[str | None] = mapped_column(default=None)
    current_state: Mapped[str] = mapped_column(nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"LocalStockEvent(id={self.id!r}, store_id={self.store_id!r}, "
            f"listing_id={self.listing_id!r}, event_type={self.event_type!r})"
        )


class LocalNotificationDelivery(Base):
    """Durable delivery tracking for one LocalStockEvent — Phase 29.

    A parallel table to NotificationDelivery (app/delivery.py, Phase 27)
    rather than a shared/polymorphic one: NotificationDelivery.event_id
    has a real foreign key into event_records, and local_stock_events is
    a different table with an independent id sequence — relaxing that FK
    to make one column point at either table would trade away real
    referential integrity for a small amount of code reuse. The state
    machine and semantics are identical; see local_stock/delivery.py,
    which reuses app/delivery.py's pure classification/backoff logic
    directly (that part has no EventRecord coupling at all)."""

    __tablename__ = "local_notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "local_stock_event_id", "provider", name="uq_local_notification_delivery_event_provider"
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed_retryable', 'failed_permanent')",
            name="ck_local_notification_delivery_status_valid",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_local_notification_delivery_attempt_count_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    local_stock_event_id: Mapped[int] = mapped_column(
        ForeignKey("local_stock_events.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(nullable=False, default="discord")
    status: Mapped[str] = mapped_column(nullable=False, default="pending")
    payload_json: Mapped[str] = mapped_column(nullable=False)

    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error_type: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return (
            f"LocalNotificationDelivery(id={self.id!r}, "
            f"local_stock_event_id={self.local_stock_event_id!r}, status={self.status!r})"
        )


LOCAL_STOCK_STATES = (
    "unknown",
    "out_of_stock",
    "in_stock",
    "low_stock",
    "click_and_collect",
    "reservation_available",
    "store_only",
)
