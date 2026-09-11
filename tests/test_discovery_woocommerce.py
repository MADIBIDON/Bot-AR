"""No real network: the discovery source's persistent client's .get() is
monkeypatched (Phase 23 connection pooling — see discovery/woocommerce.py),
response shape matches the real WooCommerce Store API search response
captured live against fuji-store.fr during Phase 22 recon.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from discovery.base import DiscoveryError
from discovery.woocommerce import WooCommerceDiscoverySource


def _item(**overrides: object) -> dict:
    defaults: dict[str, object] = {
        "slug": "alakazam-holo-1-102-set-de-base-pokemon-fr-1999",
        "name": "ALAKAZAM HOLO 1/102 - SET DE BASE - POKEMON FR 1999",
        "permalink": "https://fuji-store.fr/produit/alakazam-holo-1-102-set-de-base-pokemon-fr-1999/",
        "sku": "BIZ-SHG-270526-1",
        "prices": {"price": "8990", "currency_minor_unit": 2, "currency_code": "EUR"},
        "is_in_stock": True,
    }
    defaults.update(overrides)
    return defaults


def _source() -> WooCommerceDiscoverySource:
    return WooCommerceDiscoverySource(shop_domain="fuji-store.fr", merchant_name="Fuji Store")


def test_search_returns_connector_products(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=[_item()], request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("Alakazam")

    assert len(results) == 1
    assert results[0].external_id == "alakazam-holo-1-102-set-de-base-pokemon-fr-1999"
    assert results[0].price == Decimal("89.90")
    assert results[0].available is True
    assert results[0].mpn == "BIZ-SHG-270526-1"


def test_out_of_stock_item_reported_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json=[_item(is_in_stock=False)], request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("Alakazam")

    assert results[0].available is False


def test_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    assert source.search("nonexistent") == []


def test_incomplete_item_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json=[{"slug": "x", "name": "X"}], request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source._client, "get", fake_get)

    assert source.search("x") == []


def test_network_error_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="timeout"):
        source.search("Alakazam")


def test_http_error_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="500"):
        source.search("Alakazam")


def test_non_json_response_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, text="<html></html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="non-JSON"):
        source.search("Alakazam")
