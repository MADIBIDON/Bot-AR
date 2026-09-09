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
import contextlib
import logging

# The certifi SSL fix runs in notifications/discord/__init__.py, which
# Python always imports before this module — it must happen before the
# `import discord` below, since aiohttp caches its default SSLContext at
# import time.
import discord

from notifications.discord.config import DiscordConfig

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Connects on `async with`, sends to the configured alert channel.

    Any failure during login/connect/wait_until_ready always closes the
    underlying discord.py client (and its aiohttp session/connector) and
    cancels the background connect() task before propagating — no
    "Unclosed client session" / "Task was destroyed but it is pending".
    """

    def __init__(self, config: DiscordConfig) -> None:
        self._config = config
        self._client = discord.Client(intents=discord.Intents.default())
        self._run_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> DiscordNotifier:
        try:
            await self._client.login(self._config.bot_token)
            self._run_task = asyncio.create_task(self._client.connect())
            await self._await_ready()
        except BaseException:
            await self._safe_close()
            raise
        logger.info("Connected to Discord as %s", self._client.user)
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self._safe_close()

    async def _await_ready(self) -> None:
        """Wait for the gateway to become ready, but fail fast — with the
        real underlying error — if connect() dies first instead of hanging
        forever on wait_until_ready()."""
        assert self._run_task is not None
        ready_task = asyncio.create_task(self._client.wait_until_ready())
        done, _pending = await asyncio.wait(
            {self._run_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if ready_task in done:
            ready_task.result()
            return

        ready_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ready_task
        if self._run_task.cancelled():
            raise RuntimeError("Discord connection was cancelled before becoming ready.")
        exc = self._run_task.exception()
        if exc is not None:
            raise exc
        raise RuntimeError("Discord connection closed before becoming ready.")

    async def _safe_close(self) -> None:
        try:
            await self._client.close()
        except Exception:
            logger.exception("Error while closing the Discord client during cleanup")
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
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
