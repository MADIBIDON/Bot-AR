"""Regression tests for DiscordNotifier's async lifecycle.

No network: discord.Client's login/connect/wait_until_ready/close are
monkeypatched on the instance. discord.Client() itself does no I/O at
construction, so building a real one here is safe.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import discord
import pytest

from notifications.discord.client import DiscordNotifier
from notifications.discord.config import DiscordConfig

_FAKE_TOKEN = "fake-token-should-never-appear-in-any-error"


def _config() -> DiscordConfig:
    return DiscordConfig(bot_token=_FAKE_TOKEN, guild_id=1, alert_channel_id=2)


def test_aenter_does_not_raise_when_wait_until_ready_requires_prior_login(
    monkeypatch,
) -> None:
    """Reproduces the exact reported bug: wait_until_ready() called before
    login() has completed raises discord.py's real RuntimeError. The fixed
    __aenter__ must await login() fully before ever calling
    wait_until_ready(), regardless of when the background connect() task
    gets scheduled.
    """
    notifier = DiscordNotifier(_config())
    logged_in = False

    async def fake_login(token: str) -> None:
        nonlocal logged_in
        logged_in = True

    async def fake_connect(*, reconnect: bool = True) -> None:
        await asyncio.sleep(0)

    async def fake_wait_until_ready() -> None:
        if not logged_in:
            raise RuntimeError(
                "Client has not been properly initialised. Please use the login "
                "method or asynchronous context manager before calling this method"
            )

    monkeypatch.setattr(notifier._client, "login", fake_login)
    monkeypatch.setattr(notifier._client, "connect", fake_connect)
    monkeypatch.setattr(notifier._client, "wait_until_ready", fake_wait_until_ready)
    monkeypatch.setattr(notifier._client, "close", AsyncMock())

    async def run() -> None:
        async with notifier:
            pass

    asyncio.run(run())  # must not raise


def test_aenter_calls_login_before_scheduling_connect(monkeypatch) -> None:
    notifier = DiscordNotifier(_config())
    call_order: list[str] = []

    async def fake_login(token: str) -> None:
        call_order.append("login")

    async def fake_connect(*, reconnect: bool = True) -> None:
        call_order.append("connect")
        await asyncio.sleep(0)

    async def fake_wait_until_ready() -> None:
        call_order.append("wait_until_ready")

    monkeypatch.setattr(notifier._client, "login", fake_login)
    monkeypatch.setattr(notifier._client, "connect", fake_connect)
    monkeypatch.setattr(notifier._client, "wait_until_ready", fake_wait_until_ready)
    monkeypatch.setattr(notifier._client, "close", AsyncMock())

    async def run() -> None:
        async with notifier:
            pass

    asyncio.run(run())

    assert call_order[0] == "login"
    assert "wait_until_ready" in call_order


def test_aexit_closes_client_and_awaits_background_task(monkeypatch) -> None:
    notifier = DiscordNotifier(_config())
    closed = False

    async def fake_login(token: str) -> None:
        pass

    async def fake_connect(*, reconnect: bool = True) -> None:
        await asyncio.sleep(0)

    async def fake_wait_until_ready() -> None:
        pass

    async def fake_close() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(notifier._client, "login", fake_login)
    monkeypatch.setattr(notifier._client, "connect", fake_connect)
    monkeypatch.setattr(notifier._client, "wait_until_ready", fake_wait_until_ready)
    monkeypatch.setattr(notifier._client, "close", fake_close)

    async def run() -> None:
        async with notifier:
            pass

    asyncio.run(run())

    assert closed is True
    assert notifier._run_task is not None
    assert notifier._run_task.done()


def test_login_failure_still_closes_client_and_propagates(monkeypatch) -> None:
    notifier = DiscordNotifier(_config())
    closed = AsyncMock()

    async def fake_login(token: str) -> None:
        raise discord.LoginFailure("Improper token has been passed.")

    monkeypatch.setattr(notifier._client, "login", fake_login)
    monkeypatch.setattr(notifier._client, "close", closed)

    async def run() -> None:
        async with notifier:
            pass

    with pytest.raises(discord.LoginFailure) as excinfo:
        asyncio.run(run())

    closed.assert_awaited_once()
    assert _FAKE_TOKEN not in str(excinfo.value)


def test_connect_failure_before_ready_closes_client_and_propagates_real_error(
    monkeypatch,
) -> None:
    notifier = DiscordNotifier(_config())
    closed = AsyncMock()

    async def fake_login(token: str) -> None:
        pass

    async def fake_connect(*, reconnect: bool = True) -> None:
        raise ConnectionError("gateway unreachable")

    async def fake_wait_until_ready() -> None:
        # Would hang forever in the real client if connect() died first;
        # the fix must not wait for this to resolve.
        await asyncio.sleep(3600)

    monkeypatch.setattr(notifier._client, "login", fake_login)
    monkeypatch.setattr(notifier._client, "connect", fake_connect)
    monkeypatch.setattr(notifier._client, "wait_until_ready", fake_wait_until_ready)
    monkeypatch.setattr(notifier._client, "close", closed)

    async def run() -> None:
        async with notifier:
            pass

    with pytest.raises(ConnectionError, match="gateway unreachable"):
        asyncio.run(run())

    closed.assert_awaited_once()


def test_error_while_sending_still_closes_client(monkeypatch) -> None:
    notifier = DiscordNotifier(_config())
    closed = AsyncMock()

    async def fake_login(token: str) -> None:
        pass

    async def fake_connect(*, reconnect: bool = True) -> None:
        await asyncio.sleep(0)

    async def fake_wait_until_ready() -> None:
        pass

    def fake_get_channel(channel_id: int) -> None:
        return None

    async def fake_fetch_channel(channel_id: int):
        raise discord.NotFound(AsyncMock(status=404), "Unknown Channel")

    monkeypatch.setattr(notifier._client, "login", fake_login)
    monkeypatch.setattr(notifier._client, "connect", fake_connect)
    monkeypatch.setattr(notifier._client, "wait_until_ready", fake_wait_until_ready)
    monkeypatch.setattr(notifier._client, "get_channel", fake_get_channel)
    monkeypatch.setattr(notifier._client, "fetch_channel", fake_fetch_channel)
    monkeypatch.setattr(notifier._client, "close", closed)

    async def run() -> None:
        async with notifier as n:
            await n.send_message("hello")

    with pytest.raises(discord.NotFound):
        asyncio.run(run())

    closed.assert_awaited_once()
