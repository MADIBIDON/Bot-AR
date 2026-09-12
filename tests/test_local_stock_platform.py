"""No real network: RbsPlatformClient._client.post is monkeypatched.
Response shapes here are trimmed copies of what this session's real,
live research actually returned from JouéClub/La Grande Récré's public
Rbs/Storelocator and Rbs/Storeshipping endpoints."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from local_stock.rbs_platform import RbsPlatformClient, RbsPlatformError
from local_stock.store_discovery import build_rbs_client, discover_stores

_STORE_LIST_JSON = {
    "name": "Rbs/Storelocator/Store/",
    "pagination": {"offset": 0, "limit": 150, "count": 2},
    "items": [
        {
            "common": {
                "id": 92480433,
                "title": "Test Store PARIS",
                "URL": {"printMap": "https://shop.test/stores/paris"},
            },
            "address": {
                "fields": {
                    "street": "1 Rue de Test",
                    "zipCode": "75002",
                    "locality": "PARIS",
                }
            },
            "coordinates": {"latitude": 48.8712, "longitude": 2.34363},
        },
        {
            "common": {"id": 92001818, "title": "Test Store LORIENT", "URL": {}},
            "address": {
                "fields": {"street": "2 Rue de Test", "zipCode": "56100", "locality": "LORIENT"}
            },
            "coordinates": {"latitude": 47.75, "longitude": -3.36},
        },
    ],
}

_STOCK_HIT_JSON = {
    "name": "Rbs/Storeshipping/Store/",
    "pagination": {"offset": 0, "count": 1},
    "items": [{"common": {"id": 92480433, "title": "Test Store PARIS"}}],
}

_STOCK_EMPTY_JSON = {
    "name": "Rbs/Storeshipping/Store/",
    "pagination": {"offset": 0, "count": 0},
    "items": [],
}


def _client() -> RbsPlatformClient:
    return RbsPlatformClient(shop_domain="shop.test", website_id=100000)


def _json_response(url: str, payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("POST", url))


def test_list_all_stores_parses_real_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client._client, "post", lambda url, **kw: _json_response(url, _STORE_LIST_JSON)
    )

    stores = client.list_all_stores()

    assert len(stores) == 2
    paris = stores[0]
    assert paris.external_store_id == "92480433"
    assert paris.name == "Test Store PARIS"
    assert paris.city == "PARIS"
    assert paris.postal_code == "75002"
    assert paris.latitude == Decimal("48.8712")


def test_check_pickup_availability_reports_matching_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client._client, "post", lambda url, **kw: _json_response(url, _STOCK_HIT_JSON)
    )

    results = client.check_pickup_availability(
        sku="1234567890123", latitude=Decimal("48.8"), longitude=Decimal("2.3")
    )

    assert len(results) == 1
    assert results[0].external_store_id == "92480433"
    assert results[0].can_pick_up is True


def test_check_pickup_availability_empty_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client._client, "post", lambda url, **kw: _json_response(url, _STOCK_EMPTY_JSON)
    )

    results = client.check_pickup_availability(
        sku="1234567890123", latitude=Decimal("48.8"), longitude=Decimal("2.3")
    )

    assert results == []


def test_http_error_raises_rbs_platform_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client._client,
        "post",
        lambda url, **kw: httpx.Response(500, request=httpx.Request("POST", url)),
    )

    with pytest.raises(RbsPlatformError):
        client.list_all_stores()


def test_timeout_raises_rbs_platform_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()

    def raise_timeout(url: str, **kw: object):
        raise httpx.TimeoutException("timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(client._client, "post", raise_timeout)

    with pytest.raises(RbsPlatformError):
        client.list_all_stores()


def test_a_store_with_no_coordinates_is_parsed_but_flagged_absent() -> None:
    from local_stock.rbs_platform import _parse_store

    store = _parse_store({"common": {"id": 1, "title": "No Coords Store"}, "address": {}})

    assert store is not None
    assert store.latitude is None
    assert store.longitude is None


def test_item_missing_id_or_title_is_skipped_not_a_crash() -> None:
    from local_stock.rbs_platform import _parse_store

    assert _parse_store({"common": {"title": "No ID"}}) is None
    assert _parse_store({"common": {"id": 1, "title": ""}}) is None


# --- store_discovery.py --------------------------------------------------


def test_build_rbs_client_returns_none_for_an_unregistered_retailer() -> None:
    assert build_rbs_client("Some Random Shop") is None


def test_build_rbs_client_returns_a_real_client_for_a_registered_retailer() -> None:
    client = build_rbs_client("JouéClub")
    assert client is not None
    assert isinstance(client, RbsPlatformClient)


def test_discover_stores_raises_for_an_unsupported_retailer(session) -> None:
    with pytest.raises(RbsPlatformError):
        discover_stores(session, "Some Random Shop")


def test_discover_stores_upserts_real_shaped_data(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from database import crud

    client = build_rbs_client("JouéClub")
    monkeypatch.setattr(
        client._client, "post", lambda url, **kw: _json_response(url, _STORE_LIST_JSON)
    )
    monkeypatch.setattr("local_stock.store_discovery.build_rbs_client", lambda retailer: client)

    count = discover_stores(session, "JouéClub", now=datetime.now(UTC))

    assert count == 2
    stores = crud.list_retail_stores(session, retailer="JouéClub")
    assert len(stores) == 2


def test_discover_stores_is_idempotent(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from database import crud

    client = build_rbs_client("JouéClub")
    monkeypatch.setattr(
        client._client, "post", lambda url, **kw: _json_response(url, _STORE_LIST_JSON)
    )
    monkeypatch.setattr("local_stock.store_discovery.build_rbs_client", lambda retailer: client)

    discover_stores(session, "JouéClub", now=datetime.now(UTC))
    discover_stores(session, "JouéClub", now=datetime.now(UTC))

    stores = crud.list_retail_stores(session, retailer="JouéClub")
    assert len(stores) == 2  # not 4 — re-running discovery never duplicates a store
