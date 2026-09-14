"""Shopify UCP discovery — reuses the same `search_catalog` capability
already confirmed live for purchase/merchants/shopify_ucp.py (Phase 21),
not a second, redundant implementation. One generic class for Kairyu,
RelicTCG, and any future Shopify UCP-compatible merchant.

Each result's `external_id` is the product handle — exactly what
connectors/shopify.py's ShopifyConnector.get_product() already expects,
so a discovered candidate plugs straight into the existing monitoring
pipeline with no new connector logic.
"""

from __future__ import annotations

from decimal import Decimal

from connectors.base import ConnectorProduct
from discovery.base import DiscoveryError, DiscoveryUnavailableError, RetailDiscoverySource
from ucp.client import (
    DEFAULT_TIMEOUT_SECONDS,
    UCPError,
    UCPServiceUnavailableError,
    call_tool,
    discover,
)


def _minor_to_decimal(amount: int) -> Decimal:
    return Decimal(amount) / 100


class ShopifyUCPDiscoverySource(RetailDiscoverySource):
    def __init__(
        self,
        *,
        shop_domain: str,
        merchant_name: str,
        agent_profile_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._shop_domain = shop_domain
        self._merchant_name = merchant_name
        self._agent_profile_url = agent_profile_url
        self._timeout = timeout

    def search(
        self, query: str, *, ean: str | None = None, mpn: str | None = None, limit: int = 10
    ) -> list[ConnectorProduct]:
        if not self._agent_profile_url:
            raise DiscoveryUnavailableError(
                f"{self._merchant_name}: PURCHASE_UCP_AGENT_PROFILE_URL is not configured."
            )
        try:
            endpoint = discover(self._shop_domain, timeout=self._timeout).mcp_endpoint
        except UCPServiceUnavailableError as exc:
            raise DiscoveryUnavailableError(f"{self._merchant_name}: {exc}") from exc
        except UCPError as exc:
            # Phase 33 fix: a transient network/timeout failure fetching
            # the UCP profile (found live — httpcore.ReadError, "Connection
            # reset by peer", recurring every few hours) raises the base
            # UCPError, not UCPServiceUnavailableError — left uncaught
            # here, it fell through to app/discovery.py's catch-all
            # `except Exception`, logged as an alarming "crashed" ERROR
            # instead of the same routine WARNING a search_catalog network
            # blip already gets below. Never crashed the worker or blocked
            # other merchants (per-merchant isolation already worked) —
            # this only fixes the log severity/classification.
            raise DiscoveryError(f"{self._merchant_name}: {exc}") from exc

        try:
            result = call_tool(
                endpoint,
                "search_catalog",
                {"catalog": {"query": query}},
                agent_profile_url=self._agent_profile_url,
                timeout=self._timeout,
            )
        except UCPError as exc:
            raise DiscoveryError(f"{self._merchant_name} UCP search failed: {exc}") from exc

        products: list[ConnectorProduct] = []
        for item in result.get("products", [])[:limit]:
            candidate = self._to_connector_product(item)
            if candidate is not None:
                products.append(candidate)
        return products

    def _to_connector_product(self, item: dict) -> ConnectorProduct | None:
        handle = item.get("handle")
        title = item.get("title")
        url = item.get("url")
        price_range = item.get("price_range") or {}
        min_price = (price_range.get("min") or {}).get("amount")
        currency = (price_range.get("min") or {}).get("currency")
        if not (handle and title and url and min_price is not None and currency):
            return None

        variants = item.get("variants") or []
        available = any(v.get("availability", {}).get("available") for v in variants)
        mpn = None
        if len(variants) == 1:
            mpn = variants[0].get("sku") or None

        return ConnectorProduct(
            external_id=handle,
            name=title,
            price=_minor_to_decimal(min_price),
            currency=currency,
            available=available,
            seller=self._merchant_name,
            url=url,
            ean=None,  # not exposed by UCP catalog.search — never invented
            mpn=mpn,
        )
