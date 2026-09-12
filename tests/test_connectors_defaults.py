from __future__ import annotations

import pytest

from connectors.defaults import (
    CATALOG_SEARCH,
    MERCHANTS,
    ONLINE_STOCK,
    PRICE,
    UNSUPPORTED_RETAILERS,
    build_default_registry,
    find_merchant_for_domain,
    supported_domains_summary,
)
from connectors.fake_store import FakeStoreConnector
from connectors.generic_schema_org import GenericSchemaOrgConnector
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


# --- Phase 28: new retailers ------------------------------------------


@pytest.mark.parametrize(
    ("merchant_name", "domain", "shop_domain"),
    [
        ("Cultura", "cultura.com", "www.cultura.com"),
        ("JouéClub", "joueclub.fr", "www.joueclub.fr"),
        ("E.Leclerc", "e.leclerc", "www.e.leclerc"),
        ("La Grande Récré", "lagranderecre.fr", "www.lagranderecre.fr"),
    ],
)
def test_registry_has_phase_28_generic_connector(
    merchant_name: str, domain: str, shop_domain: str
) -> None:
    registry = build_default_registry()
    connector = registry.get(merchant_name)
    assert isinstance(connector, GenericSchemaOrgConnector)
    assert connector._shop_domain == shop_domain
    assert find_merchant_for_domain(domain) is not None
    assert find_merchant_for_domain(domain).name == merchant_name


def test_phase_28_merchants_use_full_path_external_id() -> None:
    by_name = {m.name: m for m in MERCHANTS}
    for name in ("Cultura", "JouéClub", "E.Leclerc", "La Grande Récré"):
        assert by_name[name].full_path_external_id is True


def test_existing_merchants_have_catalog_search_new_ones_dont() -> None:
    by_name = {m.name: m for m in MERCHANTS}

    for name in ("Kairyu", "RelicTCG", "Fuji Store"):
        assert CATALOG_SEARCH in by_name[name].capabilities

    for name in ("Cultura", "JouéClub", "E.Leclerc", "La Grande Récré"):
        assert CATALOG_SEARCH not in by_name[name].capabilities
        assert ONLINE_STOCK in by_name[name].capabilities
        assert PRICE in by_name[name].capabilities


def test_unsupported_retailers_are_documented_not_silently_dropped() -> None:
    names = {name for name, _reason in UNSUPPORTED_RETAILERS}
    assert {"Fnac", "King Jouet", "Smyths Toys", "Carrefour", "Micromania", "Amazon"} <= names
    for _name, reason in UNSUPPORTED_RETAILERS:
        assert reason  # every documented blocker has an actual reason, never blank
