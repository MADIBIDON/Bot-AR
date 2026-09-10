"""Common market-data source abstraction.

Deliberately not shaped like connectors.base.BaseConnector: a merchant
connector fetches ONE known listing by external_id, but a market-data
source answers "search for comparable sold/asking prices" — a query-based
question, not an id-based one. Reusing BaseConnector's shape here would
force-fit a different problem, which is exactly what was asked not to do.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from market_data.models import MarketObservation


class MarketDataError(Exception):
    """Generic market-data source failure: network, auth, rate limit, or
    a response the source's own format changed under us. Never lets a
    caller crash — engine/worker.py-style callers catch this and continue
    with the other rules."""


class MarketDataSource(ABC):
    @abstractmethod
    def search(self, query: str, *, limit: int = 20) -> list[MarketObservation]:
        """Free-text search for comparable listings. Raises
        MarketDataError on any failure — never returns a fabricated
        result."""
        raise NotImplementedError
