"""Cultura discovery — Phase 36 (Pokémon 30e, Cultura-only campaign).

Cultura's full product sitemap was already rejected for discovery
(discovery/defaults.py: 75-87 files of 50,000 URLs each, a "grosse
charge" this project avoids). This is a different, much lighter surface:
the same public, unauthenticated Magento 2 storefront GraphQL endpoint
(`/m2/graphql`, a `getPlpProducts` query) Cultura's own search page
calls — confirmed live this session via a plain, honest GET (no
scraping, no headless browser, no bypass of anything: this API returns
data to a request with no auth header or cookie, exactly like
purchase/merchants/fuji_store.py's WooCommerce Store API or
connectors/schema_org.py's plain HTML fetch). One targeted search query
per call, never a catalog crawl.

Real product page URLs follow `/p-<url_key>.html` (confirmed live
against two distinct, real Cultura listings this session) — matches
connectors/defaults.py's `full_path_external_id=True` for Cultura, so a
discovered ConnectorProduct's external_id is that same full path,
directly usable by connectors/generic_schema_org.py's
GenericSchemaOrgConnector without any further lookup.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from connectors.base import ConnectorProduct
from discovery.base import DiscoveryError, DiscoveryUnavailableError, RetailDiscoverySource

_DEFAULT_RETRY_AFTER_SECONDS = 60  # used only when a 429 gives no parseable Retry-After

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+public catalog search; no auto-purchase)"

_GRAPHQL_ENDPOINT_PATH = "m2/graphql"

_QUERY = """
query getPlpProducts($currentPage: Int $pageSize: Int $search: String) {
  products(currentPage: $currentPage pageSize: $pageSize search: $search resolverLight: 1) {
    total_count
    items {
      sku
      name
      url_key
      ean
      price_range { minimum_price { final_price { value currency } } }
      stock_item_extra { front_availability }
    }
  }
}
"""


class CulturaSearchDiscoverySource(RetailDiscoverySource):
    def __init__(
        self,
        *,
        shop_domain: str = "www.cultura.com",
        merchant_name: str = "Cultura",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = f"https://{shop_domain}/{_GRAPHQL_ENDPOINT_PATH}"
        self._merchant_name = merchant_name
        # Phase 23 pattern (connectors/schema_org.py): one persistent
        # client, built once, reused for this source's whole lifetime.
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": DEFAULT_USER_AGENT})
        # Phase 36 section "respect absolu de 429/Retry-After": a real
        # rate-limit response sets this, and every search() call before
        # it elapses short-circuits WITHOUT even attempting the request —
        # never just logged and immediately retried.
        self._retry_not_before: datetime | None = None

    def search(
        self, query: str, *, ean: str | None = None, mpn: str | None = None, limit: int = 10
    ) -> list[ConnectorProduct]:
        now = datetime.now(UTC)
        if self._retry_not_before is not None and now < self._retry_not_before:
            raise DiscoveryUnavailableError(
                f"{self._merchant_name}: respecting a prior 429's Retry-After — next attempt "
                f"not before {self._retry_not_before.isoformat()}"
            )

        variables = {"currentPage": 1, "pageSize": limit, "search": query}
        try:
            response = self._client.get(
                self._base_url, params={"query": _QUERY, "variables": json.dumps(variables)}
            )
        except httpx.TimeoutException as exc:
            raise DiscoveryError(f"{self._merchant_name}: timeout searching catalog") from exc
        except httpx.RequestError as exc:
            raise DiscoveryError(
                f"{self._merchant_name}: network error searching catalog: {exc}"
            ) from exc

        if response.status_code == 429:
            retry_seconds = _parse_retry_after_seconds(response.headers.get("Retry-After"))
            self._retry_not_before = now + timedelta(seconds=retry_seconds)
            raise DiscoveryError(
                f"{self._merchant_name}: rate limited (429) — backing off {retry_seconds}s "
                f"per its own Retry-After header"
            )
        if response.status_code >= 400:
            raise DiscoveryError(
                f"{self._merchant_name}: catalog search returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DiscoveryError(
                f"{self._merchant_name}: catalog search returned non-JSON"
            ) from exc

        if "errors" in body:
            raise DiscoveryError(f"{self._merchant_name}: GraphQL error: {body['errors']}")

        items = (body.get("data") or {}).get("products", {}).get("items") or []
        products: list[ConnectorProduct] = []
        for item in items:
            candidate = self._to_connector_product(item)
            if candidate is not None:
                products.append(candidate)
        return products

    def _to_connector_product(self, item: dict) -> ConnectorProduct | None:
        url_key = item.get("url_key")
        name = item.get("name")
        price_info = (item.get("price_range") or {}).get("minimum_price", {}).get("final_price")
        if not (url_key and name and price_info and price_info.get("value") is not None):
            return None
        availability = (item.get("stock_item_extra") or {}).get("front_availability")
        return ConnectorProduct(
            external_id=f"p-{url_key}.html",
            name=name,
            price=Decimal(str(price_info["value"])),
            currency=price_info.get("currency") or "EUR",
            available=availability not in (None, "unavailable"),
            seller=self._merchant_name,
            url=f"https://www.cultura.com/p-{url_key}.html",
            ean=item.get("ean") or None,
            mpn=item.get("sku") or None,
        )


def _parse_retry_after_seconds(raw: str | None) -> int:
    """Retry-After is either a plain integer (delta-seconds, the common
    case) or an HTTP-date. Only the integer form is parsed; an HTTP-date
    or anything unparseable falls back to a conservative fixed wait
    rather than guessing a shorter one — never less respectful than what
    the header actually gave when it's readable."""
    if raw is not None:
        try:
            return max(1, int(raw.strip()))
        except ValueError:
            pass
    return _DEFAULT_RETRY_AFTER_SECONDS
