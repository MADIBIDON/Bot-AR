"""Discord configuration, loaded from environment variables only.

Never logs or includes a variable's value in an error message — only its
name. `.env` itself is never read here (see scripts/ for where a manual
run loads it); this module just reads whatever is already in os.environ.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_REQUIRED_VARS = ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_ALERT_CHANNEL_ID")


class MissingDiscordConfigError(Exception):
    """Raised when a required Discord environment variable is missing or invalid."""


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    bot_token: str
    guild_id: int
    alert_channel_id: int


def load_discord_config() -> DiscordConfig:
    values = {name: os.environ.get(name) for name in _REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise MissingDiscordConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in your .env file (see .env.example)."
        )

    try:
        guild_id = int(values["DISCORD_GUILD_ID"])  # type: ignore[arg-type]
        alert_channel_id = int(values["DISCORD_ALERT_CHANNEL_ID"])  # type: ignore[arg-type]
    except ValueError as exc:
        raise MissingDiscordConfigError(
            "DISCORD_GUILD_ID and DISCORD_ALERT_CHANNEL_ID must be numeric Discord IDs."
        ) from exc

    return DiscordConfig(
        bot_token=values["DISCORD_BOT_TOKEN"],  # type: ignore[arg-type]
        guild_id=guild_id,
        alert_channel_id=alert_channel_id,
    )
