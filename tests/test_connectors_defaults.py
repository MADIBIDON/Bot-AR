from __future__ import annotations

from connectors.defaults import build_default_registry
from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorNotRegisteredError
from connectors.shopify import ShopifyConnector


def test_registry_has_fakestore() -> None:
    registry = build_default_registry()
    assert isinstance(registry.get("FakeStore"), FakeStoreConnector)


def test_registry_has_kairyu_shopify_connector() -> None:
    registry = build_default_registry()
    connector = registry.get("Kairyu")
    assert isinstance(connector, ShopifyConnector)
    assert connector._shop_domain == "kairyu.fr"


def test_registry_has_no_unexpected_merchant() -> None:
    registry = build_default_registry()
    try:
        registry.get("SomeOtherShop")
    except ConnectorNotRegisteredError:
        pass
    else:
        raise AssertionError("expected ConnectorNotRegisteredError")


def test_two_calls_produce_independent_registries() -> None:
    a = build_default_registry()
    b = build_default_registry()
    assert a is not b
    assert a.get("FakeStore") is not b.get("FakeStore")
