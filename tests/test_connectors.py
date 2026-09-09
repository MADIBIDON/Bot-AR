from __future__ import annotations

import pytest

from connectors.base import BaseConnector, ConnectorError, ConnectorProduct, ProductNotFoundError
from connectors.fake_store import FakeStoreConnector


def _sample_products() -> dict[str, dict[str, object]]:
    return {
        "fake-123": {
            "name": "Duopack Evoli 30 ans",
            "price": 13.99,
            "currency": "EUR",
            "available": True,
            "seller": "FakeStore",
            "url": "https://fake-store.example/p/fake-123",
            "ean": "1234567890123",
        },
        "fake-456": {
            "name": "ETB Pokemon 30 ans",
            "price": 59.99,
            "currency": "EUR",
            "available": False,
            "seller": "FakeStore Marketplace",
            "url": "https://fake-store.example/p/fake-456",
        },
    }


def test_base_connector_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        BaseConnector()  # type: ignore[abstract]


def test_fake_store_connector_implements_base_connector_interface() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert isinstance(connector, BaseConnector)


def test_fake_store_connector_instantiates_empty() -> None:
    connector = FakeStoreConnector()
    assert isinstance(connector, BaseConnector)


def test_get_existing_product() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    product = connector.get_product("fake-123")
    assert isinstance(product, ConnectorProduct)
    assert product.name == "Duopack Evoli 30 ans"


def test_get_missing_product_raises_product_not_found() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    with pytest.raises(ProductNotFoundError):
        connector.get_product("does-not-exist")


def test_product_available() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-123").available is True


def test_product_unavailable() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-456").available is False


def test_product_price() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-123").price == 13.99


def test_product_currency() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-123").currency == "EUR"


def test_product_seller() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-456").seller == "FakeStore Marketplace"


def test_product_with_ean() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-123").ean == "1234567890123"


def test_product_without_ean_defaults_to_none() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-456").ean is None


def test_product_external_id_matches_key() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-456").external_id == "fake-456"


def test_product_url() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-123").url == "https://fake-store.example/p/fake-123"


def test_product_mpn_when_present() -> None:
    connector = FakeStoreConnector(
        products={
            "fake-mpn": {
                "name": "Item with MPN",
                "price": 9.99,
                "available": True,
                "seller": "FakeStore",
                "url": "https://fake-store.example/p/fake-mpn",
                "mpn": "MPN-001",
            }
        }
    )
    assert connector.get_product("fake-mpn").mpn == "MPN-001"


def test_product_without_mpn_defaults_to_none() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    assert connector.get_product("fake-123").mpn is None


def test_update_product_simulates_price_change() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    connector.update_product("fake-123", price=12.49)
    assert connector.get_product("fake-123").price == 12.49


def test_update_product_simulates_stock_change() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    connector.update_product("fake-456", available=True)
    assert connector.get_product("fake-456").available is True


def test_update_product_simulates_seller_change() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    connector.update_product("fake-123", seller="New Seller")
    assert connector.get_product("fake-123").seller == "New Seller"


def test_update_missing_product_raises_product_not_found() -> None:
    connector = FakeStoreConnector(products=_sample_products())
    with pytest.raises(ProductNotFoundError):
        connector.update_product("does-not-exist", price=1.0)


def test_simulated_merchant_error() -> None:
    connector = FakeStoreConnector(
        products=_sample_products(),
        errors={"fake-down": ConnectorError("merchant is down")},
    )
    with pytest.raises(ConnectorError):
        connector.get_product("fake-down")


def test_product_missing_required_field_raises_value_error() -> None:
    with pytest.raises(ValueError, match="missing required field"):
        FakeStoreConnector(products={"broken": {"name": "Incomplete"}})
