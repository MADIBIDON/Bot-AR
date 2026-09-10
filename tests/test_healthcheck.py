"""No real network, no real Discord, no real eBay: DiscordNotifier and
the market/discord config loaders are all monkeypatched inside the
app.healthcheck namespace, same pattern as tests/test_watch_cli.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import app.healthcheck as healthcheck
from connectors.defaults import MERCHANTS
from database import crud
from market_data.ebay import MissingEbayConfigError
from notifications.discord.config import DiscordConfig, MissingDiscordConfigError

_FAKE_TOKEN = "fake-token-should-never-appear-in-any-output"


class _FakeNotifierOK:
    def __init__(self, config: object) -> None:
        pass

    async def __aenter__(self) -> _FakeNotifierOK:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def verify_access(self) -> None:
        return None


class _FakeNotifierAuthFailure:
    def __init__(self, config: object) -> None:
        pass

    async def __aenter__(self) -> _FakeNotifierAuthFailure:
        raise RuntimeError("Improper token has been passed.")

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def verify_access(self) -> None:
        return None


def _discord_config() -> DiscordConfig:
    return DiscordConfig(bot_token=_FAKE_TOKEN, guild_id=1, alert_channel_id=2)


# --- database -------------------------------------------------------------


def test_database_ok_when_reachable(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "get_engine", lambda: object())
    monkeypatch.setattr(healthcheck, "create_all", lambda engine: None)
    monkeypatch.setattr(healthcheck, "get_session_factory", lambda engine: lambda: session)

    result, returned_session = healthcheck._check_database()

    assert result.detail == "OK"
    assert result.blocking_failure is False
    assert returned_session is session


def test_database_failure_is_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(healthcheck, "get_engine", raise_error)

    result, returned_session = healthcheck._check_database()

    assert result.blocking_failure is True
    assert "disk full" in result.detail
    assert returned_session is None


# --- active watch rules / connectors ---------------------------------------


def test_active_watch_rules_counts_only_enabled(session: Session) -> None:
    product = crud.create_product(session, "P1")
    crud.create_watch_rule(session, product_id=product.id, check_interval=60, max_quantity=1)
    product2 = crud.create_product(session, "P2")
    disabled = crud.create_watch_rule(
        session, product_id=product2.id, check_interval=60, max_quantity=1
    )
    crud.disable_watch_rule(session, disabled.id)

    result = healthcheck._check_active_watch_rules(session)

    assert result.detail == "1"


def test_retail_connectors_reports_merchant_count() -> None:
    result = healthcheck._check_retail_connectors()

    assert result.detail == str(len(MERCHANTS))
    assert result.blocking_failure is False


# --- discord ----------------------------------------------------------------


def test_discord_not_configured_is_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_missing() -> None:
        raise MissingDiscordConfigError(
            "Missing required environment variable(s): DISCORD_BOT_TOKEN"
        )

    monkeypatch.setattr(healthcheck, "load_discord_config", raise_missing)

    result = asyncio.run(healthcheck._check_discord())

    assert result.blocking_failure is True
    assert "NOT CONFIGURED" in result.detail


def test_discord_ok_when_auth_and_access_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "load_discord_config", _discord_config)
    monkeypatch.setattr(healthcheck, "DiscordNotifier", _FakeNotifierOK)

    result = asyncio.run(healthcheck._check_discord())

    assert result.detail == "OK"
    assert result.blocking_failure is False


def test_discord_auth_failure_is_blocking_and_never_leaks_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(healthcheck, "load_discord_config", _discord_config)
    monkeypatch.setattr(healthcheck, "DiscordNotifier", _FakeNotifierAuthFailure)

    result = asyncio.run(healthcheck._check_discord())

    assert result.blocking_failure is True
    assert "AUTH FAILED" in result.detail
    assert _FAKE_TOKEN not in result.detail


# --- worker -------------------------------------------------------------


def test_worker_stopped_when_no_pid_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "PID_FILE", tmp_path / "worker.pid")

    result = healthcheck._check_worker()

    assert result.detail == "STOPPED"


def test_worker_running_when_pid_alive(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    pid_file = tmp_path / "worker.pid"
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(healthcheck, "PID_FILE", pid_file)

    result = healthcheck._check_worker()

    assert result.detail == "RUNNING"


# --- eBay -----------------------------------------------------------------


def test_ebay_waiting_for_credentials_is_never_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_missing() -> None:
        raise MissingEbayConfigError("Missing required environment variable(s): EBAY_APP_ID")

    monkeypatch.setattr(healthcheck, "build_default_market_registry", raise_missing)

    result = healthcheck._check_ebay()

    assert result.detail == "WAITING FOR CREDENTIALS"
    assert result.blocking_failure is False


def test_ebay_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "build_default_market_registry", lambda: object())

    result = healthcheck._check_ebay()

    assert result.detail == "CONFIGURED"


# --- last observation / recent errors ---------------------------------


def test_last_observation_none_yet(session: Session) -> None:
    result = healthcheck._check_last_observation(session)

    assert result.detail == "none yet"


def test_recent_errors_no_log_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healthcheck, "LOG_FILE", tmp_path / "worker.log")

    result = healthcheck._check_recent_errors()

    assert result.detail == "no log file yet"


def test_recent_errors_counts_error_lines(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "worker.log"
    log_file.write_text(
        "2026-01-01 12:00:00 INFO worker started\n"
        "2026-01-01 12:00:01 ERROR connector failed\n"
        "2026-01-01 12:00:02 INFO rule=1 no event\n"
    )
    monkeypatch.setattr(healthcheck, "LOG_FILE", log_file)

    result = healthcheck._check_recent_errors()

    assert "1 in last 3 log line(s)" == result.detail


# --- overall status / no secret leakage -------------------------------


def test_overall_ready_despite_ebay_missing(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(healthcheck, "get_engine", lambda: object())
    monkeypatch.setattr(healthcheck, "create_all", lambda engine: None)
    monkeypatch.setattr(healthcheck, "get_session_factory", lambda engine: lambda: session)
    monkeypatch.setattr(healthcheck, "load_discord_config", _discord_config)
    monkeypatch.setattr(healthcheck, "DiscordNotifier", _FakeNotifierOK)

    def raise_missing() -> None:
        raise MissingEbayConfigError("Missing required environment variable(s): EBAY_APP_ID")

    monkeypatch.setattr(healthcheck, "build_default_market_registry", raise_missing)
    monkeypatch.setattr(healthcheck, "PID_FILE", Path("/nonexistent/worker.pid"))
    monkeypatch.setattr(healthcheck, "LOG_FILE", Path("/nonexistent/worker.log"))

    ready = asyncio.run(healthcheck.run())

    output = capsys.readouterr().out
    assert ready is True
    assert "WAITING FOR CREDENTIALS" in output
    assert "OVERALL STATUS" in output
    assert "READY" in output


def test_overall_not_ready_when_discord_fails(
    session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(healthcheck, "get_engine", lambda: object())
    monkeypatch.setattr(healthcheck, "create_all", lambda engine: None)
    monkeypatch.setattr(healthcheck, "get_session_factory", lambda engine: lambda: session)
    monkeypatch.setattr(healthcheck, "load_discord_config", _discord_config)
    monkeypatch.setattr(healthcheck, "DiscordNotifier", _FakeNotifierAuthFailure)

    def raise_missing() -> None:
        raise MissingEbayConfigError("missing")

    monkeypatch.setattr(healthcheck, "build_default_market_registry", raise_missing)
    monkeypatch.setattr(healthcheck, "PID_FILE", Path("/nonexistent/worker.pid"))
    monkeypatch.setattr(healthcheck, "LOG_FILE", Path("/nonexistent/worker.log"))

    ready = asyncio.run(healthcheck.run())

    output = capsys.readouterr().out
    assert ready is False
    assert "NOT READY" in output
    assert _FAKE_TOKEN not in output
