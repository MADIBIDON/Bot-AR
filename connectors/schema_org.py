"""Shared schema.org Product JSON-LD parsing and HTTP fetch mechanics.

Many e-commerce platforms embed a standard schema.org Product JSON-LD
block for search engines — Shopify does it by default, WooCommerce does
it via SEO plugins (Yoast, RankMath), and most others follow the same
schema. The parsing logic below has always been platform-agnostic; a
platform-specific connector only needs to know how to build a product
page URL from an identifier (`_build_url`). See connectors/shopify.py and
connectors/woocommerce.py.

Never bypasses anti-bot protection, CAPTCHAs, or rate limits: a single GET
with a short timeout and an honest, explicit User-Agent. If a site starts
blocking this, the correct response is to stop, not to work around it.
"""

from __future__ import annotations

import json
import re
from abc import abstractmethod
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


class SchemaOrgProductConnector(BaseConnector):
    """Base for any connector whose product pages embed a schema.org
    Product JSON-LD block. Subclasses implement only `_build_url`.

    external_id convention (the one thing BaseConnector.get_product()
    can't express — it only receives external_id, never a URL):
    external_id is the product's URL path identifier (Shopify: the handle
    in `/products/<handle>`; WooCommerce: the slug in `/produit/<slug>/`),
    optionally suffixed with `:<variant_sku>` to select a specific variant
    on a multi-offer listing (condition grades, art variants, ...).
    Without a suffix, the first offer on the page is used.
    """

    def __init__(
        self,
        *,
        merchant_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._merchant_name = merchant_name
        self._timeout = timeout
        self._user_agent = user_agent

    @abstractmethod
    def _build_url(self, path_id: str) -> str:
        """Build the product page URL from the identifier extracted out
        of external_id (before any `:variant_sku` suffix)."""
        raise NotImplementedError

    def get_product(self, external_id: str) -> ConnectorProduct:
        path_id, variant_sku = _split_external_id(external_id)
        url = self._build_url(path_id)
        html = self._fetch(url)

        product_data = _extract_product_ld_json(html, url)
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


def normalize_domain(shop_domain: str) -> str:
    return shop_domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def _split_external_id(external_id: str) -> tuple[str, str | None]:
    path_id, sep, sku = external_id.partition(":")
    return (path_id, sku) if sep and sku else (external_id, None)


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/").split("?", 1)[0].lower()


def _product_candidates(html: str) -> list[dict]:
    candidates: list[dict] = []
    for raw in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        candidates.extend(
            item for item in items if isinstance(item, dict) and item.get("@type") == "Product"
        )
    return candidates


def _extract_product_ld_json(html: str, page_url: str) -> dict | None:
    """A page can embed more than one Product block — real-world sites
    have been seen shipping a generic theme placeholder (wrong price,
    wrong currency, no url/sku) alongside the real per-product data.
    Prefer whichever candidate's own `url` matches the page we fetched;
    failing that, prefer one with an Offer carrying a `sku` (a sign of
    real per-product data, not a placeholder); otherwise fall back to the
    first candidate found."""
    candidates = _product_candidates(html)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    normalized_page_url = _normalize_url(page_url)
    for candidate in candidates:
        url = candidate.get("url")
        if isinstance(url, str) and _normalize_url(url) == normalized_page_url:
            return candidate

    for candidate in candidates:
        if any(offer.get("sku") for offer in _offers_list(candidate)):
            return candidate

    return candidates[0]


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
