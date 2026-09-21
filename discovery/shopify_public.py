"""Keyword discovery for any Shopify storefront, via the shop's own
public predictive-search endpoint.

    GET https://<shop>/search/suggest.json
        ?q=<query>&resources[type]=product&resources[limit]=<n>

This is the same endpoint a Shopify theme's own search box calls while
you type: public, unauthenticated, no key, no cookie, and returning the
shop's own catalogue. Confirmed live against hikarudistribution.com,
which answers with title / handle / url / price / available / image for
each hit.

Distinct from discovery/shopify_ucp.py, which speaks the UCP/MCP agent
protocol — only a handful of shops implement that, while every Shopify
storefront exposes this one. Where a shop supports both, UCP is
preferred (richer, purpose-built for agents) and this is the fallback.

No crawl, no scraping, no bypass: one targeted query per call, exactly
like discovery/cultura.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from connectors.base import ConnectorProduct
from discovery.base import DiscoveryError, DiscoveryUnavailableError, RetailDiscoverySource

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+public catalog search; no auto-purchase)"

_DEFAULT_RETRY_AFTER_SECONDS = 60


def _parse_price(raw: object) -> Decimal | None:
    """Shopify renders this per shop settings, so it can arrive as
    "329.90", "1 299,00" or with a currency symbol attached. Anything
    that does not parse cleanly yields None rather than a wrong number —
    a bad price on a drop alert is worse than a missing one."""
    if isinstance(raw, int | float):
        return Decimal(str(raw))
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace("\xa0", "").replace(" ", "")
    cleaned = "".join(ch for ch in cleaned if ch.isdigit() or ch in ".,-")
    if cleaned.count(",") == 1 and cleaned.count(".") == 0:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


class ShopifyPublicSearchDiscoverySource(RetailDiscoverySource):
    def __init__(
        self,
        *,
        shop_domain: str,
        merchant_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        currency: str = "EUR",
    ) -> None:
        self._shop_domain = shop_domain
        self._merchant_name = merchant_name
        self._currency = currency
        self._client = httpx.Client(
            timeout=timeout, headers={"User-Agent": DEFAULT_USER_AGENT}, follow_redirects=True
        )
        # Same posture as discovery/cultura.py: a real 429 parks this
        # source until its Retry-After has elapsed, rather than being
        # logged and immediately retried.
        self._retry_not_before: datetime | None = None

    def search(
        self, query: str, *, ean: str | None = None, mpn: str | None = None, limit: int = 10
    ) -> list[ConnectorProduct]:
        now = datetime.now(UTC)
        if self._retry_not_before is not None and now < self._retry_not_before:
            raise DiscoveryUnavailableError(
                f"{self._merchant_name}: rate limited, not retrying before "
                f"{self._retry_not_before.isoformat()}"
            )

        try:
            response = self._client.get(
                f"https://{self._shop_domain}/search/suggest.json",
                params={
                    "q": query,
                    "resources[type]": "product",
                    "resources[limit]": max(1, min(limit, 10)),
                },
            )
        except httpx.RequestError as exc:
            raise DiscoveryError(f"{self._merchant_name}: search request failed: {exc}") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            seconds = _DEFAULT_RETRY_AFTER_SECONDS
            if retry_after and retry_after.isdigit():
                seconds = int(retry_after)
            self._retry_not_before = now + timedelta(seconds=seconds)
            raise DiscoveryUnavailableError(
                f"{self._merchant_name}: rate limited (429), pausing {seconds}s"
            )
        if response.status_code == 404:
            raise DiscoveryUnavailableError(
                f"{self._merchant_name}: no public predictive-search endpoint"
            )
        if response.status_code >= 400:
            raise DiscoveryError(
                f"{self._merchant_name}: search returned HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise DiscoveryError(f"{self._merchant_name}: search returned non-JSON") from exc

        items = (
            payload.get("resources", {}).get("results", {}).get("products", [])
            if isinstance(payload, dict)
            else []
        )

        results: list[ConnectorProduct] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            handle = item.get("handle")
            title = item.get("title")
            price = _parse_price(item.get("price"))
            if not handle or not title or price is None:
                # Never fabricate a missing identifier or price.
                continue
            image = item.get("image") or item.get("featured_image")
            results.append(
                ConnectorProduct(
                    external_id=handle,
                    name=title.strip(),
                    price=price,
                    currency=self._currency,
                    available=bool(item.get("available")),
                    seller=self._merchant_name,
                    url=f"https://{self._shop_domain}/products/{handle}",
                    image_url=image
                    if isinstance(image, str) and image.startswith("http")
                    else None,
                )
            )
        return results
