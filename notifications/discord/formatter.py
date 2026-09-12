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

from engine.change_detection import EventType

if TYPE_CHECKING:
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
}

_EVENT_COLORS: dict[EventType, discord.Color] = {
    EventType.STOCK_AVAILABLE: discord.Color.green(),
    EventType.STOCK_UNAVAILABLE: discord.Color.greyple(),
    EventType.PRICE_DROP: discord.Color.green(),
    EventType.PRICE_INCREASE: discord.Color.orange(),
    EventType.TARGET_PRICE_REACHED: discord.Color.gold(),
    EventType.PRICE_CHANGED: discord.Color.blue(),
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
    return embed


_PURCHASE_TITLE_COLORS: dict[str, discord.Color] = {
    "PURCHASE STARTED": discord.Color.blue(),
    "PURCHASE SUCCESS": discord.Color.green(),
    "PURCHASE FAILED": discord.Color.red(),
    "HUMAN ACTION REQUIRED": discord.Color.orange(),
    "AUTOMATED CHECKOUT UNSUPPORTED": discord.Color.orange(),
}


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
    embed.add_field(name="Reason", value=reason, inline=False)
    return embed
