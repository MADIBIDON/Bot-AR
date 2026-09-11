"""WooCommerce discovery — the same public, unauthenticated Store API
(`/wp-json/wc/store/v1/products`) already used by
purchase/merchants/fuji_store.py, just called with `search=` instead of
`slug=`. No new API surface, no scraping.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from connectors.base import ConnectorProduct
from discovery.base import DiscoveryError, RetailDiscoverySource

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+public catalog search; no auto-purchase)"

_STORE_API = "wp-json/wc/store/v1/products"


class WooCommerceDiscoverySource(RetailDiscoverySource):
    def __init__(
        self,
        *,
        shop_domain: str,
        merchant_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = f"https://{shop_domain}/{_STORE_API}"
        self._merchant_name = merchant_name
        self._timeout = timeout

    def search(
        self, query: str, *, ean: str | None = None, mpn: str | None = None, limit: int = 10
    ) -> list[ConnectorProduct]:
        try:
            response = httpx.get(
                self._base_url,
                params={"search": query, "per_page": str(limit)},
                headers={"User-Agent": DEFAULT_USER_AGENT},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise DiscoveryError(f"{self._merchant_name}: timeout searching catalog") from exc
        except httpx.RequestError as exc:
            raise DiscoveryError(
                f"{self._merchant_name}: network error searching catalog: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise DiscoveryError(
                f"{self._merchant_name}: catalog search returned HTTP {response.status_code}"
            )
        try:
            items = response.json()
        except ValueError as exc:
            raise DiscoveryError(
                f"{self._merchant_name}: catalog search returned non-JSON"
            ) from exc

        products: list[ConnectorProduct] = []
        for item in items:
            candidate = self._to_connector_product(item)
            if candidate is not None:
                products.append(candidate)
        return products

    def _to_connector_product(self, item: dict) -> ConnectorProduct | None:
        slug = item.get("slug")
        name = item.get("name")
        permalink = item.get("permalink")
        prices = item.get("prices") or {}
        price = prices.get("price")
        minor_unit = prices.get("currency_minor_unit")
        currency = prices.get("currency_code")
        if not (
            slug
            and name
            and permalink
            and price is not None
            and minor_unit is not None
            and currency
        ):
            return None
        return ConnectorProduct(
            external_id=slug,
            name=name,
            price=Decimal(str(price)) / (Decimal(10) ** minor_unit),
            currency=currency,
            available=bool(item.get("is_in_stock", item.get("is_purchasable", False))),
            seller=self._merchant_name,
            url=permalink,
            ean=None,  # WooCommerce Store API doesn't expose GTIN/EAN here
            mpn=item.get("sku") or None,
        )
