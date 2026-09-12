"""Compares two successive observations of a Listing, produces events.

Pure: takes already-loaded records, does no I/O, no Discord formatting.
`reason` is a plain factual description, not Discord-ready text — a later
phase turns a MonitoringEvent into a Discord embed, it doesn't live here.

Event policy:
- No previous observation -> no events at all, ever, even if the current
  observation is already out of stock or already under target_price. The
  first check establishes a baseline; it is not a "change".
- PRICE_CHANGED is kept in EventType only as the conceptual parent of
  PRICE_DROP/PRICE_INCREASE. It is never emitted on its own: a Decimal
  price comparison always resolves to exactly one of the two, so a
  separate PRICE_CHANGED event would be pure duplication (and a second
  Discord notification for one price change, which is undesirable).
- TARGET_PRICE_REACHED is edge-triggered on the previous/current pair only:
  it fires when previous > target and current <= target. Staying at or
  below target on the next check does not re-fire it (previous is then
  already <= target). Rising back above target and dropping again does
  re-fire it — that is a genuinely new opportunity, not a duplicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import ObservationRecord, WatchRule


class EventType(StrEnum):
    STOCK_AVAILABLE = "stock_available"
    STOCK_UNAVAILABLE = "stock_unavailable"
    PRICE_CHANGED = "price_changed"
    PRICE_DROP = "price_drop"
    PRICE_INCREASE = "price_increase"
    TARGET_PRICE_REACHED = "target_price_reached"


@dataclass(frozen=True, slots=True)
class MonitoringEvent:
    event_type: EventType
    listing_id: int
    watch_rule_id: int
    occurred_at: datetime
    reason: str
    previous_value: str | None = None
    current_value: str | None = None
    observation_record_id: int | None = None
    # Phase 27: the persisted EventRecord.id, attached by
    # engine/monitoring.py::store_check_result() once create_event_record()
    # actually writes the row — None here (detect_changes() never sets it,
    # since nothing is persisted yet at that point) is how a caller detects
    # "this event was never durably identifiable" and must not create a
    # NotificationDelivery row for it (see app/delivery.py).
    record_id: int | None = None


def detect_changes(
    previous: ObservationRecord | None,
    current: ObservationRecord,
    watch_rule: WatchRule,
) -> list[MonitoringEvent]:
    if previous is None:
        return []

    events: list[MonitoringEvent] = []

    if previous.available != current.available:
        events.append(_stock_event(previous, current, watch_rule))

    if previous.price != current.price:
        events.append(_price_event(previous, current, watch_rule))

    target_event = _target_price_event(previous, current, watch_rule)
    if target_event is not None:
        events.append(target_event)

    return events


def _stock_event(
    previous: ObservationRecord, current: ObservationRecord, watch_rule: WatchRule
) -> MonitoringEvent:
    event_type = EventType.STOCK_AVAILABLE if current.available else EventType.STOCK_UNAVAILABLE
    reason = "Stock became available." if current.available else "Stock became unavailable."
    return MonitoringEvent(
        event_type=event_type,
        listing_id=current.listing_id,
        watch_rule_id=watch_rule.id,
        occurred_at=current.observed_at,
        reason=reason,
        previous_value=str(previous.available),
        current_value=str(current.available),
        observation_record_id=current.id,
    )


def _price_event(
    previous: ObservationRecord, current: ObservationRecord, watch_rule: WatchRule
) -> MonitoringEvent:
    event_type = (
        EventType.PRICE_DROP if current.price < previous.price else EventType.PRICE_INCREASE
    )
    return MonitoringEvent(
        event_type=event_type,
        listing_id=current.listing_id,
        watch_rule_id=watch_rule.id,
        occurred_at=current.observed_at,
        reason=f"Price changed from {previous.price} to {current.price}.",
        previous_value=str(previous.price),
        current_value=str(current.price),
        observation_record_id=current.id,
    )


def _target_price_event(
    previous: ObservationRecord, current: ObservationRecord, watch_rule: WatchRule
) -> MonitoringEvent | None:
    target = watch_rule.target_price
    if target is None:
        return None
    if not (previous.price > target and current.price <= target):
        return None
    return MonitoringEvent(
        event_type=EventType.TARGET_PRICE_REACHED,
        listing_id=current.listing_id,
        watch_rule_id=watch_rule.id,
        occurred_at=current.observed_at,
        reason=f"Price reached target {target} (now {current.price}).",
        previous_value=str(previous.price),
        current_value=str(current.price),
        observation_record_id=current.id,
    )
