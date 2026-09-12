"""eBay Browse API market-data source.

Official, documented, OAuth2 client-credentials API — not scraping. eBay's
public HTML search pages (sold and active) both actively block plain HTTP
requests (verified: 403 on a bare GET, no anti-bot bypass attempted), so
this is the only clean path onto eBay data for this project.

Limitation, stated plainly rather than hidden: the free/instant Browse
API tier returns ACTIVE listings (asking prices), not confirmed sold
transactions — eBay's sold-item data (Marketplace Insights API) requires
a separate application review this project doesn't have. Every
MarketObservation from this source has `sold=None` (unknown, not
fabricated as False), and the estimator is expected to weight that as
lower-confidence "asking price" data, not a market-clearing price.

Needs a free eBay Developer account (App ID / Cert ID) in .env — see
EBAY_APP_ID / EBAY_CERT_ID. Never bypasses anti-bot protection or scrapes.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from market_data import ebay_status
from market_data.base import MarketDataError, MarketDataSource
from market_data.models import MarketObservation

DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+read-only market data; no auto-purchase)"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MARKETPLACE_ID = "EBAY_FR"

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"


class MissingEbayConfigError(Exception):
    """Raised when EBAY_APP_ID/EBAY_CERT_ID are not set. Never includes a
    secret value — only which variable is missing."""


@dataclass(frozen=True, slots=True)
class EbayConfig:
    app_id: str
    cert_id: str
    marketplace_id: str = DEFAULT_MARKETPLACE_ID


def load_ebay_config() -> EbayConfig:
    app_id = os.environ.get("EBAY_APP_ID", "").strip()
    cert_id = os.environ.get("EBAY_CERT_ID", "").strip()
    required = (("EBAY_APP_ID", app_id), ("EBAY_CERT_ID", cert_id))
    missing = [name for name, value in required if not value]
    if missing:
        raise MissingEbayConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Create a free eBay Developer account and app at "
            "https://developer.ebay.com/my/keys, then set them in .env."
        )
    marketplace_id = os.environ.get("EBAY_MARKETPLACE_ID", DEFAULT_MARKETPLACE_ID).strip()
    return EbayConfig(app_id=app_id, cert_id=cert_id, marketplace_id=marketplace_id)


class EbayMarketDataSource(MarketDataSource):
    def __init__(
        self,
        *,
        config: EbayConfig,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._config = config
        self._timeout = timeout
        self._user_agent = user_agent
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    def search(self, query: str, *, limit: int = 20) -> list[MarketObservation]:
        token = self._get_access_token()
        try:
            response = httpx.get(
                _SEARCH_URL,
                params={"q": query, "limit": str(limit)},
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": self._config.marketplace_id,
                    "User-Agent": self._user_agent,
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise MarketDataError(f"timeout searching eBay for {query!r}") from exc
        except httpx.RequestError as exc:
            raise MarketDataError(f"network error searching eBay for {query!r}: {exc}") from exc

        if response.status_code == 429:
            raise MarketDataError("rate limited (429) by eBay Browse API")
        if response.status_code >= 400:
            raise MarketDataError(f"HTTP {response.status_code} from eBay Browse API")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataError("eBay Browse API returned a non-JSON response") from exc

        return [
            observation
            for item in payload.get("itemSummaries", [])
            if (observation := self._parse_item(item)) is not None
        ]

    def _get_access_token(self) -> str:
        now = datetime.now(UTC)
        if self._access_token is not None and self._token_expires_at is not None:
            if now < self._token_expires_at:
                return self._access_token

        try:
            token, expires_in = self._request_new_token()
        except MarketDataError as exc:
            ebay_status.record_failure(str(exc))
            raise
        ebay_status.record_success()

        self._access_token = token
        # Refresh a little early rather than exactly at expiry.
        self._token_expires_at = now + timedelta(seconds=max(int(expires_in) - 60, 0))
        return token

    def _request_new_token(self) -> tuple[str, int]:
        credentials = base64.b64encode(
            f"{self._config.app_id}:{self._config.cert_id}".encode()
        ).decode()
        try:
            response = httpx.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials", "scope": _OAUTH_SCOPE},
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise MarketDataError("timeout obtaining eBay OAuth token") from exc
        except httpx.RequestError as exc:
            raise MarketDataError(f"network error obtaining eBay OAuth token: {exc}") from exc

        if response.status_code >= 400:
            # Never echoes the response body — it can include request
            # details tied to the credentials.
            raise MarketDataError(f"HTTP {response.status_code} obtaining eBay OAuth token")

        payload = response.json()
        token = payload.get("access_token")
        expires_in = payload.get("expires_in", 0)
        if not isinstance(token, str) or not token:
            raise MarketDataError("eBay OAuth response had no access_token")
        return token, expires_in

    def _parse_item(self, item: dict) -> MarketObservation | None:
        price_data = item.get("price")
        if not isinstance(price_data, dict):
            return None
        try:
            price = Decimal(str(price_data["value"]))
        except (KeyError, InvalidOperation, TypeError):
            return None
        currency = price_data.get("currency")
        if not isinstance(currency, str) or not currency:
            return None

        title = item.get("title")
        item_id = item.get("itemId")
        url = item.get("itemWebUrl")
        if not title or not item_id or not url:
            return None

        shipping_price = None
        shipping_options = item.get("shippingOptions")
        if isinstance(shipping_options, list) and shipping_options:
            shipping_cost = shipping_options[0].get("shippingCost")
            if isinstance(shipping_cost, dict) and shipping_cost.get("value") is not None:
                try:
                    shipping_price = Decimal(str(shipping_cost["value"]))
                except InvalidOperation:
                    shipping_price = None

        condition = item.get("condition")
        condition = condition if isinstance(condition, str) else None

        return MarketObservation(
            source="ebay",
            product_name=str(title),
            price=price,
            currency=currency.strip().upper(),
            listing_url=str(url),
            external_id=str(item_id),
            observed_at=datetime.now(UTC),
            condition=condition,
            sold=None,  # Browse API: active listings only, never fabricated as sold.
            sold_at=None,
            shipping_price=shipping_price,
        )
