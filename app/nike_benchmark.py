"""Phase 31 section 8: measures real release -> detection -> Discord
latency for the Nike SNKRS scheduled-release test case
(connectors/nike_launch.py, WatchRule.scheduled_release_at). Purely
observational — reads what monitoring/delivery already recorded, never
adjusts the rule's own state and never fabricates a number before the
real release has actually happened (status="not_yet_released" until
then, not a guessed ETA).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from database import crud
from database.time_utils import ensure_utc
from engine.change_detection import EventType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_DISCORD_PROVIDER = "discord"


@dataclass(frozen=True, slots=True)
class NikeBenchmarkReport:
    status: str  # "no_data" | "not_yet_released" | "awaiting_discord_send" | "measured"
    scheduled_release_at: datetime | None
    event_detected_at: datetime | None
    discord_notified_at: datetime | None
    release_to_detection_seconds: float | None
    detection_to_discord_seconds: float | None
    total_latency_seconds: float | None


def build_nike_benchmark_report(session: Session, watch_rule_id: int) -> NikeBenchmarkReport:
    rule = crud.get_watch_rule(session, watch_rule_id)
    if rule is None or rule.listing_id is None:
        return NikeBenchmarkReport(
            "no_data",
            None,
            None,
            None,
            None,
            None,
            None,
        )

    scheduled_release_at = (
        ensure_utc(rule.scheduled_release_at) if rule.scheduled_release_at is not None else None
    )

    events = crud.list_event_records_for_listing(session, rule.listing_id)
    stock_available_events = [e for e in events if e.event_type == EventType.STOCK_AVAILABLE.value]
    if not stock_available_events:
        status = "not_yet_released" if scheduled_release_at is not None else "no_data"
        return NikeBenchmarkReport(status, scheduled_release_at, None, None, None, None, None)

    # The first ever STOCK_AVAILABLE for this listing is the real release
    # moment BOT-AR detected — a later restock (if it ever sells out and
    # comes back) would be a different, later event, not this one.
    release_event = min(stock_available_events, key=lambda e: e.occurred_at)
    event_detected_at = ensure_utc(release_event.occurred_at)

    delivery = crud.get_notification_delivery(
        session, event_id=release_event.id, provider=_DISCORD_PROVIDER
    )
    discord_notified_at = (
        ensure_utc(delivery.sent_at) if delivery is not None and delivery.sent_at else None
    )

    release_to_detection = (
        (event_detected_at - scheduled_release_at).total_seconds()
        if scheduled_release_at is not None
        else None
    )
    detection_to_discord = (
        (discord_notified_at - event_detected_at).total_seconds()
        if discord_notified_at is not None
        else None
    )
    total_latency = (
        (discord_notified_at - scheduled_release_at).total_seconds()
        if scheduled_release_at is not None and discord_notified_at is not None
        else None
    )

    status = "measured" if discord_notified_at is not None else "awaiting_discord_send"
    return NikeBenchmarkReport(
        status,
        scheduled_release_at,
        event_detected_at,
        discord_notified_at,
        release_to_detection,
        detection_to_discord,
        total_latency,
    )
