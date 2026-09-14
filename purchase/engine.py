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

Phase 25 — profitability-based decisions: a watch in profitability mode
(engine.decision.is_profitability_mode) is no longer gated on a flat
max_price alone. evaluate_purchase_intent() checks the pre-computed
opportunity/resale_confidence (built by the caller from the item price —
see app/notify.py's compute_opportunity, which purchase/ must not import
directly: it depends on market_data/, and purchase/ stays a peer of
engine/, never a dependent of app/). attempt_purchase() then re-checks
profitability a second time after revalidate() returns, this time against
the REAL revalidated total (item + real shipping + real tax) — matching
"produit + livraison = 60€" needing to be evaluated as a whole, not just
the item price — using engine.opportunity.build_opportunity_inputs(),
which both this module and app/notify.py share so the same fee/threshold
assumptions are never computed two different ways. This module does
import market_data.estimator.Confidence directly (a small, dependency-
free StrEnum with no I/O), the same pragmatic exception engine/opportunity.py
already makes — never anything that performs a market lookup itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from database import crud
from database.time_utils import ensure_utc
from engine.decision import (
    MIN_FINANCIAL_MATCH_CONFIDENCE,
    effective_max_price,
    effective_minimum_resale_confidence,
    effective_resale_updated_at,
    is_profitability_mode,
)
from engine.opportunity import (
    PurchaseRecommendation,
    build_opportunity_inputs,
    evaluate_opportunity,
    recommend_purchase,
)
from market_data.estimator import Confidence
from notifications.discord.formatter import format_purchase_embed
from purchase.base import AutomatedCheckoutUnsupportedError, HumanActionRequiredError
from purchase.config import is_merchant_allowed
from purchase.models import PurchaseDecision, PurchaseIntent, PurchaseStatus
from purchase.registry import PurchaseConnectorNotRegisteredError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.notify import EmbedSender
    from database.models import WatchRule
    from engine.opportunity import OpportunityResult
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
    fetched — no network call here. max_price_allowed is the effective
    max_price (the rule's own, or its product's shared ceiling — see
    engine.decision.effective_max_price) when one is configured. A
    profitability-mode rule (Phase 25) can legitimately have none —
    profitability decides instead of a fixed cap — so this falls back to
    the observed total cost rather than a bare 0, which would fail
    PurchaseAttempt's "max_price_allowed > 0" check and also read as a
    confusing "€0 allowed" in the Discord embed. A rule with neither a
    max_price nor profitability thresholds configured never becomes a
    purchase candidate at all (see evaluate_purchase_intent)."""
    max_price = effective_max_price(watch_rule)
    if max_price is None:
        max_price = observation.price * watch_rule.max_quantity
    return PurchaseIntent(
        watch_rule_id=watch_rule.id,
        product_id=watch_rule.product_id,
        listing_id=watch_rule.listing_id,
        merchant=observation.merchant,
        product_name=watch_rule.product.name,
        url=observation.url,
        observed_price=observation.price,
        max_price_allowed=max_price,
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
    opportunity: OpportunityResult | None = None,
    resale_confidence: Confidence | None = None,
    blocking_attempts_for_product: int = 0,
    now: datetime | None = None,
) -> PurchaseDecision:
    """Every check fails closed: any missing or ambiguous signal refuses
    the purchase. Order matches the spec (Phase 19, item 16), checked
    top to bottom, first failure wins.

    opportunity/resale_confidence (Phase 25) are only consulted when the
    watch is in profitability mode (engine.decision.is_profitability_mode
    — a minimum_net_profit or minimum_roi_pct is configured, at rule or
    Product level); every other watch keeps the original price-only gate
    exactly as before. Both default to None so existing callers/tests
    that never pass them are unaffected as long as they aren't in
    profitability mode.

    blocking_attempts_for_product (Phase 33): count of active-or-purchased
    PurchaseAttempt rows across *every* Listing of this same Product (not
    just this one) — see database/models.py::PurchaseAttempt and
    database/crud.py::get_blocking_purchase_attempts_for_product. Defaults
    to 0 (existing callers/tests unaffected) but every real caller
    (attempt_purchase below) always passes the real count: the same
    product briefly in stock at several retailers at once must never
    result in more than one purchase."""

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

    profitability_mode = is_profitability_mode(watch_rule)
    max_price = effective_max_price(watch_rule)
    if max_price is None and not profitability_mode:
        return _reject(
            f"Watch rule {watch_rule.id} has no max_price or profitability thresholds configured."
        )

    if max_price is not None and total_cost > max_price:
        return _reject(f"Total cost {total_cost} exceeds this watch rule's max_price {max_price}.")

    if profitability_mode:
        resale_updated_at = effective_resale_updated_at(watch_rule)
        if (
            policy.max_resale_age_seconds is not None
            and resale_updated_at is not None
            and (now or datetime.now(UTC)) - ensure_utc(resale_updated_at)
            > timedelta(seconds=policy.max_resale_age_seconds)
        ):
            return _reject(
                f"STALE_MARKET_DATA: resale estimate last updated "
                f"{ensure_utc(resale_updated_at).isoformat()}, older than "
                f"PURCHASE_MAX_RESALE_AGE_SECONDS={policy.max_resale_age_seconds}s."
            )
        recommendation, recommendation_reason = recommend_purchase(
            opportunity,
            resale_confidence or Confidence.LOW,
            effective_minimum_resale_confidence(watch_rule),
        )
        if recommendation not in (PurchaseRecommendation.BUY, PurchaseRecommendation.STRONG_BUY):
            return _reject(f"Profitability check failed: {recommendation_reason}")

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

    if blocking_attempts_for_product > 0:
        return _reject(
            f"Product {intent.product_id} already has an active or completed purchase "
            f"attempt on another listing — max one successful purchase per product, "
            f"enforced across all retailers."
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
    return (now - ensure_utc(last.created_at)).total_seconds()


def _spent_today(session: Session, now: datetime) -> Decimal:
    since = (now - timedelta(days=1)).replace(tzinfo=None)
    return crud.sum_purchased_total_since(session, since)


def build_decision_context(
    session: Session, intent: PurchaseIntent, *, now: datetime | None = None
) -> tuple[bool, float | None, Decimal, int]:
    """Everything evaluate_purchase_intent() needs that only the DB can
    answer — read-only, safe to call from a dry run too. The 4th element
    (Phase 33) is the product-wide blocking-attempt count — see
    evaluate_purchase_intent's docstring and
    database/crud.py::get_blocking_purchase_attempts_for_product."""
    now = now or datetime.now(UTC)
    active_attempt = crud.get_active_purchase_attempt_for_listing(session, intent.listing_id)
    has_active = active_attempt is not None
    since_last = _seconds_since_last_attempt(session, intent.listing_id, now)
    spent_today = _spent_today(session, now)
    blocking_for_product = len(
        crud.get_blocking_purchase_attempts_for_product(session, intent.product_id)
    )
    return has_active, since_last, spent_today, blocking_for_product


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
    opportunity: OpportunityResult | None = None,
    resale_confidence: Confidence | None = None,
    policy_provider: Callable[[], PurchasePolicy] | None = None,
) -> PurchaseOutcome:
    """Real attempt only — never call this for a dry run. Safe to run as
    a background asyncio task: any exception here is caught and reported
    (never propagated), and it never touches other WatchRules' state.

    policy_provider (Phase 33): when given, called fresh immediately
    before checkout to re-read the kill switch — `policy` above is only a
    snapshot from the top of this call, and real network I/O
    (revalidate(), possibly several seconds against a slow merchant)
    happens in between. Defaults to None (reuse `policy` unchanged,
    exactly the old behavior) so every test/caller that constructs a
    PurchasePolicy test double directly — never via real environment
    variables — is unaffected; app/worker.py (the real caller) passes
    purchase.config.load_purchase_policy so production genuinely re-reads
    .env, not a stale in-memory snapshot.

    opportunity/resale_confidence (Phase 25) come pre-computed from the
    caller (app/worker.py already computes them for the alert embed via
    app.notify.compute_opportunity — purchase/ must not depend on app/ or
    market_data/'s network-touching pieces itself, see
    evaluate_purchase_intent's docstring) and only matter for a
    profitability-mode watch; every other watch ignores both.
    """
    now = now or datetime.now(UTC)
    intent = build_purchase_intent(watch_rule, observation, match_result, now=now)
    total_cost = intent.observed_price * intent.quantity
    max_price = effective_max_price(watch_rule)
    has_active, since_last, spent_today, blocking_for_product = build_decision_context(
        session, intent, now=now
    )

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
        opportunity=opportunity,
        resale_confidence=resale_confidence,
        blocking_attempts_for_product=blocking_for_product,
        now=now,
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

    # Row created immediately after the has_active_attempt/blocking-for-
    # product checks above, with no `await` in between either here or
    # inside evaluate_purchase_intent() (pure, no I/O) — the idempotency
    # lock, now enforced both per-listing and product-wide (Phase 33).
    # uq_one_active_or_purchased_attempt_per_product (database/models.py)
    # is a real DB-level backstop for this same invariant — see the
    # IntegrityError handling a few lines below.
    try:
        attempt = crud.create_purchase_attempt(
            session,
            watch_rule_id=watch_rule.id,
            listing_id=watch_rule.listing_id,
            product_id=intent.product_id,
            status=PurchaseStatus.CREATED.value,
            observed_price=intent.observed_price,
            # max_price_allowed is NOT NULL and CHECK > 0; None
            # (profitability mode with no hard_max_total set — Phase 25)
            # has no fixed ceiling to record, so this uses intent's own
            # value, which build_purchase_intent() already resolved the
            # same way.
            max_price_allowed=intent.max_price_allowed,
            quantity=intent.quantity,
        )
    except IntegrityError:
        # Defense-in-depth only — see uq_one_active_or_purchased_attempt_
        # per_product's own comment in database/models.py. The Python-
        # level blocking_attempts_for_product check above is expected to
        # catch every real case (no `await` between that check and this
        # insert, in this project's single-event-loop architecture); this
        # branch existing at all means that reasoning was somehow wrong,
        # which is exactly why the constraint is a real DB index and not
        # just a comment. Roll back so this session's own transaction
        # isn't left in a failed state, then report a clean rejection —
        # never an unhandled exception out of a purchase attempt.
        session.rollback()
        logger.warning(
            "watch_rule=%s purchase attempt insert blocked by the database-level "
            "one-per-product constraint — another listing won the race",
            watch_rule.id,
        )
        return PurchaseOutcome(
            status=PurchaseStatus.CANCELLED,
            reason=(
                f"Product {intent.product_id} already claimed by another listing's purchase "
                "attempt (database-level constraint)."
            ),
            intent=intent,
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
    tax = revalidated.tax_amount if revalidated.tax_amount is not None else Decimal("0")
    revalidated_total = revalidated.price * intent.quantity + shipping + tax
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
    if max_price is not None and revalidated_total > max_price:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.CANCELLED,
            f"Revalidated total cost {revalidated_total} exceeds max_price "
            f"{max_price} (price or shipping changed before checkout).",
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

    if is_profitability_mode(watch_rule) and opportunity is not None:
        # Re-run profitability against the REAL acquisition total (item +
        # real shipping + real tax), not just the item price the initial
        # alert used — "produit + livraison = 60€" only becomes knowable
        # once revalidate() has actually quoted shipping/tax.
        config, thresholds = build_opportunity_inputs(
            watch_rule, opportunity.estimated_resale_price
        )
        revalidated_opportunity = evaluate_opportunity(revalidated_total, config, thresholds)
        revalidated_recommendation, revalidated_reason = recommend_purchase(
            revalidated_opportunity,
            resale_confidence or Confidence.LOW,
            effective_minimum_resale_confidence(watch_rule),
        )
        if revalidated_recommendation not in (
            PurchaseRecommendation.BUY,
            PurchaseRecommendation.STRONG_BUY,
        ):
            return await _finish(
                session,
                notifier,
                attempt.id,
                intent,
                PurchaseStatus.CANCELLED,
                f"Profitability check failed on the revalidated total {revalidated_total}: "
                f"{revalidated_reason}",
            )

    # Phase 33 hardening: re-read the kill switch fresh, immediately
    # before the one genuinely transactional step in this whole function.
    # `policy` above is a snapshot from the top of this call — real
    # network I/O (revalidate(), possibly several seconds against a slow
    # merchant) happened since then. Checked here, not only at the top:
    # "juste avant tout chemin transactionnel dangereux, pas seulement au
    # démarrage." See policy_provider's own docstring above for why this
    # doesn't just call load_purchase_policy() directly.
    fresh_policy = policy_provider() if policy_provider is not None else policy
    if not fresh_policy.enabled:
        return await _finish(
            session,
            notifier,
            attempt.id,
            intent,
            PurchaseStatus.CANCELLED,
            "PURCHASES_ENABLED was turned off during this attempt — aborting before checkout.",
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


def reconcile_orphaned_purchase_attempts(session: Session, *, now: datetime | None = None) -> int:
    """Phase 33 section 24 ("worker restart during checkout preparation"):
    a PurchaseAttempt left in CREATED/VALIDATING/CHECKOUT_STARTED means
    the process died mid-attempt — we genuinely don't know whether a real
    checkout got submitted on the merchant's side or not, so the only
    safe move is to mark it FAILED (never PURCHASED, never silently
    retried) and let a fresh check re-evaluate the product from scratch.
    Without this, get_active_purchase_attempt_for_listing/
    get_blocking_purchase_attempts_for_product would treat that orphaned
    row as still "in flight" forever, permanently blocking every future
    attempt at that product across every retailer — a real, if not yet
    triggered, gap (PURCHASES_ENABLED has stayed false this whole
    project, so no real PurchaseAttempt row has ever existed to orphan).
    Call once, at worker startup, before the first tick. Returns how many
    rows were reconciled (0 on every normal, clean-shutdown startup)."""
    now = now or datetime.now(UTC)
    orphaned = crud.list_active_purchase_attempts(session)
    for attempt in orphaned:
        crud.update_purchase_attempt(
            session,
            attempt.id,
            status=PurchaseStatus.FAILED.value,
            failure_reason=(
                "Orphaned by a worker restart while this attempt was in progress "
                f"(status was {attempt.status!r}) — outcome on the merchant's side is "
                "unknown, never assumed successful, never auto-resumed."
            ),
        )
        logger.warning(
            "purchase_attempt=%s reconciled at startup: was %s, marked failed (orphaned)",
            attempt.id,
            attempt.status,
        )
    return len(orphaned)
