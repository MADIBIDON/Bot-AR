"""No real network: the discovery source's persistent client's .get() is
monkeypatched (Phase 23 connection pooling — see discovery/cultura.py).
Response shape matches the real Magento 2 GraphQL `getPlpProducts`
response captured live against www.cultura.com during Phase 36 recon —
this exact query returned real EAN/price/url_key/front_availability for
real Pokémon TCG products on 2026-09-15.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from discovery.base import DiscoveryError, DiscoveryUnavailableError
from discovery.cultura import CulturaSearchDiscoverySource


def _item(**overrides: object) -> dict:
    defaults: dict[str, object] = {
        "sku": "10816948",
        "name": "EV08 coffret Dresseur d'Elite - Pokémon",
        "url_key": "pokemon-ev08-coffret-dresseur-d-elite-10816948",
        "ean": "0820650559259",
        "price_range": {"minimum_price": {"final_price": {"value": 55.99, "currency": "EUR"}}},
        "stock_item_extra": {"front_availability": "available"},
    }
    defaults.update(overrides)
    return defaults


def _graphql_response(items: list[dict]) -> dict:
    return {"data": {"products": {"total_count": len(items), "items": items}}}


def _source() -> CulturaSearchDiscoverySource:
    return CulturaSearchDiscoverySource()


def test_search_returns_connector_products(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json=_graphql_response([_item()]), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("pokemon ev08 dresseur elite")

    assert len(results) == 1
    result = results[0]
    assert result.external_id == "p-pokemon-ev08-coffret-dresseur-d-elite-10816948.html"
    assert (
        result.url
        == "https://www.cultura.com/p-pokemon-ev08-coffret-dresseur-d-elite-10816948.html"
    )
    assert result.price == Decimal("55.99")
    assert result.ean == "0820650559259"
    assert result.mpn == "10816948"
    assert result.available is True
    assert result.seller == "Cultura"


def test_unavailable_item_reported_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        item = _item(stock_item_extra={"front_availability": "unavailable"})
        return httpx.Response(
            200, json=_graphql_response([item]), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("pokemon ev08")

    assert results[0].available is False


def test_no_ean_is_reported_as_none_not_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        item = _item(ean=None)
        return httpx.Response(
            200, json=_graphql_response([item]), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("pokemon ev08")

    assert results[0].ean is None


def test_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=_graphql_response([]), request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    assert source.search("pokemon 30e anniversaire etb") == []


def test_incomplete_item_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        item = {"sku": "x", "name": "X"}  # no url_key, no price_range
        return httpx.Response(
            200, json=_graphql_response([item]), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source._client, "get", fake_get)

    assert source.search("x") == []


def test_graphql_error_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        body = {"errors": [{"message": "Query complexity exceeded"}]}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="GraphQL error"):
        source.search("x")


def test_network_error_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="timeout"):
        source.search("x")


def test_http_error_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="500"):
        source.search("x")


def test_non_json_response_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, text="<html></html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="non-JSON"):
        source.search("x")


def test_real_search_response_shape_from_live_recon() -> None:
    """Regression: the exact live response shape captured 2026-09-15
    against www.cultura.com for 'pokemon 30 ans dresseur elite' — proves
    _to_connector_product handles the real payload, not just a
    hand-simplified fixture."""
    source = _source()
    real_item = {
        "id": 42960264,
        "sku": "10816948",
        "name": "EV08 coffret Dresseur d'Elite - Pokémon",
        "url_key": "pokemon-ev08-coffret-dresseur-d-elite-10816948",
        "ean": "0820650559259",
        "price_range": {"minimum_price": {"final_price": {"value": 55.99, "currency": "EUR"}}},
        "stock_item_extra": {"front_availability": "unavailable"},
    }

    result = source._to_connector_product(real_item)

    assert result is not None
    assert result.name == "EV08 coffret Dresseur d'Elite - Pokémon"
    assert result.price == Decimal("55.99")
    assert result.available is False


# --- Phase 36: 429 / Retry-After -----------------------------------------


def test_429_with_retry_after_header_backs_off_for_that_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    calls: list[int] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "30"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="429"):
        source.search("x")
    assert len(calls) == 1

    # Immediately retrying must NOT even attempt a second real request.
    with pytest.raises(DiscoveryUnavailableError, match="Retry-After"):
        source.search("x")
    assert len(calls) == 1  # still 1 — the second call never touched the network


def test_429_without_retry_after_uses_a_conservative_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", fake_get)

    with pytest.raises(DiscoveryError, match="429"):
        source.search("x")

    assert source._retry_not_before is not None


def test_backoff_expires_and_search_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    source = _source()
    source._retry_not_before = datetime.now(UTC) - timedelta(seconds=1)  # already expired

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json=_graphql_response([_item()]), request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("pokemon ev08")

    assert len(results) == 1
