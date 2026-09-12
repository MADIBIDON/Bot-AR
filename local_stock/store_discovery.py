"""Store directory discovery — Phase 29. Fetches a retailer's full,
public store list (local_stock.rbs_platform.RbsPlatformClient) and
upserts it into RetailStore. Idempotent by construction: crud.
upsert_retail_store() keys on (retailer, external_store_id), so running
this daily never duplicates a store.

Only registered for retailers actually confirmed on the Rbs/Proximis
platform this session — JouéClub and La Grande Récré. See
connectors/defaults.py's module docstring for Fnac/King Jouet/Smyths
(confirmed blocked at the domain level, no separate store API found) and
Cultura/E.Leclerc (different platform, not researched this session).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from database import crud
from local_stock.rbs_platform import RbsPlatformClient, RbsPlatformError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

RBS_PLATFORM_RETAILERS: dict[str, dict[str, object]] = {
    "JouéClub": {"shop_domain": "www.joueclub.fr", "website_id": 100185},
    "La Grande Récré": {"shop_domain": "www.lagranderecre.fr", "website_id": 100052},
}


def build_rbs_client(retailer: str) -> RbsPlatformClient | None:
    config = RBS_PLATFORM_RETAILERS.get(retailer)
    if config is None:
        return None
    return RbsPlatformClient(shop_domain=config["shop_domain"], website_id=config["website_id"])


def discover_stores(session: Session, retailer: str, *, now: datetime | None = None) -> int:
    """Returns the number of stores upserted. Raises nothing — a
    discovery failure for one retailer must never stop the others (same
    posture as discovery/ and connectors/); callers should catch
    RbsPlatformError around this call in a multi-retailer loop."""
    now = now or datetime.now(UTC)
    client = build_rbs_client(retailer)
    if client is None:
        raise RbsPlatformError(f"{retailer} is not on the Rbs platform — no store API registered")

    stores = client.list_all_stores()
    for store in stores:
        crud.upsert_retail_store(
            session,
            retailer=retailer,
            external_store_id=store.external_store_id,
            name=store.name,
            city=store.city,
            postal_code=store.postal_code,
            address=store.street,
            latitude=store.latitude,
            longitude=store.longitude,
            url=store.url,
            now=now,
        )
    return len(stores)
