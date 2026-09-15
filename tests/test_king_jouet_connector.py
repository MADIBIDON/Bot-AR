"""connectors/king_jouet.py — Phase 39. No real network: the connector's
persistent client's .get() is monkeypatched. Response shape matches the
real King Jouet /api/product/<ref> JSON captured live this session
against 3 real refs (2026-09-15) — never through their (DataDome-
blocked) product page, and their cart/basket API is never called by
this connector at all.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from connectors.base import ConnectorError, ProductNotFoundError
from connectors.king_jouet import KingJouetConnector


def _real_shaped_response(
    *,
    ref: str = "1034916",
    label: str = "Pokémon 30 ans - Coffret dresseur d'élite ",
    price: float = 64.99,
    available_on_web: bool = False,
) -> dict:
    return {
        "id": "692f3d26-0683-453f-9697-169f54f687ea",
        "ref": ref,
        "label": label,
        "availability": {
            "isAvailableOnWeb": available_on_web,
            "isAvailableFromOther": False,
            "isAvailableForShipFromStore": False,
            "stores": [],
        },
        "price": {"isOnSale": False, "originalPrice": price, "price": price},
        "shippingChoices": [],
    }


def test_get_product_returns_real_shaped_data(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = KingJouetConnector()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "https://www.king-jouet.com/api/product/1034916"
        return httpx.Response(200, json=_real_shaped_response(), request=httpx.Request("GET", url))

    monkeypatch.setattr(connector._client, "get", fake_get)

    product = connector.get_product("1034916")

    assert product.name == "Pokémon 30 ans - Coffret dresseur d'élite"
    assert product.price == Decimal("64.99")
    assert product.available is False
    assert product.ean is None
    assert product.mpn == "1034916"
    assert product.seller == "King Jouet"
    assert "1034916" in product.url


def test_ref_dash_prefix_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generic add-by-URL flow would extract 'ref-1034916' (the real
    product page's own URL segment) — must resolve identically to the
    bare ref."""
    connector = KingJouetConnector()
    seen_urls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        seen_urls.append(url)
        return httpx.Response(200, json=_real_shaped_response(), request=httpx.Request("GET", url))

    monkeypatch.setattr(connector._client, "get", fake_get)

    connector.get_product("ref-1034916")

    assert seen_urls == ["https://www.king-jouet.com/api/product/1034916"]


def test_available_when_any_availability_flag_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = KingJouetConnector()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json=_real_shaped_response(available_on_web=True),
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(connector._client, "get", fake_get)

    assert connector.get_product("1034916").available is True


def test_http_404_raises_product_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = KingJouetConnector()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(connector._client, "get", fake_get)

    with pytest.raises(ProductNotFoundError):
        connector.get_product("0000000")


def test_http_403_raises_connector_error_never_bypassed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the product API ever starts returning the same DataDome
    holding page as the cart/basket API, this must be a normal,
    reportable failure — never an attempt to work around it."""
    connector = KingJouetConnector()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(403, request=httpx.Request("GET", url))

    monkeypatch.setattr(connector._client, "get", fake_get)

    with pytest.raises(ConnectorError, match="403"):
        connector.get_product("1034916")


def test_missing_name_or_price_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = KingJouetConnector()

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json={"ref": "1034916", "availability": {}}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(connector._client, "get", fake_get)

    with pytest.raises(ConnectorError, match="missing"):
        connector.get_product("1034916")


def test_network_error_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = KingJouetConnector()

    def fake_get(url: str, **kwargs: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(connector._client, "get", fake_get)

    with pytest.raises(ConnectorError, match="timeout"):
        connector.get_product("1034916")


def test_real_response_shape_from_live_recon() -> None:
    """Regression: the exact live shape captured 2026-09-15 against
    www.king-jouet.com/api/product/1034916, 1034914, 1034909 — proves
    the connector handles the real payload, not a hand-simplified one."""
    real_body = {
        "id": "692f3d26-0683-453f-9697-169f54f687ea",
        "ref": "1034916",
        "label": "Pokémon 30 ans - Coffret dresseur d'élite ",
        "images": [],
        "availability": {
            "isAvailableOnWeb": False,
            "isAvailableFromOther": False,
            "isAvailableForShipFromStore": False,
            "stores": [],
        },
        "price": {"isOnSale": False, "originalPrice": 64.99, "price": 64.99},
        "shippingChoices": [],
        "brand": "Asmodée",
    }
    connector = KingJouetConnector()

    class _FakeClient:
        def get(self, url: str, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, json=real_body, request=httpx.Request("GET", url))

    connector._client = _FakeClient()

    product = connector.get_product("1034916")

    assert product.price == Decimal("64.99")
    assert product.name == "Pokémon 30 ans - Coffret dresseur d'élite"
