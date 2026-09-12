"""Durable, at-least-once Discord delivery for business events — Phase 27.

Audit finding this phase closes (see database/models.py::EventRecord's old
docstring): an EventRecord durably proves a restock/price-drop happened,
but nothing tracked whether anyone was ever actually told about it. A
Discord failure was logged and the alert was gone for good; a crash
between "event persisted" and "Discord sent" had no recovery path at all.

Design: NotificationDelivery (database/models.py) is a small state machine
per (event_id, provider) — pending -> sending -> sent, or -> failed_retryable
(bounded retries with backoff) -> failed_permanent. The embed to send is
serialized once, when first prepared (app/notify.py, right after building
it), and every attempt — including one made minutes or hours later after a
restart — resends that exact frozen payload. This means a retry never
re-runs opportunity/resale computation against numbers that may have
drifted, and never re-hits a market data source.

Claiming a delivery (claim_delivery_for_sending, database/crud.py) is one
atomic conditional UPDATE, not a check-then-write — so the immediate
in-tick attempt and a periodic recovery sweep racing on the same row can
never both send it (see database/crud.py's docstring; also directly
covered by a real-thread concurrency test in tests/test_delivery.py,
matching this project's existing SQLite-concurrency-test pattern).

Residual, documented gap: for a "market" mode WatchRule, computing the
resale estimate before the embed exists involves a real `await` (a network
lookup offloaded via asyncio.to_thread — see app/notify.py::
compute_opportunity). A process crash during that specific window (event
already persisted, delivery row not created yet, no embed built yet) is
not recoverable — there is no frozen payload to resend, and rebuilding one
would mean re-deriving the resale estimate from scratch anyway. No
currently-active WatchRule uses market mode. For "manual" mode (every
currently-active rule), there is no `await` between the event being
persisted and the embed being built, so this window does not exist in
practice today.

The narrower gap this module *does* close: an EventRecord committed but
the process crashing before its NotificationDelivery row itself was
created (a much smaller window — no network, no await, just Python
object construction and one more commit). process_due_deliveries()
rebuilds a plain (non-opportunity-enriched) embed for any EventRecord
still missing a delivery row for the current provider, using only what is
already durably stored (ObservationRecord + Listing + WatchRule/Product) —
never fabricated data, and match confidence is genuinely recomputed via
products/matcher.match_product(), not guessed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from database import crud
from database.models import EventRecord
from database.time_utils import ensure_utc
from engine.change_detection import EventType, MonitoringEvent
from notifications.discord.formatter import format_event_embed
from products.matcher import match_product
from products.observation import ProductObservation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from database.models import NotificationDelivery, WatchRule
    from products.matcher import MatchResult

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "discord"

_SEND_TIMEOUT_SECONDS = 10.0
_BASE_RETRY_SECONDS = 30.0
_MAX_RETRY_SECONDS = 1800.0  # 30 minutes
_MAX_ATTEMPTS = 6  # ~30s,60s,120s,240s,480s,960s of backoff before giving up

_RETRYABLE_ERROR_TYPES = frozenset(
    {"timeout", "network_error", "rate_limited", "discord_5xx", "unknown"}
)


def classify_send_error(exc: BaseException) -> tuple[str, bool]:
    """(error_type_label, is_retryable). error_type_label is stored in
    NotificationDelivery.last_error_type — never the exception's raw
    message, which for a Forbidden/HTTPException can echo request
    details; a short, stable label is enough to act on and to show in
    healthcheck/status.

    An error type this classifier doesn't recognize is treated as
    retryable rather than permanent: for an alert-must-never-be-lost
    system, wrongly giving up on a transient problem is worse than
    wrongly retrying a handful of times (bounded by _MAX_ATTEMPTS
    regardless) before it is eventually surfaced as failed_permanent."""
    if isinstance(exc, TimeoutError):
        return "timeout", True
    if isinstance(exc, discord.NotFound):
        return "not_found", False
    if isinstance(exc, discord.Forbidden):
        return "forbidden", False
    if isinstance(exc, discord.RateLimited):
        return "rate_limited", True
    if isinstance(exc, discord.DiscordServerError):
        return "discord_5xx", True
    if isinstance(exc, discord.HTTPException):
        if exc.status == 429:
            return "rate_limited", True
        if exc.status >= 500:
            return "discord_5xx", True
        return "http_error", False
    if isinstance(exc, (ConnectionError, OSError)):
        return "network_error", True
    return "unknown", True


def _next_retry_delay(attempt_count: int) -> float:
    """Same bounded-exponential shape as engine/backoff.py, a separate
    constant set: this is about one Discord channel, not a merchant."""
    return min(_BASE_RETRY_SECONDS * (2 ** max(attempt_count - 1, 0)), _MAX_RETRY_SECONDS)


def create_pending_delivery(
    session: Session, *, event_id: int, embed: discord.Embed, provider: str = DEFAULT_PROVIDER
) -> NotificationDelivery:
    """Freezes `embed` as the payload every future attempt for this event
    will resend — called once, right after the embed is built (see
    app/notify.py). Idempotent: see crud.create_notification_delivery."""
    payload_json = json.dumps(embed.to_dict())
    return crud.create_notification_delivery(
        session, event_id=event_id, provider=provider, payload_json=payload_json
    )


async def attempt_delivery(
    session: Session,
    delivery_id: int,
    notifier: EmbedSender,
    *,
    now: datetime | None = None,
) -> None:
    """Claims the delivery (a no-op if something else already claimed it
    or it isn't due yet), sends the frozen payload once, and records the
    outcome. Never raises — a delivery attempt failing must never crash
    its caller, whether that's the immediate in-tick dispatch or a
    background recovery sweep."""
    now = now or datetime.now(UTC)
    claimed = crud.claim_delivery_for_sending(session, delivery_id, now=now)
    if claimed is None:
        return

    try:
        embed = discord.Embed.from_dict(json.loads(claimed.payload_json))
        await asyncio.wait_for(notifier.send_embed(embed), timeout=_SEND_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - classified below, never re-raised
        error_type, retryable = classify_send_error(exc)
        logger.warning(
            "delivery=%s event=%s attempt %d failed (%s): %s",
            delivery_id,
            claimed.event_id,
            claimed.attempt_count,
            error_type,
            exc,
        )
        if retryable and claimed.attempt_count < _MAX_ATTEMPTS:
            crud.mark_delivery_failed_retryable(
                session,
                delivery_id,
                error_type=error_type,
                next_retry_at=now + timedelta(seconds=_next_retry_delay(claimed.attempt_count)),
            )
        else:
            logger.error(
                "delivery=%s event=%s FAILED PERMANENTLY after %d attempt(s) (%s) — "
                "alert was not delivered",
                delivery_id,
                claimed.event_id,
                claimed.attempt_count,
                error_type,
            )
            crud.mark_delivery_failed_permanent(session, delivery_id, error_type=error_type)
        return

    crud.mark_delivery_sent(session, delivery_id, now=now)
    logger.info("delivery=%s event=%s sent", delivery_id, claimed.event_id)


def _reconstruct_context(
    session: Session, event_record: EventRecord
) -> tuple[ProductObservation, MatchResult, WatchRule] | None:
    """Rebuilds, from durable data alone, everything needed to both
    re-evaluate the original notification decision and (if it would still
    be allowed) render the same plain embed the live path would have
    built. Never fabricates a value: the observation is the exact
    ObservationRecord this event was detected from, and match confidence
    is genuinely recomputed via products.matcher.match_product(), not
    guessed."""
    from database.models import Listing, ObservationRecord, WatchRule

    observation_record = session.get(ObservationRecord, event_record.observation_record_id)
    listing = session.get(Listing, event_record.listing_id)
    watch_rule = session.get(WatchRule, event_record.watch_rule_id)
    if observation_record is None or listing is None or watch_rule is None:
        logger.error(
            "event=%s cannot rebuild delivery context — missing observation/listing/watch_rule",
            event_record.id,
        )
        return None

    observation = ProductObservation(
        merchant=listing.merchant.name,
        external_id=observation_record.external_id,
        name=observation_record.name,
        price=observation_record.price,
        currency=observation_record.currency,
        available=observation_record.available,
        url=listing.url,
        observed_at=ensure_utc(observation_record.observed_at),
        ean=observation_record.ean,
        mpn=observation_record.mpn,
        seller=observation_record.seller,
    )
    match_result = match_product(watch_rule.product, observation, expected_listing=listing)
    return observation, match_result, watch_rule


def _backfill_missing_deliveries(session: Session, *, provider: str) -> None:
    """An EventRecord with no delivery row is NOT automatically a crash
    victim: most of them are events whose notification decision was
    legitimately REJECTED (price still above max, rule disabled, low
    match confidence, ...) and were never supposed to get a delivery row
    in the first place — see app/notify.py's `if decision.allowed:` gate,
    which this mirrors exactly. Re-evaluating the same, pure, deterministic
    engine.decision.evaluate() against the reconstructed state is how this
    tells "genuinely orphaned by a crash" apart from "correctly never
    notified" — backfilling the latter would mean sending an alert the
    system already, correctly, decided not to send."""
    from engine.decision import evaluate

    for event_record in crud.list_event_records_missing_delivery(session, provider=provider):
        context = _reconstruct_context(session, event_record)
        if context is None:
            continue
        observation, match_result, watch_rule = context
        decision = evaluate(watch_rule, observation, match_result)
        if not decision.allowed:
            continue  # correctly never notified — not a crash, nothing to recover

        event = MonitoringEvent(
            event_type=EventType(event_record.event_type),
            listing_id=event_record.listing_id,
            watch_rule_id=event_record.watch_rule_id,
            occurred_at=ensure_utc(event_record.occurred_at),
            reason="rebuilt from persisted state for delivery recovery",
            previous_value=event_record.previous_value,
            current_value=event_record.current_value,
            observation_record_id=event_record.observation_record_id,
            record_id=event_record.id,
        )
        embed = format_event_embed(event, observation, match_result)
        create_pending_delivery(session, event_id=event_record.id, embed=embed, provider=provider)
        logger.info(
            "event=%s had no delivery row (recovered from a crash before it was created) — "
            "queued a plain alert",
            event_record.id,
        )


async def process_due_deliveries(
    session: Session,
    notifier: EmbedSender,
    *,
    now: datetime | None = None,
    provider: str = DEFAULT_PROVIDER,
) -> int:
    """Backfills any orphaned EventRecord first, then attempts every due
    delivery (pending, or failed_retryable whose backoff has elapsed) in
    order. Called once per tick (see app/worker.py) — cheap when there is
    nothing to do (two queries, no rows) and the natural recovery path
    after a restart, since the very first tick's call picks up whatever
    was pending or overdue when the process died. Returns how many
    deliveries were attempted, for tests/observability only."""
    now = now or datetime.now(UTC)
    _backfill_missing_deliveries(session, provider=provider)
    due = crud.list_due_deliveries(session, now=now, provider=provider)
    for delivery in due:
        await attempt_delivery(session, delivery.id, notifier, now=now)
    return len(due)
