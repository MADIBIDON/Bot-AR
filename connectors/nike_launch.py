"""Minimal, single-purpose connector for ONE Nike SNKRS launch page —
Phase 30's scheduled-release test case. NOT a sneaker platform: this
connector only ever answers for the one launch it was constructed with.

Real, public data: Nike's SNKRS launch pages (nike.com/<locale>/launch/t/
<slug>) are server-rendered Next.js pages that embed the full page state
as JSON in a `<script id="__NEXT_DATA__">` block — the same kind of
embedded structured data this project already reads via schema.org
JSON-LD elsewhere (connectors/schema_org.py), just a different, equally
public envelope. A plain GET with an honest User-Agent returns it in
full; no login, no app, no bypass of anything.

Confirmed live 2026-09-13 for the "Nike SB Air Force 1 x Yuto" launch
(https://www.nike.com/fr/launch/t/nike-sb-air-force-1-yuto-light-bone-and-iron-grey):
the embedded state carries a real `commerceStartDate` ("2026-09-15T07:00:00Z")
per product id under `product.products.data.items` — i.e. a genuine,
Nike-published UPCOMING/AVAILABLE boundary, not a guess. availability is
derived by comparing wall-clock time to that timestamp: before it, the
product cannot be bought yet (available=False — the same shape the rest
of this codebase already treats as "out of stock" and knows how to alert
a transition out of); at/after it, available=True. If Nike reshapes this
page around the actual release, get_product() raises ConnectorError (a
real, honest connector failure, logged and never treated as a false
stock signal) rather than guessing.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx

from connectors.base import BaseConnector, ConnectorError, ConnectorProduct, ProductNotFoundError

DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+read-only price monitor; no auto-purchase)"
DEFAULT_TIMEOUT_SECONDS = 8.0

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class NikeLaunchConnector(BaseConnector):
    def __init__(
        self,
        *,
        launch_url: str,
        style_color: str,
        product_name: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._launch_url = launch_url
        self._style_color = style_color
        self._product_name = product_name
        self._client = httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    def get_product(self, external_id: str) -> ConnectorProduct:
        try:
            response = self._client.get(self._launch_url)
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"timeout fetching {self._launch_url}") from exc
        except httpx.RequestError as exc:
            raise ConnectorError(f"network error fetching {self._launch_url}: {exc}") from exc

        if response.status_code == 404:
            raise ProductNotFoundError(f"launch page not found: {self._launch_url}")
        if response.status_code >= 400:
            raise ConnectorError(f"HTTP {response.status_code} fetching {self._launch_url}")

        item = _find_launch_item(response.text, self._style_color)
        if item is None:
            raise ConnectorError(
                f"could not locate launch item {self._style_color!r} on {self._launch_url} "
                "(page structure may have changed)"
            )

        commerce_start_raw = item.get("commerceStartDate")
        if not isinstance(commerce_start_raw, str):
            raise ConnectorError(f"no commerceStartDate on {self._launch_url}")
        try:
            commerce_start = datetime.fromisoformat(commerce_start_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConnectorError(f"unparseable commerceStartDate {commerce_start_raw!r}") from exc

        available = datetime.now(UTC) >= commerce_start

        return ConnectorProduct(
            external_id=external_id,
            name=self._product_name,
            price=_parse_price(item.get("currentPrice") or item.get("fullPrice")),
            currency=str(item.get("currency") or "EUR"),
            available=available,
            seller="Nike SNKRS",
            url=self._launch_url,
            ean=None,
            mpn=self._style_color,
        )


def _find_launch_item(html: str, style_color: str) -> dict | None:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        return None
    try:
        next_data = json.loads(match.group(1))
        initial_state = json.loads(next_data["props"]["pageProps"]["initialState"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None

    items = initial_state.get("product", {}).get("products", {}).get("data", {}).get("items", {})
    for item in items.values():
        if isinstance(item, dict) and item.get("styleColor") == style_color:
            return item
    return None


def _parse_price(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")
