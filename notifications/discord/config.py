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


def _read_env(name: str) -> str | None:
    """Read one env var, stripped of accidental surrounding whitespace or
    quotes a hand-edited .env line can pick up."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value or None


def load_discord_config() -> DiscordConfig:
    values = {name: _read_env(name) for name in _REQUIRED_VARS}
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


_PRESENCE_LABELS: dict[str, str] = {
    "DISCORD_BOT_TOKEN": "token",
    "DISCORD_GUILD_ID": "guild id",
    "DISCORD_ALERT_CHANNEL_ID": "channel id",
}


def describe_config_presence() -> dict[str, bool]:
    """Diagnostic only: which required vars are set and non-empty.

    Never returns or logs a value — presence only. Safe to print directly.
    """
    return {name: _read_env(name) is not None for name in _REQUIRED_VARS}


def format_config_presence_report() -> str:
    presence = describe_config_presence()
    return "\n".join(
        f"{label} present: {'yes' if presence[name] else 'no'}"
        for name, label in _PRESENCE_LABELS.items()
    )
