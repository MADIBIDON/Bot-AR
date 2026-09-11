"""Maps a merchant name to its RetailDiscoverySource — same pattern as
connectors/registry.py, market_data/registry.py, and purchase/registry.py.
"""

from __future__ import annotations

from discovery.base import DiscoveryError, RetailDiscoverySource


class DiscoverySourceNotRegisteredError(DiscoveryError):
    """Raised when no discovery source has been registered for a merchant."""


class DiscoveryRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, RetailDiscoverySource] = {}

    def register(self, merchant_name: str, source: RetailDiscoverySource) -> None:
        self._sources[merchant_name] = source

    def get(self, merchant_name: str) -> RetailDiscoverySource:
        try:
            return self._sources[merchant_name]
        except KeyError:
            raise DiscoverySourceNotRegisteredError(
                f"No discovery source registered for {merchant_name!r}."
            ) from None

    def names(self) -> list[str]:
        return sorted(self._sources)
