"""Wires Monitoring -> Decision -> Notification.

Depends on both engine/ and notifications/discord/ — this is the one place
that is allowed to know about both. engine/ never imports discord.py or
anything under notifications/; notifications/discord/ never imports this
module or engine/monitoring.py. Kept as a light, manually-invoked function
for Phase 9 — not wired into a scheduler yet (no long-running process
consumes it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from engine.decision import DecisionResult, evaluate
from notifications.discord.formatter import format_event_embed

if TYPE_CHECKING:
    import discord

    from database.models import WatchRule
    from engine.change_detection import MonitoringEvent
    from products.matcher import MatchResult
    from products.observation import ProductObservation


class EmbedSender(Protocol):
    """What notify_events_if_allowed needs — satisfied by DiscordNotifier
    and trivially by a fake in tests, with no discord.py client required."""

    async def send_embed(self, embed: discord.Embed) -> None: ...


async def notify_events_if_allowed(
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    events: tuple[MonitoringEvent, ...],
    notifier: EmbedSender,
) -> DecisionResult:
    """Evaluate current state once; notify one embed per event only if allowed.

    No events -> nothing to report even when allowed (avoids notifying on
    every unchanged check). Not allowed -> never notifies, regardless of
    what changed.
    """
    decision = evaluate(watch_rule, observation, match_result)
    if decision.allowed:
        for event in events:
            embed = format_event_embed(event, observation, match_result)
            await notifier.send_embed(embed)
    return decision
