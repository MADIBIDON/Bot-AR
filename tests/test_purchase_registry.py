from __future__ import annotations

import pytest

from purchase.base import AutomatedCheckoutUnsupportedError
from purchase.defaults import build_default_purchase_registry
from purchase.merchants.unsupported import UnsupportedPurchaseConnector
from purchase.registry import PurchaseConnectorNotRegisteredError, PurchaseConnectorRegistry


def test_get_unregistered_merchant_raises() -> None:
    registry = PurchaseConnectorRegistry()

    with pytest.raises(PurchaseConnectorNotRegisteredError, match="Kairyu"):
        registry.get("Kairyu")


def test_register_and_get_round_trips() -> None:
    registry = PurchaseConnectorRegistry()
    connector = UnsupportedPurchaseConnector(merchant_name="Kairyu")

    registry.register("Kairyu", connector)

    assert registry.get("Kairyu") is connector
    assert registry.names() == ["Kairyu"]


def test_unsupported_connector_raises_on_revalidate() -> None:
    connector = UnsupportedPurchaseConnector(merchant_name="Kairyu")

    with pytest.raises(AutomatedCheckoutUnsupportedError, match="Kairyu"):
        connector.revalidate(intent=None)  # type: ignore[arg-type]


def test_unsupported_connector_raises_on_checkout() -> None:
    connector = UnsupportedPurchaseConnector(merchant_name="Kairyu")

    with pytest.raises(AutomatedCheckoutUnsupportedError, match="Kairyu"):
        connector.checkout(intent=None, revalidated=None)  # type: ignore[arg-type]


def test_default_registry_covers_every_retail_merchant() -> None:
    from connectors.defaults import MERCHANTS

    registry = build_default_purchase_registry()

    for merchant in MERCHANTS:
        connector = registry.get(merchant.name)
        assert isinstance(connector, UnsupportedPurchaseConnector)
