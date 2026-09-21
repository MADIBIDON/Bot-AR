"""discovery/shopify_public.py. No real network: the source's own client
is monkeypatched. Response shape matches the real
hikarudistribution.com/search/suggest.json payload captured live.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from discovery.base import DiscoveryError, DiscoveryUnavailableError
from discovery.shopify_public import ShopifyPublicSearchDiscoverySource, _parse_price


def _source() -> ShopifyPublicSearchDiscoverySource:
    return ShopifyPublicSearchDiscoverySource(
        shop_domain="hikarudistribution.com", merchant_name="Hikaru Distribution"
    )


def _payload(**overrides: object) -> dict:
    product = {
        "title": "Display Pokémon 151 - SV2A - Japonais",
        "handle": "display-pokemon-151-sv2a-japonais",
        "url": "/products/display-pokemon-151-sv2a-japonais?_pos=1",
        "price": "329.90",
        "available": True,
        "image": "https://cdn.shopify.com/s/files/1/0798/display.png?v=1729122336",
    }
    product.update(overrides)
    return {"resources": {"results": {"products": [product]}}}


def _patch(source, monkeypatch, *, status: int = 200, json_body=None, headers=None):
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            status,
            json=json_body if json_body is not None else _payload(),
            headers=headers or {},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(source._client, "get", fake_get)


def test_search_maps_the_real_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _patch(source, monkeypatch)

    results = source.search("pokemon")

    assert len(results) == 1
    p = results[0]
    assert p.external_id == "display-pokemon-151-sv2a-japonais"
    assert p.name == "Display Pokémon 151 - SV2A - Japonais"
    assert p.price == Decimal("329.90")
    assert p.available is True
    assert p.url == ("https://hikarudistribution.com/products/display-pokemon-151-sv2a-japonais")
    assert p.image_url.startswith("https://cdn.shopify.com/")
    assert p.seller == "Hikaru Distribution"


def test_out_of_stock_is_reported_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _patch(source, monkeypatch, json_body=_payload(available=False))

    assert source.search("pokemon")[0].available is False


def test_item_without_price_is_skipped_never_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _patch(source, monkeypatch, json_body=_payload(price="n/a"))

    assert source.search("pokemon") == []


def test_item_without_handle_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _patch(source, monkeypatch, json_body=_payload(handle=""))

    assert source.search("pokemon") == []


def test_429_parks_the_source_and_respects_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    _patch(source, monkeypatch, status=429, headers={"Retry-After": "120"})

    with pytest.raises(DiscoveryUnavailableError, match="429"):
        source.search("pokemon")

    # Parked: the next call must not even attempt a request.
    def fail(*args: object, **kwargs: object):
        raise AssertionError("must not call the merchant while rate limited")

    monkeypatch.setattr(source._client, "get", fail)
    with pytest.raises(DiscoveryUnavailableError, match="rate limited"):
        source.search("pokemon")


def test_404_means_no_public_search_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _patch(source, monkeypatch, status=404)

    with pytest.raises(DiscoveryUnavailableError):
        source.search("pokemon")


def test_server_error_is_a_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    _patch(source, monkeypatch, status=503)

    with pytest.raises(DiscoveryError):
        source.search("pokemon")


def test_network_failure_is_a_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()

    def boom(url: str, **kwargs: object):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(source._client, "get", boom)

    with pytest.raises(DiscoveryError):
        source.search("pokemon")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("329.90", Decimal("329.90")),
        ("1 299,00", Decimal("1299.00")),
        ("1,299.00", Decimal("1299.00")),
        (14.99, Decimal("14.99")),
        ("n/a", None),
        (None, None),
    ],
)
def test_price_parsing_handles_real_shop_formats(raw: object, expected: Decimal | None) -> None:
    assert _parse_price(raw) == expected
