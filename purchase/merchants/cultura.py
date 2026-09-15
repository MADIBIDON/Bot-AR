"""Cultura PurchaseConnector — Phase 39/40.

Real, live-verified purchase-path progress for the one genuinely open
retailer in this project (no anti-bot protection at all — Cultura is
reachable by a plain, honest HTTP client the same way its monitoring
already is). Every mutation this module calls is Magento 2's own
STANDARD, public, core-module GraphQL schema (not a Cultura-specific
customization), used identically by any Magento 2 storefront:

  createEmptyCart -> String (a masked, anonymous guest cart id)
  addSimpleProductsToCart(input: {cart_id, cart_items: [{data: {sku, quantity}}]})
    -> real, CURRENT sku/name/ean/price/quantity_available for that offer
  setShippingAddressesOnCart(input: {cart_id, shipping_addresses: [{address}]})
    -> real, live shipping methods + amounts for that address
  setShippingMethodsOnCart(input: {cart_id, shipping_methods: [{carrier_code, method_code}]})
  setBillingAddressOnCart(input: {cart_id, billing_address: {same_as_shipping}})

createEmptyCart/addSimpleProductsToCart were exercised via a genuine,
manual, one-time browser action this session (adding a real, non-drop
"produit banal" item to a real guest cart) — never through scripted
browser automation. setShippingAddressesOnCart/setShippingMethodsOnCart/
setBillingAddressOnCart are the standard Magento 2 core schema (publicly
documented at devdocs.magento.com, not something reverse-engineered) —
see CHANGELOG note in this module's git history for how far live testing
of each one actually got this session; the connector never assumes a
step worked without a real response confirming it (see last_checkout_state).

revalidate() uses these to resolve real price/stock, AND (Phase 40)
real shipping cost when a local shipping profile is configured
(purchase/shipping.py — never fabricated, never touches payment). It
deliberately does NOT re-validate EAN itself — that hard-reject already
happens upstream, before a PurchaseIntent is ever built (products/
matcher.py, gated by engine.decision.MIN_FINANCIAL_MATCH_CONFIDENCE) —
matching every other connector in this project (shopify_ucp.py,
fuji_store.py), none of which re-derive identity fields PurchaseIntent
itself doesn't carry.

checkout() always raises HumanActionRequiredError. This is not a lazy
placeholder: Cultura's real GraphQL schema was introspected this session
(read-only, no cart/order touched) and DOES expose setPaymentMethodOnCart
/ placeOrder, plus Adyen-backed tokenized-card support
(createVaultCardPaymentToken, adyenPaymentDetails) — confirmed to exist,
never called. Their exact input shapes (Adyen's Magento2 plugin extends
the core schema with its own input types) were deliberately NOT probed
live this session — Adyen's plugin schema is not core Magento 2's public
documentation, and the environment's own real-world-transaction safeguard
intervened on the deeper introspection call that would have revealed it.
Even with the exact shape known, this project never calls a payment
mutation or placeOrder automatically: that is a real financial
transaction, categorically out of scope for automation here — see
purchase/base.py's module docstring. HumanActionRequiredError at exactly
this boundary is the correct, final state for this connector, not a gap
to be closed later.

Price nuance worth knowing: Cultura integrates third-party marketplace
sellers (Mirakl) — the catalog search's own price_range can differ from
what a specific cart offer actually charges (observed live: a search hit
at 55.99EUR vs. a marketplace "vendeur partenaire" offer on the same
page at 188.60EUR). revalidate() always returns the CART's own price
(the actual offer being added), never the catalog search price, for
exactly this reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx

from purchase.base import (
    CheckoutResult,
    HumanActionRequiredError,
    PurchaseConnector,
    PurchaseError,
    StaleListingError,
)
from purchase.models import RevalidationResult
from purchase.shipping import load_shipping_address

if TYPE_CHECKING:
    from purchase.models import PurchaseIntent

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+public cart API; no automated payment)"
GRAPHQL_ENDPOINT = "https://www.cultura.com/magento/graphql"


class CulturaCheckoutState(StrEnum):
    """Every state this connector has actually verified a real response
    for — never advanced past what the last real GraphQL call confirmed.
    PAYMENT_METHOD_SET / ORDER_READY / ORDER_SUBMITTED are declared here
    for completeness (matching the schema's real, confirmed-to-exist
    mutations) but this connector never transitions into them — see
    module docstring."""

    EMPTY_CART = "empty_cart"
    CART_WITH_PRODUCT = "cart_with_product"
    CART_UNAVAILABLE = "cart_unavailable"
    SHIPPING_ADDRESS_SET = "shipping_address_set"
    SHIPPING_METHOD_SET = "shipping_method_set"
    BILLING_ADDRESS_SET = "billing_address_set"
    PAYMENT_HUMAN_REQUIRED = "payment_human_required"


_PRODUCT_BY_URL_KEY_QUERY = """
query getProductByUrlKey($urlKey: String) {
  products(filter: {url_key: {eq: $urlKey}}) {
    items {
      sku
      name
      ean
      price_range { minimum_price { final_price { value currency } } }
      stock_item_extra { front_availability }
    }
  }
}
"""

_CREATE_EMPTY_CART_MUTATION = "mutation { createEmptyCart }"

_ADD_TO_CART_MUTATION = """
mutation addSimpleProductsToCart($cartId: String!, $sku: String!, $quantity: Float!) {
  addSimpleProductsToCart(input: {
    cart_id: $cartId
    cart_items: [{ data: { quantity: $quantity, sku: $sku } }]
  }) {
    cart {
      id
      cart_error
      items {
        quantity
        quantity_available
        product { sku ean name }
        prices { row_total_including_tax { value currency } }
      }
    }
  }
}
"""

_SET_SHIPPING_ADDRESS_MUTATION = """
mutation setShippingAddress($cartId: String!, $address: CartAddressInput!) {
  setShippingAddressesOnCart(input: {
    cart_id: $cartId
    shipping_addresses: [{ address: $address }]
  }) {
    cart {
      shipping_addresses {
        available_shipping_methods {
          carrier_code
          method_code
          amount { value currency }
          available
        }
      }
    }
  }
}
"""

_SET_SHIPPING_METHOD_MUTATION = """
mutation setShippingMethod($cartId: String!, $carrierCode: String!, $methodCode: String!) {
  setShippingMethodsOnCart(input: {
    cart_id: $cartId
    shipping_methods: [{ carrier_code: $carrierCode, method_code: $methodCode }]
  }) {
    cart {
      shipping_addresses {
        selected_shipping_method { carrier_code method_code amount { value currency } }
      }
    }
  }
}
"""

_SET_BILLING_ADDRESS_MUTATION = """
mutation setBillingAddress($cartId: String!, $sameAsShipping: Boolean!) {
  setBillingAddressOnCart(input: {
    cart_id: $cartId
    billing_address: { same_as_shipping: $sameAsShipping }
  }) {
    cart { billing_address { firstname lastname } }
  }
}
"""


@dataclass(frozen=True, slots=True)
class PaymentReadiness:
    """Result of check_payment_readiness() — see that method's docstring.
    reason is set only when something failed before reaching the state
    described by checkout_state (e.g. the probe product itself is gone)."""

    checkout_state: CulturaCheckoutState
    shipping_cost: Decimal | None
    reason: str | None


_URL_KEY_RE = re.compile(r"^p-(.+)\.html$")


def _extract_url_key(url: str) -> str:
    """Cultura product URLs look like
    https://www.cultura.com/p-<url_key>.html — see discovery/cultura.py.
    Falls back to the raw filename if the p-...-.html shape ever changes,
    rather than raising — a downstream StaleListingError (no product
    found for that key) is a clearer failure than a crash here."""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    filename = path.rsplit("/", 1)[-1]
    match = _URL_KEY_RE.match(filename)
    return match.group(1) if match else filename


class CulturaPurchaseConnector(PurchaseConnector):
    def __init__(
        self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, client: httpx.Client | None = None
    ) -> None:
        self._timeout = timeout
        # Phase 34 pattern: a persistent client, when the caller provides
        # one (see purchase/defaults.py), is reused across every call;
        # None preserves the exact per-call httpx.post() behavior this
        # module's test suite monkeypatches.
        self._client = client
        # Phase 40: the furthest CulturaCheckoutState the last
        # revalidate()/check_payment_readiness() call actually confirmed
        # with a real response — never advanced speculatively. Read by
        # scripts/watch.py's payment-setup command for reporting; not
        # part of the shared PurchaseConnector ABI since no other
        # connector in this project goes this deep yet.
        self.last_checkout_state: CulturaCheckoutState = CulturaCheckoutState.EMPTY_CART
        # Phase 40: the real reason the last shipping/billing attempt
        # stopped early, if any — None means either it fully succeeded
        # or was never attempted (no local profile configured). Never a
        # secret: GraphQL error text or a missing-field name only.
        self.last_shipping_error: str | None = None

    def warm_up(self) -> None:
        """Phase 35 section 14: one cheap, read-only lookup (a url_key
        guaranteed not to match anything real) to establish the
        persistent connection ahead of a known drop — never a cart,
        never a reservation. Never raises."""
        try:
            self._graphql(_PRODUCT_BY_URL_KEY_QUERY, {"urlKey": "__bot-ar-warmup-probe__"})
        except PurchaseError:
            pass

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        send = self._client.post if self._client is not None else httpx.post
        try:
            response = send(
                GRAPHQL_ENDPOINT,
                json=payload,
                timeout=self._timeout,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
        except httpx.TimeoutException as exc:
            raise PurchaseError("timeout calling Cultura GraphQL") from exc
        except httpx.RequestError as exc:
            raise PurchaseError(f"network error calling Cultura GraphQL: {exc}") from exc
        if response.status_code >= 400:
            raise PurchaseError(f"Cultura GraphQL returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise PurchaseError("Cultura GraphQL returned non-JSON") from exc
        if "errors" in body:
            raise PurchaseError(f"Cultura GraphQL error: {body['errors']}")
        return body["data"]

    def _resolve_and_add_to_cart(self, url: str, quantity: int) -> tuple[str, dict | None, dict]:
        """Shared by revalidate() and check_payment_readiness(): resolves
        the real SKU from the product page URL, creates a fresh guest
        cart, and adds it. Returns (cart_id, cart_item_or_None,
        fallback_lookup_item) — cart_item is None when the cart itself
        reports unavailable/errored (caller decides what that means).
        Raises StaleListingError if the product can't be resolved at
        all — same hard-reject as before this refactor."""
        url_key = _extract_url_key(url)
        lookup = self._graphql(_PRODUCT_BY_URL_KEY_QUERY, {"urlKey": url_key})
        items = (lookup.get("products") or {}).get("items") or []
        if not items:
            raise StaleListingError(f"Product url_key {url_key!r} no longer exists on Cultura.")
        if len(items) > 1:
            raise StaleListingError(
                f"url_key {url_key!r} matched {len(items)} products on Cultura — ambiguous, "
                "refusing to guess which one."
            )
        real_sku = items[0]["sku"]

        cart_data = self._graphql(_CREATE_EMPTY_CART_MUTATION)
        cart_id = cart_data["createEmptyCart"]

        add_data = self._graphql(
            _ADD_TO_CART_MUTATION,
            {"cartId": cart_id, "sku": real_sku, "quantity": float(quantity)},
        )
        cart = add_data["addSimpleProductsToCart"]["cart"]
        cart_items = cart.get("items") or []
        if cart.get("cart_error") or not cart_items:
            return cart_id, None, items[0]
        return cart_id, cart_items[0], items[0]

    def _advance_shipping_and_billing(self, cart_id: str) -> Decimal | None:
        """Best-effort: sets a real shipping address (from the local
        checkout profile — purchase/shipping.py), picks the cheapest
        available real shipping method, and marks billing the same as
        shipping. Updates self.last_checkout_state as each real response
        confirms the step. Any failure at any point (missing local
        profile, network error, GraphQL error, no methods available)
        degrades gracefully to "go no further" — this must never make
        revalidate() itself fail, since shipping is a bonus, not a
        requirement, for the cart-level safety checks that came before
        it. Returns the resolved shipping cost, or None."""
        self.last_shipping_error = None
        address = load_shipping_address()
        if address is None:
            return None

        try:
            shipping_data = self._graphql(
                _SET_SHIPPING_ADDRESS_MUTATION,
                {
                    "cartId": cart_id,
                    "address": {
                        "firstname": address.firstname,
                        "lastname": address.lastname,
                        "street": [address.street],
                        "city": address.city,
                        "postcode": address.postal_code,
                        "country_code": address.country_code,
                        "telephone": address.telephone,
                    },
                },
            )
            self.last_checkout_state = CulturaCheckoutState.SHIPPING_ADDRESS_SET
            shipping_addresses = (
                shipping_data.get("setShippingAddressesOnCart", {})
                .get("cart", {})
                .get("shipping_addresses")
                or []
            )
            methods = shipping_addresses[0].get("available_shipping_methods") or []
            available_methods = [m for m in methods if m.get("available")]
            if not available_methods:
                return None
            cheapest = min(available_methods, key=lambda m: m["amount"]["value"])

            self._graphql(
                _SET_SHIPPING_METHOD_MUTATION,
                {
                    "cartId": cart_id,
                    "carrierCode": cheapest["carrier_code"],
                    "methodCode": cheapest["method_code"],
                },
            )
            self.last_checkout_state = CulturaCheckoutState.SHIPPING_METHOD_SET

            self._graphql(
                _SET_BILLING_ADDRESS_MUTATION, {"cartId": cart_id, "sameAsShipping": True}
            )
            self.last_checkout_state = CulturaCheckoutState.BILLING_ADDRESS_SET

            self.last_shipping_error = None
            return Decimal(str(cheapest["amount"]["value"]))
        except (PurchaseError, KeyError, IndexError, TypeError) as exc:
            # Shipping/billing is an enhancement on top of the cart-level
            # revalidation that already happened — never let a shipping
            # hiccup turn into a false "product unavailable" rejection.
            # The real reason is still captured (never a secret/PII —
            # PurchaseError/KeyError/TypeError messages here are GraphQL
            # error text or a missing field name, nothing from the local
            # shipping profile) so an operator running payment-setup
            # before a drop can actually see why, instead of a silent
            # BLOCKED with no explanation.
            self.last_shipping_error = f"{type(exc).__name__}: {exc}"
            return None

    def revalidate(self, intent: PurchaseIntent) -> RevalidationResult:
        self.last_checkout_state = CulturaCheckoutState.EMPTY_CART
        cart_id, cart_item, fallback_item = self._resolve_and_add_to_cart(
            intent.url, intent.quantity
        )
        if cart_item is None:
            self.last_checkout_state = CulturaCheckoutState.CART_UNAVAILABLE
            fallback_price_info = fallback_item["price_range"]["minimum_price"]["final_price"]
            return RevalidationResult(
                available=False,
                price=Decimal(str(fallback_price_info["value"])),
                shipping_cost=None,
                quantity_available=0,
            )

        cart_quantity = cart_item.get("quantity")
        if cart_quantity is not None and cart_quantity != intent.quantity:
            # Phase 40 section 27: the merchant can silently adjust the
            # quantity actually placed in the cart (e.g. a per-customer
            # limit) — always re-read it and abort rather than trust the
            # requested quantity blindly.
            raise StaleListingError(
                f"Cart quantity {cart_quantity} does not match the requested quantity "
                f"{intent.quantity} — refusing to proceed."
            )

        self.last_checkout_state = CulturaCheckoutState.CART_WITH_PRODUCT
        row_total = cart_item["prices"]["row_total_including_tax"]["value"]
        price = Decimal(str(row_total)) / Decimal(intent.quantity)
        quantity_available = cart_item.get("quantity_available")
        available = quantity_available is None or quantity_available > 0

        shipping_cost = self._advance_shipping_and_billing(cart_id) if available else None

        return RevalidationResult(
            available=available,
            price=price,
            shipping_cost=shipping_cost,
            quantity_available=quantity_available,
        )

    def check_payment_readiness(self, product_url: str, *, quantity: int = 1) -> PaymentReadiness:
        """Non-transactional readiness probe for scripts/watch.py's
        `payment-setup` command (Phase 40 sections 8/11) — goes exactly as
        far as revalidate() would (resolve product, cart, shipping,
        billing) against a real product of the caller's choosing, and
        reports the exact state reached. Never touches payment, never
        creates a PurchaseAttempt, never called from the automatic
        worker pipeline — meant to be run by hand, ahead of a drop,
        against a real but deliberately non-drop product so it never
        disturbs a drop-critical listing's own state."""
        self.last_checkout_state = CulturaCheckoutState.EMPTY_CART
        try:
            cart_id, cart_item, _fallback = self._resolve_and_add_to_cart(product_url, quantity)
        except PurchaseError as exc:
            return PaymentReadiness(
                checkout_state=self.last_checkout_state, shipping_cost=None, reason=str(exc)
            )
        if cart_item is None:
            self.last_checkout_state = CulturaCheckoutState.CART_UNAVAILABLE
            return PaymentReadiness(
                checkout_state=self.last_checkout_state,
                shipping_cost=None,
                reason="Product resolved but the cart reports it unavailable right now.",
            )
        self.last_checkout_state = CulturaCheckoutState.CART_WITH_PRODUCT
        shipping_cost = self._advance_shipping_and_billing(cart_id)
        return PaymentReadiness(
            checkout_state=self.last_checkout_state, shipping_cost=shipping_cost, reason=None
        )

    def checkout(self, intent: PurchaseIntent, revalidated: RevalidationResult) -> CheckoutResult:
        self.last_checkout_state = CulturaCheckoutState.PAYMENT_HUMAN_REQUIRED
        raise HumanActionRequiredError(
            "Cultura checkout reached the payment step (cart/shipping/billing confirmed real — "
            "see last_checkout_state) but payment/placeOrder is never automated by this project — "
            "complete the purchase manually via the product page."
        )
