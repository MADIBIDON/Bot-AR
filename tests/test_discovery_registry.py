from __future__ import annotations

import pytest

from discovery.defaults import build_default_discovery_registry
from discovery.registry import DiscoveryRegistry, DiscoverySourceNotRegisteredError
from discovery.shopify_ucp import ShopifyUCPDiscoverySource
from discovery.woocommerce import WooCommerceDiscoverySource


def test_get_unregistered_raises() -> None:
    registry = DiscoveryRegistry()

    with pytest.raises(DiscoverySourceNotRegisteredError, match="Kairyu"):
        registry.get("Kairyu")


def test_register_and_get_round_trips() -> None:
    registry = DiscoveryRegistry()
    source = WooCommerceDiscoverySource(shop_domain="fuji-store.fr", merchant_name="Fuji Store")

    registry.register("Fuji Store", source)

    assert registry.get("Fuji Store") is source
    assert registry.names() == ["Fuji Store"]


def test_default_registry_always_has_fuji_store() -> None:
    registry = build_default_discovery_registry()

    assert isinstance(registry.get("Fuji Store"), WooCommerceDiscoverySource)


def test_default_registry_kairyu_relictcg_registered_without_any_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 23: config.settings.get_ucp_agent_profile_url() falls back to
    this project's own hosted default when the env var isn't set, so
    discovery for Kairyu/RelicTCG works out of the box."""
    monkeypatch.delenv("PURCHASE_UCP_AGENT_PROFILE_URL", raising=False)

    registry = build_default_discovery_registry()

    assert isinstance(registry.get("Kairyu"), ShopifyUCPDiscoverySource)
    assert isinstance(registry.get("RelicTCG"), ShopifyUCPDiscoverySource)


def test_default_registry_kairyu_relictcg_registered_with_profile_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PURCHASE_UCP_AGENT_PROFILE_URL", "https://ucp-profile.vercel.app/agent-profile.json"
    )

    registry = build_default_discovery_registry()

    assert isinstance(registry.get("Kairyu"), ShopifyUCPDiscoverySource)
    assert isinstance(registry.get("RelicTCG"), ShopifyUCPDiscoverySource)
