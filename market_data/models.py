"""Normalized market-data observation — the market-side analog of
ConnectorProduct (connectors/base.py). A single search result from a
market-data source, before any product-matching or estimation happens.

Never invent an absent field — None means the source didn't provide it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarketObservation:
    source: str
    product_name: str
    price: Decimal
    currency: str
    listing_url: str
    external_id: str
    observed_at: datetime
    condition: str | None = None
    sold: bool | None = None
    sold_at: datetime | None = None
    shipping_price: Decimal | None = None
