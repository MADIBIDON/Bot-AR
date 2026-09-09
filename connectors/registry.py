"""Associates a merchant name with the connector instance that serves it.

Avoids scattering `if merchant.name == "X"` checks through the codebase. A
plain in-memory mapping — no plugin system, no auto-discovery.
"""

from __future__ import annotations

from connectors.base import BaseConnector, ConnectorError


class ConnectorNotRegisteredError(ConnectorError):
    """Raised when no connector has been registered for a merchant name."""


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, BaseConnector] = {}

    def register(self, merchant_name: str, connector: BaseConnector) -> None:
        self._connectors[merchant_name] = connector

    def get(self, merchant_name: str) -> BaseConnector:
        try:
            return self._connectors[merchant_name]
        except KeyError:
            raise ConnectorNotRegisteredError(
                f"No connector registered for merchant {merchant_name!r}"
            ) from None
