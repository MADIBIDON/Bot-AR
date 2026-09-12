"""Monitoring engine: runs a WatchRule, produces a ProductObservation.

Layers stay separate on purpose:
    connector (raw fetch) -> normalization (ProductObservation) ->
    matching (MatchResult) -> [optional] storage (ObservationRecord)

`run_check` is pure — no database access, no side effects — so it can be
tested without a session. `run_check_and_store` composes it with
persistence. `run_all_active_watch_rules` is the single entry point a real
scheduler would call on each tick; no APScheduler/interval logic lives here
yet since nothing consumes it in this phase (no Discord bot, no app
entrypoint running continuously) — wiring one now would be untested dead
code. Not coupled to Discord or purchasing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from connectors.base import ConnectorError, ProductNotFoundError
from database import crud
from engine.change_detection import MonitoringEvent, detect_changes
from products.matcher import match_product
from products.observation import ProductObservation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from connectors.registry import ConnectorRegistry
    from database.models import WatchRule
    from products.matcher import MatchResult


@dataclass(frozen=True, slots=True)
class MonitoringResult:
    """Explains what happened for one WatchRule check, success or failure."""

    watch_rule_id: int
    observation: ProductObservation | None
    match_result: MatchResult | None
    success: bool
    error: str | None
    checked_at: datetime
    events: tuple[MonitoringEvent, ...] = ()


def run_check(watch_rule: WatchRule, registry: ConnectorRegistry) -> MonitoringResult:
    """Execute a single WatchRule check right now. No database access."""
    checked_at = datetime.now(UTC)

    if not watch_rule.enabled:
        return MonitoringResult(
            watch_rule_id=watch_rule.id,
            observation=None,
            match_result=None,
            success=False,
            error=f"watch rule {watch_rule.id} is disabled",
            checked_at=checked_at,
        )

    listing = watch_rule.listing
    if listing is None:
        return MonitoringResult(
            watch_rule_id=watch_rule.id,
            observation=None,
            match_result=None,
            success=False,
            error=(
                "watch rule has no listing; global-rule listing resolution is not implemented yet"
            ),
            checked_at=checked_at,
        )

    if not listing.external_id:
        return MonitoringResult(
            watch_rule_id=watch_rule.id,
            observation=None,
            match_result=None,
            success=False,
            error=f"listing {listing.id} has no external_id; cannot query connector",
            checked_at=checked_at,
        )

    try:
        connector = registry.get(listing.merchant.name)
    except ConnectorError as exc:
        return MonitoringResult(
            watch_rule_id=watch_rule.id,
            observation=None,
            match_result=None,
            success=False,
            error=str(exc),
            checked_at=checked_at,
        )

    try:
        connector_product = connector.get_product(listing.external_id)
    except (ProductNotFoundError, ConnectorError) as exc:
        return MonitoringResult(
            watch_rule_id=watch_rule.id,
            observation=None,
            match_result=None,
            success=False,
            error=str(exc),
            checked_at=checked_at,
        )

    try:
        observation = ProductObservation.from_connector_product(
            connector_product, merchant=listing.merchant.name, observed_at=checked_at
        )
    except ValueError as exc:
        return MonitoringResult(
            watch_rule_id=watch_rule.id,
            observation=None,
            match_result=None,
            success=False,
            error=f"invalid observation data: {exc}",
            checked_at=checked_at,
        )

    match_result = match_product(watch_rule.product, observation, expected_listing=listing)

    return MonitoringResult(
        watch_rule_id=watch_rule.id,
        observation=observation,
        match_result=match_result,
        success=True,
        error=None,
        checked_at=checked_at,
    )


def store_check_result(
    session: Session, watch_rule: WatchRule, result: MonitoringResult
) -> MonitoringResult:
    """The database-writing half of run_check_and_store, split out (Phase
    23) so a caller can run run_check()'s network I/O for several
    WatchRules concurrently (e.g. via asyncio.to_thread, bounded) and then
    replay this — the only part that touches the shared SQLAlchemy Session
    — sequentially, on the session's own thread. See engine/worker.py's
    run_monitoring_tick for that concurrent caller.

    Order matters: the previous observation is fetched *before* the new one
    is persisted, otherwise "previous" would be the record we just wrote.
    Change detection only runs if the product was actually matched — a
    misidentified product is still recorded for audit, but never produces
    a STOCK_AVAILABLE/PRICE_DROP/... event.
    """
    if not (result.success and result.observation is not None):
        return result

    previous_records = crud.list_observation_records_for_listing(session, watch_rule.listing_id)
    previous_record = previous_records[-1] if previous_records else None

    current_record = crud.create_observation_record(
        session, listing_id=watch_rule.listing_id, observation=result.observation
    )

    events: tuple[MonitoringEvent, ...] = ()
    if result.match_result is not None and result.match_result.matched:
        detected = detect_changes(previous_record, current_record, watch_rule)
        persisted = []
        for event in detected:
            record = crud.create_event_record(
                session,
                event_type=event.event_type.value,
                listing_id=event.listing_id,
                watch_rule_id=event.watch_rule_id,
                observation_record_id=current_record.id,
                occurred_at=event.occurred_at,
                previous_value=event.previous_value,
                current_value=event.current_value,
            )
            if record is not None:
                # Phase 27: carry the persisted row's id forward — the one
                # durable identifier app/delivery.py needs to track this
                # specific event's notification delivery across restarts.
                persisted.append(replace(event, record_id=record.id))
        events = tuple(persisted)

    return replace(result, events=events)


def run_check_and_store(
    session: Session, watch_rule: WatchRule, registry: ConnectorRegistry
) -> MonitoringResult:
    """run_check() then store_check_result() — the original, fully
    sequential single-rule helper, still used where there's no reason to
    split network from storage (run_all_active_watch_rules, most tests)."""
    return store_check_result(session, watch_rule, run_check(watch_rule, registry))


def run_all_active_watch_rules(
    session: Session, registry: ConnectorRegistry
) -> list[MonitoringResult]:
    """One check pass over every enabled WatchRule — call this on each tick."""
    rules = crud.list_watch_rules(session, enabled=True)
    return [run_check_and_store(session, rule, registry) for rule in rules]
