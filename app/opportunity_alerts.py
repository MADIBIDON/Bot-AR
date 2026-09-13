"""Phase 31 sections 7/11: notifies on a genuine opportunity-score upward
crossing even when no stock/price transition happened this tick — most
relevant for resale_price_mode="market" rules, whose resale estimate can
drift tick to tick with nothing changing on the retailer side at all.

Deliberately separate from app/notify.py::notify_events_if_allowed(),
which stays byte-for-byte backward compatible and untouched by this
feature: app/worker.py only ever calls this on the branch where a tick's
MonitoringResult.events was empty, so the two paths can never double-
notify for the same check.

Dedup: gated on WatchRule.last_alert_tier (Phase 31 DB column), not just
EventRecord's own (event_type, watch_rule_id, observation_record_id)
uniqueness — every tick creates a fresh ObservationRecord regardless of
whether anything changed, so that constraint alone would allow one
synthetic event per tick for as long as the tier merely stays HIGH/URGENT.
last_alert_tier is updated on every call (notified or not) so a later
genuine downgrade-then-reupgrade is still detected as a new crossing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.delivery import attempt_delivery, create_pending_delivery
from database import crud
from database.time_utils import ensure_utc
from engine.alerting import NOTIFIABLE_TIERS
from engine.change_detection import EventType, MonitoringEvent
from notifications.discord.formatter import format_event_embed

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from database.models import WatchRule
    from engine.alerting import OpportunityIntelligence
    from products.matcher import MatchResult
    from products.observation import ProductObservation

logger = logging.getLogger(__name__)

_NOTIFIABLE_VALUES = {tier.value for tier in NOTIFIABLE_TIERS}


async def maybe_notify_opportunity_shift(
    session: Session,
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    evaluation: OpportunityIntelligence | None,
    notifier: EmbedSender,
    *,
    dispatch: Callable[[Coroutine[object, object, None]], None] | None = None,
) -> bool:
    """Only meaningful when this tick's MonitoringResult.events was empty
    — app/worker.py enforces that by only calling this on that branch.
    Returns True if a notification was queued/sent."""
    if evaluation is None:
        return False

    new_tier = evaluation.alert_tier.value
    previous_tier = watch_rule.last_alert_tier
    was_already_notifiable = previous_tier in _NOTIFIABLE_VALUES
    is_now_notifiable = new_tier in _NOTIFIABLE_VALUES
    crossed_upward = is_now_notifiable and not was_already_notifiable

    # Persisted unconditionally — see module docstring on why a downgrade
    # must be recorded too, not just an upward crossing.
    crud.update_watch_rule(session, watch_rule.id, last_alert_tier=new_tier)

    if not crossed_upward:
        return False
    if watch_rule.listing_id is None:
        return False

    records = crud.list_observation_records_for_listing(session, watch_rule.listing_id)
    if not records:
        return False
    current_record = records[-1]

    synthetic_event = MonitoringEvent(
        event_type=EventType.OPPORTUNITY_SCORE_IMPROVED,
        listing_id=watch_rule.listing_id,
        watch_rule_id=watch_rule.id,
        occurred_at=ensure_utc(current_record.observed_at),
        reason=(
            f"Opportunity alert tier improved from {previous_tier or 'none'} to {new_tier} "
            f"(score={evaluation.score})."
        ),
        previous_value=previous_tier,
        current_value=new_tier,
        observation_record_id=current_record.id,
    )
    record = crud.create_event_record(
        session,
        event_type=synthetic_event.event_type.value,
        listing_id=synthetic_event.listing_id,
        watch_rule_id=synthetic_event.watch_rule_id,
        observation_record_id=synthetic_event.observation_record_id,
        occurred_at=synthetic_event.occurred_at,
        previous_value=synthetic_event.previous_value,
        current_value=synthetic_event.current_value,
    )
    if record is None:
        return False  # already recorded for this exact observation — dedup safety net

    embed = format_event_embed(synthetic_event, observation, match_result, evaluation=evaluation)
    delivery = create_pending_delivery(session, event_id=record.id, embed=embed)
    send_coro = attempt_delivery(session, delivery.id, notifier)
    if dispatch is not None:
        dispatch(send_coro)
    else:
        await send_coro
    return True
