"""No real network: SitemapDiscoverySource._client.get is monkeypatched
to return locally-built httpx.Response objects, and the product-page
connector is a small fake — same pattern as tests/test_woocommerce_connector.py.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from connectors.base import BaseConnector, ConnectorError, ConnectorProduct, ProductNotFoundError
from discovery.base import DiscoveryError, DiscoveryUnavailableError
from discovery.sitemap import SitemapDiscoverySource

_INDEX_XML = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.test/sitemap-a.xml</loc></sitemap>
  <sitemap><loc>https://shop.test/sitemap-b.xml</loc></sitemap>
</sitemapindex>"""

_LEAF_A_XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/pokemon-coffret-dresseur-elite-30e-anniversaire.html</loc></url>
  <url><loc>https://shop.test/pokemon-peluche-30-cm.html</loc></url>
</urlset>"""

_LEAF_B_XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/lego-star-wars.html</loc></url>
</urlset>"""

_FLAT_URLSET_XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/pokemon-coffret-dresseur-elite-30e-anniversaire.html</loc></url>
</urlset>"""


class FakeConnector(BaseConnector):
    def __init__(self, *, products: dict[str, ConnectorProduct] | None = None) -> None:
        self._products = products or {}
        self.fetched_ids: list[str] = []

    def get_product(self, external_id: str) -> ConnectorProduct:
        self.fetched_ids.append(external_id)
        if external_id not in self._products:
            raise ProductNotFoundError(external_id)
        return self._products[external_id]


def _product(external_id: str) -> ConnectorProduct:
    return ConnectorProduct(
        external_id=external_id,
        name="Pokemon Coffret Dresseur Elite 30e Anniversaire",
        price=Decimal("55.99"),
        currency="EUR",
        available=False,
        seller="Test Shop",
        url=f"https://shop.test/{external_id}",
    )


def _source(connector: BaseConnector) -> SitemapDiscoverySource:
    return SitemapDiscoverySource(
        sitemap_index_url="https://shop.test/sitemap-index.xml",
        connector=connector,
        merchant_name="Test Shop",
    )


def _xml_response(url: str, body: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), request=httpx.Request("GET", url))


def test_finds_a_matching_product_across_an_index_of_leaf_sitemaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_id = "pokemon-coffret-dresseur-elite-30e-anniversaire.html"
    connector = FakeConnector(products={fake_id: _product(fake_id)})
    source = _source(connector)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        bodies = {
            "https://shop.test/sitemap-index.xml": _INDEX_XML,
            "https://shop.test/sitemap-a.xml": _LEAF_A_XML,
            "https://shop.test/sitemap-b.xml": _LEAF_B_XML,
        }
        return _xml_response(url, bodies[url])

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("Pokemon Coffret Dresseur Elite 30e Anniversaire")

    assert len(results) == 1
    assert results[0].name == "Pokemon Coffret Dresseur Elite 30e Anniversaire"
    assert fake_id in connector.fetched_ids


def test_no_matching_product_returns_empty_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = FakeConnector()
    source = _source(connector)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        bodies = {
            "https://shop.test/sitemap-index.xml": _INDEX_XML,
            "https://shop.test/sitemap-a.xml": _LEAF_A_XML,
            "https://shop.test/sitemap-b.xml": _LEAF_B_XML,
        }
        return _xml_response(url, bodies[url])

    monkeypatch.setattr(source._client, "get", fake_get)

    results = source.search("Nonexistent Product Nobody Sells")

    assert results == []
    assert connector.fetched_ids == []  # never fetches a product page for a non-match


def test_a_flat_urlset_with_no_index_is_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_id = "pokemon-coffret-dresseur-elite-30e-anniversaire.html"
    connector = FakeConnector(products={fake_id: _product(fake_id)})
    source = _source(connector)

    monkeypatch.setattr(
        source._client, "get", lambda url, **kw: _xml_response(url, _FLAT_URLSET_XML)
    )

    results = source.search("Pokemon Coffret Dresseur Elite 30e Anniversaire")

    assert len(results) == 1


def test_stale_sitemap_entry_is_skipped_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A product page removed since the last crawl (404) must never stop
    discovery for the retailer's other matches."""
    fake_id = "pokemon-coffret-dresseur-elite-30e-anniversaire.html"
    connector = FakeConnector()  # empty -> every get_product() raises ProductNotFoundError
    source = _source(connector)

    monkeypatch.setattr(
        source._client, "get", lambda url, **kw: _xml_response(url, _FLAT_URLSET_XML)
    )

    results = source.search("Pokemon Coffret Dresseur Elite 30e Anniversaire")

    assert results == []
    assert fake_id in connector.fetched_ids  # it did try


def test_connector_error_on_one_match_does_not_stop_others(monkeypatch: pytest.MonkeyPatch) -> None:
    class FlakyThenGoodConnector(BaseConnector):
        def __init__(self) -> None:
            self.calls = 0

        def get_product(self, external_id: str) -> ConnectorProduct:
            self.calls += 1
            if self.calls == 1:
                raise ConnectorError("simulated timeout")
            return _product(external_id)

    leaf = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/pokemon-coffret-dresseur-elite-30e-anniversaire-a.html</loc></url>
  <url><loc>https://shop.test/pokemon-coffret-dresseur-elite-30e-anniversaire-b.html</loc></url>
</urlset>"""
    connector = FlakyThenGoodConnector()
    source = _source(connector)
    monkeypatch.setattr(source._client, "get", lambda url, **kw: _xml_response(url, leaf))

    results = source.search("Pokemon Coffret Dresseur Elite 30e Anniversaire")

    assert len(results) == 1
    assert connector.calls == 2


def test_repeated_search_reuses_the_cached_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Several Products searching the same retailer in one discovery
    cycle must not each re-download the whole catalog."""
    connector = FakeConnector()
    source = _source(connector)
    fetch_count = 0

    def fake_get(url: str, **kw: object) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        return _xml_response(url, _FLAT_URLSET_XML)

    monkeypatch.setattr(source._client, "get", fake_get)

    source.search("Pokemon Coffret Dresseur Elite 30e Anniversaire")
    fetch_count_after_first = fetch_count
    source.search("Pokemon Peluche")  # a second, different query

    assert fetch_count == fetch_count_after_first  # no new sitemap fetch for the second search


def test_sitemap_404_raises_discovery_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = FakeConnector()
    source = _source(connector)
    monkeypatch.setattr(
        source._client,
        "get",
        lambda url, **kw: httpx.Response(404, request=httpx.Request("GET", url)),
    )

    with pytest.raises(DiscoveryUnavailableError):
        source.search("anything")


def test_malformed_xml_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = FakeConnector()
    source = _source(connector)
    monkeypatch.setattr(
        source._client,
        "get",
        lambda url, **kw: httpx.Response(
            200, content=b"not xml at all", request=httpx.Request("GET", url)
        ),
    )

    with pytest.raises(DiscoveryError):
        source.search("anything")


def test_empty_query_returns_empty_without_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = FakeConnector()
    source = _source(connector)

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("should never fetch for an empty query")

    monkeypatch.setattr(source._client, "get", fail)

    assert source.search("   ") == []
