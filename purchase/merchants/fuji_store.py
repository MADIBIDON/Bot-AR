"""Fuji Store (fuji-store.fr) PurchaseConnector — the first real one.

Uses WooCommerce's official Store API (`/wp-json/wc/store/v1/*`), the
same public, documented, unauthenticated REST API the store's own
block-based cart/checkout UI calls. Not scraping, not a private endpoint,
no anti-bot bypass: a plain JSON API request with an honest User-Agent,
same posture as connectors/schema_org.py.

What this can do (see revalidate()): look up the product by its listing
slug, add it to a fresh anonymous cart, optionally price shipping against
a configured buyer address, and read back the real total (item + shipping
+ tax) — all via cart endpoints, which never create a persistent order on
the merchant's side. That is deliberately as far as this connector goes
automatically.

What this will never do (see checkout()): create the actual WooCommerce
order or attempt payment. Fuji Store's only payment methods are PayPal
Commerce Platform gateways (ppcp-gateway / ppcp-credit-card-gateway) —
completing either one requires either a PayPal login/approval or entering
real card details, both human-only steps. checkout() always raises
HumanActionRequiredError, unconditionally, regardless of the
PURCHASES_ENABLED kill switch — there is no automated path past this
point for this merchant today, and none is attempted.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

from purchase.base import (
    AutomatedCheckoutUnsupportedError,
    CheckoutResult,
    HumanActionRequiredError,
    PurchaseConnector,
    PurchaseError,
    StaleListingError,
)
from purchase.models import RevalidationResult

if TYPE_CHECKING:
    from purchase.models import PurchaseIntent

DEFAULT_SHOP_DOMAIN = "fuji-store.fr"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+dry-run cart preview; no automated payment)"

_STORE_API = "wp-json/wc/store/v1"

_CHALLENGE_MARKERS = ("cf-browser-verification", "cf-challenge", "captcha", "checking your browser")


def _extract_slug(url: str) -> str:
    """Fuji Store product URLs look like
    https://fuji-store.fr/produit/<slug>/ — see connectors/woocommerce.py.

    Phase 33: strips any query string/fragment before extracting the slug
    — defensive, same fix as purchase/merchants/shopify_ucp.py's
    _extract_handle() (a real bug there, confirmed live against every
    Kairyu/RelicTCG listing this session — Fuji Store URLs haven't been
    observed with one, but there is no reason to leave the same footgun
    here)."""
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]


class FujiStorePurchaseConnector(PurchaseConnector):
    def __init__(
        self,
        *,
        shop_domain: str = DEFAULT_SHOP_DOMAIN,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = f"https://{shop_domain}/{_STORE_API}"
        self._timeout = timeout
        self._headers = {"User-Agent": user_agent, "Content-Type": "application/json"}
        # Phase 34 (100ms warm-path target): same persistent-client
        # pattern as purchase/merchants/shopify_ucp.py — None (default)
        # preserves this connector's exact per-call httpx.request()
        # behavior, which this file's whole test suite monkeypatches.
        self._client = client

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        send = self._client.request if self._client is not None else httpx.request
        try:
            response = send(method, f"{self._base_url}{path}", timeout=self._timeout, **kwargs)
        except httpx.TimeoutException as exc:
            raise PurchaseError(f"timeout calling Fuji Store Store API {path}") from exc
        except httpx.RequestError as exc:
            raise PurchaseError(
                f"network error calling Fuji Store Store API {path}: {exc}"
            ) from exc

        body_sample = response.text[:2000].lower()
        if any(marker in body_sample for marker in _CHALLENGE_MARKERS):
            raise HumanActionRequiredError(
                "Fuji Store returned an anti-bot challenge page — never bypassed."
            )
        if response.status_code == 403:
            raise HumanActionRequiredError(
                "Fuji Store rejected the request with 403 (likely bot protection or an "
                "expired session) — never bypassed."
            )
        if response.status_code >= 500:
            raise PurchaseError(f"Fuji Store Store API returned HTTP {response.status_code}")
        return response

    def _find_product(self, slug: str) -> dict:
        response = self._request("GET", "/products", params={"slug": slug}, headers=self._headers)
        try:
            results = response.json()
        except ValueError as exc:
            raise PurchaseError("Fuji Store Store API returned a non-JSON response") from exc
        if not results:
            raise StaleListingError(f"Product with slug {slug!r} no longer exists on Fuji Store.")
        return results[0]

    def _resolve_line_item_id(self, product: dict) -> int:
        if not product.get("has_options"):
            return int(product["id"])
        variations = product.get("variations") or []
        if len(variations) != 1:
            raise AutomatedCheckoutUnsupportedError(
                f"Product {product.get('slug')!r} has {len(variations)} variant(s) — cannot "
                "determine which one to purchase automatically; buy manually."
            )
        return int(variations[0]["id"])

    def _fresh_cart_session(self) -> tuple[str, str]:
        response = self._request("GET", "/cart", headers=self._headers)
        nonce = response.headers.get("Nonce")
        cart_token = response.headers.get("Cart-Token")
        if not nonce or not cart_token:
            raise PurchaseError(
                "Fuji Store did not return cart session headers (Nonce/Cart-Token)."
            )
        return nonce, cart_token

    def _cart_headers(self, nonce: str, cart_token: str) -> dict[str, str]:
        return {**self._headers, "Nonce": nonce, "Cart-Token": cart_token}

    @staticmethod
    def _minor_to_decimal(value: str | int, minor_unit: int) -> Decimal:
        try:
            return Decimal(str(value)) / (Decimal(10) ** minor_unit)
        except InvalidOperation as exc:
            raise PurchaseError(f"Fuji Store returned an unparseable amount: {value!r}") from exc

    def revalidate(self, intent: PurchaseIntent) -> RevalidationResult:
        slug = _extract_slug(intent.url)
        product = self._find_product(slug)

        if not product.get("is_purchasable", False):
            return RevalidationResult(
                available=False,
                price=intent.observed_price,
                shipping_cost=None,
                quantity_available=0,
            )

        line_item_id = self._resolve_line_item_id(product)
        nonce, cart_token = self._fresh_cart_session()

        add_response = self._request(
            "POST",
            "/cart/add-item",
            headers=self._cart_headers(nonce, cart_token),
            json={"id": line_item_id, "quantity": intent.quantity},
        )
        cart = add_response.json()
        cart_token = add_response.headers.get("Cart-Token", cart_token)

        if cart.get("errors"):
            reasons = "; ".join(e.get("message", str(e)) for e in cart["errors"])
            if "stock" in reasons.lower():
                return RevalidationResult(
                    available=False,
                    price=intent.observed_price,
                    shipping_cost=None,
                    quantity_available=None,
                )
            raise PurchaseError(f"Fuji Store cart rejected the item: {reasons}")

        postal_code = os.environ.get("PURCHASE_SHIPPING_POSTAL_CODE")
        if postal_code:
            addr_response = self._request(
                "POST",
                "/cart/update-customer",
                headers=self._cart_headers(nonce, cart_token),
                json={
                    "shipping_address": {
                        "postcode": postal_code,
                        "country": os.environ.get("PURCHASE_SHIPPING_COUNTRY", "FR"),
                        "city": os.environ.get("PURCHASE_SHIPPING_CITY", ""),
                    }
                },
            )
            cart = addr_response.json()

        totals = cart["totals"]
        minor_unit = int(totals["currency_minor_unit"])
        item_price = self._minor_to_decimal(totals["total_items"], minor_unit) / intent.quantity

        shipping_cost = None
        if totals.get("total_shipping") is not None:
            shipping_cost = self._minor_to_decimal(totals["total_shipping"], minor_unit)

        tax_amount = None
        if totals.get("total_tax") not in (None, "0"):
            tax_amount = self._minor_to_decimal(totals["total_tax"], minor_unit)

        matched_item = next((i for i in cart.get("items", []) if i.get("id") == line_item_id), None)
        quantity_available = None
        if matched_item is not None:
            limits = matched_item.get("quantity_limits") or {}
            quantity_available = limits.get("maximum")

        return RevalidationResult(
            available=True,
            price=item_price,
            shipping_cost=shipping_cost,
            quantity_available=quantity_available,
            tax_amount=tax_amount,
        )

    def checkout(self, intent: PurchaseIntent, revalidated: RevalidationResult) -> CheckoutResult:
        raise HumanActionRequiredError(
            "Fuji Store only supports PayPal Commerce Platform payment methods "
            "(ppcp-gateway / ppcp-credit-card-gateway) — completing payment requires a human "
            "PayPal approval or card entry step this project never automates. "
            "Complete the purchase manually via the product page."
        )
