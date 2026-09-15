"""Turns already-evaluated business objects into a Discord Embed.

No business logic: this module never decides whether an opportunity is
good — it only presents what the monitoring/matcher/decision layers
already produced. It imports business objects (the allowed direction);
engine/ never imports this.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import discord

from engine.alerting import AlertTier
from engine.change_detection import EventType

if TYPE_CHECKING:
    from engine.alerting import OpportunityIntelligence
    from engine.change_detection import MonitoringEvent
    from engine.opportunity import OpportunityResult, PurchaseRecommendation
    from market_data.estimator import Confidence, ResaleEstimate
    from products.matcher import MatchResult
    from products.observation import ProductObservation
    from purchase.models import PurchaseIntent

_RECOMMENDATION_LABELS: dict[str, str] = {
    "strong_buy": "STRONG BUY",
    "buy": "BUY",
    "alert_only": "ALERT ONLY",
    "reject": "REJECT",
}

_EVENT_TITLES: dict[EventType, str] = {
    EventType.STOCK_AVAILABLE: "📦 Back in Stock",
    EventType.STOCK_UNAVAILABLE: "📦 Out of Stock",
    EventType.PRICE_DROP: "💰 Price Drop",
    EventType.PRICE_INCREASE: "📈 Price Increase",
    EventType.TARGET_PRICE_REACHED: "🎯 Target Price Reached",
    EventType.PRICE_CHANGED: "💱 Price Changed",
    EventType.OPPORTUNITY_SCORE_IMPROVED: "🚀 Opportunity Improved",
}

_EVENT_COLORS: dict[EventType, discord.Color] = {
    EventType.STOCK_AVAILABLE: discord.Color.green(),
    EventType.STOCK_UNAVAILABLE: discord.Color.greyple(),
    EventType.PRICE_DROP: discord.Color.green(),
    EventType.PRICE_INCREASE: discord.Color.orange(),
    EventType.TARGET_PRICE_REACHED: discord.Color.gold(),
    EventType.PRICE_CHANGED: discord.Color.blue(),
    EventType.OPPORTUNITY_SCORE_IMPROVED: discord.Color.gold(),
}

_ALERT_TIER_LABELS: dict[AlertTier, str] = {
    AlertTier.IGNORE: "IGNORE",
    AlertTier.NEEDS_MARKET_DATA: "NEEDS MARKET DATA",
    AlertTier.WATCH: "WATCH",
    AlertTier.HIGH: "HIGH",
    AlertTier.URGENT: "URGENT",
}

_PRICE_EVENTS_WITH_PREVIOUS = (
    EventType.PRICE_DROP,
    EventType.PRICE_INCREASE,
    EventType.TARGET_PRICE_REACHED,
)


def format_event_embed(
    event: MonitoringEvent,
    observation: ProductObservation,
    match_result: MatchResult,
    *,
    opportunity: OpportunityResult | None = None,
    resale_estimate: ResaleEstimate | None = None,
    resale_confidence: Confidence | None = None,
    recommendation: PurchaseRecommendation | None = None,
    recommendation_reason: str | None = None,
    evaluation: OpportunityIntelligence | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=_EVENT_TITLES.get(event.event_type, str(event.event_type)),
        url=observation.url,
        color=_EVENT_COLORS.get(event.event_type, discord.Color.blurple()),
        timestamp=event.occurred_at,
    )
    embed.add_field(name="Product", value=observation.name, inline=False)
    embed.add_field(name="Merchant", value=observation.merchant, inline=True)
    embed.add_field(name="Price", value=f"{observation.price} {observation.currency}", inline=True)
    if event.previous_value is not None and event.event_type in _PRICE_EVENTS_WITH_PREVIOUS:
        embed.add_field(name="Previous price", value=event.previous_value, inline=True)
    embed.add_field(
        name="Availability",
        value="In stock" if observation.available else "Out of stock",
        inline=True,
    )
    embed.add_field(name="Match confidence", value=f"{match_result.confidence}%", inline=True)
    if opportunity is not None:
        embed.add_field(
            name="Estimated resale",
            value=f"{opportunity.estimated_resale_price} {observation.currency}",
            inline=True,
        )
        embed.add_field(
            name="Net profit",
            value=f"{opportunity.net_profit} {observation.currency}",
            inline=True,
        )
        embed.add_field(name="ROI", value=f"{opportunity.roi_pct}%", inline=True)
        embed.add_field(name="Margin", value=f"{opportunity.net_margin_pct}%", inline=True)
        embed.add_field(name="Opportunity", value=opportunity.status.value, inline=True)
    if resale_confidence is not None:
        embed.add_field(name="Resale confidence", value=resale_confidence.value, inline=True)
    if recommendation is not None:
        embed.add_field(
            name="Decision",
            value=_RECOMMENDATION_LABELS.get(recommendation.value, recommendation.value),
            inline=True,
        )
    if recommendation_reason is not None:
        embed.add_field(name="Reason", value=recommendation_reason, inline=False)
    if resale_estimate is not None and resale_estimate.sample_size > 0:
        embed.add_field(name="Market source", value=resale_estimate.source, inline=True)
        embed.add_field(
            name="Market sample size", value=str(resale_estimate.sample_size), inline=True
        )
        embed.add_field(
            name="Market confidence", value=resale_estimate.confidence.value, inline=True
        )
    if evaluation is not None:
        _add_opportunity_intelligence_fields(embed, evaluation)
    return embed


def _add_opportunity_intelligence_fields(
    embed: discord.Embed, evaluation: OpportunityIntelligence
) -> None:
    """Phase 31: the opportunity-scoring layer on top of whatever
    engine.opportunity/market_data fields format_event_embed already
    added above — never duplicates them, only adds Score/Priority/Reason
    codes, plus an explicit MARKET DATA MISSING callout for a real stock/
    price event on a listing with no resale estimate at all (a genuine
    restock is still worth knowing about even with nothing to judge
    profitability by yet — see engine/alerting.py's module docstring)."""
    if evaluation.alert_tier == AlertTier.NEEDS_MARKET_DATA:
        embed.add_field(
            name="⚠️ Market Data",
            value=(
                "MARKET DATA MISSING — a real stock/price event was detected, but no resale "
                "estimate is configured yet. Still being monitored."
            ),
            inline=False,
        )
        return
    embed.add_field(name="Score", value=f"{evaluation.score}/100", inline=True)
    embed.add_field(
        name="Priority",
        value=_ALERT_TIER_LABELS.get(evaluation.alert_tier, evaluation.alert_tier.value),
        inline=True,
    )
    if evaluation.resale_price_source is not None:
        embed.add_field(
            name="Resale price source", value=evaluation.resale_price_source, inline=True
        )
    if evaluation.reason_codes:
        embed.add_field(name="Reason codes", value=", ".join(evaluation.reason_codes), inline=False)


_PURCHASE_TITLE_COLORS: dict[str, discord.Color] = {
    "⚡ AUTO PURCHASE STARTED": discord.Color.blue(),
    "✅ PURCHASED": discord.Color.green(),
    "❌ PURCHASE FAILED": discord.Color.red(),
    "🟠 HUMAN ACTION REQUIRED": discord.Color.orange(),
    "🚨 BUY NOW": discord.Color.gold(),
}

# Phase 40 section 22: these two statuses are exactly "automation cannot
# finish this — a human must act right now" — both get the same
# actionable fields (a visible direct link + an explicit next action),
# on top of the normal lifecycle fields every purchase embed already
# carries. Not a second embed: enriching the one that already fires
# keeps this simple and doesn't double-notify for the same event.
_ACTIONABLE_TITLES = frozenset({"🟠 HUMAN ACTION REQUIRED", "🚨 BUY NOW"})


def format_purchase_embed(
    title: str,
    intent: PurchaseIntent,
    *,
    reason: str,
    final_price: Decimal | None = None,
    shipping_cost: Decimal | None = None,
    total_cost: Decimal | None = None,
    order_reference: str | None = None,
) -> discord.Embed:
    """Purchase-lifecycle embed — distinct from format_event_embed's "a
    price/stock event happened" alert. Never includes payment data: only
    product/merchant/price/quantity/order-reference fields ever appear
    here, matching purchase/base.py's guarantee end to end."""
    embed = discord.Embed(
        title=title,
        url=intent.url,
        color=_PURCHASE_TITLE_COLORS.get(title, discord.Color.blurple()),
        timestamp=intent.created_at,
    )
    embed.add_field(name="Product", value=intent.product_name, inline=False)
    embed.add_field(name="Merchant", value=intent.merchant, inline=True)
    embed.add_field(name="Observed price", value=f"{intent.observed_price}", inline=True)
    embed.add_field(name="Quantity", value=str(intent.quantity), inline=True)
    embed.add_field(name="Max allowed", value=f"{intent.max_price_allowed}", inline=True)
    if final_price is not None:
        embed.add_field(name="Product price", value=f"{final_price}", inline=True)
    if shipping_cost is not None:
        embed.add_field(name="Shipping", value=f"{shipping_cost}", inline=True)
    if total_cost is not None:
        embed.add_field(name="Total", value=f"{total_cost}", inline=True)
    if order_reference is not None:
        embed.add_field(name="Order ID", value=order_reference, inline=True)
    if title in _ACTIONABLE_TITLES:
        embed.add_field(name="Direct URL", value=intent.url, inline=False)
        embed.add_field(name="Action", value="OPEN CHECKOUT", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    return embed


def format_discovery_embed(
    *,
    product_name: str,
    merchant: str,
    url: str,
    price: Decimal,
    currency: str,
    ean: str | None,
) -> discord.Embed:
    """Phase 36: a brand-new auto-linked Listing found by discovery —
    distinct from format_event_embed's "an already-monitored listing
    changed" alert. Fired at most once per Listing (discovery only ever
    auto-links a given merchant/external_id pair once, see
    app/discovery.py::_get_or_create_listing). Never claims a purchase
    happened or is imminent — direct monitoring of the exact listing
    takes over from here."""
    embed = discord.Embed(
        title="🆕 New Listing Discovered",
        url=url,
        color=discord.Color.green(),
    )
    embed.add_field(name="Product", value=product_name, inline=False)
    embed.add_field(name="Merchant", value=merchant, inline=True)
    embed.add_field(name="Price", value=f"{price} {currency}", inline=True)
    if ean:
        embed.add_field(name="EAN", value=ean, inline=True)
    embed.add_field(
        name="Status", value="Exact EAN match — direct monitoring now active.", inline=False
    )
    return embed
