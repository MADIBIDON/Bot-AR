"""No real network calls: WooCommerceConnector._fetch is monkeypatched to
return fixture HTML, or httpx.get is monkeypatched to raise/return a
locally-built httpx.Response.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from connectors.base import ConnectorError, ProductNotFoundError
from connectors.woocommerce import WooCommerceConnector

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _connector_with_fixture(
    monkeypatch: pytest.MonkeyPatch, fixture_name: str
) -> WooCommerceConnector:
    connector = WooCommerceConnector(shop_domain="example-wc-shop.test", merchant_name="Example WC")
    html = _load_fixture(fixture_name)
    monkeypatch.setattr(connector, "_fetch", lambda url: html)
    return connector


def test_product_in_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "woocommerce_in_stock.html")

    product = connector.get_product("alakazam-test-fr")

    assert product.available is True
    assert product.name == "ALAKAZAM TEST - POKEMON FR"
    assert product.price == Decimal("89.90")
    assert product.currency == "EUR"
    assert product.seller == "Example WC"
    assert product.external_id == "alakazam-test-fr"


def test_product_out_of_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "woocommerce_out_of_stock.html")

    product = connector.get_product("charmander-test-fr")

    assert product.available is False
    assert product.price == Decimal("39.90")


def test_url_uses_produit_path_by_default() -> None:
    connector = WooCommerceConnector(shop_domain="example-wc-shop.test", merchant_name="X")
    assert connector._build_url("alakazam-test-fr") == (
        "https://example-wc-shop.test/produit/alakazam-test-fr/"
    )


def test_custom_product_path() -> None:
    connector = WooCommerceConnector(
        shop_domain="example-wc-shop.test", merchant_name="X", product_path="product"
    )
    assert connector._build_url("alakazam-test-fr") == (
        "https://example-wc-shop.test/product/alakazam-test-fr/"
    )


def test_ean_and_mpn_absent_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "woocommerce_in_stock.html")

    product = connector.get_product("alakazam-test-fr")

    assert product.ean is None
    assert product.mpn is None


def test_http_404_raises_product_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = WooCommerceConnector(shop_domain="example-wc-shop.test", merchant_name="X")

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ProductNotFoundError):
        connector.get_product("does-not-exist")


def test_http_429_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = WooCommerceConnector(shop_domain="example-wc-shop.test", merchant_name="X")

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ConnectorError, match="429"):
        connector.get_product("some-slug")


def test_timeout_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = WooCommerceConnector(shop_domain="example-wc-shop.test", merchant_name="X")

    def fake_get(url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ConnectorError, match="timeout"):
        connector.get_product("some-slug")
