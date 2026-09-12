"""Tiny persisted record of the eBay Browse API's real OAuth outcomes.

Phase 26 audit: app/healthcheck.py used to report eBay as "CONFIGURED"
purely from EBAY_APP_ID/EBAY_CERT_ID being present in the environment —
it never reflected whether a real OAuth token exchange had ever actually
succeeded, so a bot with a live 401 invalid_client still reported
"CONFIGURED". Healthcheck is also not supposed to make its own network
call just to answer that question (it runs often and shouldn't add load
or risk rate limits) — so instead, the *real* OAuth attempts already
made during normal operation (market_data/ebay.py's
EbayMarketDataSource._get_access_token, called whenever a market-mode
WatchRule needs a resale estimate) record their own outcome here, and
healthcheck just reads the last-known state back.

Best-effort only: a failure to read/write this file must never break the
real eBay lookup it's reporting on, so every function here swallows I/O
errors rather than raising. Never stores anything beyond a timestamp and
an already-secret-free error string (the same MarketDataError messages
market_data/ebay.py already raises, which never include credentials).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_STATUS_FILE = Path("data") / "ebay_oauth_status.json"


@dataclass(frozen=True, slots=True)
class EbayOAuthStatus:
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_reason: str | None = None


def _load() -> EbayOAuthStatus:
    try:
        raw = _STATUS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return EbayOAuthStatus()
    return EbayOAuthStatus(
        last_success_at=data.get("last_success_at"),
        last_failure_at=data.get("last_failure_at"),
        last_failure_reason=data.get("last_failure_reason"),
    )


def _save(status: EbayOAuthStatus) -> None:
    try:
        _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATUS_FILE.write_text(json.dumps(asdict(status)), encoding="utf-8")
    except OSError:
        pass  # diagnostic side-channel only — never breaks the real caller


def record_success() -> None:
    previous = _load()
    _save(
        EbayOAuthStatus(
            last_success_at=datetime.now(UTC).isoformat(),
            last_failure_at=previous.last_failure_at,
            last_failure_reason=previous.last_failure_reason,
        )
    )


def record_failure(reason: str) -> None:
    previous = _load()
    _save(
        EbayOAuthStatus(
            last_success_at=previous.last_success_at,
            last_failure_at=datetime.now(UTC).isoformat(),
            last_failure_reason=reason,
        )
    )


def get_status() -> EbayOAuthStatus:
    return _load()
