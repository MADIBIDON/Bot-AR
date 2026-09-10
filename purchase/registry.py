"""Maps a merchant name to its PurchaseConnector — same pattern as
connectors/registry.py and market_data/registry.py, for the same reason:
avoid scattering `if merchant == "Kairyu"` checks through the codebase.
"""

from __future__ import annotations

from purchase.base import PurchaseConnector, PurchaseError


class PurchaseConnectorNotRegisteredError(PurchaseError):
    """Raised when no purchase connector has been registered for a merchant."""


class PurchaseConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, PurchaseConnector] = {}

    def register(self, merchant_name: str, connector: PurchaseConnector) -> None:
        self._connectors[merchant_name] = connector

    def get(self, merchant_name: str) -> PurchaseConnector:
        try:
            return self._connectors[merchant_name]
        except KeyError:
            raise PurchaseConnectorNotRegisteredError(
                f"No purchase connector registered for {merchant_name!r}. "
                f"Known merchants: {', '.join(sorted(self._connectors)) or 'none'}."
            ) from None

    def names(self) -> list[str]:
        return sorted(self._connectors)
