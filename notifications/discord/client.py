"""Thin discord.py wrapper: connect, fetch the configured channel, send.

No business logic, no formatting — this only moves an already-built
discord.Embed or plain string onto the wire. Never started automatically;
only a manual script (scripts/) or a future long-running process should
construct and use this.

Required bot permissions (see the Phase 9 setup instructions): View
Channel, Send Messages, Embed Links. Nothing else — no Administrator, no
Manage Server, no financial/purchase-related permission exists to grant in
the first place.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from notifications.discord.config import DiscordConfig

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Connects on `async with`, sends to the configured alert channel."""

    def __init__(self, config: DiscordConfig) -> None:
        self._config = config
        self._client = discord.Client(intents=discord.Intents.default())
        self._run_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> DiscordNotifier:
        self._run_task = asyncio.create_task(self._client.start(self._config.bot_token))
        await self._client.wait_until_ready()
        logger.info("Connected to Discord as %s", self._client.user)
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self._client.close()
        if self._run_task is not None:
            await self._run_task

    async def _get_channel(self) -> discord.abc.Messageable:
        channel = self._client.get_channel(self._config.alert_channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self._config.alert_channel_id)
        return channel

    async def send_message(self, content: str) -> None:
        channel = await self._get_channel()
        await channel.send(content=content)

    async def send_embed(self, embed: discord.Embed) -> None:
        channel = await self._get_channel()
        await channel.send(embed=embed)
