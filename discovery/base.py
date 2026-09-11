"""Multi-merchant product discovery — deliberately separate from
connectors/base.py's BaseConnector (fetch ONE known listing by
external_id): discovery answers "does this merchant sell something
matching this product at all", a query-based question, before any
Listing/WatchRule exists. Once discovery finds a candidate, ongoing
monitoring goes right back through the existing per-merchant
RetailConnector — this module never re-implements price/stock fetching.

No CAPTCHA bypass, no Cloudflare bypass, no aggressive scraping, no
external search engine: every RetailDiscoverySource implementation must
use a merchant's own public, documented search/catalog mechanism (the
same posture as connectors/schema_org.py and market_data/ebay.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from connectors.base import ConnectorProduct


class DiscoveryError(Exception):
    """Generic discovery-source failure — never crashes the caller; a
    failure for one merchant must never stop discovery on the others."""


class DiscoveryUnavailableError(DiscoveryError):
    """This merchant has no clean, public way to search its catalog (or
    the discovery-specific configuration, e.g. a UCP agent profile URL,
    isn't set). Maps to the DISCOVERY_UNAVAILABLE outcome — not an error
    to alarm over, just "can't check this merchant yet"."""


class RetailDiscoverySource(ABC):
    @abstractmethod
    def search(
        self, query: str, *, ean: str | None = None, mpn: str | None = None, limit: int = 10
    ) -> list[ConnectorProduct]:
        """Free-text (optionally identifier-assisted) search for
        candidate products. Raises DiscoveryUnavailableError if this
        merchant/configuration can't search at all, DiscoveryError for
        any other failure (network, malformed response). Never returns a
        fabricated result — an empty list means "nothing found", not
        "unavailable"."""
        raise NotImplementedError
