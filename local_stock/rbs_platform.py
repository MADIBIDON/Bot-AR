"""Client for the Proximis/"Rbs" e-commerce platform's public store
APIs — confirmed live this session (Phase 29) for JouéClub and La Grande
Récré by reading their own published JavaScript (not guessed): the
front-end's `RbsChange.AjaxAPI` service POSTs to
`/ajax.V1.php/fr_FR/<actionPath>` with an `X-HTTP-Method-Override`
header carrying the real HTTP verb — see local_stock/rbs_platform.py's
_call() for the exact reconstruction. No authentication, no session,
same as a plain page load; not an anti-bot bypass of any kind — this is
the same request the retailer's own site makes from a fresh browser.

Two actions used:
  - `Rbs/Storelocator/Store/`  — the national store directory (id, code,
    name, address, coordinates). Confirmed: 103 stores (La Grande Récré),
    256 stores (JouéClub) returned in one call each.
  - `Rbs/Storeshipping/Store/` — for one SKU + a search location, which
    stores currently have it available for pickup. Confirmed structurally
    correct (echoes back parsed coordinates, well-formed pagination) —
    returned zero stores in this session's live test because the test
    SKU was genuinely out of stock everywhere, not because the call is
    wrong.

Never used for anything beyond read-only availability lookups — no
reservation, no cart, no checkout action exists here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+public store locator; no auto-purchase)"
_LCID = "fr_FR"


class RbsPlatformError(Exception):
    """Any failure calling the platform's public API — network, HTTP, or
    an unexpected response shape. Never crashes the caller; store/stock
    checks are always allowed to fail for one retailer without affecting
    others (same posture as connectors/ and discovery/)."""


@dataclass(frozen=True, slots=True)
class RbsStore:
    external_store_id: str
    name: str
    street: str | None
    postal_code: str | None
    city: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    url: str | None


@dataclass(frozen=True, slots=True)
class RbsStoreStock:
    external_store_id: str
    can_pick_up: bool


class RbsPlatformClient:
    def __init__(
        self,
        *,
        shop_domain: str,
        website_id: int,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._base_url = f"https://{shop_domain}/ajax.V1.php/{_LCID}/"
        self._website_id = website_id
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": user_agent})

    def list_all_stores(self, *, limit: int = 500) -> list[RbsStore]:
        """The full national directory in one call — every retailer
        confirmed on this platform returns it whole (103-256 stores),
        well under `limit`. Filtering to specific cities is the caller's
        job (local_stock/store_discovery.py) so the same fetch can serve
        every city at once."""
        payload = self._call(
            "Rbs/Storelocator/Store/",
            data={
                "dataSets": "coordinates,address,card,allow",
                "pagination": f"0,{limit}",
                "data": {
                    "currentStoreId": 0,
                    "distanceUnit": "kilometers",
                    "distance": "3000kilometers",
                },
            },
        )
        return [store for item in payload.get("items", []) if (store := _parse_store(item))]

    def check_pickup_availability(
        self, *, sku: str, latitude: Decimal, longitude: Decimal, radius_km: int = 3000
    ) -> list[RbsStoreStock]:
        """Which stores can currently fulfil a Click & Collect / in-store
        pickup for this one SKU, searched from (latitude, longitude). A
        large default radius (national) so one call covers every
        monitored city; the caller filters to its own city afterward."""
        payload = self._call(
            "Rbs/Storeshipping/Store/",
            data={
                "URLFormats": "canonical",
                "dataSetNames": "address,coordinates,hoursSummary",
                "data": {
                    "search": {
                        "coordinates": {"latitude": float(latitude), "longitude": float(longitude)},
                        "distance": f"{radius_km}kilometers",
                        "distanceUnit": "kilometers",
                    },
                    "skuQuantities": [{"sku": sku, "quantity": 1}],
                    "forReservation": False,
                    "forPickUp": True,
                    "allowSelect": True,
                },
            },
        )
        results: list[RbsStoreStock] = []
        for item in payload.get("items", []):
            store_id = _get(item, "common", "id")
            if store_id is None:
                continue
            results.append(RbsStoreStock(external_store_id=str(store_id), can_pick_up=True))
        return results

    def _call(self, action_path: str, *, data: dict) -> dict:
        body = {"websiteId": self._website_id, "sectionId": None, "pageId": None}
        body.update(data)
        try:
            response = self._client.post(
                self._base_url + action_path,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-HTTP-Method-Override": "GET",
                },
            )
        except httpx.TimeoutException as exc:
            raise RbsPlatformError(f"timeout calling {action_path}") from exc
        except httpx.RequestError as exc:
            raise RbsPlatformError(f"network error calling {action_path}: {exc}") from exc

        if response.status_code == 429:
            raise RbsPlatformError(f"rate limited (429) calling {action_path}")
        if response.status_code >= 400:
            raise RbsPlatformError(f"HTTP {response.status_code} calling {action_path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RbsPlatformError(f"{action_path} returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise RbsPlatformError(f"{action_path} returned an unexpected shape")
        return payload


def _get(item: dict, *path: str) -> object | None:
    node: object = item
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _parse_store(item: dict) -> RbsStore | None:
    external_store_id = _get(item, "common", "id")
    name = _get(item, "common", "title")
    if external_store_id is None or not isinstance(name, str) or not name.strip():
        return None
    fields = _get(item, "address", "fields") or {}
    lat = _get(item, "coordinates", "latitude")
    lon = _get(item, "coordinates", "longitude")
    url = _get(item, "common", "URL", "printMap")
    return RbsStore(
        external_store_id=str(external_store_id),
        name=name.strip(),
        street=fields.get("street") if isinstance(fields, dict) else None,
        postal_code=fields.get("zipCode") if isinstance(fields, dict) else None,
        city=fields.get("locality") if isinstance(fields, dict) else None,
        latitude=Decimal(str(lat)) if isinstance(lat, (int, float)) else None,
        longitude=Decimal(str(lon)) if isinstance(lon, (int, float)) else None,
        url=url if isinstance(url, str) else None,
    )
