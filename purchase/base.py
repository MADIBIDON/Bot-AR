"""Purchase connector abstraction — deliberately separate from
connectors/base.py's BaseConnector (read-only price/stock) per the
explicit split requested for this phase: RetailConnector reads,
PurchaseConnector buys. A merchant can have one without the other (every
merchant wired up today has a RetailConnector; none has a real
PurchaseConnector yet — see purchase/merchants/unsupported.py).

Hard rule for every implementation of this interface, no exceptions:
never enter or store a card number, CVV, password, payment token, or
session cookie — not in code, not in the database, not in a log line.
Never solve or click through a CAPTCHA, Cloudflare challenge, queue/
waiting room, or other anti-bot mechanism; never attempt 3-D Secure or
any other step that requires a human. Any of those must raise
HumanActionRequiredError (or AutomatedCheckoutUnsupportedError if the
merchant simply has no clean automatable path at all) — never be worked
around.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from purchase.models import PurchaseIntent, RevalidationResult


class PurchaseError(Exception):
    """Base class for every purchase-path failure. Never includes a
    secret, payment detail, or credential in its message."""


class AutomatedCheckoutUnsupportedError(PurchaseError):
    """No clean, reliable, anti-bot-free checkout automation exists for
    this merchant. Not a bug and not retried — the caller falls back to
    a Discord alert with the manual purchase link."""


class HumanActionRequiredError(PurchaseError):
    """Checkout hit a CAPTCHA, Cloudflare/anti-bot challenge, a queue or
    waiting room, 3-D Secure, or any other step only a human can complete.
    Always aborts the attempt immediately — never bypassed, never
    retried automatically."""


class StaleListingError(PurchaseError):
    """Raised by revalidate() (or checkout()) when the product observed
    right before checkout no longer matches what triggered the
    PurchaseIntent closely enough to trust — see purchase/engine.py's
    pre-checkout revalidation step."""


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """What actually happened, in non-sensitive terms only.
    order_reference is a merchant order id/number — never a payment
    token or anything that could be replayed."""

    success: bool
    order_reference: str | None
    final_price: Decimal | None
    shipping_cost: Decimal | None
    total_cost: Decimal | None
    failure_reason: str | None


class PurchaseConnector(ABC):
    @abstractmethod
    def revalidate(self, intent: PurchaseIntent) -> RevalidationResult:
        """Re-fetches the product (and, ideally, puts it in a cart to
        learn the real shipping cost) right before committing to
        checkout — the PurchaseIntent's own observed_price/quantity/
        availability may already be stale by the time checkout would
        start. Must raise AutomatedCheckoutUnsupportedError if this
        merchant has no way to do this without scripting a protected
        page."""
        raise NotImplementedError

    @abstractmethod
    def checkout(self, intent: PurchaseIntent, revalidated: RevalidationResult) -> CheckoutResult:
        """Attempts the actual purchase using the just-revalidated
        state. Must raise HumanActionRequiredError the moment any
        anti-bot/CAPTCHA/3DS/queue step appears, and
        AutomatedCheckoutUnsupportedError if no clean automated path
        exists at all. Never called by purchase/engine.py in dry-run
        mode."""
        raise NotImplementedError
