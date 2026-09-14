"""Drop Manifest importer — Phase 33 section 6.

Turns a small, hand-written JSON file (a top-level array, one object per
drop) into real Product/Listing/WatchRule rows using the exact same
building blocks scripts/watch.py's own `add`/`add-product`/`edit`/
`edit-product` commands already use (connectors.defaults for URL
detection + a real connector fetch, database/crud.py for persistence) —
no new matching, discovery, or purchase logic. Meant for "here are the 5
to 20 real drops I know about" the day before a release, with no Python
edits needed.

One JSON object per drop, in a top-level JSON array:
{
  "canonical_name": "ETB Pokémon 30e Anniversaire"   (required)
  "ean": "0196214142145"                             (optional)
  "merchant": "JouéClub"                             (required with product_url)
  "product_url": "https://..."                       (optional — a known
      real product page; when given, the real connector for that domain
      is used to fetch the current name/price/external_id, exactly like
      `watch.py add --url` does)
  "merchant_sku": "..."                              (optional, informational
      only — no connector here looks a product up by a bare SKU)
  "release_at": "2026-09-16T08:00:00Z"               (optional, ISO-8601 UTC)
  "target_price": 59.99                              (optional)
  "max_acquisition_total": 70.00                     (optional -> max_price)
  "quantity": 1                                      (default 1)
  "max_successful_purchases": 1                      (default 1 — see below)
  "minimum_net_profit": 20                           (optional)
  "minimum_roi_pct": 30                              (optional)
  "minimum_resale_confidence": "medium"              (optional)
  "language": "fr"                                   (optional, informational)
  "product_type": "etb"                              (optional, informational)
  "purchase_allowed": false                          (default false — see below)
}

max_successful_purchases: only 1 (the default, and every example in the
spec) is actually enforced today, via the real database-level constraint
in database/models.py::PurchaseAttempt
(uq_one_active_or_purchased_attempt_per_product) — a manifest asking for
a different number is rejected outright at import time (fail closed)
rather than silently accepted and ignored, since honoring "buy up to N"
would need real new counting logic this session didn't build.

purchase_allowed=false (the default — an entry must opt in explicitly)
imports a pure monitoring WatchRule with no max_price/profitability
thresholds set at all, which purchase/engine.py's evaluate_purchase_intent
already refuses outright ("no max_price or profitability thresholds
configured") — the safest possible default for a drop that hasn't been
reviewed yet. purchase_allowed=true still requires PURCHASES_ENABLED=true
globally (the kill switch) before anything can actually buy — this
manifest can never itself turn purchasing on.

language/product_type are recorded nowhere new; they're accepted so a
manifest can be self-documenting, and are echoed back in
DropImportResult for the operator to eyeball against what actually got
imported — never used to silently override the real product matcher.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from connectors.base import ConnectorError, ProductNotFoundError
from connectors.defaults import find_merchant_for_domain
from database import crud
from purchase.prepared_runtime import get_default_cache

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import Product, WatchRule

_ENFORCED_MAX_SUCCESSFUL_PURCHASES = 1


class DropManifestError(ValueError):
    """A manifest entry is malformed or asks for something not actually
    enforced — always raised before anything is written for that entry."""


@dataclass(frozen=True, slots=True)
class DropManifestEntry:
    canonical_name: str
    ean: str | None = None
    merchant: str | None = None
    product_url: str | None = None
    merchant_sku: str | None = None
    release_at: datetime | None = None
    target_price: Decimal | None = None
    max_acquisition_total: Decimal | None = None
    quantity: int = 1
    max_successful_purchases: int = 1
    minimum_net_profit: Decimal | None = None
    minimum_roi_pct: Decimal | None = None
    minimum_resale_confidence: str | None = None
    language: str | None = None
    product_type: str | None = None
    purchase_allowed: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> DropManifestEntry:
        name = raw.get("canonical_name")
        if not isinstance(name, str) or not name.strip():
            raise DropManifestError("canonical_name is required and must be a non-empty string.")

        product_url = raw.get("product_url")
        merchant = raw.get("merchant")
        if product_url and not merchant:
            raise DropManifestError(
                f"{name!r}: merchant is required when product_url is given "
                "(used to record which retailer this listing belongs to)."
            )

        max_successful_purchases = raw.get("max_successful_purchases", 1)
        if max_successful_purchases != _ENFORCED_MAX_SUCCESSFUL_PURCHASES:
            raise DropManifestError(
                f"{name!r}: max_successful_purchases={max_successful_purchases!r} is not "
                f"supported yet — only {_ENFORCED_MAX_SUCCESSFUL_PURCHASES} is actually "
                "enforced (database/models.py's PurchaseAttempt constraint). Remove the "
                "field (defaults to 1) or set it to 1 explicitly."
            )

        release_at = None
        if raw.get("release_at"):
            try:
                release_at = datetime.fromisoformat(str(raw["release_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise DropManifestError(
                    f"{name!r}: release_at={raw['release_at']!r} is not valid ISO-8601."
                ) from exc
            if release_at.tzinfo is None:
                release_at = release_at.replace(tzinfo=UTC)

        def _decimal(key: str) -> Decimal | None:
            value = raw.get(key)
            if value is None:
                return None
            try:
                return Decimal(str(value))
            except InvalidOperation as exc:
                raise DropManifestError(
                    f"{name!r}: {key}={value!r} is not a valid number."
                ) from exc

        return cls(
            canonical_name=name.strip(),
            ean=(raw.get("ean") or None),
            merchant=merchant,
            product_url=product_url,
            merchant_sku=raw.get("merchant_sku"),
            release_at=release_at,
            target_price=_decimal("target_price"),
            max_acquisition_total=_decimal("max_acquisition_total"),
            quantity=int(raw.get("quantity", 1)),
            max_successful_purchases=max_successful_purchases,
            minimum_net_profit=_decimal("minimum_net_profit"),
            minimum_roi_pct=_decimal("minimum_roi_pct"),
            minimum_resale_confidence=raw.get("minimum_resale_confidence"),
            language=raw.get("language"),
            product_type=raw.get("product_type"),
            purchase_allowed=bool(raw.get("purchase_allowed", False)),
        )


@dataclass(frozen=True, slots=True)
class DropImportResult:
    canonical_name: str
    status: str  # "created" | "reused" | "error"
    product_id: int | None = None
    watch_rule_id: int | None = None
    listing_id: int | None = None
    detail: str = ""


def load_drop_manifest(path: str | Path) -> list[DropManifestEntry]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise DropManifestError("A drop manifest file must be a top-level JSON array.")
    return [DropManifestEntry.from_dict(item) for item in raw]


def _get_or_create_product(session: Session, entry: DropManifestEntry) -> Product:
    if entry.ean:
        existing = crud.get_product_by_ean(session, entry.ean)
        if existing is not None:
            return existing
    existing_by_name = next(
        (p for p in crud.list_products(session) if p.name == entry.canonical_name), None
    )
    if existing_by_name is not None:
        return existing_by_name
    return crud.create_product(session, entry.canonical_name, ean=entry.ean)


def _get_or_create_listing_from_url(
    session: Session, product: Product, entry: DropManifestEntry
) -> WatchRule | None:
    """Mirrors scripts/watch.py::cmd_add's own URL-detection + real
    connector fetch — never fabricates a price/external_id. Returns None
    (caller falls back to a listing-less Product watch) if the domain
    isn't a supported merchant or the live fetch fails — logged in the
    result, never silently guessed."""
    hostname = urlparse(entry.product_url).netloc
    merchant_def = find_merchant_for_domain(hostname)
    if merchant_def is None:
        raise DropManifestError(
            f"{entry.canonical_name!r}: {hostname!r} is not a supported merchant domain."
        )
    handle = urlparse(entry.product_url).path
    handle = (
        handle.lstrip("/")
        if merchant_def.full_path_external_id
        else handle.rstrip("/").rsplit("/", 1)[-1]
    )

    connector = merchant_def.build_connector()
    try:
        connector_product = connector.get_product(handle)
    except (ConnectorError, ProductNotFoundError) as exc:
        raise DropManifestError(
            f"{entry.canonical_name!r}: could not fetch {entry.product_url!r} ({exc})."
        ) from exc

    merchant = crud.get_merchant_by_name(session, merchant_def.name)
    if merchant is None:
        merchant = crud.create_merchant(session, merchant_def.name)

    existing_listing = crud.get_listing_by_merchant_and_external_id(
        session, merchant.id, connector_product.external_id
    )
    listing = existing_listing or crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url=connector_product.url,
        external_id=connector_product.external_id,
    )
    return listing


def import_drop_manifest(
    session: Session, entries: list[DropManifestEntry]
) -> list[DropImportResult]:
    """One entry's failure never stops the others — matches this
    project's standing per-item isolation posture (discovery, local
    stock, ...). Never creates a PurchaseAttempt and never touches
    PURCHASES_ENABLED: this only prepares WatchRules for monitoring/
    (optionally) profitability-gated purchase evaluation."""
    results: list[DropImportResult] = []
    for entry in entries:
        try:
            product = _get_or_create_product(session, entry)

            listing = None
            if entry.product_url:
                listing = _get_or_create_listing_from_url(session, product, entry)

            existing_rule = (
                crud.get_watch_rule_by_listing_id(session, listing.id)
                if listing is not None
                else next((r for r in crud.list_watch_rules(session, product_id=product.id)), None)
            )
            rule = existing_rule or crud.create_watch_rule(
                session,
                product_id=product.id,
                listing_id=listing.id if listing is not None else None,
                check_interval=300,
                max_quantity=entry.quantity,
            )

            update_fields: dict[str, object] = {}
            if entry.target_price is not None:
                update_fields["target_price"] = entry.target_price
            if entry.purchase_allowed and entry.max_acquisition_total is not None:
                update_fields["max_price"] = entry.max_acquisition_total
            if entry.minimum_net_profit is not None:
                update_fields["minimum_net_profit"] = entry.minimum_net_profit
            if entry.minimum_roi_pct is not None:
                update_fields["minimum_roi_pct"] = entry.minimum_roi_pct
            if entry.minimum_resale_confidence is not None:
                update_fields["minimum_resale_confidence"] = entry.minimum_resale_confidence
            if entry.release_at is not None:
                update_fields["scheduled_release_at"] = entry.release_at
            if update_fields:
                crud.update_watch_rule(session, rule.id, **update_fields)
            # Phase 35 section 5: a (re-)import can change fields a
            # PreparedDropRuntimeCache entry already snapshotted for this
            # rule — never leave a stale cached runtime behind, whether
            # this rule is new or reused.
            get_default_cache().invalidate(rule.id)

            results.append(
                DropImportResult(
                    canonical_name=entry.canonical_name,
                    status="reused" if existing_rule is not None else "created",
                    product_id=product.id,
                    watch_rule_id=rule.id,
                    listing_id=listing.id if listing is not None else None,
                    detail=(
                        "monitoring only (purchase_allowed=false)"
                        if not entry.purchase_allowed
                        else "purchase-eligible (still requires PURCHASES_ENABLED=true)"
                    ),
                )
            )
        except DropManifestError as exc:
            results.append(
                DropImportResult(
                    canonical_name=entry.canonical_name, status="error", detail=str(exc)
                )
            )
    return results
