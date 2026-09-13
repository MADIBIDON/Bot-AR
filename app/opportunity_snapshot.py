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

Phase 31: also the home of evaluate_opportunity_intelligence(), the single
composed entrypoint the spec calls `evaluate_opportunity(...)` — score,
priority/alert tier, net profit, ROI, margin, confidence, reason_codes,
all from one call. It deliberately does not reimplement any of that
math: it calls build_opportunity_candidate() (above), then
engine.ranking.rank_opportunities() with a single-item list (the exact
same scoring path scripts/watch.py's `opportunities` command already
uses for every candidate), then engine.alerting's thin reason-code/tier
layer on top of that one already-ranked result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.resale import resolve_resale_confidence, resolve_resale_price_for_opportunity
from database import crud
from database.time_utils import ensure_utc
from engine.alerting import OpportunityIntelligence, build_opportunity_intelligence
from engine.decision import effective_market_source, effective_resale_price_mode
from engine.opportunity import build_opportunity_inputs, evaluate_opportunity
from engine.ranking import (
    OpportunityCandidate,
    RankingConfig,
    RankingThresholds,
    rank_opportunities,
)
from products.matcher import match_product
from products.observation import ProductObservation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import Product, WatchRule
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
    resale_confidence_value: str | None = None
    resale_price_source: str | None = None
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
        # Phase 31 fix: previously left None for manual mode (only ever
        # set from a market ResaleEstimate), which the ranking engine then
        # scored as a hardcoded full 100 confidence — an untrusted manual
        # guess must never automatically outrank a well-sampled market
        # estimate. resolve_resale_confidence() already applies the right
        # rule (manual: "high" only if estimated_resale_trusted, else
        # "low"; market: whatever the estimator itself concluded) — it was
        # already used by app/notify.py's Discord embed, just not wired
        # into the candidate the ranking/alerting engine scores.
        resale_confidence_value = resolve_resale_confidence(watch_rule, resale_estimate).value
        resale_price_source = (
            (effective_market_source(watch_rule) or "unknown")
            if effective_resale_price_mode(watch_rule) == "market"
            else "manual"
        )

    return OpportunityCandidate(
        watch_rule_id=watch_rule.id,
        merchant=observation.merchant,
        product_name=watch_rule.product.name,
        purchase_price=observation.price,
        estimated_resale_price=resale_price,
        net_profit=net_profit,
        roi_pct=roi_pct,
        net_margin_pct=net_margin_pct,
        resale_confidence=resale_confidence_value,
        in_stock=observation.available,
        match_confidence=match_result.confidence,
        market_sample_size=resale_estimate.sample_size if resale_estimate else None,
        resale_price_source=resale_price_source,
    )


def evaluate_opportunity_intelligence(
    session: Session,
    watch_rule: WatchRule,
    market_registry: MarketDataRegistry | None,
    cache: TTLCache | None,
    *,
    ranking_config: RankingConfig | None = None,
    ranking_thresholds: RankingThresholds | None = None,
) -> OpportunityIntelligence | None:
    """Phase 31's single entrypoint: build the candidate, score it through
    the existing ranking engine (unchanged, unduplicated), then attach an
    alert tier and reason codes. None only when build_opportunity_candidate
    itself returns None (no listing or no observation yet — nothing to
    evaluate at all, not even "needs market data")."""
    candidate = build_opportunity_candidate(session, watch_rule, market_registry, cache)
    if candidate is None:
        return None
    ranking_config = ranking_config or RankingConfig()
    ranked = rank_opportunities([candidate], ranking_config, ranking_thresholds)[0]
    return build_opportunity_intelligence(candidate, ranked, ranking_config)


def best_buy_source_for_product(
    session: Session,
    product: Product,
    market_registry: MarketDataRegistry | None,
    cache: TTLCache | None,
    *,
    ranking_config: RankingConfig | None = None,
    ranking_thresholds: RankingThresholds | None = None,
) -> OpportunityIntelligence | None:
    """Phase 31 section 10: when a Product has more than one monitored
    Listing (multi-retailer), compares every one of them through the same
    ranking engine every other evaluation in this module uses and returns
    the single best-ranked one — the spec's "BEST BUY SOURCE". Never
    removes or disables any other Listing/WatchRule: this is a read-only
    comparison, called on demand (a report, a Discord embed extra field),
    not something that mutates monitoring state.

    None when no WatchRule of this Product has both a listing and at
    least one observation yet, or when every one of them is excluded
    (out of stock / no resale estimate) — there being nothing to declare
    "best" is not an error."""
    rules = crud.list_watch_rules(session, product_id=product.id)
    candidates = [
        c
        for rule in rules
        if (c := build_opportunity_candidate(session, rule, market_registry, cache)) is not None
    ]
    if not candidates:
        return None

    ranking_config = ranking_config or RankingConfig()
    ranked_list = rank_opportunities(candidates, ranking_config, ranking_thresholds)
    candidates_by_rule_id = {c.watch_rule_id: c for c in candidates}

    best_ranked = next((r for r in ranked_list if not r.excluded), None)
    if best_ranked is None:
        return None
    return build_opportunity_intelligence(
        candidates_by_rule_id[best_ranked.watch_rule_id], best_ranked, ranking_config
    )
