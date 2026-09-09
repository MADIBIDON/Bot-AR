"""Turns already-evaluated business objects into a Discord Embed.

No business logic: this module never decides whether an opportunity is
good — it only presents what the monitoring/matcher/decision layers
already produced. It imports business objects (the allowed direction);
engine/ never imports this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from engine.change_detection import EventType

if TYPE_CHECKING:
    from engine.change_detection import MonitoringEvent
    from products.matcher import MatchResult
    from products.observation import ProductObservation

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
    return embed
