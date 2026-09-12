"""Builds one engine.ranking.OpportunityCandidate from a WatchRule's
current state — the one place allowed to depend on database/, products/,
engine/, market_data/, and app.resale all at once, so scripts/watch.py's
`opportunities` command doesn't have to mix CLI display logic with this
lookup/compute plumbing itself.

Deliberately small: no caching of its own (app.resale's TTLCache already
covers market lookups), no batching — one WatchRule in, one candidate (or
None) out. Never raises: a WatchRule with no listing, no stored
observation yet, or a market lookup failure simply yields None or a
candidate with no resale estimate — the ranking engine already knows how
to exclude those cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.resale import resolve_resale_price_for_opportunity
from database import crud
from database.time_utils import ensure_utc
from engine.opportunity import build_opportunity_inputs, evaluate_opportunity
from engine.ranking import OpportunityCandidate
from products.matcher import match_product
from products.observation import ProductObservation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import WatchRule
    from market_data.cache import TTLCache
    from market_data.registry import MarketDataRegistry


def build_opportunity_candidate(
    session: Session,
    watch_rule: WatchRule,
    market_registry: MarketDataRegistry | None,
    cache: TTLCache | None,
) -> OpportunityCandidate | None:
    """None when there is nothing yet to rank: no listing attached to the
    rule, or no monitoring check has ever been stored for it."""
    if watch_rule.listing_id is None or watch_rule.listing is None:
        return None

    records = crud.list_observation_records_for_listing(session, watch_rule.listing_id)
    if not records:
        return None
    latest = records[-1]
    observed_at = ensure_utc(latest.observed_at)

    observation = ProductObservation(
        merchant=watch_rule.listing.merchant.name,
        external_id=latest.external_id,
        name=latest.name,
        price=latest.price,
        currency=latest.currency,
        available=latest.available,
        url=watch_rule.listing.url,
        observed_at=observed_at,
        ean=latest.ean,
        mpn=latest.mpn,
        seller=latest.seller,
    )
    match_result = match_product(
        watch_rule.product, observation, expected_listing=watch_rule.listing
    )

    resale_price, resale_estimate = resolve_resale_price_for_opportunity(
        watch_rule, market_registry, cache
    )

    net_profit = roi_pct = net_margin_pct = None
    if resale_price is not None:
        # Phase 26: was building OpportunityConfig from watch_rule's own
        # fee fields directly, ignoring a Product-level fallback entirely
        # — a rule relying on its Product's shared fee/threshold config
        # (Phase 25) would silently rank differently here (all fees = 0,
        # no thresholds) than the real Discord alert for the exact same
        # opportunity. build_opportunity_inputs() is the one place this
        # assembly happens now, shared with app/notify.py and
        # purchase/engine.py.
        config, thresholds = build_opportunity_inputs(watch_rule, resale_price)
        opportunity = evaluate_opportunity(observation.price, config, thresholds)
        if opportunity is not None:
            net_profit = opportunity.net_profit
            roi_pct = opportunity.roi_pct
            net_margin_pct = opportunity.net_margin_pct

    return OpportunityCandidate(
        watch_rule_id=watch_rule.id,
        merchant=observation.merchant,
        product_name=watch_rule.product.name,
        purchase_price=observation.price,
        estimated_resale_price=resale_price,
        net_profit=net_profit,
        roi_pct=roi_pct,
        net_margin_pct=net_margin_pct,
        resale_confidence=resale_estimate.confidence.value if resale_estimate else None,
        in_stock=observation.available,
        match_confidence=match_result.confidence,
        market_sample_size=resale_estimate.sample_size if resale_estimate else None,
    )
