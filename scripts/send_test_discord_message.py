"""Manual, one-off Discord connectivity check. Not run by pytest.

Loads .env, connects the bot, sends a plain test message, then sends one
Embed built from a fictional FakeStore price-drop event so you can see the
real formatting in your channel. Requires DISCORD_BOT_TOKEN,
DISCORD_GUILD_ID, DISCORD_ALERT_CHANNEL_ID in your .env — see the setup
instructions.

Usage:
    python scripts/send_test_discord_message.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from dotenv import load_dotenv

from engine.change_detection import EventType, MonitoringEvent
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import load_discord_config
from notifications.discord.formatter import format_event_embed
from products.matcher import MatchResult
from products.observation import ProductObservation


async def main() -> None:
    # override=True: a stale value already exported in the current shell
    # (e.g. a leftover manual `export DISCORD_BOT_TOKEN=...`) must never
    # shadow what's actually in .env.
    load_dotenv(override=True)
    config = load_discord_config()

    observation = ProductObservation(
        merchant="FakeStore",
        external_id="fake-123",
        name="Duopack Evoli 30 ans (test)",
        price=Decimal("13.99"),
        currency="EUR",
        available=True,
        url="https://example.invalid/fake-store/p/fake-123",
        observed_at=datetime.now(UTC),
        ean="1234567890123",
    )
    match_result = MatchResult(
        matched=True, confidence=100, method="ean_exact", reason="EAN matches exactly."
    )
    event = MonitoringEvent(
        event_type=EventType.PRICE_DROP,
        listing_id=0,
        watch_rule_id=0,
        occurred_at=observation.observed_at,
        reason="Price changed from 16.99 to 13.99.",
        previous_value="16.99",
        current_value="13.99",
    )

    async with DiscordNotifier(config) as notifier:
        await notifier.send_message(
            "✅ Retail Opportunity & Purchase Assistant — Discord connectivity test."
        )
        embed = format_event_embed(event, observation, match_result)
        await notifier.send_embed(embed)

    print("Sent test message and embed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
