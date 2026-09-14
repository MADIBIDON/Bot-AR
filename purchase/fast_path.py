"""Phase 35: the ONE hot path — identity already validated -> atomic
claim -> purchase decision — extracted unchanged out of
purchase/engine.py::attempt_purchase() so there is exactly one source of
truth for this sequence, never a second, competing implementation.

Order is decide-then-claim, not claim-then-decide: a decision that
rejects (e.g. PURCHASES_ENABLED=false, price over ceiling, cooldown
active) must leave ZERO PurchaseAttempt row behind — enshrined by
tests/test_purchase_engine_integration.py::
test_purchase_disabled_creates_no_attempt_row since Phase 19 — so this
keeps the exact order every existing test already audits, rather than
reintroducing Phase 34's draft claim-then-decide ordering (which would
have silently created-then-cancelled a row for that same case, a real
behavioral change nobody asked for). Section 11/12's concurrency
guarantee is unaffected either way: the claim's atomicity comes from
"no `await` between the has_active/blocking-for-product check and the
INSERT" (this project's single-event-loop architecture) plus the real
SQLite unique index as a database-level backstop — both are preserved
verbatim here.

purchase/engine.py::attempt_purchase() is the only real caller: it
delegates its decision+claim section to run_hot_path() below, then
continues with connector.revalidate()/checkout() exactly as before.
Never call this from anywhere else — a second call site would be a
second hot path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from database import crud
from engine.decision import effective_max_price
from purchase.models import PurchaseDecision, PurchaseIntent, PurchaseStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import PurchaseAttempt, WatchRule
    from engine.opportunity import OpportunityResult
    from market_data.estimator import Confidence
    from products.matcher import MatchResult
    from products.observation import ProductObservation
    from purchase.config import PurchasePolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HotPathTrace:
    """Light, secret-free timing instrumentation (Phase 35 section 8) —
    four perf_counter_ns() reads, no I/O, no logging by default.
    claim_acquired_ns is None when the decision rejected before any claim
    was attempted (the common case); checkout_dispatch_ns starts None and
    is filled in by attempt_purchase() (via dataclasses.replace) right
    before the connector's first real network call."""

    stock_received_ns: int
    identity_validated_ns: int
    claim_acquired_ns: int | None
    decision_completed_ns: int
    checkout_dispatch_ns: int | None = None

    def _ms(self, end_ns: int | None) -> float | None:
        return None if end_ns is None else (end_ns - self.stock_received_ns) / 1_000_000

    @property
    def t0_to_identity_ms(self) -> float:
        return self._ms(self.identity_validated_ns)

    @property
    def t0_to_claim_ms(self) -> float | None:
        return self._ms(self.claim_acquired_ns)

    @property
    def t0_to_decision_ms(self) -> float:
        return self._ms(self.decision_completed_ns)

    @property
    def t0_to_dispatch_ms(self) -> float | None:
        return self._ms(self.checkout_dispatch_ns)


@dataclass(frozen=True, slots=True)
class HotPathResult:
    proceed: bool
    decision: PurchaseDecision
    intent: PurchaseIntent
    attempt: PurchaseAttempt | None
    max_price: Decimal | None
    trace: HotPathTrace


def run_hot_path(
    session: Session,
    watch_rule: WatchRule,
    observation: ProductObservation,
    match_result: MatchResult,
    policy: PurchasePolicy,
    merchant_domains: tuple[str, ...],
    *,
    opportunity: OpportunityResult | None = None,
    resale_confidence: Confidence | None = None,
    now: datetime | None = None,
    stock_received_ns: int | None = None,
) -> HotPathResult:
    """Identical logic to what used to live inline in attempt_purchase():
    build_purchase_intent -> build_decision_context (DB reads) ->
    evaluate_purchase_intent (pure decision) -> create_purchase_attempt
    (the atomic claim, with the IntegrityError backstop). match_result is
    already computed by the caller (the monitoring fast path, same tick)
    — identity_validated_ns marks the instant this hot path confirms it
    has that result in hand, not a re-validation."""
    from purchase.engine import (
        build_decision_context,
        build_purchase_intent,
        evaluate_purchase_intent,
    )

    t0 = stock_received_ns if stock_received_ns is not None else time.perf_counter_ns()
    now = now or datetime.now(UTC)

    intent = build_purchase_intent(watch_rule, observation, match_result, now=now)
    t_identity = time.perf_counter_ns()

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
    t_decision = time.perf_counter_ns()

    if not decision.proceed:
        trace = HotPathTrace(t0, t_identity, None, t_decision)
        return HotPathResult(
            proceed=False,
            decision=decision,
            intent=intent,
            attempt=None,
            max_price=max_price,
            trace=trace,
        )

    # The atomic claim: no `await` since has_active/blocking_for_product
    # were read above (this project's single-event-loop architecture is
    # what makes that gap safe) — see database/models.py::PurchaseAttempt
    # and uq_one_active_or_purchased_attempt_per_product for the real
    # database-level backstop this try/except defends.
    try:
        attempt = crud.create_purchase_attempt(
            session,
            watch_rule_id=watch_rule.id,
            listing_id=watch_rule.listing_id,
            product_id=intent.product_id,
            status=PurchaseStatus.CREATED.value,
            observed_price=intent.observed_price,
            max_price_allowed=intent.max_price_allowed,
            quantity=intent.quantity,
        )
    except IntegrityError:
        session.rollback()
        logger.warning(
            "watch_rule=%s purchase attempt insert blocked by the database-level "
            "one-per-product constraint — another listing won the race",
            watch_rule.id,
        )
        t_claim = time.perf_counter_ns()
        rejected = PurchaseDecision(
            proceed=False,
            status=PurchaseStatus.CANCELLED,
            reason=(
                f"Product {intent.product_id} already claimed by another listing's purchase "
                "attempt (database-level constraint)."
            ),
            intent=intent,
        )
        trace = HotPathTrace(t0, t_identity, t_claim, t_decision)
        return HotPathResult(
            proceed=False,
            decision=rejected,
            intent=intent,
            attempt=None,
            max_price=max_price,
            trace=trace,
        )

    t_claim = time.perf_counter_ns()
    trace = HotPathTrace(t0, t_identity, t_claim, t_decision)
    return HotPathResult(
        proceed=True,
        decision=decision,
        intent=intent,
        attempt=attempt,
        max_price=max_price,
        trace=trace,
    )
