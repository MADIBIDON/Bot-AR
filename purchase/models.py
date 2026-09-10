"""Normalized purchase-side data model — analogous to
products/observation.py for the monitoring side. PurchaseIntent is the
one object that crosses from the monitoring fast path into the purchase
path (see purchase/engine.py's module docstring for why that boundary
exists at all).

Never invent a field, never carry payment data: nothing here is or ever
becomes a card number, CVV, password, session cookie, or payment token —
see purchase/base.py for the same guarantee on the connector side.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class PurchaseStatus(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    CHECKOUT_STARTED = "checkout_started"
    PURCHASED = "purchased"
    FAILED = "failed"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    AUTOMATED_CHECKOUT_UNSUPPORTED = "automated_checkout_unsupported"
    CANCELLED = "cancelled"


# A PurchaseAttempt row in this state still represents a live/in-flight
# attempt — the idempotency guard (purchase/engine.py) refuses to start a
# second one for the same listing while one of these is outstanding.
ACTIVE_STATUSES = (
    PurchaseStatus.CREATED,
    PurchaseStatus.VALIDATING,
    PurchaseStatus.CHECKOUT_STARTED,
)


@dataclass(frozen=True, slots=True)
class PurchaseIntent:
    """A candidate purchase, built from state the monitoring fast path
    already fetched — no additional network call to construct one.
    max_price_allowed is a TOTAL-cost ceiling (product + known mandatory
    fees such as shipping), not a per-item price — see purchase/engine.py
    for the explicit reasoning."""

    watch_rule_id: int
    product_id: int
    listing_id: int
    merchant: str
    product_name: str
    url: str
    observed_price: Decimal
    max_price_allowed: Decimal
    quantity: int
    match_confidence: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RevalidationResult:
    """What a PurchaseConnector.revalidate() reports right before
    checkout — deliberately richer than products.observation.
    ProductObservation (which the monitoring fast path uses): shipping
    cost and exact available quantity are usually only knowable once an
    item is actually in a cart, which is what revalidate() is expected to
    do. quantity_available=None means "unknown, assume enough for the
    requested quantity if available=True" — never invented as a number."""

    available: bool
    price: Decimal
    shipping_cost: Decimal | None
    quantity_available: int | None


@dataclass(frozen=True, slots=True)
class PurchaseDecision:
    """The pure gate's verdict — never touches the network or a
    connector. `proceed=True` means every safety check passed and the
    caller may go on to revalidate + checkout; it never means a purchase
    has happened."""

    proceed: bool
    status: PurchaseStatus
    reason: str
    intent: PurchaseIntent
