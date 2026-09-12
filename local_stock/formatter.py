"""Discord embed for one local-stock event — Phase 29. Deliberately not
notifications/discord/formatter.py::format_event_embed(): that function's
signature is built around MonitoringEvent/ProductObservation/MatchResult,
none of which a local-stock check produces (there's a store, not a
match_result). Same visual conventions (title per event type, a color,
one field per fact) — just a different, smaller set of facts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from database.models import LocalStockEvent, RetailStore

_TITLES = {
    "local_restock": "POKÉMON LOCAL STOCK",
    "local_click_and_collect": "POKÉMON LOCAL STOCK",
}

_COLOR = discord.Color.green()


def format_local_stock_embed(
    event: LocalStockEvent,
    *,
    store: RetailStore,
    product_name: str,
    price: str | None,
    url: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=_TITLES.get(event.event_type, "POKÉMON LOCAL STOCK"),
        url=url,
        color=_COLOR,
        timestamp=event.occurred_at,
    )
    embed.add_field(name="Product", value=product_name, inline=False)
    embed.add_field(name="Retailer", value=store.retailer, inline=True)
    embed.add_field(name="Store", value=store.name, inline=True)
    embed.add_field(name="City", value=store.city or "unknown", inline=True)
    if price is not None:
        embed.add_field(name="Price", value=price, inline=True)
    embed.add_field(
        name="Status",
        value=event.current_state.replace("_", " ").upper(),
        inline=True,
    )
    embed.add_field(
        name="Click & Collect",
        value="YES" if event.current_state == "click_and_collect" else "NO",
        inline=True,
    )
    if store.address:
        embed.add_field(name="Address", value=store.address, inline=False)
    return embed
