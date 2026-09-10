"""Resolves the resale price to feed into engine/opportunity.py, from
either WatchRule.estimated_resale_price (manual mode) or live market data
(market mode). The one place allowed to depend on both engine/opportunity
and market_data/ — same reasoning as app/notify.py depending on both
engine/ and notifications/discord/.

A market-data failure (source down, misconfigured, no comparable
observations) never raises out of here and never fabricates a price:
callers get (None, estimate_or_none) and Opportunity is simply marked
unavailable. The monitoring pipeline itself is never affected.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from market_data.estimator import estimate_resale_price
from market_data.product_filter import is_comparable

if TYPE_CHECKING:
    from database.models import WatchRule
    from market_data.cache import TTLCache
    from market_data.estimator import ResaleEstimate
    from market_data.registry import MarketDataRegistry

logger = logging.getLogger(__name__)

MARKET_SEARCH_LIMIT = 20


def resolve_resale_estimate(
    watch_rule: WatchRule,
    market_registry: MarketDataRegistry,
    cache: TTLCache,
) -> ResaleEstimate | None:
    """None if market data could not be obtained at all (misconfigured
    source, network failure) — as opposed to a ResaleEstimate with
    sample_size=0, which means the fetch worked but nothing comparable
    was found."""
    if not watch_rule.market_source:
        logger.warning("watch_rule=%s mode=market but no market_source configured", watch_rule.id)
        return None

    product_name = watch_rule.product.name
    cache_key = f"{watch_rule.market_source}:{product_name}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    try:
        source = market_registry.get(watch_rule.market_source)
        observations = source.search(product_name, limit=MARKET_SEARCH_LIMIT)
    except Exception:
        logger.exception("watch_rule=%s market data fetch failed", watch_rule.id)
        return None

    filtered = [obs for obs in observations if is_comparable(product_name, obs.product_name)]
    estimate = estimate_resale_price(filtered)
    cache.set(cache_key, estimate)
    return estimate


def resolve_resale_price_for_opportunity(
    watch_rule: WatchRule,
    market_registry: MarketDataRegistry | None = None,
    cache: TTLCache | None = None,
) -> tuple[Decimal | None, ResaleEstimate | None]:
    """Returns (resale_price_to_use, resale_estimate_or_none).

    resale_price_mode == "manual" (the default, and Phase 15's only
    behavior): returns WatchRule.estimated_resale_price as-is, no market
    lookup, no ResaleEstimate — identical to before this phase.

    resale_price_mode == "market": returns the estimate's median price
    when one could be computed with at least one comparable observation;
    otherwise (no registry/cache provided, fetch failed, or zero
    comparable observations) returns (None, estimate_or_none) — Opportunity
    is then unavailable rather than built on a fabricated number.
    """
    if watch_rule.resale_price_mode != "market":
        return watch_rule.estimated_resale_price, None

    if market_registry is None or cache is None:
        return None, None

    estimate = resolve_resale_estimate(watch_rule, market_registry, cache)
    if estimate is not None and estimate.estimated_price is not None:
        return estimate.estimated_price, estimate
    return None, estimate
