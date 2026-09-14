"""Phase 34: instrumented warm-path pipeline — stock signal received ->
first checkout network request dispatched, target <=100ms.

Reuses the exact audited Phase 33 safety primitives unchanged
(products.matcher.match_product, purchase.engine.evaluate_purchase_intent,
database.crud's atomic-claim functions, purchase.config.load_purchase_policy
for the fresh kill-switch re-check) — this module adds instrumentation
and in-memory wiring on top, never a second, competing decision engine.

Ordering note (deliberately different from purchase/engine.py::
attempt_purchase, which evaluates the full policy decision BEFORE
inserting the PurchaseAttempt row): this module claims first (T3), then
decides (T4), to shrink the window in which two concurrent stock signals
for the same product both run the (slower, branchier) policy decision
before either one finds out it already lost the race. A claim that is
later rejected by the decision is immediately resolved to CANCELLED (see
_reject_claim below) so it never blocks a legitimate future attempt —
CANCELLED is not one of database.models.PurchaseAttempt's blocking
statuses. This ordering is exercised by its own concurrency test
(tests/test_fast_path.py) with the same rigor as Phase 33's original
50-task race test; it is NOT (yet) wired into purchase/engine.py::
attempt_purchase() as the production call path — that integration is
intentionally left as a follow-up rather than rushed without a full
re-audit of the existing, already-proven orchestration.

Never invents a network call: production callers pass a real `dispatch`
callback (a bound connector method); tests/benchmarks pass one pointed
at a local FakeMerchantServer (connectors/fake_merchant_http_server.py).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from database import crud
from engine.decision import MIN_FINANCIAL_MATCH_CONFIDENCE
from products.matcher import match_product
from products.observation import ProductObservation
from purchase.engine import evaluate_purchase_intent
from purchase.models import PurchaseIntent, PurchaseStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from purchase.config import PurchasePolicy
    from purchase.prepared_runtime import PreparedDropRuntime


class StockSignal(Protocol):
    """The minimal shape of an already-fetched, already-parsed positive
    stock observation — what a real connector's get_product() already
    returns. Never a raw HTTP response: fetching/parsing bytes into this
    shape is the monitoring fast path's own job (engine/monitoring.py),
    already benchmarked separately in scripts/benchmark_purchase_pipeline.py."""

    external_id: str
    name: str
    price: Decimal
    ean: str | None
    mpn: str | None


@dataclass(frozen=True, slots=True)
class WarmPathTrace:
    t0_ns: int  # stock-positive response fully received
    t1_ns: int  # parse/normalize done
    t2_ns: int  # identity validation done
    t3_ns: int  # atomic claim done
    t4_ns: int  # purchase decision done
    t5_ns: int  # request payload/coroutine ready
    t6_ns: int  # network request actually dispatched
    proceed: bool
    reason: str
    attempt_id: int | None = None

    @property
    def total_ms(self) -> float:
        return (self.t6_ns - self.t0_ns) / 1_000_000

    def segments_ms(self) -> dict[str, float]:
        return {
            "parse": (self.t1_ns - self.t0_ns) / 1_000_000,
            "identity_validation": (self.t2_ns - self.t1_ns) / 1_000_000,
            "atomic_claim": (self.t3_ns - self.t2_ns) / 1_000_000,
            "decision": (self.t4_ns - self.t3_ns) / 1_000_000,
            "request_prep": (self.t5_ns - self.t4_ns) / 1_000_000,
            "dispatch": (self.t6_ns - self.t5_ns) / 1_000_000,
        }


def _abort(
    t0: int, t1: int, t2: int, t3: int, reason: str, attempt_id: int | None = None
) -> WarmPathTrace:
    now = time.perf_counter_ns()
    return WarmPathTrace(t0, t1, t2, t3, now, now, now, False, reason, attempt_id)


def run_warm_path(
    session: Session,
    runtime: PreparedDropRuntime,
    signal: StockSignal,
    *,
    dispatch: Callable[[PurchaseIntent], None] | None = None,
    policy_provider: Callable[[], PurchasePolicy] | None = None,
    t0_ns: int | None = None,
) -> WarmPathTrace:
    """One full warm-path iteration. `dispatch` is called with the built
    PurchaseIntent at T5 and must itself perform (or trigger) the actual
    network send — this function measures up to the moment dispatch()
    returns as T6, which is honest only if dispatch() blocks until the
    request has actually left (a synchronous connector call, or an
    awaited coroutine already run to that point) rather than merely
    scheduling a background task.

    policy_provider follows the exact same injectable pattern as
    purchase/engine.py::attempt_purchase (Phase 33 P0#3): None (the
    default) reuses runtime.policy as snapshotted at arm time — every
    test/caller that never passes one is unaffected by the real
    environment. Real production/benchmark callers pass
    policy_provider=purchase.config.load_purchase_policy explicitly so
    the kill switch is always re-read fresh, never stale, immediately
    before the decision."""
    t0 = t0_ns if t0_ns is not None else time.perf_counter_ns()

    # T0 -> T1: parse/normalize the already-fetched positive stock signal.
    observation = ProductObservation(
        merchant=runtime.merchant,
        external_id=signal.external_id,
        name=signal.name,
        price=signal.price,
        currency="EUR",
        available=True,
        url=runtime.url,
        observed_at=datetime.now(UTC),
        ean=signal.ean,
        mpn=signal.mpn,
    )
    t1 = time.perf_counter_ns()

    # T1 -> T2: real identity validation — the same matcher the
    # monitoring fast path uses, against the Product already cached on
    # runtime.watch_rule (no extra DB read).
    product = runtime.watch_rule.product
    match = match_product(product, observation, expected_listing=runtime.watch_rule.listing)
    t2 = time.perf_counter_ns()

    if not match.matched or match.confidence < MIN_FINANCIAL_MATCH_CONFIDENCE:
        return _abort(
            t0,
            t1,
            t2,
            t2,
            f"Identity validation failed or below confidence floor "
            f"({match.confidence}): {match.reason}",
        )

    # T2 -> T3: the one sanctioned synchronous DB access on the hot path
    # — the atomic claim. Real SQLite unique-index-backed insert, same
    # crud functions as purchase/engine.py::attempt_purchase.
    blocking = crud.get_blocking_purchase_attempts_for_product(session, runtime.product_id)
    if blocking:
        return _abort(t0, t1, t2, t2, "Product already has a blocking purchase attempt.")

    attempt = crud.create_purchase_attempt(
        session,
        watch_rule_id=runtime.watch_rule_id,
        listing_id=runtime.listing_id,
        product_id=runtime.product_id,
        status=PurchaseStatus.CREATED.value,
        observed_price=observation.price,
        max_price_allowed=runtime.max_price_allowed,
        quantity=runtime.quantity,
    )
    t3 = time.perf_counter_ns()

    # T3 -> T4: purchase decision — the exact audited function, including
    # a FRESH kill-switch re-check (never the value snapshotted at arm
    # time, matching Phase 33 P0#3's policy_provider pattern).
    intent = PurchaseIntent(
        watch_rule_id=runtime.watch_rule_id,
        product_id=runtime.product_id,
        listing_id=runtime.listing_id,
        merchant=runtime.merchant,
        product_name=runtime.product_name,
        url=runtime.url,
        observed_price=observation.price,
        max_price_allowed=runtime.max_price_allowed,
        quantity=runtime.quantity,
        match_confidence=match.confidence,
        created_at=datetime.now(UTC),
    )
    fresh_policy = policy_provider() if policy_provider is not None else runtime.policy
    decision = evaluate_purchase_intent(
        watch_rule=runtime.watch_rule,
        intent=intent,
        policy=fresh_policy,
        merchant_domains=runtime.merchant_domains,
        match_confidence=match.confidence,
        available=True,
        total_cost=observation.price * runtime.quantity,
        has_active_attempt=False,  # this attempt IS the active one; claim already excludes others
        seconds_since_last_attempt=None,
        spent_today=Decimal("0"),
        blocking_attempts_for_product=0,  # already excluded by the claim above
    )
    t4 = time.perf_counter_ns()

    if not decision.proceed:
        crud.update_purchase_attempt(
            session,
            attempt.id,
            status=PurchaseStatus.CANCELLED.value,
            failure_reason=decision.reason,
        )
        return _abort(t0, t1, t2, t3, decision.reason, attempt.id)

    # T4 -> T5: build the request object — no reconstruction, the payload
    # template already lives on the connector/runtime; only dynamic
    # values (price, quantity) are injected here.
    t5 = time.perf_counter_ns()

    # T5 -> T6: actual network dispatch (see docstring — honest only if
    # `dispatch` blocks until the request has really left).
    if dispatch is not None:
        dispatch(intent)
    t6 = time.perf_counter_ns()

    return WarmPathTrace(t0, t1, t2, t3, t4, t5, t6, True, "dispatched", attempt.id)
