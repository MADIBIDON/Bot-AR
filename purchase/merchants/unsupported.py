"""Fallback PurchaseConnector for a merchant with no clean, reliable,
anti-bot-free checkout automation available — see purchase/defaults.py
for why every merchant wired up today (Kairyu, RelicTCG, Fuji Store)
uses this. Never attempts a scripted checkout, never touches payment
data: both methods raise immediately, before any network call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from purchase.base import AutomatedCheckoutUnsupportedError, PurchaseConnector

if TYPE_CHECKING:
    from purchase.base import CheckoutResult
    from purchase.models import PurchaseIntent, RevalidationResult


class UnsupportedPurchaseConnector(PurchaseConnector):
    def __init__(self, *, merchant_name: str) -> None:
        self._merchant_name = merchant_name

    def _unsupported(self) -> AutomatedCheckoutUnsupportedError:
        return AutomatedCheckoutUnsupportedError(
            f"{self._merchant_name} has no automated checkout path — buy manually via the "
            "product URL."
        )

    def revalidate(self, intent: PurchaseIntent) -> RevalidationResult:
        raise self._unsupported()

    def checkout(self, intent: PurchaseIntent, revalidated: RevalidationResult) -> CheckoutResult:
        raise self._unsupported()
