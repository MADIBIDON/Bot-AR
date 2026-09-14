"""PreparedDropRuntime — Phase 34 (100ms warm-path target).

Everything the warm path needs must already be resolved and sitting in
memory before a drop, so the hot path (stock signal received -> first
checkout network request dispatched) never reads YAML/JSON, never does a
non-essential DB lookup, and never resolves a merchant dynamically. The
ONE database read this needs (loading the WatchRule/Product/Listing
once) happens here, at "arm" time — well before the drop — never on the
hot path itself.

Trade-off, stated explicitly rather than hidden: the WatchRule ORM
object is cached by reference for the runtime's whole lifetime. Product-
identity fields (EAN/MPN/external_id) and quantity/max_price snapshotted
this way cannot change mid-drop without re-arming — an accepted,
documented cost of hitting the <=100ms target, not a silent gap. The
kill switch (PURCHASES_ENABLED) is NOT part of this snapshot: it is
re-read fresh from the environment on every attempt (see
purchase/engine.py's policy_provider, Phase 33 P0#3) precisely because
that one check must never be stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from engine.decision import effective_max_price

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import WatchRule
    from purchase.base import PurchaseConnector
    from purchase.config import PurchasePolicy


class DropNotArmableError(ValueError):
    """Raised at build time (never on the hot path) when a WatchRule is
    missing something the warm path cannot function without."""


@dataclass(frozen=True, slots=True)
class PreparedDropRuntime:
    watch_rule: WatchRule  # cached ORM object — see module docstring
    watch_rule_id: int
    product_id: int
    listing_id: int
    merchant: str
    product_name: str
    url: str
    expected_ean: str | None
    expected_mpn: str | None
    expected_external_id: str | None
    quantity: int
    max_price_allowed: Decimal
    policy: PurchasePolicy
    merchant_domains: tuple[str, ...]
    connector: PurchaseConnector


def build_prepared_drop_runtime(
    session: Session,
    watch_rule_id: int,
    *,
    connector: PurchaseConnector,
    policy: PurchasePolicy,
    merchant_domains: tuple[str, ...],
) -> PreparedDropRuntime:
    """Called once, ahead of the drop ("arming" it) — the only place this
    module touches the database. Raises DropNotArmableError rather than
    guessing if the rule has no listing or no usable price ceiling."""
    from database import crud

    rule = crud.get_watch_rule(session, watch_rule_id)
    if rule is None:
        raise DropNotArmableError(f"WatchRule {watch_rule_id} does not exist.")
    if rule.listing is None:
        raise DropNotArmableError(f"WatchRule {watch_rule_id} has no Listing — cannot arm.")

    max_price = effective_max_price(rule)
    if max_price is None:
        raise DropNotArmableError(
            f"WatchRule {watch_rule_id} has no max_price configured — refuse to arm a warm "
            "path with no price ceiling."
        )

    product = rule.product
    listing = rule.listing
    return PreparedDropRuntime(
        watch_rule=rule,
        watch_rule_id=rule.id,
        product_id=rule.product_id,
        listing_id=rule.listing_id,
        merchant=listing.merchant.name,
        product_name=product.name,
        url=listing.url,
        expected_ean=product.ean,
        expected_mpn=product.mpn,
        expected_external_id=listing.external_id,
        quantity=rule.max_quantity,
        max_price_allowed=max_price,
        policy=policy,
        merchant_domains=merchant_domains,
        connector=connector,
    )
