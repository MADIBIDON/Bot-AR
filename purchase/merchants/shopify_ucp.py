"""Generic Shopify UCP PurchaseConnector — one implementation reusable for
every Shopify store that publishes a Universal Commerce Protocol (UCP,
https://ucp.dev) merchant profile: Kairyu and RelicTCG today, any future
Shopify UCP-compatible merchant without new code.

Every request/response shape referenced below was observed live against
Kairyu and/or RelicTCG's real MCP endpoints during Phase 21 — see
ucp/client.py's module docstring for the profile-hosting prerequisite
(our own UCP agent profile, published at PURCHASE_UCP_AGENT_PROFILE_URL,
must declare the capabilities below or every call fails with
"Tool not found", confirmed live). Nothing here is guessed from the spec
alone; the totals `type` enum (subtotal/fulfillment/tax/fee/discount) is
the one exception — taken verbatim from the checkout capability spec's
own documented examples (ucp.dev/2026-08-25/specification/shopping/
checkout/), not yet independently observed with a nonzero shipping/tax
line in this project's own testing (the one live product tested had no
deliverable address for the destination used).

Required capabilities (see ucp/profile.py — already declared):
  dev.ucp.shopping.catalog.search, dev.ucp.shopping.catalog.lookup,
  dev.ucp.shopping.cart, dev.ucp.shopping.checkout,
  dev.ucp.shopping.fulfillment (checkout creation was observed to fail
  with "Tool not found" until this extension was also declared).

What this can do (see revalidate()): resolve the watched product to its
Shopify variant GID via search_catalog, create a checkout directly from
line_items (cart_id alone was rejected live: create_checkout requires
line_items regardless), submit buyer contact + shipping destination via
update_checkout, and read back the real total (subtotal + shipping +
tax) from the checkout resource's `totals` array.

What this will never do (see checkout()): call complete_checkout with a
real payment instrument. UCP's own protocol requires buyer-approved
payment (confirmed live: every checkout we created came back with
status "incomplete"/"requires_escalation" and an
"extension_interaction_required" / "requires_buyer_input" message) —
checkout() always raises HumanActionRequiredError, unconditionally.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING

from config.settings import get_ucp_agent_profile_url
from purchase.base import (
    AutomatedCheckoutUnsupportedError,
    CheckoutResult,
    HumanActionRequiredError,
    PurchaseConnector,
    PurchaseError,
    StaleListingError,
)
from purchase.models import RevalidationResult
from ucp.client import (
    DEFAULT_TIMEOUT_SECONDS,
    UCPError,
    UCPProfileUnreachableError,
    UCPServiceUnavailableError,
    call_tool,
    discover,
)

if TYPE_CHECKING:
    from purchase.models import PurchaseIntent

# Totals line-item types, verbatim from the checkout capability spec's
# own examples (ucp.dev/2026-08-25/specification/shopping/checkout/) —
# not independently confirmed live with nonzero shipping/tax yet.
_SHIPPING_TOTAL_TYPE = "fulfillment"
_TAX_TOTAL_TYPE = "tax"


def _extract_handle(url: str) -> str:
    """Shopify product URLs look like
    https://<shop>/products/<handle> — see connectors/shopify.py."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _minor_to_decimal(amount: int) -> Decimal:
    return Decimal(amount) / 100


class ShopifyUCPPurchaseConnector(PurchaseConnector):
    def __init__(
        self,
        *,
        shop_domain: str,
        agent_profile_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._shop_domain = shop_domain
        self._agent_profile_url = agent_profile_url or get_ucp_agent_profile_url()
        self._timeout = timeout
        self._mcp_endpoint: str | None = None

    def _endpoint(self) -> str:
        if self._mcp_endpoint is None:
            try:
                self._mcp_endpoint = discover(self._shop_domain, timeout=self._timeout).mcp_endpoint
            except UCPServiceUnavailableError as exc:
                raise AutomatedCheckoutUnsupportedError(str(exc)) from exc
        return self._mcp_endpoint

    def _call(self, tool: str, arguments: dict) -> dict:
        if not self._agent_profile_url:
            raise PurchaseError(
                "PURCHASE_UCP_AGENT_PROFILE_URL is not configured — cannot call any UCP tool."
            )
        try:
            return call_tool(
                self._endpoint(),
                tool,
                arguments,
                agent_profile_url=self._agent_profile_url,
                timeout=self._timeout,
            )
        except UCPProfileUnreachableError as exc:
            raise PurchaseError(f"our own UCP agent profile is unreachable: {exc}") from exc
        except UCPError as exc:
            raise PurchaseError(str(exc)) from exc

    def _resolve_variant(self, intent: PurchaseIntent) -> tuple[str, Decimal, bool]:
        handle = _extract_handle(intent.url)
        result = self._call("search_catalog", {"catalog": {"query": intent.product_name}})
        products = result.get("products", [])
        product = next((p for p in products if p.get("handle") == handle), None)
        if product is None:
            raise StaleListingError(
                f"Product handle {handle!r} was not found in a UCP catalog search for "
                f"{intent.product_name!r} — the listing may have changed or been removed."
            )
        variants = product.get("variants", [])
        if not variants:
            raise AutomatedCheckoutUnsupportedError("Product has no purchasable variants.")
        if len(variants) > 1:
            # Conservative, same policy as purchase/merchants/fuji_store.py:
            # nothing here tells us which variant (condition, edition, ...)
            # the watched listing actually tracks — never guess.
            raise AutomatedCheckoutUnsupportedError(
                f"Product {handle!r} has {len(variants)} variants — cannot determine which "
                "one to purchase automatically; buy manually."
            )
        variant = variants[0]
        price = _minor_to_decimal(variant["price"]["amount"])
        available = bool(variant.get("availability", {}).get("available", False))
        return variant["id"], price, available

    def _checkout_totals(self, checkout: dict) -> tuple[Decimal, Decimal | None, Decimal | None]:
        item_total = shipping_total = tax_total = Decimal("0")
        has_shipping = has_tax = False
        for line in checkout.get("totals", []):
            amount = _minor_to_decimal(line.get("amount", 0))
            line_type = line.get("type")
            if line_type == "subtotal":
                item_total = amount
            elif line_type == _SHIPPING_TOTAL_TYPE:
                shipping_total += amount
                has_shipping = True
            elif line_type == _TAX_TOTAL_TYPE:
                tax_total += amount
                has_tax = True
        return (
            item_total,
            (shipping_total if has_shipping else None),
            (tax_total if has_tax else None),
        )

    def revalidate(self, intent: PurchaseIntent) -> RevalidationResult:
        variant_id, price, available = self._resolve_variant(intent)
        if not available:
            return RevalidationResult(
                available=False, price=price, shipping_cost=None, quantity_available=0
            )

        line_items = [{"item": {"id": variant_id}, "quantity": intent.quantity}]
        checkout = self._call("create_checkout", {"checkout": {"line_items": line_items}})

        postal_code = os.environ.get("PURCHASE_SHIPPING_POSTAL_CODE")
        contact_email = os.environ.get("PURCHASE_CONTACT_EMAIL")
        if postal_code and contact_email:
            checkout = self._call(
                "update_checkout",
                {
                    "id": checkout["id"],
                    "checkout": {
                        "line_items": line_items,
                        "buyer": {"email": contact_email},
                        "fulfillment": {
                            "methods": [
                                {
                                    "type": "shipping",
                                    "destinations": [
                                        {
                                            "first_name": os.environ.get(
                                                "PURCHASE_SHIPPING_FIRST_NAME", ""
                                            ),
                                            "last_name": os.environ.get(
                                                "PURCHASE_SHIPPING_LAST_NAME", ""
                                            ),
                                            "street_address": os.environ.get(
                                                "PURCHASE_SHIPPING_ADDRESS", ""
                                            ),
                                            "address_locality": os.environ.get(
                                                "PURCHASE_SHIPPING_CITY", ""
                                            ),
                                            "postal_code": postal_code,
                                            "address_country": os.environ.get(
                                                "PURCHASE_SHIPPING_COUNTRY", "FR"
                                            ),
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                },
            )

        messages = checkout.get("messages", [])
        no_delivery = any(m.get("code") == "delivery_no_delivery_available" for m in messages)
        if no_delivery:
            return RevalidationResult(
                available=False, price=price, shipping_cost=None, quantity_available=None
            )

        item_total, shipping_cost, tax_amount = self._checkout_totals(checkout)
        return RevalidationResult(
            available=True,
            price=item_total if item_total > 0 else price,
            shipping_cost=shipping_cost,
            quantity_available=None,
            tax_amount=tax_amount,
        )

    def checkout(self, intent: PurchaseIntent, revalidated: RevalidationResult) -> CheckoutResult:
        raise HumanActionRequiredError(
            "Shopify UCP checkout requires buyer-approved payment (complete_checkout needs a "
            "real payment instrument — Shop Pay, Google Pay, or card) — this project never "
            "constructs one. Complete the purchase manually via the checkout continue_url."
        )
