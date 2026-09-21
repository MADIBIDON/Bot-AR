"""King Jouet monitoring connector — Phase 39.

King Jouet's own PRODUCT PAGE is genuinely blocked by DataDome (confirmed
again this session, 403 with the JS-challenge holding page) — never
worked around, per this project's standing rule.

BUT: `https://www.king-jouet.com/api/product/<ref>` is a real, distinct,
publicly reachable JSON endpoint that answers 200 with real structured
product data (price, availability, an embedded schema.org Product
JSON-LD) to a plain, honest GET with no special headers — confirmed live
this session against 3 real refs. Their own cart/basket API
(`/api/cart`, `/api/basket`) IS behind the same DataDome challenge as the
product page (confirmed 403 with the same holding-page marker) — this
connector only ever reads the product endpoint, never touches cart/
checkout, and this project builds no PurchaseConnector for King Jouet.

No EAN is exposed by this API (checked: neither at the top level nor
inside the embedded JSON-LD) — identity here relies on King Jouet's own
`ref`/`sku` (they're identical in every response observed), an exact-SKU
match per products/matcher.py's existing MPN/SKU-exact tier (confidence
95, still auto-link-eligible) — not a downgrade to name-only matching.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from connectors.base import BaseConnector, ConnectorError, ConnectorProduct, ProductNotFoundError

DEFAULT_USER_AGENT = "RetailOpportunityAssistant/0.1 (+read-only price monitor; no auto-purchase)"
DEFAULT_TIMEOUT_SECONDS = 8.0

_API_BASE = "https://www.king-jouet.com/api/product"
_PRODUCT_URL_TEMPLATE = (
    "https://www.king-jouet.com/jeu-jouet/jeux-societes/cartes-a-collectionner/ref-{ref}"
)


class KingJouetConnector(BaseConnector):
    def __init__(
        self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, user_agent: str = DEFAULT_USER_AGENT
    ) -> None:
        # Phase 23/34 pattern: one persistent client for this connector's
        # whole lifetime (built once in connectors/defaults.py).
        self._client = httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    def get_product(self, external_id: str) -> ConnectorProduct:
        # scripts/watch.py's generic add-by-URL flow would extract
        # "ref-1034916" (the real product page's own URL segment) rather
        # than the bare "1034916" this API actually wants — normalize
        # both shapes rather than silently failing on one of them.
        ref = external_id.removeprefix("ref-")
        try:
            response = self._client.get(f"{_API_BASE}/{ref}")
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"timeout fetching King Jouet product {ref}") from exc
        except httpx.RequestError as exc:
            raise ConnectorError(f"network error fetching King Jouet product {ref}: {exc}") from exc

        if response.status_code == 404:
            raise ProductNotFoundError(f"King Jouet has no product for ref {ref}")
        if response.status_code == 429:
            raise ConnectorError(f"rate limited (429) fetching King Jouet product {ref}")
        if response.status_code == 403:
            # The same DataDome holding page the cart/basket API returns —
            # if this ever starts happening on the product endpoint too,
            # that means it's no longer safely distinct from the blocked
            # surface, and this connector must stop, not adapt around it.
            raise ConnectorError(
                f"King Jouet returned 403 fetching product {ref} — the product API may now be "
                "behind the same protection as the rest of the site; never worked around."
            )
        if response.status_code >= 400:
            raise ConnectorError(f"HTTP {response.status_code} fetching King Jouet product {ref}")

        try:
            data = response.json()
        except ValueError as exc:
            raise ConnectorError(f"King Jouet product {ref} returned non-JSON") from exc

        name = data.get("label")
        price_info = data.get("price") or {}
        price = price_info.get("price")
        availability = data.get("availability") or {}
        if not name or price is None:
            raise ConnectorError(f"King Jouet product {ref} response is missing name/price")

        # King Jouet reports three INDEPENDENT channels, and their own
        # buying guide is explicit that they are not synchronised: "le
        # stock du site national et celui des magasins ne sont pas
        # synchronisés — surveillez toujours les deux". Collapsing them
        # into one boolean (as this did until now) means announcing
        # "in stock" with a link to a web page where the item cannot
        # actually be ordered. Keep `available` as the union so nothing
        # downstream changes behaviour, but carry WHICH channel it is so
        # the alert can say where to go.
        on_web = bool(availability.get("isAvailableOnWeb"))
        ship_from_store = bool(availability.get("isAvailableForShipFromStore"))
        from_other = bool(availability.get("isAvailableFromOther"))
        available = on_web or ship_from_store or from_other

        channels = []
        if on_web:
            channels.append("web")
        if ship_from_store:
            channels.append("retrait magasin")
        if from_other:
            channels.append("autre vendeur")
        stores = availability.get("stores")
        store_count = len(stores) if isinstance(stores, list) else 0
        availability_detail = None
        if channels:
            availability_detail = " + ".join(channels)
            if store_count:
                availability_detail += f" ({store_count} magasin{'s' if store_count > 1 else ''})"

        real_ref = data.get("ref") or ref

        # Confirmed live shape: "images" is a list of absolute URLs, e.g.
        # ["https://images.king-jouet.com/6/gu1034916_6.jpg"]. Anything
        # else is ignored rather than guessed at — a broken thumbnail on
        # a drop alert is noise.
        images = data.get("images")
        image_url = None
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str) and first.startswith("http"):
                image_url = first

        return ConnectorProduct(
            external_id=ref,
            name=name.strip(),
            price=Decimal(str(price)),
            currency="EUR",
            available=available,
            seller="King Jouet",
            url=_PRODUCT_URL_TEMPLATE.format(ref=real_ref),
            ean=None,  # not exposed by this API — see module docstring
            mpn=real_ref,
            image_url=image_url,
            availability_detail=availability_detail,
        )
