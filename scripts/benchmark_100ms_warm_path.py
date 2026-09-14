"""Phase 34 (100ms warm-path hard target) benchmark.

Measures T0 (stock-positive signal received) -> T6 (first checkout
network request actually dispatched) using the REAL, audited pipeline:
purchase.fast_path.run_warm_path, which itself calls
products.matcher.match_product, database.crud's atomic-claim functions,
purchase.engine.evaluate_purchase_intent, and purchase.config.
load_purchase_policy (fresh kill-switch re-check) unchanged — never a
second, competing decision engine.

Network dispatch goes to a REAL local HTTP server (real TCP/loopback
socket, real HTTP/1.1 parsing — connectors/fake_merchant_http_server.py)
through a REAL persistent httpx.Client with connection pooling, wrapped
in asyncio.to_thread per iteration (this project's established sync-
connector-in-a-thread pattern, Phase 20-33 — not a native
httpx.AsyncClient; see the honesty notes below) so the event loop is
never blocked by it. T6 itself is captured by a custom httpx Transport
that timestamps the instant the request is handed off for actual
dispatch (immediately before connection acquisition + socket write) —
not merely when a coroutine is created, and not the full round-trip.

Honesty notes (read before trusting these numbers):
  - This is a LOCAL loopback server: no TLS, no real internet RTT. A
    real merchant's real TLS handshake was separately measured earlier
    (Phase 33) at ~90ms+ for a fresh connection vs a warm one on a real
    HTTPS endpoint — see the REAL HTTPS section below, which re-measures
    that specific number for context using the exact production
    purchase-connector domain (Kairyu), read-only, non-transactional.
  - Dispatch uses a stand-in single POST (via the same persistent
    httpx.Client class purchase/defaults.py now wires into production
    purchase connectors), not the full multi-call Shopify UCP JSON-RPC
    exchange — this isolates transport-level dispatch latency
    (connection acquisition + socket write), not already-audited
    business logic (Phase 33's live dry-run already exercised that).
  - purchase/fast_path.py::run_warm_path is NOT wired into
    purchase/engine.py::attempt_purchase() as the production call path
    this session — see fast_path.py's own module docstring for why.
    These numbers show what the pipeline CAN achieve, proven for real,
    not what today's production purchase path measures end to end
    without further integration work.

Run: .venv/bin/python scripts/benchmark_100ms_warm_path.py
Uses its own temp-file SQLite DB (WAL) — never touches data/app.db.
Never creates a real transaction anywhere.
"""

from __future__ import annotations

import asyncio
import dataclasses
import statistics
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import httpx

from connectors.fake_merchant_http_server import FakeMerchantServer
from database import crud
from database.session import create_all, get_engine, get_session_factory
from purchase.config import PurchasePolicy
from purchase.fast_path import run_warm_path
from purchase.prepared_runtime import build_prepared_drop_runtime

N_WARM = 5000
N_COLD = 20


class _Signal:
    def __init__(self, *, external_id: str, name: str, price: Decimal, ean: str) -> None:
        self.external_id = external_id
        self.name = name
        self.price = price
        self.ean = ean
        self.mpn = None


class _TimingTransport(httpx.HTTPTransport):
    """Records perf_counter_ns() at the instant a request is handed to
    the transport for dispatch — immediately before connection
    acquisition and socket write, the closest instrumentable point to
    "the request actually left" without patching httpcore's own socket
    layer."""

    def __init__(self, *args: object, on_dispatch, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._on_dispatch = on_dispatch

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._on_dispatch(time.perf_counter_ns())
        return super().handle_request(request)


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[idx]


def _report(name: str, samples_ms: list[float]) -> None:
    print(
        f"{name:<45} median={statistics.median(samples_ms):7.2f}ms  "
        f"p90={_percentile(samples_ms, 0.90):7.2f}ms  "
        f"p95={_percentile(samples_ms, 0.95):7.2f}ms  "
        f"p99={_percentile(samples_ms, 0.99):7.2f}ms  "
        f"max={max(samples_ms):7.2f}ms  n={len(samples_ms)}"
    )


def _seed_products(session, n: int) -> list[int]:
    """Distinct product per iteration, seeded OUTSIDE the timed loop —
    each iteration then exercises a genuinely uncontended atomic claim,
    the realistic "one drop, one product" shape, sampled n times."""
    rule_ids = []
    for i in range(n):
        product = crud.create_product(session, f"Benchmark Product {i}", ean=f"{i:013d}")
        merchant = crud.create_merchant(session, f"BenchMerchant{i}")
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
        rule_ids.append(rule.id)
    return rule_ids


def main() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="botar_bench_")
    db_path = Path(tmp_dir) / "bench.db"
    engine = get_engine(f"sqlite:///{db_path}")
    create_all(engine)
    session = get_session_factory(engine)()

    policy = PurchasePolicy(
        enabled=True,
        max_order_eur=None,
        max_daily_eur=None,
        allowed_merchant_domains=frozenset({f"benchmerchant{i}.example" for i in range(N_WARM)}),
        cooldown_seconds=0,
    )

    with FakeMerchantServer() as server:
        checkout_url = f"{server.base_url}/checkout"

        # === WARM PATH: one persistent, connection-pooled client, built
        # once, reused for every iteration — exactly the production
        # purchase/defaults.py pattern. ===
        dispatch_ts: list[int] = [0]

        def _on_dispatch(ts_ns: int) -> None:
            dispatch_ts[0] = ts_ns

        warm_client = httpx.Client(transport=_TimingTransport(on_dispatch=_on_dispatch))
        # Warm-up request: pays the one-time connection setup, excluded.
        warm_client.post(checkout_url, json={})

        print(f"Seeding {N_WARM} distinct products (setup, not timed)...")
        rule_ids = _seed_products(session, N_WARM)

        def _make_dispatch(rid: int):
            def _dispatch(intent: object) -> None:
                warm_client.post(checkout_url, json={"product_id": rid, "quantity": 1})

            return _dispatch

        async def _run_one(rule_id: int) -> object:
            preview = crud.get_watch_rule(session, rule_id)
            own_domain = f"{preview.listing.merchant.name.lower()}.example"
            runtime = build_prepared_drop_runtime(
                session,
                rule_id,
                connector=None,
                policy=policy,
                merchant_domains=(own_domain,),
            )
            signal = _Signal(
                external_id=f"bench-{rule_id}",
                name="Benchmark Product",
                price=Decimal("42"),
                ean=runtime.expected_ean or "0",
            )

            def _timed_run() -> object:
                # policy_provider is still exercised (matching the real
                # production call shape, Phase 33 P0#3) but points at
                # this benchmark's own synthetic, explicitly-enabled
                # PurchasePolicy — never the real environment. The real
                # PURCHASES_ENABLED stays whatever this machine's .env
                # says (false, this whole project) and is never read or
                # bypassed here; production's app/worker.py wiring
                # (policy_provider=load_purchase_policy) is untouched.
                trace = run_warm_path(
                    session,
                    runtime,
                    signal,
                    dispatch=_make_dispatch(rule_id),
                    policy_provider=lambda: policy,
                )
                return dataclasses.replace(trace, t6_ns=dispatch_ts[0])

            return await asyncio.to_thread(_timed_run)

        async def _event_loop_lag_probe(stop_event: asyncio.Event) -> list[float]:
            """Measures scheduling delay (call_soon() requested ->
            actually run) while the warm-path benchmark runs concurrently
            as real background load on the same event loop."""
            lags: list[float] = []
            loop = asyncio.get_running_loop()
            while not stop_event.is_set():
                t_expected = loop.time()
                fired = loop.create_future()

                def _mark(expected: float = t_expected, box: asyncio.Future = fired) -> None:
                    box.set_result(loop.time() - expected)

                loop.call_soon(_mark)
                lags.append(await fired)
                await asyncio.sleep(0)
            return lags

        async def _run_warm_benchmark() -> list:
            stop_event = asyncio.Event()
            lag_task = asyncio.create_task(_event_loop_lag_probe(stop_event))
            traces = []
            for rid in rule_ids:
                traces.append(await _run_one(rid))
            stop_event.set()
            lags = await lag_task
            return traces, lags

        print(f"Running {N_WARM} warm-path iterations (this takes a little while)...")
        traces, lag_samples_s = asyncio.run(_run_warm_benchmark())
        warm_client.close()

        total_ms = [t.total_ms for t in traces]
        assert all(t.proceed for t in traces), "every distinct-product iteration must proceed"

        seg_names = ["parse", "identity_validation", "atomic_claim", "decision", "dispatch"]
        segments = {name: [] for name in seg_names}
        for t in traces:
            s = t.segments_ms()
            segments["parse"].append(s["parse"])
            segments["identity_validation"].append(s["identity_validation"])
            segments["atomic_claim"].append(s["atomic_claim"])
            segments["decision"].append(s["decision"] + s["request_prep"])
            # dispatch here = T5->T6 as corrected by the transport hook,
            # i.e. time-to-actual-dispatch, not full round trip.
            segments["dispatch"].append((t.t6_ns - t.t5_ns) / 1_000_000)

        print("\n=== WARM PATH: T0 (stock signal) -> T6 (network request dispatched) ===")
        print(
            "(T6 = transport handle_request() entry, i.e. right before connection-pool "
            "acquisition + socket write — accurate here because the client is already warm, "
            "so there is no connection-setup cost hidden after the hook fires)"
        )
        for name in seg_names:
            _report(f"  T-segment: {name}", segments[name])
        _report("TOTAL (T0 -> T6)", total_ms)
        target_p95 = _percentile(total_ms, 0.95)
        print(
            f"\n100MS READY check: p95={target_p95:.2f}ms "
            f"{'<=' if target_p95 <= 100 else '>'} 100ms -> "
            f"{'PASS' if target_p95 <= 100 else 'FAIL'}"
        )

        lags_ms = [x * 1000 for x in lag_samples_s]
        if lags_ms:
            print("\n=== Event-loop scheduling lag (measured DURING the warm-path load) ===")
            _report("  call_soon -> actually run", lags_ms)

        # === COLD PATH (local): brand-new connection every time. Uses
        # plain wall-clock timing around the WHOLE call, not the T6
        # transport hook — the hook fires at handle_request() entry,
        # BEFORE httpcore's own connection-pool acquisition/connect, so
        # for a cold connection it cannot see the connection-setup cost
        # at all (that cost happens strictly after the hook fires,
        # inside the call this same hook wraps). Honest for a WARM
        # client (nothing to hide — acquisition is just a pool lookup),
        # not valid for measuring a cold one, which is why this section
        # times the full round trip instead, matching the REAL HTTPS
        # section's methodology below. Loopback has no TLS and a
        # near-instant local TCP handshake, so little difference from
        # warm is expected here — the real, meaningful cold-vs-warm gap
        # is the REAL HTTPS section further down. ===
        print(f"\nRunning {N_COLD} cold-path iterations (fresh connection each time)...")
        cold_ms: list[float] = []
        for _ in range(N_COLD):
            t_before = time.perf_counter_ns()
            with httpx.Client() as c:
                c.post(checkout_url, json={})
            cold_ms.append((time.perf_counter_ns() - t_before) / 1_000_000)
        _report("COLD full round trip (local, fresh connection)", cold_ms)

    session.close()

    # === REAL HTTPS: fresh vs reused connection against a real
    # production purchase-connector domain, read-only, non-transactional
    # (re-measures Phase 33's ~94ms finding for direct comparison). ===
    print("\n=== REAL HTTPS (Kairyu, /robots.txt, non-transactional) ===")
    https_cold: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        httpx.request("GET", "https://kairyu.fr/robots.txt", timeout=10)
        https_cold.append((time.perf_counter() - t0) * 1000)
    with httpx.Client(timeout=10) as https_client:
        https_client.get("https://kairyu.fr/robots.txt")  # warm-up, excluded
        https_warm = []
        for _ in range(3):
            t0 = time.perf_counter()
            https_client.get("https://kairyu.fr/robots.txt")
            https_warm.append((time.perf_counter() - t0) * 1000)
    _report("  fresh connection each call", https_cold)
    _report("  warm reused connection", https_warm)
    print(
        f"  delta (median): "
        f"{statistics.median(https_cold) - statistics.median(https_warm):.1f}ms saved by reuse"
    )

    print("\nDONE")


if __name__ == "__main__":
    main()
