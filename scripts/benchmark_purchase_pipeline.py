"""Phase 33 sections 2/3/22/23: reproducible synthetic hot-path benchmark.

Measures the REAL code paths (engine.monitoring.run_check,
engine.decision.evaluate, purchase.engine.build_purchase_intent/
evaluate_purchase_intent) against a FakeStoreConnector/PurchaseConnector
standing in for network I/O — no real transaction, no real network for
the "processing only" numbers, so the result isolates PROCESSING delay
from POLLING/NETWORK delay (never confuse the two — a 30s check_interval
can legitimately add up to 30s of *polling* latency; that says nothing
about how fast this project's own code runs once a check actually fires).

A second scenario simulates 3 retailers answering at different real
speeds (80ms/300ms/1000ms) to prove a slow one never blocks a fast one —
the same asyncio.to_thread + concurrent-gather pattern engine/worker.py's
run_monitoring_tick already uses for real, exercised here directly.

Run: .venv/bin/python scripts/benchmark_purchase_pipeline.py
Uses its own in-memory database — never touches data/app.db.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from decimal import Decimal

from connectors.fake_store import FakeStoreConnector
from connectors.registry import ConnectorRegistry
from database import crud
from database.session import create_all, get_engine, get_session_factory
from engine.decision import evaluate
from engine.monitoring import run_check
from purchase.base import PurchaseConnector
from purchase.config import PurchasePolicy
from purchase.engine import build_decision_context, build_purchase_intent, evaluate_purchase_intent
from purchase.models import RevalidationResult

_ITERATIONS = 300


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[idx]


def _report(name: str, samples_ms: list[float]) -> None:
    print(
        f"{name:<45} median={statistics.median(samples_ms):7.2f}ms  "
        f"p95={_percentile(samples_ms, 0.95):7.2f}ms  "
        f"max={max(samples_ms):7.2f}ms  n={len(samples_ms)}"
    )


class _DelayedConnector(PurchaseConnector):
    """Simulates one real merchant's network latency via a genuine
    time.sleep() inside a worker thread — not a fake number."""

    def __init__(self, *, delay_seconds: float) -> None:
        self._delay = delay_seconds

    def revalidate(self, intent):
        time.sleep(self._delay)
        return RevalidationResult(
            available=True, price=Decimal("42"), shipping_cost=Decimal("0"), quantity_available=None
        )

    def checkout(self, intent, revalidated):
        raise NotImplementedError


async def _timed_revalidate(connector: PurchaseConnector, intent, label: str) -> float:
    t0 = time.perf_counter()
    await asyncio.to_thread(connector.revalidate, intent)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"  {label}: {elapsed_ms:.1f}ms")
    return elapsed_ms


def main() -> None:
    engine = get_engine("sqlite:///:memory:")
    create_all(engine)
    session = get_session_factory(engine)()

    product = crud.create_product(session, "Benchmark Product", ean="0000000000001")
    merchant = crud.create_merchant(session, "BenchMerchant")
    listing = crud.create_listing(
        session,
        product_id=product.id,
        merchant_id=merchant.id,
        url="https://bench.example/p/1",
        external_id="bench-1",
    )
    rule = crud.create_watch_rule(
        session,
        product_id=product.id,
        listing_id=listing.id,
        check_interval=30,
        max_quantity=1,
        max_price=Decimal("100"),
    )
    rule = crud.get_watch_rule(session, rule.id)

    registry = ConnectorRegistry()
    registry.register(
        "BenchMerchant",
        FakeStoreConnector(
            products={
                "bench-1": {
                    "name": "Benchmark Product",
                    "price": 42.0,
                    "available": True,
                    "seller": "BenchMerchant",
                    "url": "https://bench.example/p/1",
                    "ean": "0000000000001",
                }
            }
        ),
    )

    detection_samples: list[float] = []
    decision_samples: list[float] = []
    result = None
    for _ in range(_ITERATIONS):
        t0 = time.perf_counter()
        result = run_check(rule, registry)
        t1 = time.perf_counter()
        evaluate(rule, result.observation, result.match_result)
        t2 = time.perf_counter()
        detection_samples.append((t1 - t0) * 1000)
        decision_samples.append((t2 - t1) * 1000)

    print("=== HOT PATH: fetch -> parse -> match (FakeStoreConnector, zero simulated network) ===")
    _report("detection processing (fetch+parse+match)", detection_samples)
    _report("decision (engine.decision.evaluate)", decision_samples)

    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"bench.example"}),
        cooldown_seconds=0,
    )
    purchase_decision_samples: list[float] = []
    for _ in range(_ITERATIONS):
        t0 = time.perf_counter()
        intent = build_purchase_intent(rule, result.observation, result.match_result)
        total_cost = intent.observed_price * intent.quantity
        has_active, since_last, spent_today, blocking = build_decision_context(session, intent)
        evaluate_purchase_intent(
            watch_rule=rule,
            intent=intent,
            policy=policy,
            merchant_domains=("bench.example",),
            match_confidence=result.match_result.confidence,
            available=result.observation.available,
            total_cost=total_cost,
            has_active_attempt=has_active,
            seconds_since_last_attempt=since_last,
            spent_today=spent_today,
            blocking_attempts_for_product=blocking,
        )
        purchase_decision_samples.append((time.perf_counter() - t0) * 1000)

    _report("purchase intent + decision (pure, no I/O)", purchase_decision_samples)
    total_median = (
        statistics.median(detection_samples)
        + statistics.median(decision_samples)
        + statistics.median(purchase_decision_samples)
    )
    print(
        f"\nTOTAL hot path (detection + decision + purchase-intent), median: {total_median:.2f}ms"
    )

    print("\n=== Section 23: 3 simulated retailers (80ms/300ms/1000ms), concurrent ===")
    intent = build_purchase_intent(rule, result.observation, result.match_result)

    async def _concurrency_demo() -> None:
        fast = _DelayedConnector(delay_seconds=0.080)
        medium = _DelayedConnector(delay_seconds=0.300)
        slow = _DelayedConnector(delay_seconds=1.000)

        t_start = time.perf_counter()
        fast_ms, _medium_ms, slow_ms = await asyncio.gather(
            _timed_revalidate(fast, intent, "fast   (target ~80ms)"),
            _timed_revalidate(medium, intent, "medium (target ~300ms)"),
            _timed_revalidate(slow, intent, "slow   (target ~1000ms)"),
        )
        t_total = (time.perf_counter() - t_start) * 1000
        print(f"  wall-clock for all 3 concurrently: {t_total:.1f}ms (sequential would be ~1380ms)")
        print(
            f"  fast retailer finished in {fast_ms:.1f}ms — not blocked by the concurrently "
            f"running slow one ({slow_ms:.1f}ms)"
        )
        if fast_ms >= 200:
            raise AssertionError("fast retailer was blocked by a slower concurrent one!")
        if t_total >= 1380 * 0.8:
            raise AssertionError("no real concurrency observed")

    asyncio.run(_concurrency_demo())
    print("\nDONE")


if __name__ == "__main__":
    main()
