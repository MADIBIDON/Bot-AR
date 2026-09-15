"""Cultura PurchaseConnector — Phase 39.

Real, live-verified purchase-path progress for the one genuinely open
retailer in this project (no anti-bot protection at all — Cultura is
reachable by a plain, honest HTTP client the same way its monitoring
already is). Two real mutations were exercised via a genuine, manual,
one-time browser action this session — adding a real (non-drop,
"produit banal" per this project's own dry-run policy) item to a real
guest cart on Cultura's Magento 2 storefront — never through scripted
browser automation, matching this project's "DevTools/browser only to
understand requests, never to bypass anything" instruction. Both
mutations are also Magento 2's own STANDARD, public, core-module GraphQL
schema (not a Cultura-specific customization), used identically by any
Magento 2 storefront:

  createEmptyCart -> String (a masked, anonymous guest cart id)
  addSimpleProductsToCart(input: {cart_id, cart_items: [{data: {sku, quantity}}]})
    -> real, CURRENT sku/name/ean/price/quantity_available for that offer

revalidate() uses both, plus a read-only products(filter:{url_key:{eq}})
lookup (also captured live) to resolve the real SKU from the Listing's
own URL before ever touching the cart. It deliberately does NOT
re-validate EAN itself — that hard-reject already happens upstream,
before a PurchaseIntent is ever built (products/matcher.py, gated by
engine.decision.MIN_FINANCIAL_MATCH_CONFIDENCE) — matching every other
connector in this project (purchase/merchants/shopify_ucp.py,
fuji_store.py), none of which re-derive identity fields PurchaseIntent
itself doesn't carry.

checkout() always raises HumanActionRequiredError. This is not a lazy
placeholder: Cultura's real GraphQL schema was introspected this session
(read-only, no cart/order touched) and DOES expose
setShippingAddressesOnCart / setBillingAddressOnCart /
setPaymentMethodOnCart / placeOrder, plus Adyen-backed tokenized-card
support (createVaultCardPaymentToken, adyenPaymentDetails) — a
genuinely promising path for a future, carefully-scoped phase. Their
exact input shapes were deliberately NOT probed further this session
(the environment's own real-world-transaction safeguard intervened on
the next, deeper introspection call, and this project never works
around a safety boundary like that) — implementing address/shipping/
payment blind, without being able to verify each step live, would risk
shipping code nobody has actually exercised against a real checkout,
which is a worse failure mode than an honest HumanActionRequiredError
today. See docs implied by this module: the mutation names above are
real and confirmed to exist; their arguments are not yet independently
verified here.

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
from decimal import Decimal
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

if TYPE_CHECKING:
    from purchase.models import PurchaseIntent

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+public cart API; no automated payment)"
GRAPHQL_ENDPOINT = "https://www.cultura.com/magento/graphql"

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

    def revalidate(self, intent: PurchaseIntent) -> RevalidationResult:
        url_key = _extract_url_key(intent.url)
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
            {"cartId": cart_id, "sku": real_sku, "quantity": float(intent.quantity)},
        )
        cart = add_data["addSimpleProductsToCart"]["cart"]
        cart_items = cart.get("items") or []
        if cart.get("cart_error") or not cart_items:
            fallback_price_info = items[0]["price_range"]["minimum_price"]["final_price"]
            return RevalidationResult(
                available=False,
                price=Decimal(str(fallback_price_info["value"])),
                shipping_cost=None,
                quantity_available=0,
            )

        item = cart_items[0]
        row_total = item["prices"]["row_total_including_tax"]["value"]
        price = Decimal(str(row_total)) / Decimal(intent.quantity)
        quantity_available = item.get("quantity_available")
        return RevalidationResult(
            available=quantity_available is None or quantity_available > 0,
            price=price,
            # Shipping is not resolved here — see module docstring:
            # address/shipping mutations exist but weren't independently
            # verified this session, so this never invents a number.
            shipping_cost=None,
            quantity_available=quantity_available,
        )

    def checkout(self, intent: PurchaseIntent, revalidated: RevalidationResult) -> CheckoutResult:
        raise HumanActionRequiredError(
            "Cultura checkout requires an address/shipping/payment flow (Adyen-backed, "
            "including tokenized-card support) that was confirmed to exist this session but "
            "not independently implemented or tested — complete the purchase manually via the "
            "product page."
        )
