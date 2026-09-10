"""Purchase Engine — the layer between a detected opportunity and an
actually attempted purchase.

Deliberately separate from engine/monitoring.py and app/worker.py's tick
loop: the monitoring FAST PATH (fetch -> parse -> price/stock -> decide)
must never be slowed down by a checkout attempt, which can be much
slower (or hang against a slow/unreliable merchant) than a single price
fetch. The caller (app/worker.py) is expected to fire attempt_purchase()
via `asyncio.create_task(...)` — fire-and-forget from the tick loop's
point of view — so other WatchRules keep being checked on schedule no
matter how long one purchase attempt takes or whether it fails. Inside
attempt_purchase(), the connector's own (synchronous, potentially slow)
revalidate()/checkout() calls are further offloaded to a worker thread
via asyncio.to_thread() so they never block the event loop that every
other coroutine (including other purchase attempts and Discord I/O)
shares. No Celery/Redis/Kafka: asyncio + this project's existing
single-process, single-worker architecture (app/pidfile.py) is enough.

Two independent pieces:
  - evaluate_purchase_intent(): pure, deterministic, no I/O — every
    safety gate from the spec, fail-closed. Reused unchanged by both a
    real attempt and a dry run, so "what a dry run says would happen" and
    "what actually gets checked before a real purchase" can never drift
    apart.
  - attempt_purchase(): the real, impure orchestration — persists a
    PurchaseAttempt, revalidates against the connector right before
    committing (price/stock can go stale in seconds), re-runs the same
    gate against that fresh state, then checks out and reports the
    result. Never called for a dry run.

Idempotency: get_active_purchase_attempt_for_listing() (checked inside
evaluate_purchase_intent's caller, immediately before
create_purchase_attempt(), with no `await` in between) is the
"one active attempt per listing" lock — see database/models.py's
PurchaseAttempt docstring for why that is safe in this architecture.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from database import crud
from engine.decision import MIN_FINANCIAL_MATCH_CONFIDENCE
from notifications.discord.formatter import format_purchase_embed
from purchase.base import AutomatedCheckoutUnsupportedError, HumanActionRequiredError
from purchase.config import is_merchant_allowed
from purchase.models import PurchaseDecision, PurchaseIntent, PurchaseStatus
from purchase.registry import PurchaseConnectorNotRegisteredError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from database.models import WatchRule
    from products.matcher import MatchResult
    from products.observation import ProductObservation
    from purchase.config import PurchasePolicy
    from purchase.registry import PurchaseConnectorRegistry

logger = logging.getLogger(__name__)


def build_purchase_intent(
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    *,
    now: datetime | None = None,
) -> PurchaseIntent:
    """Builds the candidate from state the monitoring fast path already
    fetched — no network call here. max_price_allowed is the WatchRule's
    own max_price: a rule with no max_price configured never becomes a
    purchase candidate at all (see evaluate_purchase_intent)."""
    return PurchaseIntent(
        watch_rule_id=watch_rule.id,
        product_id=watch_rule.product_id,
        listing_id=watch_rule.listing_id,
        merchant=observation.merchant,
        product_name=watch_rule.product.name,
        url=observation.url,
        observed_price=observation.price,
        max_price_allowed=watch_rule.max_price or Decimal("0"),
        quantity=watch_rule.max_quantity,
        match_confidence=match_result.confidence,
        created_at=now or datetime.now(UTC),
    )


def evaluate_purchase_intent(
    *,
    watch_rule: WatchRule,
    intent: PurchaseIntent,
    policy: PurchasePolicy,
    merchant_domains: tuple[str, ...],
    match_confidence: int,
    available: bool,
    total_cost: Decimal,
    has_active_attempt: bool,
    seconds_since_last_attempt: float | None,
    spent_today: Decimal,
) -> PurchaseDecision:
    """Every check fails closed: any missing or ambiguous signal refuses
    the purchase. Order matches the spec (Phase 19, item 16), checked
    top to bottom, first failure wins."""

    def _reject(reason: str) -> PurchaseDecision:
        return PurchaseDecision(
            proceed=False, status=PurchaseStatus.CANCELLED, reason=reason, intent=intent
        )

    if not watch_rule.enabled:
        return _reject(f"Watch rule {watch_rule.id} is disabled.")

    if not policy.enabled:
        return _reject('PURCHASES_ENABLED is not "true" — the kill switch is off.')

    if not is_merchant_allowed(policy, merchant_domains):
        return _reject(f"Merchant {intent.merchant!r} is not in PURCHASE_ALLOWED_MERCHANTS.")

    if match_confidence < MIN_FINANCIAL_MATCH_CONFIDENCE:
        return _reject(
            f"Match confidence {match_confidence} is below the minimum required for a "
            f"financial decision ({MIN_FINANCIAL_MATCH_CONFIDENCE})."
        )

    if not available:
        return _reject("Product is out of stock.")

    if watch_rule.max_price is None:
        return _reject(f"Watch rule {watch_rule.id} has no max_price configured.")

    if total_cost > watch_rule.max_price:
        return _reject(
            f"Total cost {total_cost} exceeds this watch rule's max_price {watch_rule.max_price}."
        )

    if intent.quantity > watch_rule.max_quantity:
        return _reject(
            f"Requested quantity {intent.quantity} exceeds max_quantity {watch_rule.max_quantity}."
        )

    if policy.max_order_eur is not None and total_cost > policy.max_order_eur:
        return _reject(
            f"Total cost {total_cost} exceeds PURCHASE_MAX_ORDER_EUR {policy.max_order_eur}."
        )

    if policy.max_daily_eur is not None and (spent_today + total_cost) > policy.max_daily_eur:
        return _reject(
            f"Total cost {total_cost} would push today's spend to "
            f"{spent_today + total_cost}, above PURCHASE_MAX_DAILY_EUR {policy.max_daily_eur}."
        )

    if has_active_attempt:
        return _reject(
            f"An active purchase attempt already exists for listing {intent.listing_id}."
        )

    if (
        seconds_since_last_attempt is not None
        and seconds_since_last_attempt < policy.cooldown_seconds
    ):
        return _reject(
            f"Cooldown active: {seconds_since_last_attempt:.0f}s since the last attempt for "
            f"this listing, PURCHASE_COOLDOWN_SECONDS is {policy.cooldown_seconds}."
        )

    return PurchaseDecision(
        proceed=True,
        status=PurchaseStatus.CREATED,
        reason="All purchase safety checks passed.",
        intent=intent,
    )


def _seconds_since_last_attempt(session: Session, listing_id: int, now: datetime) -> float | None:
    last = crud.get_most_recent_purchase_attempt_for_listing(session, listing_id)
    if last is None:
        return None
    created_at = last.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return (now - created_at).total_seconds()


def _spent_today(session: Session, now: datetime) -> Decimal:
    since = (now - timedelta(days=1)).replace(tzinfo=None)
    return crud.sum_purchased_total_since(session, since)


def build_decision_context(
    session: Session, intent: PurchaseIntent, *, now: datetime | None = None
) -> tuple[bool, float | None, Decimal]:
    """Everything evaluate_purchase_intent() needs that only the DB can
    answer — read-only, safe to call from a dry run too."""
    now = now or datetime.now(UTC)
    active_attempt = crud.get_active_purchase_attempt_for_listing(session, intent.listing_id)
    has_active = active_attempt is not None
    since_last = _seconds_since_last_attempt(session, intent.listing_id, now)
    spent_today = _spent_today(session, now)
    return has_active, since_last, spent_today


@dataclass(frozen=True, slots=True)
class PurchaseOutcome:
    status: PurchaseStatus
    reason: str
    intent: PurchaseIntent
    attempt_id: int | None = None
    total_cost: Decimal | None = None
    shipping_cost: Decimal | None = None
    order_reference: str | None = None


async def attempt_purchase(
    session: Session,
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    policy: PurchasePolicy,
    connector_registry: PurchaseConnectorRegistry,
    merchant_domains: tuple[str, ...],
    notifier: EmbedSender,
    *,
    now: datetime | None = None,
) -> PurchaseOutcome:
    """Real attempt only — never call this for a dry run. Safe to run as
    a background asyncio task: any exception here is caught and reported
    (never propagated), and it never touches other WatchRules' state.
    """
    now = now or datetime.now(UTC)
    intent = build_purchase_intent(watch_rule, observation, match_result, now=now)
    total_cost = intent.observed_price * intent.quantity
    has_active, since_last, spent_today = build_decision_context(session, intent, now=now)

    decision = evaluate_purchase_intent(
        watch_rule=watch_rule,
        intent=intent,
        policy=policy,
        merchant_domains=merchant_domains,
        match_confidence=match_result.confidence,
        available=observation.available,
        total_cost=total_cost,
        has_active_attempt=has_active,
        seconds_since_last_attempt=since_last,
        spent_today=spent_today,
    )
    if not decision.proceed:
        logger.info("watch_rule=%s purchase skipped: %s", watch_rule.id, decision.reason)
        return PurchaseOutcome(status=decision.status, reason=decision.reason, intent=intent)

    logger.info(
        "watch_rule=%s purchase intent created merchant=%s price=%s qty=%s",
        watch_rule.id,
        intent.merchant,
        intent.observed_price,
        intent.quantity,
    )

    # Row created immediately after the has_active_attempt check above,
    # with no `await` in between either here or inside evaluate_purchase_
    # intent() (pure, no I/O) — the idempotency lock for this listing.
    attempt = crud.create_purchase_attempt(
        session,
        watch_rule_id=watch_rule.id,
        listing_id=watch_rule.listing_id,
        status=PurchaseStatus.CREATED.value,
        observed_price=intent.observed_price,
        max_price_allowed=watch_rule.max_price,
        quantity=intent.quantity,
    )

    try:
        connector = connector_registry.get(intent.merchant)
    except PurchaseConnectorNotRegisteredError as exc:
        crud.update_purchase_attempt(
            session,
            attempt.id,
            status=PurchaseStatus.FAILED.value,
            failure_reason=str(exc),
        )
        await _notify_purchase(notifier, "PURCHASE FAILED", intent, reason=str(exc))
        return PurchaseOutcome(status=PurchaseStatus.FAILED, reason=str(exc), intent=intent)

    crud.update_purchase_attempt(session, attempt.id, status=PurchaseStatus.VALIDATING.value)

    try:
        revalidated = await asyncio.to_thread(connector.revalidate, intent)
    except AutomatedCheckoutUnsupportedError as exc:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.AUTOMATED_CHECKOUT_UNSUPPORTED,
            str(exc),
        )
    except HumanActionRequiredError as exc:
        return await _finish(
            session, notifier, attempt.id, intent, PurchaseStatus.HUMAN_ACTION_REQUIRED, str(exc)
        )
    except Exception as exc:  # noqa: BLE001 - a connector bug must never crash the worker
        return await _finish(session, notifier, attempt.id, intent, PurchaseStatus.FAILED, str(exc))

    shipping = revalidated.shipping_cost if revalidated.shipping_cost is not None else Decimal("0")
    revalidated_total = revalidated.price * intent.quantity + shipping
    if not revalidated.available:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.CANCELLED,
            "Stock disappeared during revalidation.",
        )
    insufficient_stock = (
        revalidated.quantity_available is not None
        and revalidated.quantity_available < intent.quantity
    )
    if insufficient_stock:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.CANCELLED,
            f"Only {revalidated.quantity_available} unit(s) available at checkout, "
            f"needed {intent.quantity}.",
        )
    if watch_rule.max_price is not None and revalidated_total > watch_rule.max_price:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.CANCELLED,
            f"Revalidated total cost {revalidated_total} exceeds max_price "
            f"{watch_rule.max_price} (price or shipping changed before checkout).",
        )
    if policy.max_order_eur is not None and revalidated_total > policy.max_order_eur:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.CANCELLED,
            f"Revalidated total cost {revalidated_total} exceeds PURCHASE_MAX_ORDER_EUR "
            f"{policy.max_order_eur}.",
        )

    crud.update_purchase_attempt(session, attempt.id, status=PurchaseStatus.CHECKOUT_STARTED.value)
    await _notify_purchase(notifier, "PURCHASE STARTED", intent, reason="Checkout in progress.")

    try:
        result = await asyncio.to_thread(connector.checkout, intent, revalidated)
    except AutomatedCheckoutUnsupportedError as exc:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.AUTOMATED_CHECKOUT_UNSUPPORTED,
            str(exc),
        )
    except HumanActionRequiredError as exc:
        return await _finish(
            session, notifier, attempt.id, intent, PurchaseStatus.HUMAN_ACTION_REQUIRED, str(exc)
        )
    except Exception as exc:  # noqa: BLE001 - a connector bug must never crash the worker
        return await _finish(session, notifier, attempt.id, intent, PurchaseStatus.FAILED, str(exc))

    if not result.success:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.FAILED,
            result.failure_reason or "Checkout reported failure with no reason given.",
        )

    crud.update_purchase_attempt(
        session,
        attempt.id,
        status=PurchaseStatus.PURCHASED.value,
        final_price=result.final_price,
        shipping_cost=result.shipping_cost,
        total_cost=result.total_cost,
        order_reference=result.order_reference,
    )
    logger.info(
        "purchase_attempt=%s status=purchased total=%s order_reference=%s",
        attempt.id,
        result.total_cost,
        result.order_reference,
    )
    await _notify_purchase(
        notifier,
        "PURCHASE SUCCESS",
        intent,
        reason="Purchase completed.",
        final_price=result.final_price,
        shipping_cost=result.shipping_cost,
        total_cost=result.total_cost,
        order_reference=result.order_reference,
    )
    return PurchaseOutcome(
        status=PurchaseStatus.PURCHASED,
        reason="Purchase completed.",
        intent=intent,
        attempt_id=attempt.id,
        total_cost=result.total_cost,
        shipping_cost=result.shipping_cost,
        order_reference=result.order_reference,
    )


async def _finish(
    session: Session,
    notifier: EmbedSender,
    attempt_id: int,
    intent: PurchaseIntent,
    status: PurchaseStatus,
    reason: str,
) -> PurchaseOutcome:
    crud.update_purchase_attempt(session, attempt_id, status=status.value, failure_reason=reason)
    logger.info("purchase_attempt=%s status=%s reason=%s", attempt_id, status.value, reason)
    title = {
        PurchaseStatus.HUMAN_ACTION_REQUIRED: "HUMAN ACTION REQUIRED",
        PurchaseStatus.AUTOMATED_CHECKOUT_UNSUPPORTED: "AUTOMATED CHECKOUT UNSUPPORTED",
        PurchaseStatus.FAILED: "PURCHASE FAILED",
        PurchaseStatus.CANCELLED: "PURCHASE FAILED",
    }.get(status, "PURCHASE FAILED")
    await _notify_purchase(notifier, title, intent, reason=reason)
    return PurchaseOutcome(status=status, reason=reason, intent=intent, attempt_id=attempt_id)


async def _notify_purchase(
    notifier: EmbedSender,
    title: str,
    intent: PurchaseIntent,
    *,
    reason: str,
    final_price: Decimal | None = None,
    shipping_cost: Decimal | None = None,
    total_cost: Decimal | None = None,
    order_reference: str | None = None,
) -> None:
    embed = format_purchase_embed(
        title,
        intent,
        reason=reason,
        final_price=final_price,
        shipping_cost=shipping_cost,
        total_cost=total_cost,
        order_reference=order_reference,
    )
    try:
        await notifier.send_embed(embed)
    except Exception:  # noqa: BLE001 - a Discord failure must never crash a purchase attempt
        pass
