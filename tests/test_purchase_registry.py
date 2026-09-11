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
        registry.get(merchant.name)  # must not raise for any registered merchant


def test_default_registry_wires_fuji_store_to_real_connector() -> None:
    from purchase.merchants.fuji_store import FujiStorePurchaseConnector

    registry = build_default_purchase_registry()

    assert isinstance(registry.get("Fuji Store"), FujiStorePurchaseConnector)


def test_default_registry_wires_kairyu_and_relictcg_to_ucp_without_any_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 23: config.settings.get_ucp_agent_profile_url() falls back to
    this project's own hosted default when the env var isn't set, so UCP
    works out of the box — no .env edit required."""
    from purchase.merchants.shopify_ucp import ShopifyUCPPurchaseConnector

    monkeypatch.delenv("PURCHASE_UCP_AGENT_PROFILE_URL", raising=False)

    registry = build_default_purchase_registry()

    assert isinstance(registry.get("Kairyu"), ShopifyUCPPurchaseConnector)
    assert isinstance(registry.get("RelicTCG"), ShopifyUCPPurchaseConnector)


def test_default_registry_wires_kairyu_and_relictcg_to_ucp_when_profile_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from purchase.merchants.shopify_ucp import ShopifyUCPPurchaseConnector

    monkeypatch.setenv(
        "PURCHASE_UCP_AGENT_PROFILE_URL", "https://ucp-profile.vercel.app/agent-profile.json"
    )

    registry = build_default_purchase_registry()

    assert isinstance(registry.get("Kairyu"), ShopifyUCPPurchaseConnector)
    assert isinstance(registry.get("RelicTCG"), ShopifyUCPPurchaseConnector)
