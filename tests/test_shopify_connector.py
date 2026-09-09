"""No real network calls: ShopifyConnector._fetch is monkeypatched to
return fixture HTML, or httpx.get is monkeypatched to raise/return a
locally-built httpx.Response — nothing ever leaves the machine.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from connectors.base import ConnectorError, ProductNotFoundError
from connectors.shopify import ShopifyConnector

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _connector_with_fixture(monkeypatch: pytest.MonkeyPatch, fixture_name: str) -> ShopifyConnector:
    connector = ShopifyConnector(shop_domain="example-shop.test", merchant_name="Example Shop")
    html = _load_fixture(fixture_name)
    monkeypatch.setattr(connector, "_fetch", lambda url: html)
    return connector


def test_product_in_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_in_stock.html")

    product = connector.get_product("elite-trainer-box-test-fr")

    assert product.available is True
    assert product.name == "Elite Trainer Box Test [FR]"
    assert product.price == Decimal("74.9")
    assert product.currency == "EUR"
    assert product.seller == "Example Shop"
    assert product.external_id == "elite-trainer-box-test-fr"
    assert "variant=57591808033103" in product.url


def test_product_out_of_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_out_of_stock.html")

    product = connector.get_product("display-test-fr")

    assert product.available is False


def test_price_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_in_stock.html")

    product = connector.get_product("elite-trainer-box-test-fr")

    assert isinstance(product.price, Decimal)
    assert product.price == Decimal("74.9")


def test_gtin_and_mpn_parsing_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_with_gtin_mpn.html")

    product = connector.get_product("booster-box-with-ids-fr")

    assert product.ean == "0820650953124"
    assert product.mpn == "POK-BB-TEST-01"


def test_gtin_and_mpn_are_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_in_stock.html")

    product = connector.get_product("elite-trainer-box-test-fr")

    assert product.ean is None
    assert product.mpn is None


def test_multi_variant_default_selects_first_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_multi_variant.html")

    product = connector.get_product("artset-test-fr")

    assert product.external_id == "artset-test-fr"
    assert product.price == Decimal("449.90")
    assert product.available is True


def test_multi_variant_selects_by_sku_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_multi_variant.html")

    product = connector.get_product("artset-test-fr:TESTARTSET-B")

    assert product.price == Decimal("119.90")
    assert product.available is False


def test_multi_variant_unknown_sku_raises_product_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_multi_variant.html")

    with pytest.raises(ProductNotFoundError):
        connector.get_product("artset-test-fr:DOES-NOT-EXIST")


def test_incomplete_html_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_incomplete_html.html")

    with pytest.raises(ConnectorError, match="structured data"):
        connector.get_product("redesigned-page-fr")


def test_missing_price_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_no_price.html")

    with pytest.raises(ConnectorError, match="price"):
        connector.get_product("missing-price-fr")


def test_indeterminate_stock_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "shopify_indeterminate_stock.html")

    with pytest.raises(ConnectorError, match="stock"):
        connector.get_product("weird-availability-fr")


def test_http_404_raises_product_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = ShopifyConnector(shop_domain="example-shop.test", merchant_name="Example Shop")

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ProductNotFoundError):
        connector.get_product("does-not-exist")


def test_http_429_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = ShopifyConnector(shop_domain="example-shop.test", merchant_name="Example Shop")

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ConnectorError, match="429"):
        connector.get_product("some-handle")


def test_http_403_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = ShopifyConnector(shop_domain="example-shop.test", merchant_name="Example Shop")

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(403, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ConnectorError, match="403"):
        connector.get_product("some-handle")


def test_timeout_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = ShopifyConnector(shop_domain="example-shop.test", merchant_name="Example Shop")

    def fake_get(url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ConnectorError, match="timeout"):
        connector.get_product("some-handle")


def test_network_error_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = ShopifyConnector(shop_domain="example-shop.test", merchant_name="Example Shop")

    def fake_get(url: str, **kwargs: object):
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(ConnectorError, match="network error"):
        connector.get_product("some-handle")


def test_request_uses_explicit_user_agent_and_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = ShopifyConnector(shop_domain="example-shop.test", merchant_name="Example Shop")
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        captured["url"] = url
        return httpx.Response(
            200, request=httpx.Request("GET", url), text=_load_fixture("shopify_in_stock.html")
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    connector.get_product("elite-trainer-box-test-fr")

    assert "User-Agent" in captured["headers"]
    assert captured["timeout"] <= 10
    assert captured["url"] == "https://example-shop.test/products/elite-trainer-box-test-fr"
