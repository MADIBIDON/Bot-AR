"""No real network calls: GenericSchemaOrgConnector._fetch is
monkeypatched to return fixture HTML, matching
tests/test_woocommerce_connector.py's pattern.

Phase 28: this connector backs Cultura, JouéClub, E.Leclerc, and La
Grande Récré — each confirmed reachable via a real plain HTTP GET during
this session's research, then verified end to end against the live site
before writing these fixtures (see connectors/defaults.py's module
docstring).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from connectors.base import ConnectorError, ProductNotFoundError
from connectors.generic_schema_org import GenericSchemaOrgConnector

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _connector_with_fixture(
    monkeypatch: pytest.MonkeyPatch, fixture_name: str
) -> GenericSchemaOrgConnector:
    connector = GenericSchemaOrgConnector(
        shop_domain="example-generic-shop.test", merchant_name="Example Generic Shop"
    )
    html = _load_fixture(fixture_name)
    monkeypatch.setattr(connector, "_fetch", lambda url: html)
    return connector


def test_product_in_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "generic_schema_org_in_stock.html")

    product = connector.get_product("rayon/jeux/etb-test-30-ans-0000000000001.html")

    assert product.available is True
    assert product.name == "Pokemon ETB Test 30 Ans"
    assert product.price == Decimal("55.99")
    assert product.currency == "EUR"
    assert product.ean == "0000000000001"
    assert product.external_id == "rayon/jeux/etb-test-30-ans-0000000000001.html"


def test_product_out_of_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "generic_schema_org_out_of_stock.html")

    product = connector.get_product("rayon/jeux/etb-test-30-ans-0000000000001.html")

    assert product.available is False


def test_relative_offer_url_falls_back_to_the_fetched_page_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 28 real bug found live against E.Leclerc: a retailer's own
    JSON-LD can ship a relative offers.url — that must never end up as a
    broken relative link in a Discord alert."""
    connector = _connector_with_fixture(monkeypatch, "generic_schema_org_relative_offer_url.html")

    product = connector.get_product("rayon/jeux/etb-test-30-ans-0000000000001.html")

    assert product.url == (
        "https://example-generic-shop.test/rayon/jeux/etb-test-30-ans-0000000000001.html"
    )


def test_url_uses_full_path_directly_no_fixed_prefix() -> None:
    connector = GenericSchemaOrgConnector(
        shop_domain="example-generic-shop.test", merchant_name="X"
    )

    assert connector._build_url("rayon/jeux/etb-test-30-ans.html") == (
        "https://example-generic-shop.test/rayon/jeux/etb-test-30-ans.html"
    )


def test_url_strips_a_leading_slash_on_the_path() -> None:
    connector = GenericSchemaOrgConnector(
        shop_domain="example-generic-shop.test", merchant_name="X"
    )

    assert connector._build_url("/rayon/jeux/etb-test-30-ans.html") == (
        "https://example-generic-shop.test/rayon/jeux/etb-test-30-ans.html"
    )


def test_http_404_raises_product_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = GenericSchemaOrgConnector(
        shop_domain="example-generic-shop.test", merchant_name="X"
    )

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(connector._client, "get", fake_get)

    with pytest.raises(ProductNotFoundError):
        connector.get_product("does-not-exist.html")


def test_http_403_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retailer that starts returning 403 (bot protection kicking in
    later) must be reported as a normal connector failure, never crash
    the worker or silently look like a stock change."""
    connector = GenericSchemaOrgConnector(
        shop_domain="example-generic-shop.test", merchant_name="X"
    )

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(403, request=httpx.Request("GET", url))

    monkeypatch.setattr(connector._client, "get", fake_get)

    with pytest.raises(ConnectorError, match="403"):
        connector.get_product("some-path.html")
