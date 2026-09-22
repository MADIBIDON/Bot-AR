"""discovery/shopify_ucp.py — a 429 must park the source, not be retried
on the very next cycle (found live 21-22/09: Kairyu and RelicTCG answered
429 on nearly every catalogue-watch cycle). No real network."""

from __future__ import annotations

import pytest

import discovery.shopify_ucp as module
from discovery.base import DiscoveryError, DiscoveryUnavailableError
from discovery.shopify_ucp import ShopifyUCPDiscoverySource
from ucp.client import UCPToolCallError


class _Endpoint:
    mcp_endpoint = "https://shop.example/api/ucp/mcp"


def _source() -> ShopifyUCPDiscoverySource:
    return ShopifyUCPDiscoverySource(
        shop_domain="shop.example",
        merchant_name="Kairyu",
        agent_profile_url="https://agent.example/profile",
    )


def test_429_parks_the_source_and_skips_the_next_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(module, "discover", lambda *a, **k: _Endpoint())

    def rate_limited(*args: object, **kwargs: object):
        calls.append("call")
        raise UCPToolCallError("Rate limited (429)", code=429)

    monkeypatch.setattr(module, "call_tool", rate_limited)
    source = _source()

    with pytest.raises(DiscoveryUnavailableError, match="429"):
        source.search("pokemon")
    with pytest.raises(DiscoveryUnavailableError, match="rate limited"):
        source.search("pokemon")

    assert calls == ["call"]  # the second search made no request at all


def test_other_ucp_errors_are_not_treated_as_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "discover", lambda *a, **k: _Endpoint())

    def broken(*args: object, **kwargs: object):
        raise UCPToolCallError("invalid request", code=400)

    monkeypatch.setattr(module, "call_tool", broken)
    source = _source()

    with pytest.raises(DiscoveryError, match="search failed"):
        source.search("pokemon")
    # Not parked: the next call is attempted again.
    with pytest.raises(DiscoveryError, match="search failed"):
        source.search("pokemon")
