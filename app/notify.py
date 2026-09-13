"""Wires Monitoring -> Decision -> Opportunity -> Notification.

Depends on engine/, and notifications/discord/ — this is the one place
that is allowed to know about both. engine/ never imports discord.py or
anything under notifications/; notifications/discord/ never imports this
module or engine/monitoring.py. Kept as a light, manually-invoked function
for Phase 9 — not wired into a scheduler yet (no long-running process
consumes it).

Opportunity scoring (Phase 15) only ever enriches an embed that was
already going to be sent — it never triggers a notification on its own.
The existing notification policy (allowed decision + at least one event)
is unchanged.

Phase 26 audit: Discord dispatch (notifier.send_embed) is the one real
network I/O left directly in the fast path's own await chain. Bounded by
a timeout + one retry (_send_embed_with_retry) so a hung/slow/rate-
limited Discord call can never stall forever, and — when the caller
passes `dispatch` (app/worker.py always does) — fired through it instead
of awaited inline, so a slow send for one WatchRule's alert can never
delay the next tick's monitoring of every other WatchRule. `dispatch`
defaults to None (a plain inline `await`) so every existing caller/test
keeps its exact original synchronous behavior; the timeout+retry wrapper
still applies either way, since it was always a strict improvement.

Phase 27: a failed send used to just be logged — the alert was gone for
good. When the caller now also passes `session`, each event's embed is
frozen into a durable NotificationDelivery row (app/delivery.py) *before*
any send is attempted, and the actual attempt goes through
app/delivery.py's claim/retry/backoff machinery instead of the old
in-process-only _send_embed_with_retry. `session` defaults to None (no
durable tracking, exact old inline-retry behavior) for the same reason
`dispatch` does — every existing caller/test that only cares about
decision logic is unaffected. app/worker.py (the real worker) and
scripts/watch.py's `test` command (a real, user-triggered send) both pass
it; nothing else needs to.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from app.delivery import attempt_delivery, create_pending_delivery
from app.resale import resolve_resale_confidence, resolve_resale_price_for_opportunity
from engine.alerting import build_opportunity_intelligence
from engine.decision import (
    DecisionResult,
    effective_market_source,
    effective_minimum_resale_confidence,
    effective_resale_price_mode,
    evaluate,
)
from engine.opportunity import (
    OpportunityResult,
    build_opportunity_inputs,
    evaluate_opportunity,
    recommend_purchase,
)
from engine.ranking import OpportunityCandidate, RankingConfig, rank_opportunities
from notifications.discord.formatter import format_event_embed

if TYPE_CHECKING:
    import discord
    from sqlalchemy.orm import Session

    from database.models import WatchRule
    from engine.alerting import OpportunityIntelligence
    from engine.change_detection import MonitoringEvent
    from engine.opportunity import PurchaseRecommendation
    from market_data.cache import TTLCache
    from market_data.estimator import Confidence, ResaleEstimate
    from market_data.registry import MarketDataRegistry
    from products.matcher import MatchResult
    from products.observation import ProductObservation


class EmbedSender(Protocol):
    """What notify_events_if_allowed needs — satisfied by DiscordNotifier
    and trivially by a fake in tests, with no discord.py client required."""

    async def send_embed(self, embed: discord.Embed) -> None: ...


logger = logging.getLogger(__name__)

_SEND_TIMEOUT_SECONDS = 10.0
_SEND_MAX_ATTEMPTS = 2


async def _send_embed_with_retry(
    notifier: EmbedSender, embed: discord.Embed, *, watch_rule_id: int, event_type: str
) -> None:
    """At most _SEND_MAX_ATTEMPTS tries, each bounded by
    _SEND_TIMEOUT_SECONDS — a hung, slow, or rate-limited Discord call
    must never block its caller indefinitely. Every attempt failure is
    logged; the final one at ERROR (a lost alert is a real, visible
    event — the underlying restock/event stays in the database either
    way, but nobody was told about it, which is worth knowing)."""
    last_exc: BaseException | None = None
    for attempt in range(1, _SEND_MAX_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(notifier.send_embed(embed), timeout=_SEND_TIMEOUT_SECONDS)
            return
        except Exception as exc:  # noqa: BLE001 - a notifier bug must never crash the caller
            last_exc = exc
            logger.warning(
                "watch_rule=%s event=%s notification attempt %d/%d failed: %s",
                watch_rule_id,
                event_type,
                attempt,
                _SEND_MAX_ATTEMPTS,
                exc,
            )
    logger.error(
        "watch_rule=%s event=%s notification FAILED after %d attempt(s) — "
        "alert was not delivered: %s",
        watch_rule_id,
        event_type,
        _SEND_MAX_ATTEMPTS,
        last_exc,
    )


def _compute_opportunity_sync(
    watch_rule: WatchRule,
    purchase_price: Decimal,
    market_registry: MarketDataRegistry | None,
    cache: TTLCache | None,
) -> tuple[OpportunityResult | None, ResaleEstimate | None]:
    resale_price, resale_estimate = resolve_resale_price_for_opportunity(
        watch_rule, market_registry, cache
    )
    if resale_price is None:
        return None, resale_estimate
    # Phase 25: shared with purchase/engine.py's post-revalidation
    # profitability re-check, so a Product Watch's configured thresholds
    # (0 when unset, matching engine.opportunity's own defaults) are
    # always read the same way — previously this always used hardcoded
    # defaults, so a configured minimum_net_profit/minimum_roi_pct never
    # actually reached the classification.
    config, thresholds = build_opportunity_inputs(watch_rule, resale_price)
    return evaluate_opportunity(purchase_price, config, thresholds), resale_estimate


async def compute_opportunity(
    watch_rule: WatchRule,
    purchase_price: Decimal,
    market_registry: MarketDataRegistry | None,
    cache: TTLCache | None,
) -> tuple[OpportunityResult | None, ResaleEstimate | None]:
    """Public (Phase 25) so app/worker.py can call this once and reuse the
    result for both the immediate alert and, when profitability mode
    applies, the purchase-path profitability gate — instead of each
    computing it separately. Cheap to call twice regardless: market mode's
    result is cached by resolve_resale_price_for_opportunity's TTLCache
    key, so a second call within the same tick never repeats the network
    lookup."""
    if effective_resale_price_mode(watch_rule) == "market":
        # Phase 23: offloaded to a thread so a slow/misbehaving market
        # source can't block the event loop other concurrently-checked
        # WatchRules' own alerts share.
        return await asyncio.to_thread(
            _compute_opportunity_sync, watch_rule, purchase_price, market_registry, cache
        )
    # "manual" mode is pure in-memory arithmetic — no thread-hop overhead
    # for the common case (still true of every real WatchRule today).
    return _compute_opportunity_sync(watch_rule, purchase_price, market_registry, cache)


def _build_evaluation(
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    opportunity: OpportunityResult | None,
    resale_confidence: Confidence | None,
    resale_estimate: ResaleEstimate | None,
) -> OpportunityIntelligence:
    """Phase 31: builds the OpportunityCandidate straight from data this
    function's caller already computed (opportunity/resale_confidence/
    resale_estimate) — deliberately NOT calling
    app/opportunity_snapshot.py's build_opportunity_candidate() again,
    which would re-fetch ObservationRecords and re-run match_product()
    from the database; this module already has the equivalent, freshly-
    computed values in hand and must not redo that work (or need a
    Session, which this function doesn't always have) just to reshape
    them. rank_opportunities() itself is still the single, unduplicated
    scoring path — only the candidate assembly is inlined here."""
    resale_price_source: str | None = None
    if opportunity is not None:
        resale_price_source = (
            (effective_market_source(watch_rule) or "unknown")
            if effective_resale_price_mode(watch_rule) == "market"
            else "manual"
        )
    candidate = OpportunityCandidate(
        watch_rule_id=watch_rule.id,
        merchant=observation.merchant,
        # observation.name (this check's own observed name), not
        # watch_rule.product.name: this function only ever has data the
        # caller already fetched in hand, never a reason to touch the
        # product relationship (which some lightweight orchestration
        # tests construct a bare WatchRule without loading at all).
        product_name=observation.name,
        purchase_price=observation.price,
        estimated_resale_price=opportunity.estimated_resale_price if opportunity else None,
        net_profit=opportunity.net_profit if opportunity else None,
        roi_pct=opportunity.roi_pct if opportunity else None,
        net_margin_pct=opportunity.net_margin_pct if opportunity else None,
        resale_confidence=resale_confidence.value if resale_confidence else None,
        in_stock=observation.available,
        match_confidence=match_result.confidence,
        market_sample_size=resale_estimate.sample_size if resale_estimate else None,
        resale_price_source=resale_price_source,
    )
    ranking_config = RankingConfig()
    ranked = rank_opportunities([candidate], ranking_config)[0]
    return build_opportunity_intelligence(candidate, ranked, ranking_config)


async def notify_events_if_allowed(
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    events: tuple[MonitoringEvent, ...],
    notifier: EmbedSender,
    market_registry: MarketDataRegistry | None = None,
    cache: TTLCache | None = None,
    *,
    dispatch: Callable[[Coroutine[object, object, None]], None] | None = None,
    session: Session | None = None,
) -> DecisionResult:
    """Evaluate current state once; notify one embed per event only if allowed.

    No events -> nothing to report even when allowed (avoids notifying on
    every unchanged check). Not allowed -> never notifies, regardless of
    what changed. market_registry/cache default to None so every existing
    caller and test (manual resale mode only) is unaffected; they are only
    needed when a WatchRule has resale_price_mode="market".

    Phase 25: the restock alert itself is unaffected by profitability —
    engine.decision.evaluate() already decided whether to allow this
    (skipping the old hard target_price gate in profitability mode, see
    its module docstring); here, when a resale price is known, the embed
    is enriched with the net-profit/ROI breakdown, a resale-confidence
    label, and a STRONG_BUY/BUY/ALERT_ONLY/REJECT recommendation —
    display only, never a reason to withhold the alert.

    Phase 26: `dispatch`, when given, receives each event's send coroutine
    instead of this function awaiting it inline — app/worker.py passes its
    `_fire_and_forget` so a slow Discord call for one event can never
    delay this function's return (and, by extension, the next WatchRule's
    monitoring in the same tick). Left as None (the default), every event
    is awaited in order exactly as before — every existing caller/test
    keeps its exact original behavior.

    Phase 27: when `session` is given, each event's embed is frozen into
    a durable NotificationDelivery row (app/delivery.py) before the send
    is even attempted, and the coroutine handed to `dispatch`/awaited is
    app/delivery.py's attempt_delivery() (claim + timeout + classify +
    bounded retry with backoff, persisted) instead of the old in-process-
    only _send_embed_with_retry. `session` defaults to None — every
    existing caller/test that only cares about decision logic keeps the
    exact old inline-retry behavior with no durable tracking at all. An
    event somehow missing its record_id (see engine/change_detection.py's
    MonitoringEvent docstring — not expected in practice) also falls back
    to the old inline path for that one event, since there is nothing to
    key a delivery row on.
    """
    decision = evaluate(watch_rule, observation, match_result)
    if decision.allowed:
        opportunity, resale_estimate = await compute_opportunity(
            watch_rule, observation.price, market_registry, cache
        )
        resale_confidence: Confidence | None = None
        recommendation: PurchaseRecommendation | None = None
        recommendation_reason: str | None = None
        if opportunity is not None:
            resale_confidence = resolve_resale_confidence(watch_rule, resale_estimate)
            recommendation, recommendation_reason = recommend_purchase(
                opportunity, resale_confidence, effective_minimum_resale_confidence(watch_rule)
            )
        evaluation = _build_evaluation(
            watch_rule, observation, match_result, opportunity, resale_confidence, resale_estimate
        )
        for event in events:
            embed = format_event_embed(
                event,
                observation,
                match_result,
                opportunity=opportunity,
                resale_estimate=resale_estimate,
                resale_confidence=resale_confidence,
                recommendation=recommendation,
                recommendation_reason=recommendation_reason,
                evaluation=evaluation,
            )
            if session is not None and event.record_id is not None:
                delivery = create_pending_delivery(session, event_id=event.record_id, embed=embed)
                send_coro = attempt_delivery(session, delivery.id, notifier)
            else:
                send_coro = _send_embed_with_retry(
                    notifier, embed, watch_rule_id=watch_rule.id, event_type=event.event_type.value
                )
            if dispatch is not None:
                dispatch(send_coro)
            else:
                await send_coro
    return decision
