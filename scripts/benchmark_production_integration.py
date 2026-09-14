"""Phase 35 sections 15/16: full production-orchestration latency, not
just an isolated fast_path/run_hot_path call — every iteration goes
through the REAL purchase.engine.attempt_purchase(), unchanged, exactly
as app/worker.py calls it.

T0 = stock-positive ProductObservation handed to attempt_purchase()
(HotPathTrace.stock_received_ns, captured inside attempt_purchase itself)
T6 = first checkout network request dispatched — reported two ways:
  - "light" = HotPathTrace.checkout_dispatch_ns, the real, lightweight
    production instrumentation attempt_purchase() always records (right
    before handing off to asyncio.to_thread(connector.revalidate, ...)).
  - "precise" = a transport-level hook inside a local-HTTP-server-backed
    connector, timestamping the instant httpx actually starts dispatching
    the request (after the to_thread hop + the connector's own request-
    building work) — the ground truth "light" is an early proxy for.

Real pieces: SQLite WAL atomic claim (500 distinct products, so every
claim is genuinely uncontended, matching a real "one drop, one product"
shape sampled many times), a real local HTTP server over a real loopback
socket (connectors/fake_merchant_http_server.py), asyncio.to_thread for
the connector call (this project's real dispatch mechanism — no native
httpx.AsyncClient, see purchase/fast_path.py's module docstring),
31 pre-seeded WatchRules for realistic table scale, and a concurrent
background task loop (periodic DB reads, matching this project's own
per-tick monitoring pattern) as real, running load while every iteration
and the event-loop-lag probe execute.

Honesty notes: this reconstructs the worker's concurrency pattern in a
dedicated harness rather than hooking into the actual live worker
process (which would be disruptive/risky to touch for a benchmark) — it
is not a live-production capture. checkout() always raises
HumanActionRequiredError (never a real transaction); PURCHASES_ENABLED
is never read from or written to the real environment — this script
uses its own synthetic, explicitly-enabled PurchasePolicy exactly like
purchase/fast_path.py's own tests, and never touches purchase/config's
real load_purchase_policy().

Run: .venv/bin/python scripts/benchmark_production_integration.py
Uses its own temp-file SQLite DB (WAL) — never touches data/app.db.
"""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from connectors.fake_merchant_http_server import FakeMerchantServer
from database import crud
from database.session import create_all, get_engine, get_session_factory
from products.matcher import MatchResult
from products.observation import ProductObservation
from purchase.base import HumanActionRequiredError, PurchaseConnector
from purchase.config import PurchasePolicy
from purchase.engine import attempt_purchase
from purchase.models import RevalidationResult
from purchase.registry import PurchaseConnectorRegistry

N_RUNS = 500
N_BACKGROUND_RULES = 31  # matches this project's real live rule count


class _TimingTransport(httpx.HTTPTransport):
    def __init__(self, *args: object, on_dispatch, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._on_dispatch = on_dispatch

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._on_dispatch(time.perf_counter_ns())
        return super().handle_request(request)


class _LocalHTTPPurchaseConnector(PurchaseConnector):
    """revalidate() makes a REAL POST to the local FakeMerchantServer via
    a persistent client (Phase 34 pattern) with a transport-level dispatch
    hook. checkout() always raises HumanActionRequiredError — this
    benchmark never attempts a real transaction."""

    def __init__(self, client: httpx.Client, base_url: str, dispatch_box: list) -> None:
        self._client = client
        self._base_url = base_url
        self._dispatch_box = dispatch_box

    def revalidate(self, intent):
        self._client.post(f"{self._base_url}/checkout", json={"product_id": intent.product_id})
        return RevalidationResult(
            available=True,
            price=intent.observed_price,
            shipping_cost=Decimal("0"),
            quantity_available=None,
        )

    def checkout(self, intent, revalidated):
        raise HumanActionRequiredError("benchmark connector never completes a real checkout")


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[idx]


def _report(name: str, samples_ms: list[float]) -> None:
    print(
        f"{name:<40} median={statistics.median(samples_ms):7.2f}ms  "
        f"p90={_percentile(samples_ms, 0.90):7.2f}ms  "
        f"p95={_percentile(samples_ms, 0.95):7.2f}ms  "
        f"p99={_percentile(samples_ms, 0.99):7.2f}ms  "
        f"max={max(samples_ms):7.2f}ms  n={len(samples_ms)}"
    )


def _match() -> MatchResult:
    return MatchResult(matched=True, confidence=100, method="ean_exact", reason="benchmark")


def _seed_benchmark_rules(session, n: int) -> list:
    rules = []
    merchant = crud.create_merchant(session, "BenchMerchant")
    for i in range(n):
        product = crud.create_product(session, f"Benchmark Product {i}", ean=f"{i:013d}")
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=f"https://bench.example/p/{i}",
            external_id=f"bench-{i}",
        )
        rule = crud.create_watch_rule(
            session,
            product_id=product.id,
            listing_id=listing.id,
            check_interval=30,
            max_quantity=1,
            max_price=Decimal("100"),
        )
        rules.append(rule)
    return rules


def _seed_background_rules(session, n: int) -> None:
    merchant = crud.create_merchant(session, "BackgroundMerchant")
    for i in range(n):
        product = crud.create_product(session, f"Background Product {i}", ean=f"9{i:012d}")
        listing = crud.create_listing(
            session,
            product_id=product.id,
            merchant_id=merchant.id,
            url=f"https://background.example/p/{i}",
            external_id=f"bg-{i}",
        )
        crud.create_watch_rule(
            session,
            product_id=product.id,
            listing_id=listing.id,
            check_interval=60,
            max_quantity=1,
        )


async def main() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="botar_prod_bench_")
    db_path = Path(tmp_dir) / "bench.db"
    engine = get_engine(f"sqlite:///{db_path}")
    create_all(engine)
    session = get_session_factory(engine)()

    print(f"Seeding {N_BACKGROUND_RULES} background WatchRules (realistic table scale)...")
    _seed_background_rules(session, N_BACKGROUND_RULES)
    print(f"Seeding {N_RUNS} distinct benchmark products (setup, not timed)...")
    rules = _seed_benchmark_rules(session, N_RUNS)

    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({"bench.example"}),
        cooldown_seconds=0,
    )

    class FakeNotifier:
        async def send_embed(self, embed: object) -> None:
            return None

    notifier = FakeNotifier()

    with FakeMerchantServer() as server:
        dispatch_box = [0]

        def _on_dispatch(ts_ns: int) -> None:
            dispatch_box[0] = ts_ns

        client = httpx.Client(transport=_TimingTransport(on_dispatch=_on_dispatch))
        client.post(f"{server.base_url}/checkout", json={})  # warm-up, excluded
        connector = _LocalHTTPPurchaseConnector(client, server.base_url, dispatch_box)
        registry = PurchaseConnectorRegistry()
        registry.register("BenchMerchant", connector)

        # --- background load: periodic DB reads, real concurrent asyncio
        # tasks, matching this project's own per-tick monitoring pattern
        # (engine/worker.py reads WatchRules every tick) -----------------
        stop_event = asyncio.Event()

        async def _background_activity() -> None:
            while not stop_event.is_set():
                await asyncio.to_thread(crud.list_watch_rules, session)
                await asyncio.sleep(0.02)

        async def _event_loop_lag_probe() -> list[float]:
            lags: list[float] = []
            loop = asyncio.get_running_loop()
            while not stop_event.is_set():
                t_expected = loop.time()
                fired = loop.create_future()

                def _mark(expected: float = t_expected, box=fired) -> None:
                    box.set_result(loop.time() - expected)

                loop.call_soon(_mark)
                lags.append(await fired)
                await asyncio.sleep(0)
            return lags

        background_task = asyncio.create_task(_background_activity())
        lag_task = asyncio.create_task(_event_loop_lag_probe())

        print(f"Running {N_RUNS} FULL attempt_purchase() integration runs...")
        light_ms: list[float] = []
        precise_ms: list[float] = []
        identity_ms: list[float] = []
        claim_ms: list[float] = []
        decision_ms: list[float] = []
        for rule in rules:
            observation = ProductObservation(
                merchant="BenchMerchant",
                external_id=rule.listing.external_id,
                name=rule.product.name,
                price=Decimal("42"),
                currency="EUR",
                available=True,
                url=rule.listing.url,
                observed_at=datetime.now(UTC),
                ean=rule.product.ean,
            )
            outcome = await attempt_purchase(
                session,
                rule,
                observation,
                _match(),
                policy,
                registry,
                ("bench.example",),
                notifier,
                policy_provider=lambda: policy,
            )
            assert outcome.status.value == "human_action_required", outcome.reason
            trace = outcome.trace
            light_ms.append(trace.t0_to_dispatch_ms)
            precise_ms.append((dispatch_box[0] - trace.stock_received_ns) / 1_000_000)
            identity_ms.append(trace.t0_to_identity_ms)
            claim_ms.append(trace.t0_to_claim_ms)
            decision_ms.append(trace.t0_to_decision_ms)

        stop_event.set()
        await background_task
        lags = await lag_task
        client.close()

    print("\n=== FULL PRODUCTION ORCHESTRATION: T0 -> checkout dispatch ===")
    _report("T0 -> identity_validated", identity_ms)
    _report("T0 -> claim_acquired", claim_ms)
    _report("T0 -> decision_completed", decision_ms)
    _report("light (production instrumentation)", light_ms)
    _report("precise (transport-hooked ground truth)", precise_ms)
    p95 = _percentile(precise_ms, 0.95)
    print(
        f"\nPRODUCTION 100MS READY check (precise, p95): {p95:.2f}ms "
        f"{'<=' if p95 <= 100 else '>'} 100ms -> {'PASS' if p95 <= 100 else 'FAIL'}"
    )

    lags_ms = [x * 1000 for x in lags]
    if lags_ms:
        print("\n=== Event-loop scheduling lag (measured DURING this full benchmark) ===")
        _report("call_soon -> actually run", lags_ms)

    session.close()
    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())
