"""Phase 34: purchase/fast_path.py correctness + concurrency.

Same testing philosophy as tests/test_purchase_concurrency.py (Phase 33
sections 11/12): no real network, no real Discord, no real money — a
`dispatch` stand-in just records that it was called. The concurrency
test proves the SAME product cannot be claimed twice even under this
module's different (claim-before-decision) ordering.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.orm import Session

from database import crud
from database.models import PurchaseAttempt
from purchase.config import PurchasePolicy
from purchase.fast_path import run_warm_path
from purchase.prepared_runtime import build_prepared_drop_runtime


class _Signal:
    def __init__(
        self,
        *,
        external_id: str = "etb-1",
        name: str = "ETB Chaos Ascendant FR",
        price: str = "59.90",
        ean: str | None = "0196214142145",
        mpn: str | None = None,
    ) -> None:
        self.external_id = external_id
        self.name = name
        self.price = Decimal(price)
        self.ean = ean
        self.mpn = mpn


def _seed(session: Session, *, ean: str | None = "0196214142145", max_price: str = "80") -> int:
    product = crud.create_product(session, "ETB Chaos Ascendant FR", ean=ean)
    merchant = crud.create_merchant(session, "Kairyu")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://kairyu.fr/products/etb-1",
        external_id="etb-1",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=30,
        max_quantity=1,
        max_price=Decimal(max_price),
    )
    return rule.id


def _runtime(session: Session, rule_id: int, *, calls: list | None = None):
    def _dispatch(intent: object) -> None:
        if calls is not None:
            calls.append(intent)

    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"kairyu.fr"}),
        cooldown_seconds=0,
    )
    runtime = build_prepared_drop_runtime(
        session,
        rule_id,
        connector=None,  # not used by run_warm_path directly — dispatch is injected separately
        policy=policy,
        merchant_domains=("kairyu.fr",),
    )
    return runtime, _dispatch


def test_happy_path_dispatches_and_records_all_timestamps(session: Session) -> None:
    rule_id = _seed(session)
    calls: list = []
    runtime, dispatch = _runtime(session, rule_id, calls=calls)

    trace = run_warm_path(session, runtime, _Signal(), dispatch=dispatch)

    assert trace.proceed is True
    assert len(calls) == 1
    ordered = (
        trace.t0_ns,
        trace.t1_ns,
        trace.t2_ns,
        trace.t3_ns,
        trace.t4_ns,
        trace.t5_ns,
        trace.t6_ns,
    )
    assert list(ordered) == sorted(ordered)
    assert trace.total_ms >= 0
    segments = trace.segments_ms()
    assert set(segments) == {
        "parse",
        "identity_validation",
        "atomic_claim",
        "decision",
        "request_prep",
        "dispatch",
    }
    assert all(v >= 0 for v in segments.values())
    attempt = session.get(PurchaseAttempt, trace.attempt_id)
    assert attempt.status == "created"


def test_wrong_ean_is_hard_rejected_before_any_claim(session: Session) -> None:
    rule_id = _seed(session, ean="0000000000001")
    calls: list = []
    runtime, dispatch = _runtime(session, rule_id, calls=calls)

    trace = run_warm_path(session, runtime, _Signal(ean="9999999999999"), dispatch=dispatch)

    assert trace.proceed is False
    assert "Identity" in trace.reason or "confidence" in trace.reason.lower()
    assert calls == []
    assert trace.attempt_id is None
    assert crud.list_purchase_attempts(session) == []


def test_price_over_ceiling_claims_then_cancels_and_does_not_dispatch(session: Session) -> None:
    rule_id = _seed(session, max_price="10")  # signal price 59.90 > ceiling
    calls: list = []
    runtime, dispatch = _runtime(session, rule_id, calls=calls)

    trace = run_warm_path(session, runtime, _Signal(), dispatch=dispatch)

    assert trace.proceed is False
    assert calls == []
    assert trace.attempt_id is not None
    attempt = session.get(PurchaseAttempt, trace.attempt_id)
    assert attempt.status == "cancelled"
    # The cancelled claim must not permanently block a later, valid signal.
    blocking = crud.get_blocking_purchase_attempts_for_product(session, runtime.product_id)
    assert blocking == []


def test_kill_switch_off_is_re_checked_fresh_and_blocks_dispatch(
    session: Session,
) -> None:
    """Mirrors purchase/engine.py::attempt_purchase's own kill-switch
    re-check test (Phase 33 P0#3): runtime.policy (snapshotted at arm
    time) stays enabled=True, but the injected policy_provider — the
    real production wiring — reports enabled=False, proving the fresh
    check wins over the stale snapshot."""
    rule_id = _seed(session)
    calls: list = []
    runtime, dispatch = _runtime(session, rule_id, calls=calls)
    assert runtime.policy.enabled is True

    def _disabled_policy() -> PurchasePolicy:
        return PurchasePolicy(
            enabled=False,
            max_order_eur=None,
            max_daily_eur=None,
            allowed_merchant_domains=frozenset({"kairyu.fr"}),
            cooldown_seconds=0,
        )

    trace = run_warm_path(
        session, runtime, _Signal(), dispatch=dispatch, policy_provider=_disabled_policy
    )

    assert trace.proceed is False
    assert "kill switch" in trace.reason.lower() or "PURCHASES_ENABLED" in trace.reason
    assert calls == []


def test_second_signal_after_a_claim_is_blocked_without_creating_a_new_row(
    session: Session,
) -> None:
    rule_id = _seed(session)
    calls: list = []
    runtime, dispatch = _runtime(session, rule_id, calls=calls)

    first = run_warm_path(session, runtime, _Signal(), dispatch=dispatch)
    assert first.proceed is True

    second = run_warm_path(session, runtime, _Signal(), dispatch=dispatch)

    assert second.proceed is False
    assert second.attempt_id is None
    assert len(calls) == 1
    assert len(crud.list_purchase_attempts(session)) == 1


def _seed_same_product_on_n_rules(session: Session, n: int) -> tuple[int, list[int]]:
    product = crud.create_product(session, "ETB Chaos Ascendant FR", ean="0196214142145")
    rule_ids = []
    for i in range(n):
        merchant = crud.create_merchant(session, f"Retailer{i}")
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=f"https://retailer{i}.example/p/etb-1",
            external_id="etb-1",
        )
        rule = crud.create_watch_rule(
            session,
            product_id=product.id,
            listing_id=listing.id,
            check_interval=60,
            max_quantity=1,
            max_price=Decimal("1000"),
        )
        rule_ids.append(rule.id)
    return product.id, rule_ids


def test_fifty_concurrent_signals_same_product_exactly_one_dispatch(session: Session) -> None:
    """Same stress test as tests/test_purchase_concurrency.py, adapted to
    this module's claim-before-decision ordering: 50 retailers signal
    stock for the same product at once — at most one may ever dispatch a
    real network request."""
    n = 50
    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({f"retailer{i}.example" for i in range(n)}),
        cooldown_seconds=0,
    )
    product_id, rule_ids = _seed_same_product_on_n_rules(session, n)
    calls: list = []

    def _dispatch(intent: object) -> None:
        calls.append(intent)

    runtimes = [
        build_prepared_drop_runtime(
            session,
            rid,
            connector=None,
            policy=policy,
            merchant_domains=tuple(policy.allowed_merchant_domains),
        )
        for rid in rule_ids
    ]

    async def _one(runtime) -> object:
        return run_warm_path(session, runtime, _Signal(), dispatch=_dispatch)

    async def _run_all() -> list:
        return await asyncio.gather(*(_one(r) for r in runtimes))

    traces = asyncio.run(_run_all())

    assert sum(1 for t in traces if t.proceed) == 1
    assert len(calls) == 1
    blocking = crud.get_blocking_purchase_attempts_for_product(session, product_id)
    assert len(blocking) == 1
