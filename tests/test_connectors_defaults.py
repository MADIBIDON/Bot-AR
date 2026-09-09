from __future__ import annotations

import pytest

from connectors.defaults import (
    build_default_registry,
    find_merchant_for_domain,
    supported_domains_summary,
)
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorNotRegisteredError
from connectors.shopify import ShopifyConnector
from connectors.woocommerce import WooCommerceConnector


def test_registry_has_fakestore() -> None:
    registry = build_default_registry()
    assert isinstance(registry.get("FakeStore"), FakeStoreConnector)


def test_registry_has_kairyu_shopify_connector() -> None:
    registry = build_default_registry()
    connector = registry.get("Kairyu")
    assert isinstance(connector, ShopifyConnector)
    assert connector._shop_domain == "kairyu.fr"


def test_registry_has_relictcg_shopify_connector() -> None:
    registry = build_default_registry()
    connector = registry.get("RelicTCG")
    assert isinstance(connector, ShopifyConnector)


def test_registry_has_fuji_store_woocommerce_connector() -> None:
    registry = build_default_registry()
    connector = registry.get("Fuji Store")
    assert isinstance(connector, WooCommerceConnector)
    assert connector._shop_domain == "fuji-store.fr"


def test_registry_has_no_unexpected_merchant() -> None:
    registry = build_default_registry()
    with pytest.raises(ConnectorNotRegisteredError):
        registry.get("SomeOtherShop")


def test_two_calls_produce_independent_registries() -> None:
    a = build_default_registry()
    b = build_default_registry()
    assert a is not b
    assert a.get("FakeStore") is not b.get("FakeStore")


def test_find_merchant_for_domain_matches_exact_hostname() -> None:
    merchant = find_merchant_for_domain("kairyu.fr")
    assert merchant is not None
    assert merchant.name == "Kairyu"


def test_find_merchant_for_domain_matches_www_prefix() -> None:
    merchant = find_merchant_for_domain("www.relictcg.com")
    assert merchant is not None
    assert merchant.name == "RelicTCG"


def test_find_merchant_for_domain_is_case_insensitive() -> None:
    merchant = find_merchant_for_domain("KAIRYU.FR")
    assert merchant is not None
    assert merchant.name == "Kairyu"


def test_find_merchant_for_domain_returns_none_for_unsupported() -> None:
    assert find_merchant_for_domain("some-random-shop.example") is None


def test_supported_domains_summary_lists_all_merchants() -> None:
    summary = supported_domains_summary()
    assert "Kairyu" in summary
    assert "RelicTCG" in summary
    assert "Fuji Store" in summary
