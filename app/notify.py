"""Wires Monitoring -> Decision -> Opportunity -> Notification.

Depends on engine/, and notifications/discord/ — this is the one place
that is allowed to know about both. engine/ never imports discord.py or
anything under notifications/; notifications/discord/ never imports this
module or engine/monitoring.py. Kept as a light, manually-invoked function
for Phase 9 — not wired into a scheduler yet (no long-running process
consumes it).

Opportunity scoring (Phase 15) only ever enriches an embed that was
already going to be sent — it never triggers a notification on its own.
The existing notification policy (allowed decision + at least one event)
is unchanged.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from app.resale import resolve_resale_price_for_opportunity
from engine.decision import DecisionResult, evaluate
from engine.opportunity import OpportunityConfig, OpportunityResult, evaluate_opportunity
from notifications.discord.formatter import format_event_embed

if TYPE_CHECKING:
    import discord

    from database.models import WatchRule
    from engine.change_detection import MonitoringEvent
    from market_data.cache import TTLCache
    from market_data.estimator import ResaleEstimate
    from market_data.registry import MarketDataRegistry
    from products.matcher import MatchResult
    from products.observation import ProductObservation


class EmbedSender(Protocol):
    """What notify_events_if_allowed needs — satisfied by DiscordNotifier
    and trivially by a fake in tests, with no discord.py client required."""

    async def send_embed(self, embed: discord.Embed) -> None: ...


def _opportunity_for(
    watch_rule: WatchRule,
    purchase_price: Decimal,
    market_registry: MarketDataRegistry | None,
    cache: TTLCache | None,
) -> tuple[OpportunityResult | None, ResaleEstimate | None]:
    resale_price, resale_estimate = resolve_resale_price_for_opportunity(
        watch_rule, market_registry, cache
    )
    if resale_price is None:
        return None, resale_estimate
    config = OpportunityConfig(
        estimated_resale_price=resale_price,
        platform_fee_pct=watch_rule.platform_fee_pct or Decimal("0"),
        fixed_fee=watch_rule.fixed_fee or Decimal("0"),
        shipping_cost=watch_rule.shipping_cost or Decimal("0"),
        other_costs=watch_rule.other_costs or Decimal("0"),
    )
    return evaluate_opportunity(purchase_price, config), resale_estimate


async def notify_events_if_allowed(
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    events: tuple[MonitoringEvent, ...],
    notifier: EmbedSender,
    market_registry: MarketDataRegistry | None = None,
    cache: TTLCache | None = None,
) -> DecisionResult:
    """Evaluate current state once; notify one embed per event only if allowed.

    No events -> nothing to report even when allowed (avoids notifying on
    every unchanged check). Not allowed -> never notifies, regardless of
    what changed. market_registry/cache default to None so every existing
    caller and test (manual resale mode only) is unaffected; they are only
    needed when a WatchRule has resale_price_mode="market".
    """
    decision = evaluate(watch_rule, observation, match_result)
    if decision.allowed:
        if watch_rule.resale_price_mode == "market":
            # Phase 23: the only path here with a real network call
            # (eBay, via market_registry) — offloaded to a thread so a
            # slow/misbehaving market source can't block the event loop
            # other concurrently-checked WatchRules' own alerts share.
            # "manual" mode (the only mode any real WatchRule uses today)
            # is pure in-memory arithmetic and stays directly awaited —
            # no thread-hop overhead for the common case.
            opportunity, resale_estimate = await asyncio.to_thread(
                _opportunity_for, watch_rule, observation.price, market_registry, cache
            )
        else:
            opportunity, resale_estimate = _opportunity_for(
                watch_rule, observation.price, market_registry, cache
            )
        for event in events:
            embed = format_event_embed(
                event,
                observation,
                match_result,
                opportunity=opportunity,
                resale_estimate=resale_estimate,
            )
            await notifier.send_embed(embed)
    return decision
