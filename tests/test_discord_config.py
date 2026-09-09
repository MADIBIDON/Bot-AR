from __future__ import annotations

import pytest

from notifications.discord.config import (
    MissingDiscordConfigError,
    describe_config_presence,
    format_config_presence_report,
    load_discord_config,
)

_SECRET_TOKEN = "super-secret-token-value-should-never-leak"


def test_load_config_succeeds_with_all_vars_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "987654321098765432")

    config = load_discord_config()

    assert config.bot_token == _SECRET_TOKEN
    assert config.guild_id == 123456789012345678
    assert config.alert_channel_id == 987654321098765432


def test_missing_all_vars_raises_with_no_token_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    monkeypatch.delenv("DISCORD_ALERT_CHANNEL_ID", raising=False)

    with pytest.raises(MissingDiscordConfigError) as excinfo:
        load_discord_config()

    message = str(excinfo.value)
    assert "DISCORD_BOT_TOKEN" in message
    assert "DISCORD_GUILD_ID" in message
    assert "DISCORD_ALERT_CHANNEL_ID" in message
    assert _SECRET_TOKEN not in message


def test_missing_one_var_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.delenv("DISCORD_ALERT_CHANNEL_ID", raising=False)

    with pytest.raises(MissingDiscordConfigError, match="DISCORD_ALERT_CHANNEL_ID"):
        load_discord_config()


def test_non_numeric_guild_id_raises_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("DISCORD_GUILD_ID", "not-a-number")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "987654321098765432")

    with pytest.raises(MissingDiscordConfigError) as excinfo:
        load_discord_config()

    assert _SECRET_TOKEN not in str(excinfo.value)


def test_surrounding_quotes_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", f'"{_SECRET_TOKEN}"')
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "987654321098765432")

    config = load_discord_config()

    assert config.bot_token == _SECRET_TOKEN


def test_surrounding_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", f"  {_SECRET_TOKEN}  ")
    monkeypatch.setenv("DISCORD_GUILD_ID", " 123456789012345678 ")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "987654321098765432")

    config = load_discord_config()

    assert config.bot_token == _SECRET_TOKEN
    assert config.guild_id == 123456789012345678


def test_describe_config_presence_never_exposes_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", _SECRET_TOKEN)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "987654321098765432")

    presence = describe_config_presence()

    assert presence == {
        "DISCORD_BOT_TOKEN": True,
        "DISCORD_GUILD_ID": False,
        "DISCORD_ALERT_CHANNEL_ID": True,
    }
    assert _SECRET_TOKEN not in str(presence)


def test_format_config_presence_report_is_safe_to_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")
    monkeypatch.delenv("DISCORD_ALERT_CHANNEL_ID", raising=False)

    report = format_config_presence_report()

    assert "token present: yes" in report
    assert "guild id present: yes" in report
    assert "channel id present: no" in report
    assert _SECRET_TOKEN not in report
