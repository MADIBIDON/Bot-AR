"""Discord connectivity diagnostic — not run by pytest.

Runs each check in order and stops at the first failure with a specific,
human-readable reason. Never prints the bot token, only whether it is
present. Always closes the client/aiohttp session/background task
regardless of where a step fails.

Usage:
    python scripts/discord_preflight.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
import sys

import discord
from dotenv import load_dotenv

from notifications.discord.config import (
    MissingDiscordConfigError,
    format_config_presence_report,
    load_discord_config,
)
from notifications.discord.ssl_fix import ensure_ssl_certificate_bundle


async def run_preflight() -> int:
    print("[1] Local configuration")
    print(format_config_presence_report())
    try:
        config = load_discord_config()
    except MissingDiscordConfigError as exc:
        print(f"❌ Configuration error: {exc}")
        return 1
    print("✅ Configuration loaded")
    print(f"   guild id:   {config.guild_id}")
    print(f"   channel id: {config.alert_channel_id}")

    print("\n[2] SSL certificates")
    ensure_ssl_certificate_bundle()
    cert_file = os.environ.get("SSL_CERT_FILE", "")
    try:
        ssl.create_default_context(cafile=cert_file or None)
    except Exception as exc:
        print(f"❌ SSL certificate bundle failed to load ({cert_file}): {exc}")
        return 1
    print(f"✅ SSL certificate bundle OK ({cert_file})")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    run_task: asyncio.Task[None] | None = None
    try:
        print("\n[3] Discord authentication")
        try:
            await client.login(config.bot_token)
        except discord.LoginFailure:
            print("❌ Token invalide (rejeté par Discord)")
            return 1
        except discord.HTTPException as exc:
            print(f"❌ Erreur HTTP Discord pendant l'authentification (status {exc.status})")
            return 1
        print(f"✅ Authenticated as {client.user}")
        if client.application_id is not None:
            print(f"   application (client) id: {client.application_id}")

        run_task = asyncio.create_task(client.connect())
        ready_task = asyncio.create_task(client.wait_until_ready())
        done, _pending = await asyncio.wait(
            {run_task, ready_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if ready_task not in done:
            ready_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_task
            exc = run_task.exception() if run_task.done() and not run_task.cancelled() else None
            print(f"❌ Connexion gateway échouée avant d'être prête: {exc}")
            return 1
        print("✅ Gateway connectée, cache prêt")

        print("\n[4] Server access")
        guild = client.get_guild(config.guild_id)
        if guild is None:
            print(
                "❌ Bot absent du serveur (guild introuvable dans le cache) — "
                "vérifie que le bot a bien été invité sur ce serveur"
            )
            return 1
        print(f"✅ Guild found: {guild.name}")

        print("\n[5] Channel access")
        channel = client.get_channel(config.alert_channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(config.alert_channel_id)
            except discord.NotFound:
                print("❌ Salon introuvable (mauvais channel ID)")
                return 1
            except discord.Forbidden:
                print("❌ Bot sans accès au salon (permission View Channel manquante)")
                return 1
        channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if channel_guild_id is not None and channel_guild_id != config.guild_id:
            print(
                "❌ Le salon appartient à un autre serveur que celui configuré "
                f"(salon guild={channel_guild_id}, attendu={config.guild_id})"
            )
            return 1
        print(f"✅ Channel found: #{getattr(channel, 'name', channel.id)}")

        print("\n[6] Bot permissions")
        member = guild.me
        if member is None or not hasattr(channel, "permissions_for"):
            print("❌ Impossible de déterminer les permissions du bot sur ce salon")
            return 1
        perms = channel.permissions_for(member)
        missing = []
        if not perms.view_channel:
            missing.append("View Channel")
        if not perms.send_messages:
            missing.append("Send Messages")
        if not perms.embed_links:
            missing.append("Embed Links")
        if missing:
            for name in missing:
                print(f"❌ Bot sans permission {name}")
            return 1
        print("✅ Permissions OK (View Channel, Send Messages, Embed Links)")

        print("\n[7] Sending test message")
        await channel.send(content="✅ discord_preflight: all checks passed.")
        print("✅ Test message sent")

    finally:
        await client.close()
        if run_task is not None:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run_task

    return 0


if __name__ == "__main__":
    load_dotenv(override=True)
    sys.exit(asyncio.run(run_preflight()))
