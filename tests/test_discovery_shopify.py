"""No real network: httpx.request is monkeypatched, reusing the same
real-shaped search_catalog response captured live in Phase 21/22 (see
tests/test_ucp_shopify_connector.py for the source recon).
"""

from __future__ import annotations

import httpx
import pytest

from discovery.base import DiscoveryError, DiscoveryUnavailableError
from discovery.shopify_ucp import ShopifyUCPDiscoverySource

PROFILE_URL = "https://ucp-profile.vercel.app/agent-profile.json"
MCP_ENDPOINT = "https://kairyushop.myshopify.com/api/ucp/mcp"

_DISCOVERY_BODY = {
    "ucp": {
        "version": "2026-08-25",
        "services": {
            "dev.ucp.shopping": [
                {"version": "2026-08-25", "transport": "mcp", "endpoint": MCP_ENDPOINT}
            ]
        },
    }
}


def _search_result(products: list[dict]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"structuredContent": {"products": products, "pagination": {}, "messages": []}},
    }


def _router(search_result: dict):
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/.well-known/ucp"):
            return httpx.Response(200, json=_DISCOVERY_BODY, request=httpx.Request(method, url))
        return httpx.Response(200, json=search_result, request=httpx.Request(method, url))

    return fake_request


def _product(handle: str, variants: list[dict], min_amount: int) -> dict:
    return {
        "id": "gid://shopify/Product/1",
        "title": "ETB Chaos Ascendant",
        "handle": handle,
        "url": f"https://kairyu.fr/products/{handle}",
        "price_range": {"min": {"amount": min_amount, "currency": "EUR"}},
        "variants": variants,
    }


def test_search_returns_connector_products(monkeypatch: pytest.MonkeyPatch) -> None:
    products = [
        _product(
            "etb-chaos-ascendant-fr",
            [{"id": "gid://x/1", "sku": "SKU-1", "availability": {"available": True}}],
            5990,
        )
    ]
    monkeypatch.setattr(httpx, "request", _router(_search_result(products)))

    source = ShopifyUCPDiscoverySource(
        shop_domain="kairyu.fr", merchant_name="Kairyu", agent_profile_url=PROFILE_URL
    )
    results = source.search("ETB Chaos Ascendant")

    from decimal import Decimal

    assert len(results) == 1
    assert results[0].external_id == "etb-chaos-ascendant-fr"
    assert results[0].price == Decimal("59.90")
    assert results[0].available is True
    assert results[0].mpn == "SKU-1"


def test_multiple_variants_leaves_mpn_none(monkeypatch: pytest.MonkeyPatch) -> None:
    products = [
        _product(
            "etb-chaos-ascendant-fr",
            [
                {"id": "gid://x/1", "sku": "SKU-1", "availability": {"available": True}},
                {"id": "gid://x/2", "sku": "SKU-2", "availability": {"available": False}},
            ],
            5990,
        )
    ]
    monkeypatch.setattr(httpx, "request", _router(_search_result(products)))

    source = ShopifyUCPDiscoverySource(
        shop_domain="kairyu.fr", merchant_name="Kairyu", agent_profile_url=PROFILE_URL
    )
    results = source.search("ETB Chaos Ascendant")

    assert results[0].mpn is None
    assert results[0].available is True  # at least one variant is available


def test_missing_profile_url_raises_unavailable() -> None:
    source = ShopifyUCPDiscoverySource(
        shop_domain="kairyu.fr", merchant_name="Kairyu", agent_profile_url=""
    )

    with pytest.raises(DiscoveryUnavailableError):
        source.search("ETB Chaos Ascendant")


def test_merchant_without_ucp_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    source = ShopifyUCPDiscoverySource(
        shop_domain="no-ucp.example", merchant_name="NoUCP", agent_profile_url=PROFILE_URL
    )

    with pytest.raises(DiscoveryUnavailableError):
        source.search("ETB Chaos Ascendant")


def test_tool_call_failure_raises_discovery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error_body = {
        "jsonrpc": "2.0",
        "id": "1",
        "error": {"code": -32602, "message": "Invalid params", "data": "boom"},
    }
    monkeypatch.setattr(httpx, "request", _router(error_body))

    source = ShopifyUCPDiscoverySource(
        shop_domain="kairyu.fr", merchant_name="Kairyu", agent_profile_url=PROFILE_URL
    )

    with pytest.raises(DiscoveryError):
        source.search("ETB Chaos Ascendant")


def test_incomplete_product_entry_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    products = [{"id": "gid://shopify/Product/1", "title": "Incomplete"}]  # no handle/url/price
    monkeypatch.setattr(httpx, "request", _router(_search_result(products)))

    source = ShopifyUCPDiscoverySource(
        shop_domain="kairyu.fr", merchant_name="Kairyu", agent_profile_url=PROFILE_URL
    )
    results = source.search("Incomplete")

    assert results == []
