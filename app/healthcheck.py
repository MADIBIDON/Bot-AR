"""python -m app.healthcheck — a fast, read-only readiness check for the
whole bot: config/env, database, retail connectors, Discord auth and
channel access, worker process status, eBay market data, and the most
recent monitoring activity.

Never mutates anything (no purchase, no Discord message sent, no fake
data). Never prints a secret — only presence/success/failure, same
guarantee as notifications/discord/config.py and market_data/ebay.py.

eBay being unconfigured is a WARNING, never a failure: the whole
monitoring/decision/notification pipeline works in manual resale-price
mode without it (Phase 16). Database, Discord, and having at least one
retail connector registered are the only checks that make the overall
status NOT READY — those are the bot's actual purpose; eBay is optional
enrichment.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app import pidfile
from connectors.defaults import MERCHANTS
from database import crud
from database.session import create_all, get_engine, get_session_factory
from market_data.defaults import build_default_market_registry
from market_data.ebay import MissingEbayConfigError
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import MissingDiscordConfigError, load_discord_config

PID_FILE = Path("data") / "worker.pid"
LOG_FILE = Path("logs") / "worker.log"
_DISCORD_CHECK_TIMEOUT_SECONDS = 15.0
_LOG_TAIL_LINES = 500
_LABEL_WIDTH = 22


@dataclass(frozen=True, slots=True)
class CheckResult:
    label: str
    detail: str
    blocking_failure: bool = False


def _check_database() -> tuple[CheckResult, Session | None]:
    try:
        engine = get_engine()
        create_all(engine)
        session = get_session_factory(engine)()
        crud.list_watch_rules(session)  # forces a real query, not just a connection
    except Exception as exc:  # noqa: BLE001 - any DB failure must be reported, not raised
        return CheckResult("Database", f"FAILED ({exc})", blocking_failure=True), None
    return CheckResult("Database", "OK"), session


def _check_active_watch_rules(session: Session) -> CheckResult:
    count = len(crud.list_watch_rules(session, enabled=True))
    return CheckResult("Active WatchRules", str(count))


def _check_retail_connectors() -> CheckResult:
    count = len(MERCHANTS)
    if count == 0:
        return CheckResult("Retail connectors", "0 (none registered)", blocking_failure=True)
    return CheckResult("Retail connectors", str(count))


async def _check_discord() -> CheckResult:
    try:
        config = load_discord_config()
    except MissingDiscordConfigError as exc:
        return CheckResult("Discord", f"NOT CONFIGURED ({exc})", blocking_failure=True)

    try:
        async with DiscordNotifier(config) as notifier:
            await asyncio.wait_for(notifier.verify_access(), timeout=_DISCORD_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        return CheckResult("Discord", "AUTH FAILED (timed out)", blocking_failure=True)
    except Exception as exc:  # noqa: BLE001 - report, never crash the healthcheck
        return CheckResult("Discord", f"AUTH FAILED ({exc})", blocking_failure=True)
    return CheckResult("Discord", "OK")


def _check_worker() -> CheckResult:
    status = pidfile.get_status(PID_FILE)
    detail = "RUNNING" if status.running else "STOPPED"
    if status.stale:
        detail = "STOPPED (stale pid file)"
    return CheckResult("Worker", detail)


def _check_ebay() -> CheckResult:
    try:
        build_default_market_registry()
    except MissingEbayConfigError:
        return CheckResult("eBay market data", "WAITING FOR CREDENTIALS")
    return CheckResult("eBay market data", "CONFIGURED")


def _check_last_observation(session: Session) -> CheckResult:
    record = crud.get_most_recent_observation(session)
    if record is None:
        return CheckResult("Last observation", "none yet")
    return CheckResult(
        "Last observation",
        f"{record.observed_at.isoformat()} (listing={record.listing_id}, price={record.price})",
    )


def _check_recent_errors() -> CheckResult:
    if not LOG_FILE.exists():
        return CheckResult("Recent errors", "no log file yet")
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-_LOG_TAIL_LINES:]
    error_count = sum(1 for line in lines if " ERROR " in line or " CRITICAL " in line)
    if error_count == 0:
        return CheckResult("Recent errors", f"none in last {len(lines)} log line(s)")
    return CheckResult("Recent errors", f"{error_count} in last {len(lines)} log line(s)")


def _print(result: CheckResult) -> None:
    print(f"{result.label:<{_LABEL_WIDTH}}{result.detail}")


async def run() -> bool:
    """Runs every check, prints the report, and returns True iff READY."""
    load_dotenv(override=True)

    db_result, session = _check_database()
    _print(db_result)

    results = [db_result]
    if session is not None:
        results.append(_check_active_watch_rules(session))
    results.append(_check_retail_connectors())
    results.append(await _check_discord())
    results.append(_check_worker())
    results.append(_check_ebay())
    if session is not None:
        results.append(_check_last_observation(session))
    results.append(_check_recent_errors())

    for result in results[1:]:
        _print(result)

    ready = not any(result.blocking_failure for result in results)
    print()
    print(f"{'OVERALL STATUS':<{_LABEL_WIDTH}}{'READY' if ready else 'NOT READY'}")
    return ready


def main() -> int:
    ready = asyncio.run(run())
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
