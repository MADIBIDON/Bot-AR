from __future__ import annotations

import pytest

from connectors.defaults import (
    CATALOG_SEARCH,
    CLICK_AND_COLLECT,
    LOCAL_STOCK,
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


def test_catalog_search_capability_matches_a_real_working_source() -> None:
    """Phase 28: Cultura/E.Leclerc have no confirmed public catalog
    search at all. Phase 29: JouéClub/La Grande Récré gained one via
    sitemap-based discovery (discovery/sitemap.py) — both run the same
    Proximis/Rbs platform and publish a single, reasonably-sized product
    sitemap; Cultura/E.Leclerc's sitemaps are 75-87 files of 50,000 URLs
    each and were deliberately not wired up for that (see
    discovery/defaults.py)."""
    by_name = {m.name: m for m in MERCHANTS}

    for name in ("Kairyu", "RelicTCG", "Fuji Store", "JouéClub", "La Grande Récré"):
        assert CATALOG_SEARCH in by_name[name].capabilities

    for name in ("Cultura", "E.Leclerc"):
        assert CATALOG_SEARCH not in by_name[name].capabilities
        assert ONLINE_STOCK in by_name[name].capabilities
        assert PRICE in by_name[name].capabilities


def test_unsupported_retailers_are_documented_not_silently_dropped() -> None:
    names = {name for name, _reason in UNSUPPORTED_RETAILERS}
    # Phase 39: King Jouet moved OUT of this list — their product PAGE
    # stays DataDome-blocked, but a distinct, genuinely public JSON
    # endpoint was found and verified live (see connectors/king_jouet.py)
    # — it's a real MerchantDefinition now, not fully unsupported.
    assert {"Fnac", "Smyths Toys", "Carrefour", "Micromania", "Amazon"} <= names
    assert "King Jouet" not in names
    for _name, reason in UNSUPPORTED_RETAILERS:
        assert reason  # every documented blocker has an actual reason, never blank


def test_local_stock_capability_matches_a_real_confirmed_platform() -> None:
    """Phase 29: only JouéClub and La Grande Récré were confirmed on the
    Rbs/Proximis platform this session (real store-locator + per-SKU
    store-stock endpoints, see local_stock/rbs_platform.py) — every
    other retailer, supported or not, has none of these capabilities."""
    by_name = {m.name: m for m in MERCHANTS}

    for name in ("JouéClub", "La Grande Récré"):
        assert LOCAL_STOCK in by_name[name].capabilities
        assert CLICK_AND_COLLECT in by_name[name].capabilities

    for name in ("Kairyu", "RelicTCG", "Fuji Store", "Cultura", "E.Leclerc"):
        assert LOCAL_STOCK not in by_name[name].capabilities
        assert CLICK_AND_COLLECT not in by_name[name].capabilities
