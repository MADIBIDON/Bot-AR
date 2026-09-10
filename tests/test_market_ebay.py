"""No real network: httpx.get/httpx.post are monkeypatched to return
locally-built httpx.Response objects — nothing ever leaves the machine,
and no real eBay credentials are used or required.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from market_data.base import MarketDataError
from market_data.ebay import (
    EbayConfig,
    EbayMarketDataSource,
    MissingEbayConfigError,
    load_ebay_config,
)

_TOKEN_BODY = {
    "access_token": "fake-token",
    "expires_in": 7200,
    "token_type": "Application Access Token",
}


def _token_response(url: str) -> httpx.Response:
    return httpx.Response(200, json=_TOKEN_BODY, request=httpx.Request("POST", url))


def _search_response(url: str, items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"itemSummaries": items}, request=httpx.Request("GET", url))


def _item(
    *,
    title: str = "Pokemon ETB Ecarlate Violet FR",
    price: str = "89.99",
    currency: str = "EUR",
    item_id: str = "v1|123456|0",
    url: str = "https://www.ebay.fr/itm/123456",
    shipping: str | None = None,
    condition: str | None = None,
) -> dict:
    item: dict = {
        "title": title,
        "itemId": item_id,
        "itemWebUrl": url,
        "price": {"value": price, "currency": currency},
    }
    if shipping is not None:
        item["shippingOptions"] = [{"shippingCost": {"value": shipping, "currency": currency}}]
    if condition is not None:
        item["condition"] = condition
    return item


def _source() -> EbayMarketDataSource:
    return EbayMarketDataSource(config=EbayConfig(app_id="fake-id", cert_id="fake-secret"))


def test_missing_env_raises_with_helpful_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    monkeypatch.delenv("EBAY_CERT_ID", raising=False)

    with pytest.raises(MissingEbayConfigError, match="EBAY_APP_ID"):
        load_ebay_config()


def test_load_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBAY_APP_ID", "my-app-id")
    monkeypatch.setenv("EBAY_CERT_ID", "my-cert-id")
    monkeypatch.delenv("EBAY_MARKETPLACE_ID", raising=False)

    config = load_ebay_config()

    assert config.app_id == "my-app-id"
    assert config.cert_id == "my-cert-id"
    assert config.marketplace_id == "EBAY_FR"


def test_search_parses_multiple_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return _token_response(url)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return _search_response(
            url,
            [
                _item(price="85.00", item_id="1"),
                _item(price="90.00", item_id="2", shipping="4.99", condition="New"),
            ],
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    observations = _source().search("Pokemon ETB Ecarlate Violet")

    assert len(observations) == 2
    assert observations[0].price == Decimal("85.00")
    assert observations[0].sold is None  # Browse API never confirms sold
    assert observations[1].shipping_price == Decimal("4.99")
    assert observations[1].condition == "New"


def test_item_missing_required_field_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _token_response(url))
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: _search_response(
            url, [_item(price="85.00", item_id="1"), {"title": "no price here"}]
        ),
    )

    observations = _source().search("query")

    assert len(observations) == 1


def test_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _token_response(url))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _search_response(url, []))

    assert _source().search("query") == []


def test_token_is_cached_across_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"token": 0}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls["token"] += 1
        return _token_response(url)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _search_response(url, []))

    source = _source()
    source.search("a")
    source.search("b")

    assert calls["token"] == 1


def test_search_network_error_raises_market_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _token_response(url))

    def fake_get(url: str, **kwargs: object):
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(MarketDataError):
        _source().search("query")


def test_search_timeout_raises_market_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _token_response(url))

    def fake_get(url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(MarketDataError, match="timeout"):
        _source().search("query")


def test_search_429_raises_market_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _token_response(url))
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(429, request=httpx.Request("GET", url))
    )

    with pytest.raises(MarketDataError, match="429"):
        _source().search("query")


def test_token_fetch_failure_raises_market_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Response(401, request=httpx.Request("POST", url))
    )

    with pytest.raises(MarketDataError):
        _source().search("query")
