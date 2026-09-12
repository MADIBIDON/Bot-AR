"""Durable delivery for LocalStockEvent — Phase 29. Reuses app/delivery.py's
pure, EventRecord-free classification/backoff logic directly (see
database/models.py::LocalNotificationDelivery's docstring for why this
is a parallel table rather than a shared/polymorphic one); only the crud
calls underneath differ.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from app.delivery import (
    _BASE_RETRY_SECONDS,
    _MAX_ATTEMPTS,
    _MAX_RETRY_SECONDS,
    _SEND_TIMEOUT_SECONDS,
    classify_send_error,
)
from database import crud

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from database.models import LocalNotificationDelivery

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "discord"


def _next_retry_delay(attempt_count: int) -> float:
    return min(_BASE_RETRY_SECONDS * (2 ** max(attempt_count - 1, 0)), _MAX_RETRY_SECONDS)


def create_pending_local_delivery(
    session: Session,
    *,
    local_stock_event_id: int,
    embed: discord.Embed,
    provider: str = DEFAULT_PROVIDER,
) -> LocalNotificationDelivery:
    payload_json = json.dumps(embed.to_dict())
    return crud.create_local_notification_delivery(
        session,
        local_stock_event_id=local_stock_event_id,
        provider=provider,
        payload_json=payload_json,
    )


async def attempt_local_delivery(
    session: Session, delivery_id: int, notifier: EmbedSender, *, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    claimed = crud.claim_local_delivery_for_sending(session, delivery_id, now=now)
    if claimed is None:
        return

    try:
        embed = discord.Embed.from_dict(json.loads(claimed.payload_json))
        await asyncio.wait_for(notifier.send_embed(embed), timeout=_SEND_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - classified below, never re-raised
        error_type, retryable = classify_send_error(exc)
        logger.warning(
            "local_delivery=%s event=%s attempt %d failed (%s): %s",
            delivery_id,
            claimed.local_stock_event_id,
            claimed.attempt_count,
            error_type,
            exc,
        )
        if retryable and claimed.attempt_count < _MAX_ATTEMPTS:
            crud.mark_local_delivery_failed_retryable(
                session,
                delivery_id,
                error_type=error_type,
                next_retry_at=now + timedelta(seconds=_next_retry_delay(claimed.attempt_count)),
            )
        else:
            logger.error(
                "local_delivery=%s event=%s FAILED PERMANENTLY after %d attempt(s) (%s)",
                delivery_id,
                claimed.local_stock_event_id,
                claimed.attempt_count,
                error_type,
            )
            crud.mark_local_delivery_failed_permanent(session, delivery_id, error_type=error_type)
        return

    crud.mark_local_delivery_sent(session, delivery_id, now=now)
    logger.info("local_delivery=%s event=%s sent", delivery_id, claimed.local_stock_event_id)


async def process_due_local_deliveries(
    session: Session,
    notifier: EmbedSender,
    *,
    now: datetime | None = None,
    provider: str = DEFAULT_PROVIDER,
) -> int:
    now = now or datetime.now(UTC)
    due = crud.list_due_local_deliveries(session, now=now, provider=provider)
    for delivery in due:
        await attempt_local_delivery(session, delivery.id, notifier, now=now)
    return len(due)
