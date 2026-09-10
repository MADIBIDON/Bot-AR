"""Maps a market_source name to its MarketDataSource instance — same
pattern as connectors/registry.py, for the same reason: avoid scattering
`if market_source == "ebay"` checks through the codebase.
"""

from __future__ import annotations

from market_data.base import MarketDataError, MarketDataSource


class MarketDataSourceNotRegisteredError(MarketDataError):
    """Raised when no market-data source has been registered for a name."""


class MarketDataRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, MarketDataSource] = {}

    def register(self, name: str, source: MarketDataSource) -> None:
        self._sources[name] = source

    def get(self, name: str) -> MarketDataSource:
        try:
            return self._sources[name]
        except KeyError:
            raise MarketDataSourceNotRegisteredError(
                f"No market-data source registered for {name!r}. "
                f"Known sources: {', '.join(sorted(self._sources)) or 'none'}."
            ) from None

    def names(self) -> list[str]:
        return sorted(self._sources)
