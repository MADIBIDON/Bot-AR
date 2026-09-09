"""Read-only connector for any Shopify storefront.

One plain HTTP GET per product page, parses the standard schema.org
Product JSON-LD block that Shopify themes embed for SEO — no JS
execution, no headless browser, no API key. First real merchant wired up
with this: Kairyu (kairyu.fr), a French Pokémon TCG shop; the mechanism is
generic to any Shopify store, only `shop_domain` changes.

external_id convention (this is the one piece BaseConnector.get_product()
cannot express on its own — it only receives external_id, never a URL):
external_id is the product's URL handle (the slug in
`/products/<handle>`), optionally suffixed with `:<variant_sku>` to select
a specific variant on a multi-variant listing (e.g. condition grades).
Without a suffix, the first offer on the page is used. Listing.external_id
stores this handle; Listing.url stores the human-facing canonical URL —
both already exist in the schema, no model change needed.

Never bypasses anti-bot protection, CAPTCHAs, or rate limits: a single GET
with a short timeout and an honest, explicit User-Agent. If a site starts
blocking this, the correct response is to stop, not to work around it.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

import httpx

from connectors.base import BaseConnector, ConnectorError, ConnectorProduct, ProductNotFoundError

DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+read-only price monitor; no auto-purchase)"
DEFAULT_TIMEOUT_SECONDS = 8.0

_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)

_GTIN_FIELDS = ("gtin13", "gtin12", "gtin8", "gtin")

_IN_STOCK_TOKENS = {"instock", "limitedavailability", "preorder", "onlineonly", "instorepickup"}
_OUT_OF_STOCK_TOKENS = {"outofstock", "soldout", "discontinued"}


class ShopifyConnector(BaseConnector):
    def __init__(
        self,
        *,
        shop_domain: str,
        merchant_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._shop_domain = (
            shop_domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        )
        self._merchant_name = merchant_name
        self._timeout = timeout
        self._user_agent = user_agent

    def get_product(self, external_id: str) -> ConnectorProduct:
        handle, variant_sku = _split_external_id(external_id)
        url = f"https://{self._shop_domain}/products/{handle}"
        html = self._fetch(url)

        product_data = _extract_product_ld_json(html)
        if product_data is None:
            raise ConnectorError(
                f"could not locate product structured data on {url} "
                "(page structure may have changed)"
            )

        offer = _select_offer(product_data, variant_sku)
        if offer is None:
            raise ProductNotFoundError(f"no matching variant for external_id {external_id!r}")

        return _build_connector_product(external_id, self._merchant_name, product_data, offer, url)

    def _fetch(self, url: str) -> str:
        try:
            response = httpx.get(
                url,
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"timeout fetching {url}") from exc
        except httpx.RequestError as exc:
            raise ConnectorError(f"network error fetching {url}: {exc}") from exc

        if response.status_code == 404:
            raise ProductNotFoundError(f"product page not found: {url}")
        if response.status_code == 429:
            raise ConnectorError(f"rate limited (429) fetching {url}")
        if response.status_code == 403:
            raise ConnectorError(f"access forbidden (403) fetching {url}")
        if response.status_code >= 400:
            raise ConnectorError(f"HTTP {response.status_code} fetching {url}")
        return response.text


def _split_external_id(external_id: str) -> tuple[str, str | None]:
    handle, sep, sku = external_id.partition(":")
    return (handle, sku) if sep and sku else (external_id, None)


def _extract_product_ld_json(html: str) -> dict | None:
    for raw in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                return candidate
    return None


def _offers_list(product_data: dict) -> list[dict]:
    offers = product_data.get("offers")
    if isinstance(offers, dict):
        return [offers]
    if isinstance(offers, list):
        return [offer for offer in offers if isinstance(offer, dict)]
    return []


def _select_offer(product_data: dict, variant_sku: str | None) -> dict | None:
    offers = _offers_list(product_data)
    if not offers:
        return None
    if variant_sku is None:
        return offers[0]
    return next((offer for offer in offers if offer.get("sku") == variant_sku), None)


def _parse_availability(value: object) -> bool | None:
    if not isinstance(value, str) or not value:
        return None
    token = value.strip().rstrip("/").rsplit("/", 1)[-1].lower()
    if token in _IN_STOCK_TOKENS:
        return True
    if token in _OUT_OF_STOCK_TOKENS:
        return False
    return None


def _parse_price(value: object) -> Decimal:
    if value is None:
        raise ConnectorError("offer has no price")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ConnectorError(f"unparseable price value: {value!r}") from exc


def _first_gtin(product_data: dict) -> str | None:
    for field in _GTIN_FIELDS:
        value = product_data.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_connector_product(
    external_id: str,
    merchant_name: str,
    product_data: dict,
    offer: dict,
    page_url: str,
) -> ConnectorProduct:
    name = product_data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConnectorError("product data has no name")

    currency = offer.get("priceCurrency")
    if not isinstance(currency, str) or not currency.strip():
        raise ConnectorError("offer has no priceCurrency")

    available = _parse_availability(offer.get("availability"))
    if available is None:
        # Indeterminate stock is a real, honest outcome — never guessed.
        raise ConnectorError(
            f"could not determine stock status from availability={offer.get('availability')!r}"
        )

    mpn = product_data.get("mpn")
    mpn = mpn.strip() if isinstance(mpn, str) and mpn.strip() else None

    return ConnectorProduct(
        external_id=external_id,
        name=name.strip(),
        price=_parse_price(offer.get("price")),
        currency=currency.strip().upper(),
        available=available,
        seller=merchant_name,
        url=offer.get("url") if isinstance(offer.get("url"), str) else page_url,
        ean=_first_gtin(product_data),
        mpn=mpn,
    )
