"""No real network calls: NikeLaunchConnector's httpx.Client.get is
monkeypatched to return fixture HTML captured from the real, live Nike
SNKRS launch page for "Nike SB Air Force 1 x Yuto" (Phase 30's one
scheduled-release test case — see connectors/nike_launch.py docstring).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from connectors.base import ConnectorError
from connectors.nike_launch import NikeLaunchConnector

FIXTURES_DIR = Path(__file__).parent / "fixtures"
LAUNCH_URL = "https://www.nike.com/fr/launch/t/nike-sb-air-force-1-yuto-light-bone-and-iron-grey"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _connector_with_fixture(
    monkeypatch: pytest.MonkeyPatch, fixture_name: str
) -> NikeLaunchConnector:
    connector = NikeLaunchConnector(
        launch_url=LAUNCH_URL,
        style_color="IO8439-100",
        product_name="Nike SB Air Force 1 x Yuto 'Light Bone and Iron Grey'",
    )
    html = _load_fixture(fixture_name)
    monkeypatch.setattr(
        connector._client,
        "get",
        lambda url: httpx.Response(200, text=html, request=httpx.Request("GET", url)),
    )
    return connector


def test_before_commerce_start_date_is_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "nike_launch_upcoming.html")

    product = connector.get_product("nike-sb-yuto")

    assert product.available is False
    assert product.price == Decimal("119.99")
    assert product.currency == "EUR"
    assert product.mpn == "IO8439-100"


def test_after_commerce_start_date_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = _connector_with_fixture(monkeypatch, "nike_launch_available.html")

    product = connector.get_product("nike-sb-yuto")

    assert product.available is True


def test_unknown_style_color_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = NikeLaunchConnector(
        launch_url=LAUNCH_URL, style_color="DOES-NOT-EXIST", product_name="x"
    )
    html = _load_fixture("nike_launch_available.html")
    monkeypatch.setattr(
        connector._client,
        "get",
        lambda url: httpx.Response(200, text=html, request=httpx.Request("GET", url)),
    )

    with pytest.raises(ConnectorError):
        connector.get_product("nike-sb-yuto")
